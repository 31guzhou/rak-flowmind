"""图生视频客户端（晨星 AI · ``api.chenxingai.vip``，契约 §7.5 第 1 步）。

**厂商位置**：这是**独立第三方聚合**（转售 Seedance / MiniMax 等），与火山
MediaKit（渲染）、豆包语音（配音）**不共享端点与密钥**——接口字段按**用户提供的
接口文档**实现（本仓无 PDF，故不登记 ``docs/vendor/`` 错误码表）。

调用面（文档原文）：
- ``POST {base}/videos/generations``（提交）→ 平铺 ``{id, task_id, object, model,
  status, progress, size}``；任务 id 读 ``id``（``task_id`` 同值）
- ``GET  {base}/videos/{task_id}``（轮询）→ 状态 ``queued`` / ``in_progress`` /
  ``processing`` / ``completed`` / ``succeeded`` / ``failed``；建议轮询 5–10 秒
- 完成取 ``metadata.url``（**真实 mp4 直链，无需鉴权**；亦见 ``url`` 字段），
  或带鉴权代理 ``GET /videos/{task_id}/content``
- 鉴权 ``Authorization: Bearer sk-…``（集群 Secret，**密钥只在后端**）

⚠️ **视频临时保存 24 小时**（文档）——故产物**必须转存**，不得把直链当 key（§7.3② 同款纪律）。

⚠️ **``seconds`` 取值「视模型而定」**（文档原话），**不保证连续整数**。本实现照
镜头建议时长**原样下发**；厂商拒绝即抛结构化错误（不偷偷夹取 / 不编造时长）。

⚠️ **首尾帧必须走 ``metadata.content`` + ``role``**（ADR-126）。聚合商的公开文档只写
``image`` / ``images``，但 new-api 的 Doubao 适配器把 ``images`` 转成上游 ``content`` 时
**不写 role**，上游直接 400 ``role must be specified for image contents``（2026-10-18 真跑
s1/s3 即此）。现行请求体：顶层 ``prompt`` / ``duration`` / ``size`` + ``metadata.content``
（text + ``first_frame`` + ``last_frame``）/ ``metadata.resolution`` / ``metadata.ratio``。
``metadata.content`` 会整段替换适配器拼出的 content，故文本也一并放进去。

⚠️ **运镜（ADR-076）**：本聚合端点**没有任何运动入参**（请求体只有 model/prompt/aspect_ratio/
size/seconds/quality/image/images）。故运镜**只能经提示词语言**表达——`motion_expression()`
是「本仓运镜符号 → 引擎表达」的**单点适配器**，返回值里的 ``params`` 当前**恒空**（不传）；
**引擎将来透出运动参数时，只改这一张表**（与前端 `lib/content/dsp-motion.ts` 的
`DSP_MOTION_ENGINE` **同源同值**）。**效果（模型是否真按提示词动镜头）待真跑验证。**
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from flowmind.config import DspConfig
from flowmind.skills._secrets import get_api_key

logger = logging.getLogger("flowmind.dsp.videogen")

_TERMINAL_OK = frozenset({"completed", "succeeded", "success", "done", "finished"})
_TERMINAL_FAIL = frozenset({"failed", "error", "canceled", "cancelled", "timeout"})

# 错误分类（文档给出的 error.code → 本仓三分类；语义对齐 errors.py）。
# **可重试** = 服务端临时故障 / 限流。
_VIDEOGEN_TRANSIENT_CODES = frozenset({
    "api_error",            # 服务内部错误，建议重试
    "rate_limit_exceeded",  # 请求频率过高
    "server_error", "internal_error", "timeout",
})
# **需查环境** = 鉴权 / 计费问题，别重试（改配置或充值）。
_VIDEOGEN_ENVIRONMENT_CODES = frozenset({
    "authentication_error",   # API Key 无效或未提供
    "insufficient_quota",     # 账户余额不足
    "permission_error", "account_deactivated",
})
# HTTP 状态 → 分类。**403 不是可重试错误**：它要么是网关/令牌权限，要么是上游渠道权限，
# 无限重试只会重复计费且掩盖根因（ADR-126）。
_HTTP_TRANSIENT = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_HTTP_PERMISSION = frozenset({401, 403})
# 原始响应保留上限（诊断要原文，但不能把 HTML 整页灌进日志/note）。
_RAW_EXCERPT_LIMIT = 2_000
_REQUEST_ID_HEADERS = (
    "x-request-id", "x-oneapi-request-id", "openai-request-id", "x-trace-id", "cf-ray",
    # TOS / S3 / 火山：直链下载的请求 id 在这些头里（不是 x-request-id）
    "x-tos-request-id", "x-amz-request-id", "x-volc-request-id",
)
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>]+)\?[^\s\"'<>]*")


def _redact_url(url: str) -> str:
    """去掉预签名查询串（SigV4 把凭证签在 query 里）——只留 scheme/host/path。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url>"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_text(text: str, *, limit: int = _RAW_EXCERPT_LIMIT) -> str:
    """诊断文本脱敏：Authorization/Bearer、sk- 密钥、URL 查询串。

    保留原文的**结构与错误码**，只替换凭证；截断加省略号（不静默丢长度）。
    """
    out = text or ""
    out = re.sub(r"(?i)(authorization\s*[:=]\s*)(\S+)", r"\1***", out)
    out = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***", out)
    out = re.sub(r"\bsk-[A-Za-z0-9._-]{8,}", "sk-***", out)
    out = _URL_QUERY_RE.sub(lambda m: _redact_url(m.group(1)), out)
    if len(out) > limit:
        out = out[:limit] + f"…（已截断，共 {len(text)} 字符）"
    return out


def _request_id_of(headers: requests.structures.CaseInsensitiveDict | dict | None) -> str:
    if not headers:
        return ""
    for name in _REQUEST_ID_HEADERS:
        value = headers.get(name)
        if value:
            return str(value)
    return ""


def _raw_text_of(resp: requests.Response) -> str:
    """返回原始响应文本；`resp.text` 在非 UTF-8 时也不会抛（requests 会兜底）。"""
    try:
        return resp.text or ""
    except Exception:  # noqa: BLE001 - 诊断不能因解码失败而丢原文
        try:
            return resp.content.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""


class DspVideoGenError(Exception):
    """图生视频调用失败。category/retriable 语义与 errors.py 对齐。

    诊断字段（ADR-126）随异常一路带到技能层，再落进逐镜状态与日志：
    `status` HTTP 码、`code` 厂商错误码、`request_id` 厂商/网关请求 id、
    `raw_excerpt` 脱敏后的原始响应、`stage` 提交/轮询、`global_permission`
    （鉴权/权限类失败——调用方据此**暂停同批剩余提交**）。
    """

    def __init__(
        self,
        message: str,
        category: str = "unknown",
        retriable: bool = False,
        *,
        status: int = 0,
        code: str = "",
        request_id: str = "",
        raw_excerpt: str = "",
        stage: str = "",
        global_permission: bool = False,
    ):
        super().__init__(message)
        self.category = category
        self.retriable = retriable
        self.status = status
        self.code = code
        self.request_id = request_id
        self.raw_excerpt = raw_excerpt
        self.stage = stage
        self.global_permission = global_permission

    def diagnostics(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "status": self.status,
            "code": self.code,
            "category": self.category,
            "retriable": self.retriable,
            "requestId": self.request_id,
            "globalPermission": self.global_permission,
            "raw": self.raw_excerpt,
        }


def videogen_configured(cfg: DspConfig) -> bool:
    """是否已配置图生视频密钥。"""
    return bool(get_api_key(cfg.videogen_key_env))


def _classify_videogen_error(code: str) -> tuple[str, bool]:
    """按厂商 ``error.code`` 分类为 ``(category, retriable)``；未登记兜底 ``video``。"""
    key = (code or "").strip()
    if key in _VIDEOGEN_TRANSIENT_CODES:
        return "transient", True
    if key in _VIDEOGEN_ENVIRONMENT_CODES:
        return "environment", False
    return "video", False


def _error_of(raw: object) -> tuple[str, str]:
    """从响应体取 ``(code, message)``（文档：``{"error": {"code", "message"}}``）。"""
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, dict) and err:
            return str(err.get("code") or ""), str(err.get("message") or "")
        if isinstance(err, str) and err:
            return err, str(raw.get("message") or "")
    return "", ""


def _headers(cfg: DspConfig, api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _classify_http(status: int, code: str) -> tuple[str, bool]:
    """HTTP 状态 + 厂商 code → (category, retriable)。

    规则与厂商文档一致：**只有临时故障/限流可重试**；401/403 一律不可重试；
    400/404/422 是请求协议或资源问题，也不重试（改 prompt/参数后重跑才有意义）。
    """
    category, retriable = _classify_videogen_error(code)
    if status in _HTTP_PERMISSION:
        return "environment", False
    if status in _HTTP_TRANSIENT:
        return "transient", True
    if category == "video":
        # 未登记的 code 且是 4xx：按请求/资源问题处理，不重试。
        return ("transient", True) if status in _HTTP_TRANSIENT else ("video", False)
    return category, retriable


def _classify_body_case(code: str, message: str) -> str:
    """从厂商错误文本里识别鉴权/权限类失败（大小写不敏感，英文/中文都覆盖）。"""
    text = f"{code} {message}".lower()
    if any(word in text for word in (
        "invalidparameter", "role must be specified", "invalid request", "invalid_request",
        "not valid", "unsupported", "bad request",
    )):
        return "request"
    if any(word in text for word in (
        "permission", "forbidden", "unauthorized", "authentication", "access denied",
        "权限", "鉴权", "无权限", "余额", "配额", "quota",
    )):
        return "permission"
    return ""


def _raise_for(
    action: str,
    *,
    status: int,
    raw_text: str,
    raw_json: object,
    request_id: str,
    context: dict[str, object] | None = None,
) -> None:
    """把一次失败的厂商响应转成**带完整诊断**的异常。

    关键纪律（ADR-126）：
    - **先保原文**：响应不是 JSON / 是 HTML / 为空时，`raw_text` 仍然进异常与日志；
    - 错误列表里**只出现脱敏后**的内容（Authorization / sk- / URL query 不外泄）；
    - 403 的 `global_permission=True` 由调用方用来**暂停同批剩余镜头**。
    """
    code, message = _error_of(raw_json)
    excerpt = sanitize_text(raw_text)
    category, retriable = _classify_http(status, code)
    body_kind = _classify_body_case(code, message)
    if body_kind == "permission" and status >= 400:
        category, retriable = "environment", False
    global_permission = status in _HTTP_PERMISSION or body_kind == "permission"
    detail = (f"{code or '未知'} {message}".strip()
              + (f"（原始响应：{excerpt}）" if excerpt else "（响应体为空）"))
    err = DspVideoGenError(
        f"图生视频{action}失败（HTTP {status}）：{detail}",
        category=category,
        retriable=retriable,
        status=status,
        code=code,
        request_id=request_id,
        raw_excerpt=excerpt,
        stage=action,
        global_permission=global_permission,
    )
    logger.warning(
        "[videogen] %s失败 status=%s category=%s retriable=%s requestId=%s"
        " provider=%s model=%s shot=%s attempt=%s raw=%r",
        action, status, category, retriable, request_id or "-",
        (context or {}).get("provider", "-"), (context or {}).get("model", "-"),
        (context or {}).get("shotId", "-"), (context or {}).get("attemptId", "-"),
        excerpt,
    )
    raise err


# ── 运镜适配（单点表，ADR-076）───────────────────────────────────
# 本仓运镜**符号**（与前端 `lib/content/dsp-motion.ts` 的 `DSP_MOTION_TYPES` 同源同值）。
MOTION_TYPES: tuple[str, ...] = (
    "none", "push_in", "pull_out", "pan_left", "pan_right",
    "tilt_up", "tilt_down", "zoom_in", "zoom_out", "orbit",
)
_INTENSITY_WORD: dict[str, str] = {
    "minimal": "极轻、几乎难以察觉", "subtle": "轻微", "standard": "平稳", "strong": "明显",
}
_DIRECTION_WORD: dict[str, str] = {
    "toward_subject": "朝主体", "away_from_subject": "背离主体",
    "left_to_right": "自左向右", "right_to_left": "自右向左", "none": "",
}
# 符号 → `{params, phrase}`。`params` = **引擎的运动入参**：本聚合端点没有该字段 ⇒ 一律空。
# 将来引擎透出运动参数时**只改这里**（不去猜别的模型认什么名字）。
MOTION_ENGINE: dict[str, dict[str, object]] = {
    "none": {"params": {}, "phrase": "摄影机保持固定"},
    "push_in": {"params": {}, "phrase": "摄影机缓慢推近"},
    "pull_out": {"params": {}, "phrase": "摄影机缓慢拉远"},
    "pan_left": {"params": {}, "phrase": "摄影机向左平移"},
    "pan_right": {"params": {}, "phrase": "摄影机向右平移"},
    "tilt_up": {"params": {}, "phrase": "摄影机上摇"},
    "tilt_down": {"params": {}, "phrase": "摄影机下摇"},
    "zoom_in": {"params": {}, "phrase": "镜头缓慢变焦推近"},
    "zoom_out": {"params": {}, "phrase": "镜头缓慢变焦拉远"},
    "orbit": {"params": {}, "phrase": "摄影机环绕主体"},
}

# **运镜关闭**时的固定机位条款（正向后缀）——「关」不等于「不做 I2V」，只是要求别动镜头。
# 与前端 `DSP_MOTION_STATIC_CLAUSE` **同源同值**。「画面动态强度」（连主体动态也不要）是**另一个**
# 控制项，将来单独设，**不并入本开关**——故这里明确**允许**主体/环境的自然动态。
MOTION_STATIC_CLAUSE = (
    "摄影机保持固定（固定机位、无推拉摇移、画面稳定不抖动），允许主体与环境发生自然的动态。"
)


def motion_expression(motion: object | None) -> tuple[str, dict[str, object]]:
    """**引擎适配器**：结构化运镜方案 → 该引擎的 ``(prompt 追加, 参数)``（ADR-076）。

    比照前端 `motionPromptFor` 的口径：
    - ``type == "none"`` ⇒ 出**固定机位条款**（不是空串——「不运镜」也要明确告诉模型别动镜头）；
    - 其余 ⇒ 优先用 Agent 给的 ``positive``（它看过图、能写到具体主体），缺则回落**确定性短语**
      （含幅度 / 方向 / 焦点 + 保真约束）；
    - **词表外的符号 ⇒ 空串 + 空参数**（调用方如实标注，不硬凑运动）；
    - ``params`` 当前**恒空**（本引擎无运动入参），是留给将来引擎透出参数的**接口位**。
    """
    if motion is None:
        return MOTION_STATIC_CLAUSE, {}
    mtype = str(getattr(motion, "type", "") or "").strip()
    entry = MOTION_ENGINE.get(mtype)
    if entry is None:
        return "", {}
    if mtype == "none":
        return MOTION_STATIC_CLAUSE, {}
    params = dict(entry["params"])  # type: ignore[arg-type]
    positive = str(getattr(motion, "positive", "") or "").strip()
    if positive:
        return positive, params
    intensity = _INTENSITY_WORD.get(str(getattr(motion, "intensity", "") or ""), "平稳")
    direction = _DIRECTION_WORD.get(str(getattr(motion, "direction", "") or ""), "")
    target = str(getattr(motion, "target", "") or "").strip()
    phrase = (
        f"{entry['phrase']}（幅度{intensity}）"
        f"{f'，{direction}' if direction else ''}"
        f"{f'，焦点是{target}' if target else ''}"
        "；保持原图主体的形状、颜色与构图不变，光照自然、运动真实"
    )
    return phrase, params


def seconds_str(ms: int) -> str:
    """镜头时长（ms）→ 厂商 ``seconds`` 字符串。

    文档：``seconds``「取值视模型而定（字符串或整数）」——本仓**原样下发**（整秒不带小数，
    否则保留一位），**不夹取到某枚举**（不替厂商猜）。
    """
    value = max(ms, 0) / 1000.0
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def parse_size(size: str) -> tuple[int, int]:
    """``"720x1280"`` → ``(720, 1280)``；形状不对返回 ``(0, 0)``（调用方据此**跳过**降采样）。"""
    parts = (size or "").lower().split("x")
    if len(parts) != 2:
        return 0, 0
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0
    return (w, h) if w > 0 and h > 0 else (0, 0)


def duration_value(seconds: str) -> int:
    """镜头秒数 → 厂商 ``duration``（**整数秒**，至少 1）。

    2026-10-17 真调：该接口认 ``duration``（int），旧请求体里的 ``seconds`` / ``aspect_ratio`` /
    ``quality`` 都不是它认识的字段——``seedance-2.5-global-sp`` 的 ``invalid_request`` 正是由此暴露。
    镜头窗口是 2.7–5.6s ⇒ 四舍五入到整秒（成片侧仍会裁到锁定窗口，ADR-080）。
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 4.0
    return max(1, int(round(value)))


def _resolution_of(size: str) -> str:
    """``720x1280`` → 上游分辨率档位 ``720p``（Seedance 认小写档位）。"""
    w, h = parse_size(size)
    longest = max(w, h)
    if longest >= 1920:
        return "1080p"
    if longest >= 1280:
        return "720p"
    return "480p"


def _ratio_of(size: str) -> str:
    """``720x1280`` → ``9:16``；认不出的形状交给上游自适应。"""
    w, h = parse_size(size)
    if not (w and h):
        return "adaptive"
    if w == h:
        return "1:1"
    ratio = w / h
    if abs(ratio - 9 / 16) < 0.01:
        return "9:16"
    if abs(ratio - 16 / 9) < 0.01:
        return "16:9"
    return "adaptive"


def build_payload(
    cfg: DspConfig, *, prompt: str, image_url: str, seconds: str, tail_image_url: str = "",
    motion_params: dict[str, object] | None = None,
) -> dict:
    """按**当前**接口组装提交体（ADR-111 真调 + ADR-126 首尾帧协议收口）。

    **统一图生视频与首尾帧桥**：只要带图，就走同一条 ``metadata.content``：

    ```json
    {"type": "image_url", "image_url": {"url": "..."}, "role": "first_frame"}
    {"type": "image_url", "image_url": {"url": "..."}, "role": "last_frame"}
    ```

    为什么不能再用顶层 ``image`` / ``images``：new-api 的 Doubao 适配器把 ``images``
    转成上游 ``content`` 时**不写 role**，上游直接 400
    ``role must be specified for image contents``（2026-10-18 真跑 s1/s3 即此）。
    ``metadata.content`` 会被适配器**整段替换**进上游 content，因此文本也一并放进去；
    顶层 ``prompt`` 保留（网关校验与计费要读）。

    ``motion_params`` = **运镜的引擎参数位**（ADR-076）。本聚合端点**没有**运动字段，故
    ``motion_expression()`` 当前恒返回空 dict、这里**一个键都不会并进去**（空即不传）。

    ⚠️ **不注入** ``aspect_ratio`` / ``quality`` / ``seconds``：非该视频接口字段，真调会诱发
    上游 ``invalid_request``（ADR-111）。比例用 ``metadata.ratio``、时长用 ``duration``。
    """
    payload: dict = {
        "model": cfg.videogen_model,
        "prompt": prompt,
        "duration": duration_value(seconds),
        "size": cfg.videogen_size,
    }
    if image_url:
        has_tail = bool(tail_image_url and tail_image_url != image_url)
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        content.append({
            "type": "image_url",
            "image_url": {"url": image_url},
            "role": "first_frame",
        })
        if has_tail:
            content.append({
                "type": "image_url",
                "image_url": {"url": tail_image_url},
                "role": "last_frame",
            })
        # 首尾帧模式官方建议 adaptive：按两张图的真实画幅自适应，减少跳帧；
        # 单图仍按输出尺寸锁 9:16（成片是竖屏）。
        payload["metadata"] = {
            "content": content,
            "resolution": _resolution_of(cfg.videogen_size),
            "ratio": "adaptive" if has_tail else _ratio_of(cfg.videogen_size),
        }
    # 运镜参数位（ADR-076）：**空即不传**——本引擎为空，不往请求体里塞不认识的自造字段
    if motion_params:
        payload.update({k: v for k, v in motion_params.items() if k not in payload})
    return payload


def validate_payload(payload: dict) -> list[str]:
    """提交前的**本地协议预检**（不发请求、不花钱）。

    返回空列表 = 通过；否则是逐条问题。重点挡住 2026-10-18 真跑的那类错误：
    图片 content 缺 ``role``、首帧/尾帧顺序颠倒、URL 非 http(s)。
    """
    problems: list[str] = []
    if not str(payload.get("model") or "").strip():
        problems.append("缺少 model")
    if not str(payload.get("prompt") or "").strip():
        problems.append("缺少 prompt")
    duration = payload.get("duration")
    if not isinstance(duration, int) or duration < 1:
        problems.append(f"duration 必须是 ≥1 的整数（当前 {duration!r}）")
    meta = payload.get("metadata")
    if meta is None:
        return problems
    if not isinstance(meta, dict):
        problems.append("metadata 必须是对象")
        return problems
    content = meta.get("content")
    if not isinstance(content, list):
        problems.append("metadata.content 必须是数组")
        return problems
    roles: list[str] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            problems.append(f"metadata.content[{index}] 不是对象")
            continue
        kind = item.get("type")
        if kind == "text":
            if not str(item.get("text") or "").strip():
                problems.append(f"metadata.content[{index}] 文本为空")
            continue
        if kind != "image_url":
            problems.append(f"metadata.content[{index}] 未知类型 {kind!r}")
            continue
        image = item.get("image_url")
        url = str((image or {}).get("url") if isinstance(image, dict) else "")
        role = str(item.get("role") or "")
        if not url.startswith(("http://", "https://", "asset://")):
            problems.append(f"metadata.content[{index}] 图片 URL 不是 http(s)/asset://")
        if role not in ("first_frame", "last_frame", "reference_image"):
            problems.append(
                f"metadata.content[{index}] 图片必须带 role（first_frame/last_frame/reference_image），"
                f"当前 {role!r}")
        roles.append(role)
    image_roles = [r for r in roles if r]
    if image_roles.count("first_frame") > 1:
        problems.append("first_frame 只能有一张")
    if image_roles.count("last_frame") > 1:
        problems.append("last_frame 只能有一张")
    if "last_frame" in image_roles and "first_frame" not in image_roles:
        problems.append("有 last_frame 时必须同时有 first_frame")
    if image_roles and image_roles[0] != "first_frame":
        problems.append("第一张图必须是 first_frame")
    return problems


def _context_of(context: dict[str, object] | None, cfg: DspConfig) -> dict[str, object]:
    base: dict[str, object] = {
        "provider": "chenxing", "model": cfg.videogen_model,
    }
    base.update(context or {})
    return base


def submit_with_meta(
    cfg: DspConfig, *, prompt: str, image_url: str, seconds: str, tail_image_url: str = "",
    motion_params: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> tuple[str, str]:
    """提交图生视频任务，返回 ``(task_id, provider_request_id)``。

    提交前先做**本地协议预检**（不发请求）：图片缺 role / 首帧尾帧顺序错误等
    在花钱之前就被挡住（ADR-126）。
    """
    ctx = _context_of(context, cfg)
    api_key = get_api_key(cfg.videogen_key_env)
    if not api_key:
        raise DspVideoGenError(
            f"未配置图生视频密钥 {cfg.videogen_key_env}（dsp 图生视频必需）。",
            category="environment", stage="submit")
    if not prompt.strip():
        raise DspVideoGenError("图生视频需要 prompt（镜头文案为空）。", category="video", stage="submit")

    url = cfg.videogen_api_base.rstrip("/") + cfg.videogen_submit_path
    payload = build_payload(
        cfg, prompt=prompt, image_url=image_url, seconds=seconds,
        tail_image_url=tail_image_url, motion_params=motion_params)
    problems = validate_payload(payload)
    if problems:
        raise DspVideoGenError(
            "提交前协议预检未通过（未发请求、未计费）：" + "；".join(problems),
            category="video", stage="preflight")
    logger.info(
        "[videogen] 提交 provider=%s model=%s shot=%s attempt=%s duration=%ss"
        " images=%d tail=%s",
        ctx.get("provider"), ctx.get("model"), ctx.get("shotId", "-"),
        ctx.get("attemptId", "-"), payload.get("duration"),
        len((payload.get("metadata") or {}).get("content") or []),
        "yes" if tail_image_url and tail_image_url != image_url else "no",
    )
    started = time.monotonic()
    try:
        resp = requests.post(url, json=payload, headers=_headers(cfg, api_key),
                             timeout=cfg.videogen_timeout_s)
    except requests.exceptions.Timeout as exc:
        raise DspVideoGenError(
            f"图生视频提交超时（{_redact_url(url)}）", category="environment",
            stage="submit") from exc
    except requests.exceptions.RequestException as exc:
        raise DspVideoGenError(
            f"图生视频连接失败：{sanitize_text(str(exc))}", category="environment",
            stage="submit") from exc
    elapsed = time.monotonic() - started
    request_id = _request_id_of(resp.headers)
    raw_text = _raw_text_of(resp)
    try:
        raw = resp.json()
    except ValueError:
        raw = None
    logger.info(
        "[videogen] 提交响应 status=%s requestId=%s elapsed=%.2fs shot=%s attempt=%s raw=%r",
        resp.status_code, request_id or "-", elapsed, ctx.get("shotId", "-"),
        ctx.get("attemptId", "-"), sanitize_text(raw_text, limit=600),
    )
    if not resp.ok:
        _raise_for("提交", status=resp.status_code, raw_text=raw_text, raw_json=raw,
                   request_id=request_id, context=ctx)
    if not isinstance(raw, dict):
        raise DspVideoGenError(
            f"图生视频提交返回非 JSON（HTTP {resp.status_code}）："
            f"{sanitize_text(raw_text) or '响应体为空'}",
            category="video", status=resp.status_code, request_id=request_id,
            raw_excerpt=sanitize_text(raw_text), stage="submit")

    data = raw.get("data")
    task_id = (raw.get("id") or raw.get("task_id")
               or (data.get("id") if isinstance(data, dict) else ""))
    if not task_id:
        raise DspVideoGenError(
            f"图生视频提交响应缺少任务 id：{sorted(raw)[:12]}"
            f"（原始响应：{sanitize_text(raw_text)}）",
            category="video", status=resp.status_code, request_id=request_id,
            raw_excerpt=sanitize_text(raw_text), stage="submit")
    return str(task_id), request_id


def submit(
    cfg: DspConfig, *, prompt: str, image_url: str, seconds: str, tail_image_url: str = "",
    motion_params: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> str:
    """兼容旧调用方的薄包装（只返回 task_id）。"""
    return submit_with_meta(
        cfg, prompt=prompt, image_url=image_url, seconds=seconds,
        tail_image_url=tail_image_url, motion_params=motion_params, context=context)[0]


def _query_with_meta(
    cfg: DspConfig, api_key: str, task_id: str, *,
    context: dict[str, object] | None = None,
) -> tuple[dict, str]:
    ctx = _context_of(context, cfg)
    url = cfg.videogen_api_base.rstrip("/") + cfg.videogen_task_path.format(task_id=task_id)
    try:
        resp = requests.get(url, headers=_headers(cfg, api_key), timeout=cfg.videogen_timeout_s)
    except requests.exceptions.RequestException as exc:
        raise DspVideoGenError(
            f"图生视频查询失败：{sanitize_text(str(exc))}", category="transient",
            retriable=True, stage="poll") from exc
    request_id = _request_id_of(resp.headers)
    raw_text = _raw_text_of(resp)
    try:
        raw = resp.json()
    except ValueError:
        raw = None
    logger.debug(
        "[videogen] 轮询 status=%s requestId=%s shot=%s task=%s raw=%r",
        resp.status_code, request_id or "-", ctx.get("shotId", "-"), task_id,
        sanitize_text(raw_text, limit=600),
    )
    if not resp.ok:
        _raise_for("查询", status=resp.status_code, raw_text=raw_text, raw_json=raw,
                   request_id=request_id, context=ctx)
    if not isinstance(raw, dict):
        raise DspVideoGenError(
            f"图生视频查询返回非 JSON（HTTP {resp.status_code}）："
            f"{sanitize_text(raw_text) or '响应体为空'}",
            category="video", status=resp.status_code, request_id=request_id,
            raw_excerpt=sanitize_text(raw_text), stage="poll")
    return raw, request_id


def _query(cfg: DspConfig, api_key: str, task_id: str) -> dict:
    return _query_with_meta(cfg, api_key, task_id)[0]


def _task_error_of(raw: dict) -> tuple[str, str, str]:
    """任务失败响应 → ``(code, message, raw_excerpt)``。

    兼容三种形态：``{"error":{"code","message"}}``、``{"error":"..."}``、
    以及 message 里再套一层 ``HTTP 400: {"error":{...}}`` 的字符串（真跑 s1/s3 就是这种）。
    """
    code, message = _error_of(raw)
    if code or message:
        # `error` 可能是整段字符串（new-api 把上游 400 的响应体塞进 message 再返回）。
        # 先尝试从里面解析出结构化 code/message，别把整段 JSON 当错误码。
        embedded = re.search(r"\{.*\}", f"{code} {message}", re.S)
        if embedded:
            try:
                parsed = json.loads(embedded.group(0))
                ecode, emessage = _error_of(parsed)
                if ecode or emessage:
                    return ecode, emessage, sanitize_text(f"{code} {message}")
            except ValueError:
                pass
        return code, message, sanitize_text(json.dumps(raw, ensure_ascii=False))
    for key in ("error", "failure_reason", "message", "detail"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        embedded = re.search(r"\{.*\}", text, re.S)
        if embedded:
            try:
                parsed = json.loads(embedded.group(0))
                ecode, emessage = _error_of(parsed)
                if ecode or emessage:
                    return ecode or code, emessage or text, sanitize_text(text)
            except ValueError:
                pass
        return code, text, sanitize_text(text)
    return code, message, sanitize_text(json.dumps(raw, ensure_ascii=False))


def fetch_result(
    cfg: DspConfig, task_id: str, url: str, dest_path: str, *,
    context: dict[str, object] | None = None,
) -> tuple[str, str]:
    """下载成片 → ``(本地路径, 下载通道)``，通道 = ``direct`` / ``proxy``。

    2026-10-18 真跑取证：厂商 ``metadata.url`` 指向的 TOS 直链可能返回
    ``403 AccessDenied``（``DetailErrCode 14006``，与预签名 / 代理无关），
    而带 Bearer 的 ``GET /v1/videos/{task_id}/content`` 仍可正常拉取。
    故：**先试直链；非 2xx 就回退到鉴权代理**，两条路都失败时把两边的状态码与
    （脱敏）原文一起抛出——不把「下载 403」误报成「生成失败」。
    """
    ctx = _context_of(context, cfg)
    key = get_api_key(cfg.videogen_key_env)
    failures: list[str] = []
    last_status = 0
    last_request_id = ""
    last_excerpt = ""

    def _try_get(target: str, headers: dict, label: str) -> bool:
        nonlocal last_status, last_request_id, last_excerpt
        started = time.monotonic()
        try:
            resp = requests.get(target, headers=headers, stream=True,
                                timeout=cfg.videogen_timeout_s * 4)
        except requests.exceptions.RequestException as exc:
            failures.append(f"{label} 连接失败：{sanitize_text(str(exc))}")
            return False
        elapsed = time.monotonic() - started
        last_status = resp.status_code
        last_request_id = _request_id_of(resp.headers) or last_request_id
        if not resp.ok:
            raw = _raw_text_of(resp)
            last_excerpt = sanitize_text(raw)
            failures.append(f"{label} HTTP {resp.status_code}：{last_excerpt or '响应体为空'}")
            logger.warning(
                "[videogen] 下载失败 channel=%s status=%s requestId=%s elapsed=%.2fs shot=%s raw=%r",
                label, resp.status_code, last_request_id or "-", elapsed,
                ctx.get("shotId", "-"), last_excerpt,
            )
            resp.close()
            return False
        content_type = str(resp.headers.get("content-type") or "").lower()
        if content_type.startswith(("text/", "application/json")):
            raw = _raw_text_of(resp)
            last_excerpt = sanitize_text(raw)
            failures.append(f"{label} 返回的不是视频（{content_type}）：{last_excerpt or '响应体为空'}")
            logger.warning(
                "[videogen] 下载失败 channel=%s status=%s content-type=%s shot=%s raw=%r",
                label, resp.status_code, content_type or "-", ctx.get("shotId", "-"), last_excerpt,
            )
            resp.close()
            return False
        written = 0
        with open(dest_path, "wb") as out:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    out.write(chunk)
                    written += len(chunk)
        resp.close()
        if written == 0:
            failures.append(f"{label} 返回空文件（HTTP {resp.status_code}）")
            logger.warning(
                "[videogen] 下载失败 channel=%s status=%s 空文件 shot=%s",
                label, resp.status_code, ctx.get("shotId", "-"),
            )
            return False
        logger.info(
            "[videogen] 下载完成 channel=%s status=%s requestId=%s elapsed=%.2fs shot=%s bytes=%d",
            label, resp.status_code, last_request_id or "-", elapsed, ctx.get("shotId", "-"), written,
        )
        return True

    if url and _try_get(url, {}, "direct"):
        return dest_path, "direct"
    proxy_url = cfg.videogen_api_base.rstrip("/") + cfg.videogen_content_path.format(task_id=task_id)
    if key and _try_get(proxy_url, _headers(cfg, key), "proxy"):
        return dest_path, "proxy"
    raise DspVideoGenError(
        "成片下载失败：" + "；".join(failures or ["没有可用的下载地址"]),
        category="environment" if last_status in _HTTP_PERMISSION else "transient",
        retriable=last_status in _HTTP_TRANSIENT,
        status=last_status, request_id=last_request_id, raw_excerpt=last_excerpt,
        stage="download", global_permission=last_status in _HTTP_PERMISSION,
    )


def result_url(raw: dict) -> str:
    """从完成态响应里取视频直链（文档：``metadata.url``，亦见 ``url``）。"""
    meta = raw.get("metadata")
    if isinstance(meta, dict) and meta.get("url"):
        return str(meta["url"])
    for key in ("url", "video_url"):
        if raw.get(key):
            return str(raw[key])
    data = raw.get("data")
    if isinstance(data, dict):
        inner = data.get("metadata")
        if isinstance(inner, dict) and inner.get("url"):
            return str(inner["url"])
    return ""


def wait_with_meta(
    cfg: DspConfig, task_id: str, *, api_key: str = "",
    context: dict[str, object] | None = None,
) -> tuple[dict, str]:
    """轮询至终态，返回 ``(完成态响应, 最后一次请求 id)``。

    超时**如实报错**，不假装完成；``failed`` 时透出厂商 ``error``（不编造原因）。
    """
    key = api_key or get_api_key(cfg.videogen_key_env)
    if not key:
        raise DspVideoGenError(
            f"未配置图生视频密钥 {cfg.videogen_key_env}。", category="environment",
            stage="poll")
    deadline = time.monotonic() + cfg.videogen_poll_max_s
    last_status = "unknown"
    last_request_id = ""
    while True:
        raw, last_request_id = _query_with_meta(cfg, key, task_id, context=context)
        status = str(raw.get("status") or "").lower()
        if status:
            last_status = status
        if status in _TERMINAL_OK:
            if not result_url(raw):
                raise DspVideoGenError(
                    f"图生视频任务完成但缺少视频直链（metadata.url / url）：{sorted(raw)[:12]}"
                    f"（原始响应：{sanitize_text(json.dumps(raw, ensure_ascii=False))}）",
                    category="video", status=200, request_id=last_request_id,
                    raw_excerpt=sanitize_text(json.dumps(raw, ensure_ascii=False)),
                    stage="poll")
            return raw, last_request_id
        if status in _TERMINAL_FAIL:
            code, message, excerpt = _task_error_of(raw)
            category, retriable = _classify_http(400, code)
            raise DspVideoGenError(
                f"图生视频任务失败（status={status}）：{code} {message}".strip(),
                category=category, retriable=retriable, code=code,
                request_id=last_request_id, raw_excerpt=excerpt, stage="poll",
                global_permission=_classify_body_case(code, message) == "permission",
            )
        if time.monotonic() >= deadline:
            raise DspVideoGenError(
                f"图生视频轮询超时（>{cfg.videogen_poll_max_s}s，最后状态 {last_status}）",
                category="transient", retriable=True, request_id=last_request_id,
                stage="poll")
        time.sleep(cfg.videogen_poll_interval_s)


def wait(cfg: DspConfig, task_id: str, *, api_key: str = "",
         context: dict[str, object] | None = None) -> dict:
    """兼容旧调用方的薄包装（只返回完成态响应）。"""
    return wait_with_meta(cfg, task_id, api_key=api_key, context=context)[0]


def generate(
    cfg: DspConfig, *, prompt: str, image_url: str, seconds: str, tail_image_url: str = "",
    motion_params: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> dict:
    """提交 + 轮询至完成，返回 ``{task_id, url, ...}``（薄适配层：demo 直接替换本函数）。"""
    api_key = get_api_key(cfg.videogen_key_env)
    task_id, _ = submit_with_meta(
        cfg, prompt=prompt, image_url=image_url, seconds=seconds,
        tail_image_url=tail_image_url, motion_params=motion_params, context=context)
    raw = wait(cfg, task_id, api_key=api_key, context=context)
    return {"task_id": task_id, "url": result_url(raw), "raw": raw}
