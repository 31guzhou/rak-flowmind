"""火山 AI MediaKit「多轨道剪辑」客户端（契约 §7）。

调用面（契约 §7.1，API 文档 2026-10-12 对齐）：
- ``POST {base}/api/v1/tools/multi-track-edit``（提交，**同步返** ``{success, task_id, …}``）
- ``POST {base}/api/v1/tools/apply-camera-motion``（运镜，§7.5 第 2 步；**只吃视频**）
- ``GET  {base}/api/v1/tasks/{task_id}``（轮询至 ``completed``）
- 鉴权 ``Authorization: Bearer <API Key>``（集群 Secret，**密钥只在后端**）
- 形态 **异步任务**：``result{video_url, duration(秒), resolution}``；``video_url`` 是
  **24h 签名 URL**（§7.3②）——**不得**直接当 ``storedKey``，必须转存（见 render 技能）

参数模型（§7.1 / §7.2）：``canvas`` → ``track[][]``（**索引越大越上层**）→
``element{type, target_time, source|text, extra[]}`` → ``output{format, fps, cover}``。
主轨元素按镜头取：有 ``clip_urls``（图生视频片段）即 ``video``、否则回落 ``image``。

字段已按**入仓真文档**逐条核实（``docs/vendor/`` 四份：多轨道剪辑 37 页 /
提交多轨道剪辑任务 API 27 页 / 视频添加运镜 9 页 / 公共错误码 5 页）。关键核实点：
``extra_param.upload.aigc_meta`` 是**JSON 字符串**（非对象）；``output.cover.cover_time``
是**整数数组**；text/subtitle 元素**必须**给 ``extra.transform``；subtitle 轨独占且 ≤10 条；
``output.codec.*`` 是嵌套对象（本实现用默认值，不显式下发）；运镜 ``motion_style`` 四值、
``start_time``/``end_time`` 单位**秒**、输入**只接受视频**且 ≤1080p / ≤5 分钟。
错误码按**入仓真文档** ``docs/vendor/AIMediaKit_公共错误码_1775248816.pdf`` 分类
（火山公共错误码表，code 为**字符串名**，已按中文释义逐条核对；
**对照表见 ``docs/vendor/README.md``**）。

本模块只做「EDL → MediaKit 请求体」的映射与 HTTP 往返；**入参 / 出参形状与契约 §3
一字不改**（MediaKit 是 ``render_compose`` 的后端实现细节）。
"""
from __future__ import annotations

import json
import time

import requests

from flowmind.config import DspConfig
from flowmind.skills import _dsp_common as common
from flowmind.skills._secrets import get_api_key

_TERMINAL_OK = frozenset({"completed", "success", "done", "finished", "succeeded"})
_TERMINAL_FAIL = frozenset({"failed", "error", "canceled", "cancelled", "timeout"})

# 错误码分类（真源 docs/vendor/AIMediaKit_公共错误码_1775248816.pdf，已按中文释义逐条核对）。
# 该文档是火山「公共错误码」表，**code 是字符串名**（非数字）、HTTP 状态随错误语义走；
# 分类按 name 判定（比按 HTTP 状态稳——同一个 name 可能跨多状态返回）。
# **可重试** = 服务端临时故障 / 限流，稍后重试可恢复。
_MK_TRANSIENT_CODES = frozenset({
    "UndefinedError",  # 未知错误（500）—— 未定性，按可重试处理
    "InternalError", "InternalServiceError", "InternalServiceTimeout", "FailToConnect",
    "ServiceUnavailable", "ServiceUnavailableTemp", "Unavailable", "ServiceTimeout",
    "ServerIsBusy", "Busy", "Timeout",
    "FlowLimitExceeded", "RequestLimitExceeded", "LimitExceeded",
    "ResourceUnavailable",  # 资源不可用（含欠费等临时状态）
    "ResourceInUse",  # 资源被占用（409，临时冲突）
})
# **需查环境** = 鉴权 / 签名 / 策略 / 配额问题，别重试（改配置或提工单）。
_MK_ENVIRONMENT_CODES = frozenset({
    "MissingAuthenticationToken", "MissingSignature", "MissingRequestInfo",
    "InvalidClientTokenId", "SignatureDoesNotMatch", "InvalidTimestamp",
    "LackPolicy", "LackProjectPolicy", "AccessDenied", "Forbidden", "RequestForbidden",
    "OperationDenied", "Unauthorized", "UnauthorizedOperation",
    "QuotaInsufficient", "Insufficient", "ResourceInsufficient",
})


class DspMediakitError(Exception):
    """MediaKit 调用失败。category/retriable 语义与 errors.py 对齐。"""

    def __init__(self, message: str, category: str = "unknown", retriable: bool = False):
        super().__init__(message)
        self.category = category
        self.retriable = retriable


def _mk_error(raw: object) -> tuple[str, str]:
    """从响应体里取 ``(code, message)``；兼容 ``error{}`` 与顶层 ``code``/``message``。"""
    if not isinstance(raw, dict):
        return "", ""
    err = raw.get("error")
    if isinstance(err, dict) and err:
        return str(err.get("code") or ""), str(err.get("message") or "")
    code = raw.get("code")
    if isinstance(code, str) and code and code.lower() != "success":
        return code, str(raw.get("message") or "")
    return "", ""


def _classify_mk_error(code: str) -> tuple[str, bool]:
    """按 MediaKit 错误码名分类为 ``(category, retriable)``。

    未登记的码一律 ``video``（参数 / 资源问题——改输入，别重试）。
    """
    if code in _MK_TRANSIENT_CODES:
        return "transient", True
    if code in _MK_ENVIRONMENT_CODES:
        return "environment", False
    return "video", False



def mediakit_configured(cfg: DspConfig) -> bool:
    """是否已配置 MediaKit 密钥。"""
    return bool(get_api_key(cfg.mediakit_key_env))


# ── 运镜（apply-camera-motion · 契约 §7.5 第 2 步）──────────────────
# 文档核实（docs/vendor/智能处理_视频添加运镜_1785416057.pdf）：`apply-camera-motion`
# **只接受视频**（`video_url`），故运镜必须在「图片已成视频」之后——本仓由 `video` 节点
# （图生视频）产片段，render 侧再对片段加运镜。
MOTION_STYLES: tuple[str, ...] = ("zoom", "pan-zoom", "orbit-360", "bounce")
# 本仓 EDL ``effect`` → MediaKit ``motion_style``（与前端 dsp-steps.ts 的
# DSP_EFFECT_VENDOR_MOTION 同口径：kenburns → pan-zoom）。
EFFECT_MOTION_STYLE: dict[str, str] = {"kenburns": "pan-zoom"}


def motion_style_for(effect: str | None) -> str:
    """EDL ``effect`` → MediaKit ``motion_style``；未登记即返回空串（调用方如实标注）。"""
    return EFFECT_MOTION_STYLE.get((effect or "").strip(), "")


# ── 转场（extra 滤镜 · ADR-070）──────────────────────────────────
# 厂商表达方式（契约 §7.2）：转场是**加在相邻两元素的「前一个」上的 extra 滤镜**——
# ``extra: [{type: "transition", source: <资源 ID>, duration: <ms>}]``。
# **只允许「非交叠」**：音轨是一整条实测音频、时间线严格连续，交叠会移动画面内容或改总时长
# ⇒ 音画失同步（硬约束，见 `_dsp_common.DspEdlSegment.transition`）。
#
# ⚠️ **本表当前为空**：火山 MediaKit 的**转场资源 ID 表不在本仓**（契约 §7.2 记着「文档附完整资源
# ID 表」，那份文档未落到仓内）。故**未登记即不可用**——`resolve_transition` 返回 None、
# `render_compose` 侧**如实回落硬切并告警**。**绝不编造 ID**（数据诚实）。
# 拿到 ID 表后只填这一张表（与 cross-dashboard 的 `DSP_TRANSITION_VENDOR` 同源、同值）。
TRANSITION_VENDOR: dict[str, dict[str, object]] = {
    # 例："dissolve": {"id": "vcr//????", "mode": "non-overlap", "durationMs": 400},
}


def resolve_transition(name: str | None) -> dict[str, object] | None:
    """EDL ``transition``（符号名）→ MediaKit ``extra`` 滤镜的取值；**未登记即返回 None**。"""
    key = (name or "").strip()
    if not key:
        return None
    entry = TRANSITION_VENDOR.get(key)
    if not entry or not str(entry.get("id") or "").strip():
        return None
    return entry


def transition_filters(seg: common.DspEdlSegment) -> list[dict]:
    """该段末尾的转场 → ``extra`` 滤镜列表（**未登记 = 空列表 = 硬切**）。"""
    entry = resolve_transition(seg.transition)
    if not entry:
        return []
    return [{
        "type": "transition",
        "source": entry["id"],
        "duration": int(entry.get("durationMs") or 400),  # type: ignore[arg-type]
    }]


def transition_warnings(edl: common.DspEdl) -> list[str]:
    """未登记 / 非「非交叠」的转场 → 如实的降级说明（``render_compose`` 并入 ``warnings``）。"""
    warnings: list[str] = []
    for track in edl.tracks:
        if track.kind != "image":
            continue
        for seg in track.segments:
            name = (seg.transition or "").strip()
            if not name:
                continue
            entry = TRANSITION_VENDOR.get(name)
            if not entry:
                warnings.append(
                    f"转场 {name} 未登记厂商资源 ID，已回落**硬切**（不编造 ID；登记后自动生效）。"
                )
            elif str(entry.get("mode") or "") != "non-overlap":
                warnings.append(
                    f"转场 {name} 只支持交叠模式，与「一整条配音音频严格对齐」冲突，已回落**硬切**。"
                )
    return warnings


# ── EDL → track[][]（契约 §7.2 逐字段映射）────────────────────────

def build_tracks(
    edl: common.DspEdl,
    *,
    image_urls: dict[str, str],
    clip_urls: dict[str, str] | None = None,
    audio_url: str = "",
    bgm_url: str = "",
    srt_url: str = "",
    subtitle_font_size: int = 48,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
) -> list[list[dict]]:
    """把本仓 EDL 翻成 MediaKit ``track[][]``（索引越大越上层）。

    - 主轨 = ``image`` / ``video``：``clip_urls``（**按镜头 id**）命中即用 ``video`` 元素
      （图生视频产出的片段，已含运镜），否则回落 ``image``（静图）；两者**无 trim**、
      ``target_time`` 为绝对区间 ms（§7.2）
    - ``vo`` / ``bgm`` → ``audio`` 元素（走混音，非覆盖）
    - ``subtitle`` → ``subtitle`` 元素，**独占一轨**（文档：subtitle 轨 ≤ 10 条），
      且 text/subtitle 元素**必须**在 ``extra`` 里给 ``transform``（文档要求四字段齐全）

    封面走 ``output.cover`` 抽帧（见 ``build_body``），**不**做成叠层图片；
    小黄车贴片是**平台组件**（商品链接不烧进画面，§7.2），同样不入轨。

    **转场**（ADR-070）：读该段末尾的 ``transition``（符号名），经 ``TRANSITION_VENDOR`` 解析成
    ``extra`` 滤镜**加在「前一个」元素上**（厂商口径）；**未登记即不加**（= 硬切，由
    ``transition_warnings`` 如实说明）。

    ``edl.shots`` 与 image 轨 ``segments`` **索引对齐**（timeline_build 保证），
    故用 shots[i].id 取该段的片段；无 shots 时全部回落静图。
    """
    tracks: list[list[dict]] = []
    clips = clip_urls or {}
    shots = edl.shots

    media_elements: list[dict] = []
    for track in edl.tracks:
        if track.kind != "image":
            continue
        for idx, seg in enumerate(track.segments):
            span = [seg.atMs, seg.atMs + seg.durationMs]
            shot_id = shots[idx].id if idx < len(shots) else ""
            clip = clips.get(shot_id, "")
            if clip:
                elem: dict = {"type": "video", "source": clip, "target_time": span}
            else:
                src = image_urls.get(seg.sourceKey, "")
                if not src:
                    continue  # 留空位的镜头：无图可取，如实跳过（timeline 已在 notes 说明）
                elem = {"type": "image", "source": src, "target_time": span}
            extra = transition_filters(seg)
            if extra:
                elem["extra"] = extra
            media_elements.append(elem)
    if media_elements:
        tracks.append(media_elements)

    if audio_url:
        tracks.append([{
            "type": "audio",
            "source": audio_url,
            "target_time": [0, edl.totalMs],
        }])
    if bgm_url:
        tracks.append([{
            "type": "audio",
            "source": bgm_url,
            "target_time": [0, edl.totalMs],
            # 配音起时压低 BGM（§7.2：音量 / 闪避写在 extra）
            "extra": [{"type": "a_volume", "volume": 0.4}],
        }])
    if srt_url:
        tracks.append([{
            "type": "subtitle",
            "text": srt_url,
            "target_time": [0, edl.totalMs],
            "font_size": subtitle_font_size,
            "align_type": 1,  # 横排居中（文档 align_type：横排 0 左 / 1 中 / 2 右）
            "extra": [{
                "type": "transform",
                "pos_x": 0, "pos_y": 0,
                "width": canvas_width, "height": canvas_height,
            }],
        }])

    return tracks


def build_body(
    cfg: DspConfig,
    *,
    tracks: list[list[dict]],
    client_token: str,
    cover_at_ms: int | None = None,
) -> dict:
    """组装提交体（canvas / track / output / 幂等 token / AIGC 元数据）。

    ``cover_at_ms`` 给定即用 MediaKit ``output.cover`` **抽帧**导出封面（§7.1）。
    """
    token = (client_token or "dsp")[:64]  # 契约：client_token ≤ 64 ASCII
    output: dict = {
        "format": cfg.mediakit_output_format,
        "fps": cfg.mediakit_output_fps,
    }
    if cover_at_ms is not None:
        output["cover"] = {
            "disable_cover": False,
            "cover_time": [max(0, int(cover_at_ms))],  # 文档：cover_time 为**整数数组**（ms）
            "format": "jpeg",
        }
    body: dict = {
        "canvas": {
            "width": cfg.mediakit_canvas_width,
            "height": cfg.mediakit_canvas_height,
            "background_color": "#000000FF",
        },
        "track": tracks,
        "output": output,
        "client_token": token,
    }
    if cfg.mediakit_queue_id:
        body["queue_id"] = cfg.mediakit_queue_id
    if cfg.mediakit_output_destination:
        # §7.3② 更优解：结果直写自己的 TOS，从源头免去 24h 转存
        body["media_output_destination"] = cfg.mediakit_output_destination
    if cfg.mediakit_aigc_label:
        # §7.3④ AIGC 标识合规（成片含 AI 生成内容：AI 补生图 / TTS 口播）
        # 文档核实：extra_param.upload.aigc_meta 是**JSON 字符串**（不是对象）——
        # 内层键 Label / ContentProducer / ProduceID / …
        body["extra_param"] = {"upload": {"aigc_meta": json.dumps(
            {"Label": cfg.mediakit_aigc_label, "ProduceID": token},
            ensure_ascii=False,
        )}}
    return body


def _post(cfg: DspConfig, api_key: str, path: str, body: dict) -> dict:
    url = cfg.mediakit_api_base.rstrip("/") + path
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=cfg.mediakit_timeout_s)
    except requests.exceptions.Timeout as exc:
        raise DspMediakitError(f"MediaKit 提交超时（{url}）", category="environment") from exc
    except requests.exceptions.RequestException as exc:
        raise DspMediakitError(f"MediaKit 连接失败：{exc}", category="environment") from exc

    # 先解析体里的错误码名（文档：code 是字符串名，跨多 HTTP 状态——按名分类比按状态稳）；
    # 拿不到码名才回落 HTTP 状态。
    try:
        raw = resp.json()
    except ValueError:
        raw = None

    code, message = _mk_error(raw)
    if not resp.ok and code:
        category, retriable = _classify_mk_error(code)
        raise DspMediakitError(
            f"MediaKit 错误 {code}（HTTP {resp.status_code}）：{message or '(无 message)'}",
            category=category, retriable=retriable,
        )
    if resp.status_code == 429:
        raise DspMediakitError("MediaKit 限流（429）", category="transient", retriable=True)
    if resp.status_code >= 500:
        raise DspMediakitError(f"MediaKit 服务端错误（{resp.status_code}）", category="transient",
                               retriable=True)
    if resp.status_code >= 400:
        raise DspMediakitError(f"MediaKit 拒绝请求（{resp.status_code}）：{resp.text[:300]}",
                               category="video")
    if not isinstance(raw, dict):
        raise DspMediakitError("MediaKit 返回非 JSON", category="video")
    return raw


def submit(cfg: DspConfig, body: dict) -> str:
    """提交多轨道剪辑任务，返回 ``task_id``。"""
    api_key = get_api_key(cfg.mediakit_key_env)
    if not api_key:
        raise DspMediakitError(
            f"未配置 MediaKit 密钥 {cfg.mediakit_key_env}（dsp 渲染必需）。", category="environment")
    raw = _post(cfg, api_key, cfg.mediakit_multi_track_path, body)
    if not raw.get("success", True):
        code, message = _mk_error(raw)
        category, retriable = _classify_mk_error(code)
        raise DspMediakitError(
            f"MediaKit 提交失败：{code} {message}".strip(),
            category=category, retriable=retriable,
        )
    task_id = raw.get("task_id") or raw.get("data", {}).get("task_id")
    if not task_id:
        raise DspMediakitError(f"MediaKit 响应缺少 task_id：{sorted(raw)[:12]}", category="video")
    return str(task_id)


def _query(cfg: DspConfig, api_key: str, task_id: str) -> dict:
    url = cfg.mediakit_api_base.rstrip("/") + cfg.mediakit_task_path.format(task_id=task_id)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(url, headers=headers, timeout=cfg.mediakit_timeout_s)
    except requests.exceptions.RequestException as exc:
        raise DspMediakitError(f"MediaKit 查询失败：{exc}", category="transient",
                               retriable=True) from exc
    try:
        raw = resp.json()
    except ValueError:
        raw = None

    code, message = _mk_error(raw)
    if not resp.ok and code:
        category, retriable = _classify_mk_error(code)
        raise DspMediakitError(
            f"MediaKit 查询错误 {code}（HTTP {resp.status_code}）：{message or '(无 message)'}",
            category=category, retriable=retriable,
        )
    if resp.status_code >= 500:
        raise DspMediakitError(f"MediaKit 查询服务端错误（{resp.status_code}）",
                               category="transient", retriable=True)
    if resp.status_code >= 400:
        raise DspMediakitError(f"MediaKit 查询被拒（{resp.status_code}）：{resp.text[:200]}",
                               category="video")
    if not isinstance(raw, dict):
        raise DspMediakitError("MediaKit 查询返回非 JSON", category="video")
    return raw


def render(cfg: DspConfig, body: dict) -> dict:
    """提交多轨道剪辑并轮询至终态，返回 ``result``（``{video_url, duration, resolution}``）。

    轮询上限 ``mediakit_poll_max_s``——超时**如实报错**，不假装完成。
    """
    api_key = get_api_key(cfg.mediakit_key_env)
    if not api_key:
        raise DspMediakitError(
            f"未配置 MediaKit 密钥 {cfg.mediakit_key_env}（dsp 渲染必需）。", category="environment")
    task_id = submit(cfg, body)
    return _wait_result(cfg, api_key, task_id, label="渲染")


def _wait_result(cfg: DspConfig, api_key: str, task_id: str, *, label: str) -> dict:
    """轮询至终态并返回 ``result``（``{video_url, duration, resolution}``）。

    ``label`` 只用于错误消息（渲染 / 运镜），行为一致；超时**如实报错**，不假装完成。
    """
    deadline = time.monotonic() + cfg.mediakit_poll_max_s
    last_status = "unknown"
    while True:
        raw = _query(cfg, api_key, task_id)
        status = str(raw.get("status") or raw.get("data", {}).get("status") or "").lower()
        if status:
            last_status = status
        if status in _TERMINAL_OK:
            result = raw.get("result") or raw.get("data", {}).get("result") or {}
            if not isinstance(result, dict) or not result.get("video_url"):
                raise DspMediakitError(
                    f"MediaKit {label}任务完成但缺少 result.video_url：{sorted(raw)[:12]}",
                    category="video")
            return {"task_id": task_id, **result}
        if status in _TERMINAL_FAIL:
            code, message = _mk_error(raw)
            category, retriable = _classify_mk_error(code)
            raise DspMediakitError(
                f"MediaKit {label}失败（status={status}）：{code} {message}".strip(),
                category=category, retriable=retriable,
            )
        if time.monotonic() >= deadline:
            raise DspMediakitError(
                f"MediaKit {label}超时（>{cfg.mediakit_poll_max_s}s，最后状态 {last_status}）",
                category="transient", retriable=True,
            )
        time.sleep(cfg.mediakit_poll_interval_s)


def apply_camera_motion(
    cfg: DspConfig, *, video_url: str, motion_style: str,
    start_s: float | None = None, end_s: float | None = None,
) -> dict:
    """对**视频**加运镜（§7.5 第 2 步），返回 ``result``（``{video_url, duration, resolution}``）。

    文档核实（``docs/vendor/智能处理_视频添加运镜_1785416057.pdf``）：
    - 入参 ``video_url`` **只接受视频**（故必须跑在「图片已成视频」之后）；
    - ``motion_style`` ∈ :data:`MOTION_STYLES`；
    - ``start_time`` / ``end_time`` 单位**秒**（可小数），缺省 = 叠加到整段；
    - 异步任务（提交返 ``task_id`` → 轮询 ``completed``）；输出固定 MP4、**分辨率对齐原片**。

    与 :func:`render` 同服务、同集群 Secret、同 ``task_id`` 轮询口径。
    """
    api_key = get_api_key(cfg.mediakit_key_env)
    if not api_key:
        raise DspMediakitError(
            f"未配置 MediaKit 密钥 {cfg.mediakit_key_env}（运镜必需）。", category="environment")
    if not video_url:
        raise DspMediakitError("运镜需要视频底（video_url 为空）。", category="video")
    if motion_style not in MOTION_STYLES:
        raise DspMediakitError(
            f"未知运镜样式 {motion_style!r}（可选：{'/'.join(MOTION_STYLES)}）", category="video")

    body: dict = {"video_url": video_url, "motion_style": motion_style}
    if start_s is not None:
        body["start_time"] = float(start_s)
    if end_s is not None:
        body["end_time"] = float(end_s)

    raw = _post(cfg, api_key, cfg.mediakit_camera_motion_path, body)
    if not raw.get("success", True):
        code, message = _mk_error(raw)
        category, retriable = _classify_mk_error(code)
        raise DspMediakitError(
            f"运镜提交失败：{code} {message}".strip(), category=category, retriable=retriable)
    task_id = raw.get("task_id") or raw.get("data", {}).get("task_id")
    if not task_id:
        raise DspMediakitError(
            f"运镜响应缺少 task_id：{sorted(raw)[:12]}", category="video")
    return _wait_result(cfg, api_key, str(task_id), label="运镜")
