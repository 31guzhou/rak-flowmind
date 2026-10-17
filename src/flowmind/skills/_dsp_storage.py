"""dsp 产物的**对象存储适配层**（S3/MinIO 兼容）。

契约 §2.4「目录 = 云存储（MinIO/S3）prefix」——中间产物落 ``cachePrefix``、
成片落 ``outputPrefix``；回传的 ``*Key`` 必须能被 cross-dashboard
``/api/assets/<key>`` 按**当前租户段**取到。故用 S3/MinIO（而非火山 TOS）：
cross-dashboard 的 ``files.ts`` 对 key 是 fail-closed 的（只认本租户段），
段名由调用方下发的 ``cachePrefix`` / ``outputPrefix`` 携带。

密钥纪律（AGENTS.md §3 同款）：端点 / 桶 / key 只经 ``DspConfig`` 的
**env 名**解析，密钥走 ``_secrets.get_api_key``；未配置即**显式报错**（不静默降级、
绝不返回假产物）。``boto3`` **懒加载**——不装它不影响包导入与其它技能。

可测性：本模块所有外部动作都是**模块级函数**，demo 通过替换
``_dsp_storage.put_bytes``（等）注入内存假实现，无需真对象存储。
"""
from __future__ import annotations

from pathlib import Path

import requests

from flowmind.config import load_config
from flowmind.skills._secrets import get_api_key

# 常见产物扩展名 → Content-Type（对象存储写入用；未知回落二进制）
_CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".srt": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class DspStorageError(Exception):
    """对象存储调用失败。category/retriable 语义与 errors.py 对齐。"""

    def __init__(self, message: str, category: str = "unknown", retriable: bool = False):
        super().__init__(message)
        self.category = category
        self.retriable = retriable


def content_type_for(key: str) -> str:
    """按扩展名猜 Content-Type（未知 = application/octet-stream）。"""
    return _CONTENT_TYPES.get(Path(key).suffix.lower(), "application/octet-stream")


def assert_key_shape(key: str) -> str:
    """对象 key 形状守卫：非空、无前导斜杠、无空段、无 ``..`` 回溯、无反斜杠。

    key 由本仓按 ``{prefix}/{runId}/…`` 拼出（§2.4），此处只做**形状**兜底；
    「是否本租户段」由 cross-dashboard 的 ``files.ts`` 侧 fail-closed 判定。
    """
    if not key or not key.strip():
        raise DspStorageError("对象 key 为空", category="video")
    if key.startswith("/"):
        raise DspStorageError(f"对象 key 不得以 / 开头：{key!r}", category="video")
    if "\\" in key or ".." in key.split("/"):
        raise DspStorageError(f"对象 key 含非法片段：{key!r}", category="video")
    if any(seg == "" for seg in key.split("/")):
        raise DspStorageError(f"对象 key 含空段：{key!r}", category="video")
    return key


def storage_configured() -> bool:
    """端点 + 桶 + 凭据是否齐备（调用方据此**如实降级**）。"""
    cfg = load_config().dsp
    if not cfg.storage_endpoint or not cfg.storage_bucket:
        return False
    return bool(get_api_key(cfg.storage_access_key_env)
                and get_api_key(cfg.storage_secret_key_env))


def _endpoint_url(endpoint: str, secure: bool) -> str:
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return f"{'https' if secure else 'http'}://{endpoint}"


def _client():  # noqa: ANN202  返回 boto3 S3 client（懒加载，类型不便标注）
    """构造 S3/MinIO 客户端（懒加载 boto3；缺配置/缺依赖即显式报错）。"""
    cfg = load_config().dsp
    if not cfg.storage_endpoint:
        raise DspStorageError(
            "未配置对象存储端点 FLOWMIND_DSP_S3_ENDPOINT（dsp 产物落云存储 prefix 必需）。",
            category="environment",
        )
    access = get_api_key(cfg.storage_access_key_env)
    secret = get_api_key(cfg.storage_secret_key_env)
    if not access or not secret:
        raise DspStorageError(
            f"未配置对象存储凭据 {cfg.storage_access_key_env} / {cfg.storage_secret_key_env}。",
            category="environment",
        )
    try:
        import boto3  # noqa: PLC0415  懒加载：不装 boto3 不影响包导入
        from botocore.config import Config as BotoConfig  # noqa: PLC0415
    except ImportError as exc:
        raise DspStorageError(
            "未安装 boto3（对象存储客户端）。environment.yml 已列 boto3，"
            "执行 `conda env update -n flowmind -f environment.yml` 后可用。",
            category="environment",
        ) from exc
    boto_cfg = BotoConfig(
        signature_version="s3v4",
        connect_timeout=cfg.storage_timeout_s,
        read_timeout=cfg.storage_timeout_s,
        retries={"max_attempts": 2},
    )
    return boto3.client(
        "s3",
        endpoint_url=_endpoint_url(cfg.storage_endpoint, cfg.storage_secure),
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=cfg.storage_region or None,
        config=boto_cfg,
    )


def _bucket() -> str:
    bucket = load_config().dsp.storage_bucket
    if not bucket:
        raise DspStorageError("未配置对象存储桶 FLOWMIND_DSP_S3_BUCKET", category="environment")
    return bucket


def put_bytes(key: str, data: bytes, content_type: str | None = None) -> str:
    """写入字节到对象存储，返回该 key。"""
    assert_key_shape(key)
    try:
        _client().put_object(
            Bucket=_bucket(), Key=key, Body=data,
            ContentType=content_type or content_type_for(key),
        )
    except DspStorageError:
        raise
    except Exception as exc:  # noqa: BLE001  boto 异常形态多变，统一分类
        raise DspStorageError(f"对象写入失败（{key}）：{exc}", category="transient",
                              retriable=True) from exc
    return key


def put_text(key: str, text: str, content_type: str | None = None) -> str:
    """写入 UTF-8 文本（EDL json / SRT），返回该 key。"""
    return put_bytes(key, text.encode("utf-8"), content_type=content_type)


def put_file(path: str, key: str, content_type: str | None = None) -> str:
    """把本地文件上传到对象存储（成片转存用），返回该 key。"""
    assert_key_shape(key)
    try:
        _client().upload_file(
            path, _bucket(), key,
            ExtraArgs={"ContentType": content_type or content_type_for(key)},
        )
    except DspStorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DspStorageError(f"对象上传失败（{key}）：{exc}", category="transient",
                              retriable=True) from exc
    return key


def presign_get(key: str, ttl_s: int | None = None) -> str:
    """为读取生成预签名 URL（§7.1：素材可走 ``http(s)://`` 供 MediaKit 拉取）。

    现签现用、**不入库**（契约 §7.3①：对象 key 不可变语义不变）。
    ⚠️ 2026-10-17 起 ``render_compose`` **不再走这条**：本机 / 内网端点（如 ``127.0.0.1:9000``）
    厂商拉不到 ⇒ 改成「先上传 MediaKit，再用 ``mediakit://`` 引用」（见 ``get_bytes`` 与
    ``_dsp_mediakit.upload_media``）。本函数保留给**端点确实公网可达**的部署与排障使用。
    """
    assert_key_shape(key)
    ttl = ttl_s or load_config().dsp.presign_ttl_s
    try:
        return _client().generate_presigned_url(
            "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=ttl)
    except DspStorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DspStorageError(f"预签名失败（{key}）：{exc}", category="transient",
                              retriable=True) from exc


def get_bytes(key: str) -> bytes:
    """读出对象**原始字节**（渲染素材上传 MediaKit 用；契约 §7.3① 的本地上传支）。

    与 ``presign_get`` 的区别：不签发任何对外地址——素材由**本仓自己**送出去，
    故与对象存储的**公网可达性彻底解耦**（本机 dev 也能真渲染）。
    """
    assert_key_shape(key)
    try:
        obj = _client().get_object(Bucket=_bucket(), Key=key)
        return obj["Body"].read()
    except DspStorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DspStorageError(f"对象读取失败（{key}）：{exc}", category="transient",
                              retriable=True) from exc


def exists(key: str) -> bool:
    """对象是否已存在（``head_object``）。

    ADR-079 用它实现**按镜重生成**：`regenerateShotIds` 之外的镜头若已有确定性 key 的产物，
    直接**复用**（不重复计价、不重复出片）。读不通 / 未配置 ⇒ 返回 ``False``（调用方据此照常生成，
    不因探测失败而丢镜——**宁可多做一次，不可少出一镜**）。
    """
    if not key or not storage_configured():
        return False
    try:
        _client().head_object(Bucket=_bucket(), Key=key)
        return True
    except Exception:  # noqa: BLE001  不存在 / 无权限 / 网络抖动：一律按「没有」处理
        return False


def fetch_to_file(url: str, dest_path: str, timeout_s: float | None = None) -> str:
    """把 URL 内容流式落本地文件（MediaKit 成片转存第一步）。

    ``timeout_s`` 缺省 = ``DspConfig.storage_timeout_s`` 放大 20 倍（视频体积大）。
    """
    cfg = load_config().dsp
    timeout = timeout_s or max(cfg.storage_timeout_s * 20, 300.0)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
    except requests.exceptions.Timeout as exc:
        raise DspStorageError(f"下载超时：{url}", category="environment") from exc
    except requests.exceptions.RequestException as exc:
        raise DspStorageError(f"下载失败：{url}（{exc}）", category="environment") from exc
    return str(dest)
