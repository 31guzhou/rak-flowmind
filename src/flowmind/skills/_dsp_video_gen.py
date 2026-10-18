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

import time

import requests

from flowmind.config import DspConfig
from flowmind.skills._secrets import get_api_key

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


class DspVideoGenError(Exception):
    """图生视频调用失败。category/retriable 语义与 errors.py 对齐。"""

    def __init__(self, message: str, category: str = "unknown", retriable: bool = False):
        super().__init__(message)
        self.category = category
        self.retriable = retriable


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


def _raise_for(cfg: DspConfig, action: str, raw: object, status: int) -> None:
    code, message = _error_of(raw)
    category, retriable = _classify_videogen_error(code)
    raise DspVideoGenError(
        f"图生视频{action}失败（HTTP {status}）：{code or '未知'} {message}".strip(),
        category=category, retriable=retriable,
    )


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


def submit(
    cfg: DspConfig, *, prompt: str, image_url: str, seconds: str, tail_image_url: str = "",
    motion_params: dict[str, object] | None = None,
) -> str:
    """提交图生视频任务，返回 ``task_id``（文档：读 ``id`` / ``task_id``）。"""
    api_key = get_api_key(cfg.videogen_key_env)
    if not api_key:
        raise DspVideoGenError(
            f"未配置图生视频密钥 {cfg.videogen_key_env}（dsp 图生视频必需）。",
            category="environment")
    if not prompt.strip():
        raise DspVideoGenError("图生视频需要 prompt（镜头文案为空）。", category="video")

    url = cfg.videogen_api_base.rstrip("/") + cfg.videogen_submit_path
    payload = build_payload(
        cfg, prompt=prompt, image_url=image_url, seconds=seconds,
        tail_image_url=tail_image_url, motion_params=motion_params)
    try:
        resp = requests.post(url, json=payload, headers=_headers(cfg, api_key),
                             timeout=cfg.videogen_timeout_s)
    except requests.exceptions.Timeout as exc:
        raise DspVideoGenError(f"图生视频提交超时（{url}）", category="environment") from exc
    except requests.exceptions.RequestException as exc:
        raise DspVideoGenError(f"图生视频连接失败：{exc}", category="environment") from exc

    try:
        raw = resp.json()
    except ValueError:
        raw = None
    if not resp.ok:
        _raise_for(cfg, "提交", raw, resp.status_code)
    if not isinstance(raw, dict):
        raise DspVideoGenError("图生视频提交返回非 JSON", category="video")

    task_id = raw.get("id") or raw.get("task_id") or raw.get("data", {}).get("id")
    if not task_id:
        raise DspVideoGenError(
            f"图生视频提交响应缺少任务 id：{sorted(raw)[:12]}", category="video")
    return str(task_id)


def _query(cfg: DspConfig, api_key: str, task_id: str) -> dict:
    url = cfg.videogen_api_base.rstrip("/") + cfg.videogen_task_path.format(task_id=task_id)
    try:
        resp = requests.get(url, headers=_headers(cfg, api_key), timeout=cfg.videogen_timeout_s)
    except requests.exceptions.RequestException as exc:
        raise DspVideoGenError(
            f"图生视频查询失败：{exc}", category="transient", retriable=True) from exc
    try:
        raw = resp.json()
    except ValueError:
        raw = None
    if not resp.ok:
        _raise_for(cfg, "查询", raw, resp.status_code)
    if not isinstance(raw, dict):
        raise DspVideoGenError("图生视频查询返回非 JSON", category="video")
    return raw


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


def wait(cfg: DspConfig, task_id: str, *, api_key: str = "") -> dict:
    """轮询至终态，返回完成态响应（含 ``metadata.url``）。

    超时**如实报错**，不假装完成；``failed`` 时透出厂商 ``error``（不编造原因）。
    """
    key = api_key or get_api_key(cfg.videogen_key_env)
    if not key:
        raise DspVideoGenError(
            f"未配置图生视频密钥 {cfg.videogen_key_env}。", category="environment")
    deadline = time.monotonic() + cfg.videogen_poll_max_s
    last_status = "unknown"
    while True:
        raw = _query(cfg, key, task_id)
        status = str(raw.get("status") or "").lower()
        if status:
            last_status = status
        if status in _TERMINAL_OK:
            if not result_url(raw):
                raise DspVideoGenError(
                    f"图生视频任务完成但缺少视频直链（metadata.url / url）：{sorted(raw)[:12]}",
                    category="video")
            return raw
        if status in _TERMINAL_FAIL:
            code, message = _error_of(raw)
            category, retriable = _classify_videogen_error(code)
            raise DspVideoGenError(
                f"图生视频任务失败（status={status}）：{code} {message}".strip(),
                category=category, retriable=retriable)
        if time.monotonic() >= deadline:
            raise DspVideoGenError(
                f"图生视频轮询超时（>{cfg.videogen_poll_max_s}s，最后状态 {last_status}）",
                category="transient", retriable=True)
        time.sleep(cfg.videogen_poll_interval_s)


def generate(
    cfg: DspConfig, *, prompt: str, image_url: str, seconds: str, tail_image_url: str = "",
    motion_params: dict[str, object] | None = None,
) -> dict:
    """提交 + 轮询至完成，返回 ``{task_id, url, ...}``（薄适配层：demo 直接替换本函数）。"""
    api_key = get_api_key(cfg.videogen_key_env)
    task_id = submit(
        cfg, prompt=prompt, image_url=image_url, seconds=seconds,
        tail_image_url=tail_image_url, motion_params=motion_params)
    raw = wait(cfg, task_id, api_key=api_key)
    return {"task_id": task_id, "url": result_url(raw), "raw": raw}
