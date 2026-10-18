"""图生视频**单镜头预检**（ADR-126）。

用法（本机 venv）：

    PYTHONPATH=src python scripts/dsp-videogen-probe.py --dry-run \
        --image-key _shared/dsp/generated/20261017/x.png

    PYTHONPATH=src python scripts/dsp-videogen-probe.py \
        --image-key _shared/dsp/generated/20261017/x.png \
        --tail-key  _shared/dsp/generated/20261017/y.png \
        --prompt "..." --seconds 4

它做三件事：
1. 用**公网端点**给素材签发预签名 URL，并按现行协议组装请求体（打印**脱敏**快照）；
2. 本地协议预检（role / 顺序 / URL），不通过就**不发请求**；
3. 只提交**一镜**并轮询到终态——权限 / 协议问题在批量提交之前暴露，不浪费按镜计费。

⚠️ 这不是免费接口：不加 ``--dry-run`` 会真提交一个厂商任务。
"""
from __future__ import annotations

import argparse
import json
import sys

from flowmind.config import load_config
from flowmind.skills import _dsp_storage as storage
from flowmind.skills import _dsp_video_gen as vg


def _redacted_payload(payload: dict) -> dict:
    def walk(value):
        if isinstance(value, dict):
            return {k: ("<redacted-url>" if k == "url" else walk(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    out = walk(payload)
    out["prompt"] = f"<{len(payload.get('prompt') or '')} chars>"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="图生视频单镜头预检（ADR-126）")
    ap.add_argument("--image-key", required=True, help="首帧素材对象 key")
    ap.add_argument("--tail-key", default="", help="尾帧素材对象 key（留空 = 单图）")
    ap.add_argument("--prompt", default="镜头缓慢推近，保持主体形状、颜色与构图不变，运动真实平稳。")
    ap.add_argument("--seconds", default="4")
    ap.add_argument("--dry-run", action="store_true", help="只组装 + 预检，不提交厂商")
    ap.add_argument("--poll-max", type=float, default=0, help="轮询上限秒数（0 = 用配置值）")
    args = ap.parse_args()

    cfg = load_config().dsp
    image_url = storage.presign_get_public(args.image_key)
    tail_url = storage.presign_get_public(args.tail_key) if args.tail_key else ""
    payload = vg.build_payload(
        cfg, prompt=args.prompt, image_url=image_url, seconds=args.seconds,
        tail_image_url=tail_url)
    problems = vg.validate_payload(payload)
    print("── 请求体（脱敏）──")
    print(json.dumps(_redacted_payload(payload), ensure_ascii=False, indent=2))
    print("── 协议预检 ──")
    print("通过" if not problems else "未通过：" + "；".join(problems))
    if problems or args.dry_run:
        return 0 if not problems else 1

    print("── 提交（单镜）──")
    try:
        task_id, request_id = vg.submit_with_meta(
            cfg, prompt=args.prompt, image_url=image_url, seconds=args.seconds,
            tail_image_url=tail_url, context={"shotId": "probe", "attemptId": "probe-1"})
    except vg.DspVideoGenError as exc:
        print("提交失败：", json.dumps(exc.diagnostics(), ensure_ascii=False, indent=2))
        return 2
    print(f"task_id={task_id} request_id={request_id or '-'}")
    if args.poll_max > 0:
        cfg = cfg.model_copy(update={"videogen_poll_max_s": args.poll_max})
    print("── 轮询 ──")
    try:
        raw, poll_request_id = vg.wait_with_meta(
            cfg, task_id, context={"shotId": "probe", "attemptId": "probe-1"})
    except vg.DspVideoGenError as exc:
        print("出片失败：", json.dumps(exc.diagnostics(), ensure_ascii=False, indent=2))
        return 3
    url = vg.result_url(raw)
    print(f"完成 status={raw.get('status')} request_id={poll_request_id or '-'}")
    print("视频直链（脱敏）：", vg._redact_url(url))
    print("metadata 字段：", sorted((raw.get("metadata") or {}).keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
