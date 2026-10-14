"""render_compose 技能：合成渲染（火山 AI MediaKit）——**唯一渲染、DAG 汇点**。

契约 §3.7：依赖 ``tts`` / ``timeline`` / ``bgm`` / ``subtitle`` / ``cover`` / ``cart``
（**缺席者按裁剪后的依赖表传入**）；降级 **abort**；风险 **L1**。

职责：按 EDL 铺 **主轨（图生视频片段 ``video`` / 静图 ``image``）+ 音轨**，把字幕 / 贴片 /
封面位**一次性烧制**，成片 mp4 落**成片目录**（契约 §2.4：``{outputPrefix}/{runId}/final.mp4``）。
**运镜不在这里做**（ADR-076）。

关键契约点：
- §7.3② **成片必须转存**：MediaKit 的 ``result.video_url`` 是 **24h 签名 URL**，
  **不得**直接当 ``storedKey``；须落 ``{outputPrefix}/{runId}/final.mp4`` 再回 key。
- **运镜（ADR-076 改判）**：运镜已由 ``image_to_video`` 经引擎适配器并入 prompt **随生成落地**；
  本步**不再施加**（旧的 MediaKit ``apply-camera-motion`` 事后 pan-zoom 已退役——重复施加会叠加两次运动）。
- §7.1 素材经**预签名 URL**（同源 ``/api/assets`` 火山拉不到，须换成可达引用）。
- §7.3④ AIGC 标识：``extra_param.upload.aigc_meta`` 由 MediaKit 客户端注入。

降级 **abort**：失败即抛错（``invoke()`` 兜成 ``ok=False``），运行器据此中止整单，
成片字段保持空——**绝不伪造成片**。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from flowmind.config import load_config
from flowmind.contracts import SkillOutput
from flowmind.skill import skill
from flowmind.skills import _dsp_common as common
from flowmind.skills import _dsp_mediakit as mediakit
from flowmind.skills import _dsp_storage as storage
from flowmind.skills._dsp_common import DspBaseInput

_VERSION = "0.1.0"


class RenderComposeInput(DspBaseInput):
    """渲染入参（公共上下文 + 可选字幕样式）。"""
    subtitleFontSize: int = Field(default=48)


class RenderComposeData(BaseModel):
    """出参：成片 key + 时长 + 分辨率（本仓就取 ``storedKey`` 当成品）。"""
    storedKey: str
    durationMs: int
    width: int
    height: int
    note: str = ""
    warnings: list[str] = Field(default_factory=list)


def _presign(key: str) -> str:
    """素材 key → 预签名 URL（空 key 返回空串，由调用方跳过）。"""
    return storage.presign_get(key) if key else ""


def _split_stickers(raw: object) -> tuple[list[dict], list[str]]:
    """拆贴片：带图片 source 的可烧制；纯平台组件（商品链接）不烧进画面（§7.2）。"""
    burnable: list[dict] = []
    notes: list[str] = []
    if isinstance(raw, list):
        for sticker in raw:
            if not isinstance(sticker, dict):
                continue
            if sticker.get("source"):
                burnable.append(sticker)
            else:
                ref = (sticker.get("productRef") or {}).get("value", "")
                notes.append(f"贴片（商品锚点 {ref or '未给'}）为平台组件，未烧进画面")
    return burnable, notes


# **运镜施加已退役**（ADR-076）：旧路径 `apply-camera-motion` 是对**已生成片段**做事后 pan-zoom，
# 而运镜现已由「运镜导演」产出方案、经适配器并入 I2V prompt **随生成一起落地**——本侧再施加就是
# **同一运动施加两次**（ADR-073 当初正是为此才禁止把运镜写进 prompt）。故整段删除，不留死代码。
# MediaKit 的 `apply_camera_motion` / `MOTION_STYLES` / `EFFECT_MOTION_STYLE` / `motion_style_for`
# 一并退役（见 `_dsp_mediakit.py`）；将来若要用引擎**参数级**运镜，落点是 `_dsp_video_gen.MOTION_ENGINE`。


@skill(id="render_compose", name="合成渲染", version=_VERSION,
       category="抖音成片", tags=["dsp", "render", "video"])
def render_compose(inp: RenderComposeInput) -> SkillOutput[RenderComposeData]:
    """按 EDL 铺轨并一次性烧制，成片落 ``{outputPrefix}/{runId}/final.mp4`` 并回 key。"""
    cfg = load_config().dsp
    edl = common.dep_edl(inp.deps)
    if edl is None:
        raise ValueError("render_compose 需要 deps.timeline.edl（渲染以锁定 EDL 为准）。")
    if not mediakit.mediakit_configured(cfg):
        raise mediakit.DspMediakitError(
            f"未配置渲染密钥 {cfg.mediakit_key_env}（dsp 成片必需）；"
            "契约 §7.1：MediaKit 密钥由后端经集群 Secret 持有。",
            category="environment",
        )

    warnings: list[str] = []

    # ── 逐镜素材：``video`` 节点（图生视频）的片段优先，缺片段回落静图 ──
    video_dep = common.dep(inp.deps, "video") or {}
    raw_clips = video_dep.get("clips")
    clips = [c for c in raw_clips if isinstance(c, dict)] if isinstance(raw_clips, list) else []
    clip_by_shot = {
        str(c.get("shotId")): c for c in clips if c.get("shotId") and c.get("key")
    }

    # 主轨：有 I2V 片段的镜头用片段、其余回落静图。**运镜不在这里施加**（ADR-076：已随生成落地）。
    clip_urls: dict[str, str] = {}
    for shot_id, clip in clip_by_shot.items():
        clip_urls[shot_id] = _presign(str(clip["key"]))
    if clip_urls:
        warnings.append(f"主轨用图生视频片段：{len(clip_urls)} 镜")

    # 静图轨：仅无片段（或片段缺失）的镜头需要其图
    still_keys = {
        seg.sourceKey
        for track in edl.tracks if track.kind == "image"
        for seg in track.segments if seg.sourceKey
    }
    image_urls = {key: _presign(key) for key in still_keys}

    tts = common.dep(inp.deps, "tts") or {}
    bgm = common.dep(inp.deps, "bgm") or {}
    subtitle = common.dep(inp.deps, "subtitle") or {}
    cover = common.dep(inp.deps, "cover") or {}
    cart = common.dep(inp.deps, "cart") or {}

    audio_key = str(tts.get("audioKey") or "")
    if not audio_key:
        warnings.append("无音轨（deps.tts.audioKey 缺失）：成片无口播（若已关「智能配音」属预期）")
    else:
        # 新不变量（ADR-069）：锁定时间线的 totalMs **就是音频实测总长**（timeline 按逐句实测锁）。
        # 两者不符即说明 EDL 与配音不同源，如实报出（不静默重算）。
        try:
            audio_ms = int(tts.get("durationMs"))
        except (TypeError, ValueError):
            audio_ms = 0
        if audio_ms and abs(audio_ms - edl.totalMs) > 50:
            warnings.append(
                f"配音实测总长 {audio_ms}ms 与锁定时间线 {edl.totalMs}ms 不一致"
                "（按 ADR-069，时间线应即音频总长）——请核对，渲染仍以 EDL 为准。"
            )

    stickers, sticker_notes = _split_stickers(cart.get("stickers"))
    warnings.extend(sticker_notes)

    # 转场（ADR-070）：EDL 记的是**符号名**，未登记厂商资源 ID 的会被如实回落硬切——在此告警。
    warnings.extend(mediakit.transition_warnings(edl))

    tracks = mediakit.build_tracks(
        edl,
        image_urls=image_urls,
        clip_urls=clip_urls,
        audio_url=_presign(audio_key),
        bgm_url=_presign(str(bgm.get("bgmKey") or "")),
        srt_url=_presign(str(subtitle.get("srtKey") or "")),
        subtitle_font_size=inp.subtitleFontSize,
        canvas_width=cfg.mediakit_canvas_width,
        canvas_height=cfg.mediakit_canvas_height,
    )
    if not tracks:
        raise mediakit.DspMediakitError(
            "渲染轨道为空：EDL 各镜均未选到图（timeline 已留空位）——无可渲染内容。",
            category="video",
        )

    cover_at = cover.get("atMs")
    body = mediakit.build_body(
        cfg, tracks=tracks,
        client_token=inp.idempotencyKey or f"{inp.runId}:{inp.stepId}",
        cover_at_ms=int(cover_at) if cover_at is not None else None,
    )
    result = mediakit.render(cfg, body)

    video_url = str(result.get("video_url") or "")
    duration_s = float(result.get("duration") or 0)
    duration_ms = int(duration_s * 1000) or edl.totalMs
    final = common.final_key(inp.outputPrefix, inp.runId, ext=cfg.mediakit_output_format)

    if video_url.startswith(("http://", "https://")):
        # §7.3② 转存：签名 URL（24h 失效）→ 自有对象存储的不可变 key
        with tempfile.TemporaryDirectory(prefix="dsp-render-") as tmp:
            local = storage.fetch_to_file(video_url, str(Path(tmp) / f"final.{cfg.mediakit_output_format}"))
            storage.put_file(local, final, content_type="video/mp4")
    else:
        raise mediakit.DspMediakitError(
            f"MediaKit 返回的成片地址不可直接取用（{video_url[:80]}）。"
            "本仓只支持 http(s) 结果的下载-转存；若启用了 media_output_destination（tos://…），"
            "需另配火山 TOS 客户端才能搬回自有对象存储。请清空对应 output_destination 配置"
            "或补 TOS 接线。",
            category="environment",
        )

    if result.get("resolution"):
        warnings.append(f"MediaKit 实际分辨率：{result['resolution']}")

    note = f"成片 {duration_ms}ms → {final}"
    return SkillOutput(
        data=RenderComposeData(
            storedKey=final, durationMs=duration_ms,
            width=cfg.mediakit_canvas_width, height=cfg.mediakit_canvas_height,
            note=note, warnings=warnings,
        ),
        reasoning=[common.chain(
            f"合成渲染完成：{final}（{duration_ms}ms，{cfg.mediakit_canvas_width}×{cfg.mediakit_canvas_height}）",
            causal_analysis=(
                f"按锁定 EDL（totalMs={edl.totalMs}ms）铺 {len(tracks)} 轨；"
                f"主轨 {len(clip_urls)} 段视频片段 + {len(still_keys)} 张静图；"
                "字幕/贴片/封面位一次性烧制；AIGC 标识元数据已注入。"
            ),
            risk_note=(
                "渲染为一次性烧制（不可再编辑）；成片已转存为不可变 key，重跑 = 新 runId = 新 key。"
            ),
        )],
        confidence=1.0,
        sample_size=1,
    )
