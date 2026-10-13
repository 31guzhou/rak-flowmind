"""image_to_video 技能：图生视频（逐镜产出视频底 · 契约 §7.5 第 1 步）。

契约 §7.5：运镜（``apply-camera-motion``）**只接受视频**，故「让静态图动起来」需
**先有视频底**。本技能就是那个第 1 步——**逐镜**把素材图 + **画面提示词**喂给图生视频模型，产出**真视频片段**，
落 ``{cachePrefix}/{runId}/clips/{shotId}.mp4``。

**prompt 取画面提示词、不是台词**（ADR-073）：把口播文案当画面提示词会让模型把**字幕烧进画面**，
故优先用 ``edl.shots[].visualPrompt``（``copy`` 只在缺画面提示词时兜底，并**如实告警**）。

链路位置：``timeline``（锁时长）→ **本步** → ``render`` 侧对片段加运镜并拼成成片。

粒度与时长：**一镜一段**；镜头建议时长（``shots[].durationMs``）按秒下发给模型
（文档：``seconds`` 取值视模型而定，本仓**原样下发、不夹取**）。

降级 **degrade**（本步为可关开关 ``videoGen`` 控制的增强步）：
- 某镜生成失败 → **该镜不出片段**（render 侧回落静图）+ ``warnings`` 如实说明；
- 全部失败 / 未配密钥 / 无时间线 → ``degraded=True`` + 明确 ``degradation_reason``。
**绝不伪造片段**（不拿静图当视频、不编 URL）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from flowmind.config import load_config
from flowmind.contracts import SkillOutput
from flowmind.skill import skill
from flowmind.skills import _dsp_common as common
from flowmind.skills import _dsp_storage as storage
from flowmind.skills import _dsp_video_gen as videogen
from flowmind.skills._dsp_common import DspBaseInput

_VERSION = "0.1.0"

# 负向约束（ADR-073；依据厂商《Seedance 2.5 提示词指南》「字幕约束不生效」一节）：
# 台词被写进 prompt 时模型容易把字幕**烧进画面**——除画面提示词本身不写台词外，这里再兜一层明文禁止。
# 需要更强的负向约束时经 env ``FLOWMIND_DSP_VIDEOGEN_PROMPT_SUFFIX``（``DspConfig.videogen_prompt_suffix``）追加。
_PROMPT_NEGATIVE = "。画面中不要出现任何文字、字幕、水印、logo 或品牌标识。"


class ImageToVideoInput(DspBaseInput):
    """图生视频入参（公共上下文已含 ``shots[]``——逐镜的**画面提示词**取自其 ``visualPrompt``，
    缺则回落 ``copy`` 口播文案并如实告警，见 ADR-073）。"""

    maxShots: int = Field(
        default=0, ge=0,
        description="最多生成几镜（0 = 不限）；图生视频按镜计费，用于控成本")


class ShotClip(BaseModel):
    """单镜视频片段（供 render 侧作 ``video`` 元素）。"""
    shotId: str
    key: str = Field(..., description="片段的云存储 key")
    atMs: int
    durationMs: int


class ImageToVideoData(BaseModel):
    """出参：逐镜片段 + 如实说明。"""
    clips: list[ShotClip] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _presign(key: str) -> str:
    """素材 key → 预签名 URL（图生视频要公网可达的图；空 key 返回空串）。"""
    return storage.presign_get(key) if key else ""


def _degraded(reason: str, note: str, risk: str) -> SkillOutput[ImageToVideoData]:
    """统一的「未生成」出口（不伪造片段）。"""
    return SkillOutput(
        data=ImageToVideoData(warnings=[note]),
        reasoning=[common.chain(f"图生视频未生成：{note}", risk_note=risk)],
        confidence=0.0,
        degraded=True,
        degradation_reason=reason,
    )


@skill(id="image_to_video", name="图生视频", version=_VERSION,
       category="抖音成片", tags=["dsp", "video"])
def image_to_video(inp: ImageToVideoInput) -> SkillOutput[ImageToVideoData]:
    """逐镜把素材图 + 文案喂图生视频模型，产出片段 key（失败即该镜回落静图并如实说明）。"""
    cfg = load_config().dsp
    edl = common.dep_edl(inp.deps)
    if edl is None:
        return _degraded(
            "missing-timeline", "缺少锁定时间线（deps.timeline.edl）",
            "图生视频按锁定时间线的镜头与时长出片；请先跑通 timeline。")
    if not inp.shots:
        return _degraded("no-shots", "入参无 shots[]（缺画面提示词来源）",
                         "图生视频的 prompt 取自镜头的画面提示词（缺则回落文案）；无 shots 即无可生成的镜头。")
    if not videogen.videogen_configured(cfg):
        return _degraded(
            "videogen-not-configured",
            f"未配置图生视频密钥 {cfg.videogen_key_env}——本步跳过（成片回落静图）",
            "开启「图生视频」开关即需配置该密钥；未配置时如实降级，绝不伪造片段。")

    warnings: list[str] = []
    # 文案 / 画面提示词**从 EDL 读**（ADR-070/073）：镜头在 timeline 里经收口（并/拆）后 id 会重排为
    # `s1..sK`，终版镜头的文案与画面提示词已随镜落进 `edl.shots[]`——回查 inp.shots 会**错配**。
    # 兜底：EDL 无该字段（旧快照）时按 id 回查下发的 shots（尽力而为，不臆造）。
    copy_by_shot = {s.id: s.copy_text for s in inp.shots}
    visual_by_shot = {s.id: s.visualPrompt for s in inp.shots}

    # ── 组作业：逐镜（图 + 画面提示词 + 建议时长）──
    jobs: list[tuple[str, int, int, str, str, str]] = []
    fell_back = 0
    for shot in edl.shots:
        if not shot.imageSourceKey:
            warnings.append(f"镜头 {shot.id} 无图（timeline 已留空位）——跳过图生视频")
            continue
        # **画面提示词优先**（ADR-073）：把**台词**当画面提示词会让模型把字幕烧进画面，
        # 故顺序是 visualPrompt（EDL → 下发 shots）→ copy 兜底，并对兜底**如实计数**。
        visual = (shot.visualPrompt or visual_by_shot.get(shot.id, "")).strip()
        fallback_copy = (shot.copy or copy_by_shot.get(shot.id, "")).strip()
        prompt = visual or fallback_copy
        if not prompt:
            warnings.append(f"镜头 {shot.id} 无画面提示词也无文案（prompt 为空）——跳过图生视频（不凭空编 prompt）")
            continue
        if not visual:
            fell_back += 1
        jobs.append((
            shot.id, shot.atMs, shot.durationMs,
            prompt + _PROMPT_NEGATIVE + cfg.videogen_prompt_suffix,
            shot.imageSourceKey,
            videogen.seconds_str(shot.durationMs),
        ))
    if fell_back:
        warnings.append(
            f"{fell_back} 个镜头没有画面提示词（visualPrompt），已**回落用口播文案**出 prompt——"
            "台词容易被模型画成画面字幕，建议在镜头剪辑页为这些镜补画面提示词（ADR-073）。")
    if inp.maxShots and len(jobs) > inp.maxShots:
        warnings.append(f"超过 maxShots={inp.maxShots}：仅生成前 {inp.maxShots} 镜（其余回落静图）")
        jobs = jobs[:inp.maxShots]
    if not jobs:
        return SkillOutput(
            data=ImageToVideoData(warnings=warnings),
            reasoning=[common.chain("图生视频未生成：无可生成的镜头",
                                    risk_note="各镜缺图或缺文案；请补齐素材/文案后重跑。")],
            confidence=0.0, degraded=True, degradation_reason="no-generatable-shots")

    # ── 先全部提交（厂商侧并行跑），再逐个等 ──
    submitted: list[tuple[tuple[str, int, int, str, str, str], str]] = []
    for job in jobs:
        shot_id, _at, _dur, prompt, image_key, seconds = job
        try:
            task_id = videogen.submit(
                cfg, prompt=prompt, image_url=_presign(image_key), seconds=seconds)
        except videogen.DspVideoGenError as exc:
            warnings.append(f"镜头 {shot_id} 提交失败（{exc.category}）：{exc}——该镜回落静图")
            continue
        submitted.append((job, task_id))

    # ── 收片 → 转存（文档：直链临时保存 24h，绝不当 key）──
    clips: list[ShotClip] = []
    for job, task_id in submitted:
        shot_id, at_ms, duration_ms, _prompt, _img, _sec = job
        try:
            raw = videogen.wait(cfg, task_id)
            url = videogen.result_url(raw)
            key = common.step_key(inp.cachePrefix, inp.runId, "clips", f"{shot_id}.mp4")
            with tempfile.TemporaryDirectory(prefix="dsp-i2v-") as tmp:
                local = storage.fetch_to_file(url, str(Path(tmp) / f"{shot_id}.mp4"))
                storage.put_file(local, key, content_type="video/mp4")
        except (videogen.DspVideoGenError, storage.DspStorageError) as exc:
            warnings.append(f"镜头 {shot_id} 出片/转存失败：{exc}——该镜回落静图")
            continue
        clips.append(ShotClip(shotId=shot_id, key=key, atMs=at_ms, durationMs=duration_ms))

    failed = len(jobs) - len(clips)
    if not clips:
        return SkillOutput(
            data=ImageToVideoData(warnings=warnings),
            reasoning=[common.chain("图生视频全部失败：成片回落静图",
                                    risk_note="各镜均未出片；请检查密钥/余额/模型参数后重跑。")],
            confidence=0.0, degraded=True, degradation_reason="all-shots-failed")

    total_ms = sum(c.durationMs for c in clips)
    return SkillOutput(
        data=ImageToVideoData(clips=clips, warnings=warnings),
        reasoning=[common.chain(
            f"图生视频完成：{len(clips)}/{len(jobs)} 镜出片（共 {total_ms}ms）",
            causal_analysis=(
                f"逐镜把素材图（预签名 URL）+ **画面提示词**喂 {cfg.videogen_model}（prompt 末尾统一追加"
                "「不要出现文字/字幕/水印」的负向约束，ADR-073）；"
                f"时长按镜头建议时长（{clips[0].durationMs}ms 等）原样下发；"
                "直链（24h）已转存为不可变 key。"
            ),
            risk_note=(
                f"{failed} 镜未出片，render 侧将回落静图（如实标注）。"
                if failed else
                "全部镜头已出片；render 侧对有运镜的镜头再叠加 apply-camera-motion。"
            ),
        )],
        confidence=1.0 if not failed else 0.6,
        sample_size=len(clips),
        degraded=bool(failed),
        degradation_reason="partial-shots-failed" if failed else None,
    )
