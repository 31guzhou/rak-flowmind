"""dsp_image_to_video 技能演示 —— 图生视频（逐镜产视频底 · 契约 §7.5 第 1 步）。

运行：PYTHONPATH=<repo>/src conda run -n flowmind python examples/dsp_image_to_video_demo.py

展示：
1. discover() 自动字段发现
2. happy path：2 镜 → 提交 → 完成 → **直链转存**为不可变 key（24h 直链不当 key）
3. degrade：未配密钥 → 如实降级（成片回落静图，不伪造片段）
4. degrade：缺锁定时间线 → 明确 degradation_reason
5. degrade：某镜出片失败 → 该镜回落静图 + warnings 如实说明（其余镜照常出片）
6. 成本闸：maxShots 限制生成镜数

mock 方式：替换 ``_dsp_video_gen`` 的 submit/wait（模块级薄适配层）与 ``_dsp_storage``
的 presign/fetch/put_file，不需要网络 / 厂商密钥 / 对象存储。密钥用假值经 env 注入。
"""

from __future__ import annotations

import os

import flowmind.skills  # noqa: F401  触发 @skill 注册
from flowmind.discover import field_names
from flowmind.skill import invoke
from flowmind.skills import _dsp_storage as storage
from flowmind.skills import _dsp_video_gen as videogen

_KEY_ENV = "DSP_VIDEOGEN_API_KEY"
_PUTS: list[str] = []
_SUBMITTED: list[dict] = []
_FAIL_SHOTS: set[str] = set()


def section(title: str) -> None:
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _install_fakes() -> None:
    def submit(cfg, *, prompt: str, image_url: str, seconds: str) -> str:
        _SUBMITTED.append({"prompt": prompt, "image_url": image_url, "seconds": seconds})
        return f"task_{len(_SUBMITTED)}"

    def wait(cfg, task_id: str, *, api_key: str = "") -> dict:
        if task_id in _FAIL_SHOTS:
            raise videogen.DspVideoGenError(f"任务 {task_id} 生成失败", category="video")
        return {"id": task_id, "status": "completed",
                "metadata": {"url": f"https://ivd.example/video_cache/{task_id}.mp4"}}

    def fetch_to_file(url: str, dest_path: str, timeout_s: float | None = None) -> str:
        return dest_path

    def put_file(path: str, key: str, content_type: str | None = None) -> str:
        _PUTS.append(key)
        return key

    def presign_get(key: str, ttl_s: int | None = None) -> str:
        return f"https://assets.example/{key}"

    videogen.submit = submit
    videogen.wait = wait
    storage.fetch_to_file = fetch_to_file
    storage.put_file = put_file
    storage.presign_get = presign_get


def _edl() -> dict:
    return {"timeline": {"edl": {
        "version": 1, "totalMs": 7000,
        "tracks": [
            {"kind": "image", "segments": [
                {"atMs": 0, "durationMs": 3000, "sourceKey": "dsp/scratch/t1/门店/门头1.jpg"},
                {"atMs": 3000, "durationMs": 4000, "sourceKey": "dsp/scratch/t1/产品/杯1.jpg"},
            ]},
        ],
        "shots": [
            {"id": "s1", "atMs": 0, "durationMs": 3000,
             "imageSourceKey": "dsp/scratch/t1/门店/门头1.jpg",
             # ADR-073：s1 带**画面提示词**（主路径），s2 刻意不给（走 copy 兜底 + 如实告警）
             "visualPrompt": "中景，平视，固定机位；店员弯腰拉起卷帘门，晨光斜射进门头；写实拍摄质感"},
            {"id": "s2", "atMs": 3000, "durationMs": 4000,
             "imageSourceKey": "dsp/scratch/t1/产品/杯1.jpg"},
        ],
    }}}


def _ctx(**over) -> dict:
    base = {
        "mode": "content", "platform": "douyin",
        "cachePrefix": "dsp/scratch/t1", "outputPrefix": "dsp/output/t1",
        "runId": "run-demo-8", "stepId": "video", "idempotencyKey": "run-demo-8:video",
        "shots": [
            {"id": "s1", "durationMs": 3000, "copy": "早上七点，卷帘门刚拉起"},
            {"id": "s2", "durationMs": 4000, "copy": "这杯保温 12 小时"},
        ],
        "materials": [],
        "deps": _edl(),
    }
    base.update(over)
    return base


def main() -> None:
    section("0) discover('image_to_video') —— Agent 自查字段")
    for path, names in field_names("image_to_video").items():
        print(f"  {path}: {names}")

    section("1) Happy path：2 镜 → 提交 → 完成 → 直链转存")
    os.environ[_KEY_ENV] = "sk-demo-key"
    _install_fakes()
    _PUTS.clear()
    _SUBMITTED.clear()
    r = invoke("image_to_video", _ctx())
    check(r.ok, "happy path 应 ok")
    print(f"  提交参数  : seconds={[s['seconds'] for s in _SUBMITTED]}（镜头建议时长原样下发）")
    print(f"  首镜 prompt: {_SUBMITTED[0]['prompt']}")
    print(f"  片段      : {[c.key for c in r.data.clips]}")
    print(f"  失败标记  : degraded={r.metrics.degraded}")
    check(len(r.data.clips) == 2, "两镜都应出片")
    check(_SUBMITTED[0]["seconds"] == "3" and _SUBMITTED[1]["seconds"] == "4",
          "秒数按镜头时长下发（3000ms→'3' / 4000ms→'4'）")
    # ADR-073：prompt 取**画面提示词**（不是台词）；末尾统一追加负向约束（禁文字/字幕/水印）
    check(_SUBMITTED[0]["prompt"].startswith("中景，平视，固定机位"),
          "prompt 取画面提示词（visualPrompt），不是口播文案")
    check("不要出现任何文字" in _SUBMITTED[0]["prompt"], "prompt 末尾须带负向约束（禁文字/字幕/水印）")
    check(_SUBMITTED[1]["prompt"].startswith("这杯保温 12 小时"),
          "无画面提示词的镜**回落用文案**出 prompt（如实兜底）")
    check(any("没有画面提示词" in w and "回落" in w for w in r.data.warnings),
          "回落用文案出 prompt 须**如实告警**（ADR-073）")
    check(_SUBMITTED[0]["image_url"] == "https://assets.example/dsp/scratch/t1/门店/门头1.jpg",
          "素材须经预签名 URL 供厂商拉取")
    check("dsp/scratch/t1/run-demo-8/clips/s1.mp4" in _PUTS, "直链须转存为不可变 key")
    check(r.metrics.degraded is False, "全出片不应标记降级")

    section("2) degrade：未配密钥 → 如实降级（不伪造片段）")
    os.environ.pop(_KEY_ENV, None)
    r = invoke("image_to_video", _ctx())
    print(f"  ok        : {r.ok}  degraded={r.metrics.degraded}")
    print(f"  reason    : {r.metrics.degradation_reason}")
    print(f"  warnings  : {r.data.warnings}")
    check(r.ok and r.metrics.degraded and r.metrics.degradation_reason == "videogen-not-configured",
          "无密钥须降级且原因明确")
    check(r.data.clips == [], "降级不得给出片段（不伪造）")
    os.environ[_KEY_ENV] = "sk-demo-key"

    section("3) degrade：缺锁定时间线（deps.timeline.edl）")
    r = invoke("image_to_video", _ctx(deps={}))
    print(f"  reason    : {r.metrics.degradation_reason}")
    check(r.metrics.degradation_reason == "missing-timeline", "缺时间线须降级")

    section("4) degrade：某镜出片失败 → 该镜回落静图（其余照常）")
    _PUTS.clear()
    _SUBMITTED.clear()
    _FAIL_SHOTS.clear()
    _FAIL_SHOTS.add("task_2")   # 第二镜（s2）失败
    r = invoke("image_to_video", _ctx())
    print(f"  clips     : {[c.shotId for c in r.data.clips]}")
    print(f"  degraded  : {r.metrics.degraded} / {r.metrics.degradation_reason}")
    print(f"  warnings  : {r.data.warnings}")
    check([c.shotId for c in r.data.clips] == ["s1"], "失败镜不出片段，成功的照常出片")
    check(r.metrics.degraded and r.metrics.degradation_reason == "partial-shots-failed",
          "部分失败须如实标记降级")
    check(any("回落静图" in w for w in r.data.warnings), "失败镜须如实说明回落")
    _FAIL_SHOTS.clear()

    section("5) 成本闸：maxShots 限制生成镜数")
    _PUTS.clear()
    _SUBMITTED.clear()
    r = invoke("image_to_video", _ctx(maxShots=1))
    print(f"  clips     : {[c.shotId for c in r.data.clips]}")
    print(f"  warnings  : {r.data.warnings}")
    check(len(r.data.clips) == 1 and r.data.clips[0].shotId == "s1", "maxShots=1 只生成首镜")
    check(any("maxShots" in w for w in r.data.warnings), "截断须如实说明")

    print("\n✅ dsp_image_to_video_demo 全部通过")


if __name__ == "__main__":
    main()
