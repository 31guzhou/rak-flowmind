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


def seconds_str(ms: int) -> str:
    """镜头时长（ms）→ 厂商 ``seconds`` 字符串。

    文档：``seconds``「取值视模型而定（字符串或整数）」——本仓**原样下发**（整秒不带小数，
    否则保留一位），**不夹取到某枚举**（不替厂商猜）。
    """
    value = max(ms, 0) / 1000.0
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def build_payload(cfg: DspConfig, *, prompt: str, image_url: str, seconds: str) -> dict:
    """按文档组装提交体（``image`` 单图 URL + ``images`` 数组，文档称前者兼容旧客户端）。"""
    payload: dict = {
        "model": cfg.videogen_model,
        "prompt": prompt,
        "aspect_ratio": cfg.videogen_aspect_ratio,
        "size": cfg.videogen_size,
        "seconds": seconds,
        "quality": cfg.videogen_quality,
    }
    if image_url:
        # 文档：``image`` = 单图 URL（兼容旧客户端）；``images`` = URL 数组（≤9 张）
        payload["image"] = image_url
        payload["images"] = [image_url][: max(1, cfg.videogen_max_ref_images)]
    return payload


def submit(cfg: DspConfig, *, prompt: str, image_url: str, seconds: str) -> str:
    """提交图生视频任务，返回 ``task_id``（文档：读 ``id`` / ``task_id``）。"""
    api_key = get_api_key(cfg.videogen_key_env)
    if not api_key:
        raise DspVideoGenError(
            f"未配置图生视频密钥 {cfg.videogen_key_env}（dsp 图生视频必需）。",
            category="environment")
    if not prompt.strip():
        raise DspVideoGenError("图生视频需要 prompt（镜头文案为空）。", category="video")

    url = cfg.videogen_api_base.rstrip("/") + cfg.videogen_submit_path
    payload = build_payload(cfg, prompt=prompt, image_url=image_url, seconds=seconds)
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


def generate(cfg: DspConfig, *, prompt: str, image_url: str, seconds: str) -> dict:
    """提交 + 轮询至完成，返回 ``{task_id, url, ...}``（薄适配层：demo 直接替换本函数）。"""
    api_key = get_api_key(cfg.videogen_key_env)
    task_id = submit(cfg, prompt=prompt, image_url=image_url, seconds=seconds)
    raw = wait(cfg, task_id, api_key=api_key)
    return {"task_id": task_id, "url": result_url(raw), "raw": raw}
