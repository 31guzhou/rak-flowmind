"""调用方凭证校验与租户上下文（ECO-ADR-0011）。

两种凭证形态，按配置二选一（D4 之后**优先 JWT**）：

1. **JWT（RS256，D4 推荐）**：网关用**自己的私钥**签发「网关→后端」那一跳的短命
   凭证，并公布公钥（``GET /.well-known/jwks.json``）。本服务用标准 JWT 库
   （PyJWT）+ JWKS 本地验签——**不需要与网关共享任何密钥**。
   配置：``FLOWMIND_KERNEL_JWKS_URL``（可选 ``FLOWMIND_KERNEL_ISSUER`` /
   ``FLOWMIND_KERNEL_AUDIENCE`` 收紧校验）。
2. **HMAC（A 阶段，兼容保留）**：``<base64url(JSON)> "." <hex HMAC-SHA256>``，
   payload 键序 ``{"sub":..,"tid":..,"exp":..}``，与网关共享 ``FLOWMIND_AUTH_SECRET``。
   契约真源 = 生态 ADR §4b（Go: go-kernel/internal/httpserver，
   TS: cross-dashboard 服务端），三侧逐字节一致。

两个平面不要混：
  * 两者都未配置 → **鉴权整体关闭**（dev / 独立部署语义：现有 demo 与
    直连用法不受影响，任务不记租户）。
  * 任一已配置 → 每个请求都必须带合法凭证；技能/仓储层拿不到凭证时
    **fail-closed**（拒绝访问，而不是退化成"看全部"）。安全上只有这两种
    取值，不存在"有鉴权但匿名可见全部"的中间态。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

__all__ = [
    "Caller",
    "TokenError",
    "auth_enforced",
    "auth_jwks_url",
    "bind_caller",
    "current_caller",
    "tenant_scope",
    "use_caller",
    "verify_token",
]


class TokenError(Exception):
    """凭证非法。``code`` 决定对外语义（全部映射 401，仅 detail 不同）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Caller:
    """一次调用的调用方身份（由已校验凭证派生，绝不可由请求头自填）。"""

    tenant_id: str
    principal: str


_caller: ContextVar[Caller | None] = ContextVar("flowmind_caller", default=None)


def auth_secret() -> str:
    """生效的 HMAC 校验密钥（env ``FLOWMIND_AUTH_SECRET`` → config ``auth.secret``）。"""
    from flowmind.config import get_config

    return (get_config().auth.secret or "").strip()


def auth_jwks_url() -> str:
    """生效的网关 JWKS 地址（env ``FLOWMIND_KERNEL_JWKS_URL``）；空 = 不走 JWT。"""
    from flowmind.config import get_config

    return (get_config().auth.kernel_jwks_url or "").strip()


def auth_enforced() -> bool:
    """是否启用凭证校验（JWT 或 HMAC 任一配置即启用）。"""
    return bool(auth_jwks_url() or auth_secret())


def current_caller() -> Caller | None:
    """当前请求上下文中的调用方；未在上下文绑定（后台线程/启动恢复）为 None。"""
    return _caller.get()


def bind_caller(caller: Caller | None):
    """把调用方绑进当前上下文，返回还原用的 token（配合 ``use_caller``）。"""
    return _caller.set(caller)


@contextmanager
def use_caller(caller: Caller | None) -> Iterator[Caller | None]:
    """上下文绑定作用域（异常路径也保证 reset，避免租户串到下个请求）。"""
    token = _caller.set(caller)
    try:
        yield caller
    finally:
        _caller.reset(token)


def tenant_scope() -> tuple[bool, str | None]:
    """任务级访问的租户范围判定（三态，fail-closed）。

    返回 ``(是否强制隔离, 允许访问的租户 id)``：

    * ``(False, None)`` —— 未启用鉴权：不做租户过滤（dev / 独立部署）。
    * ``(True, <tid>)`` —— 已鉴权：仅可见该租户的任务。
    * ``(True, None)``  —— 启用鉴权但当前上下文无凭证：**拒绝一切任务访问**。
      这条分支就是防泄漏的关键——后台/脚本路径若忘了绑上下文，结果是
      "查不到"而不是"全能看到"。
    """
    if not auth_enforced():
        return False, None
    caller = current_caller()
    if caller is None:
        return True, None
    return True, caller.tenant_id


def verify_token(token: str, *, now: float | None = None) -> Caller:
    """校验 Bearer token 并还原调用方身份。

    分派：配置了 ``kernel_jwks_url`` → JWT（RS256 + JWKS 本地验签）；
    否则 → HMAC 共享密钥。两条路径的**顺序铁律一致**：先验签再解载荷。
    """
    jwks_url = auth_jwks_url()
    if jwks_url:
        return _verify_jwt(token, jwks_url, now=now)

    secret = auth_secret()
    if not secret:
        raise TokenError("auth_disabled",
                         "未配置 FLOWMIND_AUTH_SECRET / FLOWMIND_KERNEL_JWKS_URL，无法校验凭证")
    raw = (token or "").strip()
    b64, sep, sig = raw.partition(".")
    if not sep or not b64 or not sig:
        raise TokenError("malformed", "token 形状非法（期望 <b64>.<hex-hmac>）")

    expected = hmac.new(secret.encode("utf-8"), b64.encode("ascii"),
                        hashlib.sha256).hexdigest()
    # 恒时比较；非 hex 签名自然不相等（不会泄露“形状对但密钥错”的区别）
    if not hmac.compare_digest(expected, sig.strip().lower()):
        raise TokenError("signature", "token 签名不匹配")

    try:
        body = base64.urlsafe_b64decode(_pad(b64))
        payload = json.loads(body.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise TokenError("malformed", "token 载荷无法解析") from exc
    if not isinstance(payload, dict):
        raise TokenError("malformed", "token 载荷不是 JSON object")
    return _caller_from_payload(payload, now=now)


# ── D4：网关自签 RS256 的本地验签（标准库做标准事）──────────────────────
# PyJWKClient 自带 JWKS 拉取 + kid 缓存 + 轮换失效，不自己实现。
_jwk_clients: dict[str, Any] = {}


def _jwks_client(url: str):
    from jwt import PyJWKClient

    client = _jwk_clients.get(url)
    if client is None:
        client = PyJWKClient(url, cache_keys=True, lifespan=300)
        _jwk_clients[url] = client
    return client


def _verify_jwt(token: str, jwks_url: str, *, now: float | None = None) -> Caller:
    """用网关公布的 JWKS 验 RS256 JWT。

    校验项：算法白名单（只允许 RS256）、``exp``（必填）、可选的
    ``iss`` / ``aud``（配置了才校验，向后兼容未带这两项的签发方）。
    """
    try:
        import jwt as pyjwt
    except ImportError as exc:  # 依赖缺失 → 按"鉴权不可用"处理，而非 500
        raise TokenError("auth_disabled", "缺少 pyjwt 依赖（pip install 'pyjwt[crypto]'）") from exc

    from flowmind.config import get_config

    cfg = get_config().auth
    raw = (token or "").strip()
    if raw.count(".") != 2:
        raise TokenError("malformed", "token 形状非法（期望 JWT 三段）")

    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(raw)
    except Exception as exc:  # 网络/解析/kid 未命中一律按验签失败处理
        raise TokenError("jwks", "无法从网关 JWKS 取得验签公钥") from exc

    options = {
        "verify_exp": False,          # 过期用 now 手工判，便于测试注入时间
        "verify_aud": bool(cfg.kernel_audience),
        "verify_iss": bool(cfg.kernel_issuer),
        "require": ["exp"],
    }
    kwargs: dict[str, Any] = {"algorithms": ["RS256"], "options": options}
    if cfg.kernel_audience:
        kwargs["audience"] = cfg.kernel_audience
    if cfg.kernel_issuer:
        kwargs["issuer"] = cfg.kernel_issuer

    try:
        payload = pyjwt.decode(raw, signing_key.key, **kwargs)
    except pyjwt.InvalidTokenError as exc:  # 签名错/算法不符/缺 exp/iss·aud 不匹配
        raise TokenError("signature", "token 验签失败") from exc

    return _caller_from_payload(payload, now=now)


def _caller_from_payload(payload: dict[str, Any], *, now: float | None = None) -> Caller:
    """把已验签的载荷还原为 Caller（两条凭证形态共用）。"""
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise TokenError("malformed", "token 缺少合法的 exp")
    if (now if now is not None else time.time()) >= float(exp):
        raise TokenError("expired", "token 已过期")

    tid = payload.get("tid")
    if tid is not None and not isinstance(tid, str):
        raise TokenError("malformed", "token 的 tid 必须是字符串")
    sub = payload.get("sub")
    if sub is not None and not isinstance(sub, str):
        raise TokenError("malformed", "token 的 sub 必须是字符串")
    return Caller(tenant_id=tid or "", principal=sub or "")


def _pad(b64: str) -> bytes:
    """补齐 base64url 填充（签发侧不写填充，解码侧容忍两者）。"""
    return (b64 + "=" * (-len(b64) % 4)).encode("ascii")
