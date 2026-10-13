"""dsp_image_to_video 技能演示 —— 图生视频（逐镜产视频底 · 契约 §7.5 第 1 步）。

运行：PYTHONPATH=<repo>/src conda run -n flowmind python examples/dsp_image_to_video_demo.py

展示：
1. discover() 自动字段发现
2. happy path：2 镜 → **输入降采样**（1152×2048 → ≤720×1280，真跑 PIL）+ **首尾帧桥**
   （s1 尾帧 = s2 的图）→ 提交 → 完成 → **直链转存**为不可变 key（24h 直链不当 key）
3. build_payload：有/无尾帧的两种提交体形状（``images`` 数组第 2 位 = 尾帧）
4. degrade：未配密钥 → 如实降级（成片回落静图，不伪造片段）
5. degrade：缺锁定时间线 → 明确 degradation_reason
6. degrade：某镜出片失败 → 该镜回落静图 + warnings 如实说明（其余镜照常出片）
7. 降采样失败 → **回落原图**并如实告警（画质风险，**不丢镜**）
8. 成本闸：maxShots 限制生成镜数
9. 开关：``FLOWMIND_DSP_VIDEOGEN_BRIDGE_NEXT_IMAGE=0`` / ``..._DOWNSCALE_INPUT=0``

mock 方式：替换 ``_dsp_video_gen`` 的 submit/wait（模块级薄适配层）与 ``_dsp_storage``
的 presign/fetch/put_file，不需要网络 / 厂商密钥 / 对象存储。密钥用假值经 env 注入。
``fetch_to_file`` **真写一张 PIL 图**（按文件名给尺寸）——故降采样是**真跑**而非假过。
"""

from __future__ import annotations

import os
from pathlib import Path

import flowmind.skills  # noqa: F401  触发 @skill 注册
from flowmind.config import load_config
from flowmind.discover import field_names
from flowmind.skill import invoke
from flowmind.skills import _dsp_storage as storage
from flowmind.skills import _dsp_video_gen as videogen
from flowmind.skills import _media as media

_KEY_ENV = "DSP_VIDEOGEN_API_KEY"
_PUTS: list[str] = []
_SUBMITTED: list[dict] = []
_FETCHED: list[str] = []
_FAIL_SHOTS: set[str] = set()

# 素材池里这些图的**实际像素**（按文件名匹配）：补生图 = 9:16 @2K（超 I2V 输出 720×1280），
# 另一张 = 小图（未超 ⇒ 原图直用，不落派生件）。
_IMAGE_PIXELS = {"门头1.jpg": (1152, 2048), "杯1.jpg": (600, 900)}
_REAL_PROBE = media.probe_image_size


def section(title: str) -> None:
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _write_image(dest_path: str, url: str) -> None:
    """按素材文件名写一张**真图**（降采样路径因此真跑 PIL，不是假过）。"""
    from PIL import Image

    w, h = next((v for k, v in _IMAGE_PIXELS.items() if k in url), (600, 900))
    Image.new("RGB", (w, h), (32, 64, 96)).save(dest_path)


def _install_fakes() -> None:
    def submit(cfg, *, prompt: str, image_url: str, seconds: str,
               tail_image_url: str = "") -> str:
        _SUBMITTED.append({"prompt": prompt, "image_url": image_url, "seconds": seconds,
                           "tail_image_url": tail_image_url})
        return f"task_{len(_SUBMITTED)}"

    def wait(cfg, task_id: str, *, api_key: str = "") -> dict:
        if task_id in _FAIL_SHOTS:
            raise videogen.DspVideoGenError(f"任务 {task_id} 生成失败", category="video")
        return {"id": task_id, "status": "completed",
                "metadata": {"url": f"https://ivd.example/video_cache/{task_id}.mp4"}}

    def fetch_to_file(url: str, dest_path: str, timeout_s: float | None = None) -> str:
        _FETCHED.append(url)
        if url.startswith("https://assets.example/"):
            _write_image(dest_path, url)          # 素材图：真写（供真跑降采样）
        else:
            Path(dest_path).write_bytes(b"\x00\x00\x00\x18ftypmp42")   # 片段：占位字节
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


def _reset() -> None:
    _PUTS.clear()
    _SUBMITTED.clear()
    _FETCHED.clear()
    _FAIL_SHOTS.clear()


def main() -> None:
    section("0) discover('image_to_video') —— Agent 自查字段")
    for path, names in field_names("image_to_video").items():
        print(f"  {path}: {names}")

    section("1) Happy path：2 镜 → 输入降采样 + 首尾帧桥 → 提交 → 完成 → 直链转存")
    os.environ[_KEY_ENV] = "sk-demo-key"
    _install_fakes()
    _reset()
    r = invoke("image_to_video", _ctx())
    check(r.ok, "happy path 应 ok")
    print(f"  提交参数  : seconds={[s['seconds'] for s in _SUBMITTED]}（镜头建议时长原样下发）")
    print(f"  首镜 prompt: {_SUBMITTED[0]['prompt']}")
    print(f"  首帧      : {_SUBMITTED[0]['image_url']}")
    print(f"  尾帧      : {_SUBMITTED[0]['tail_image_url']}")
    print(f"  片段      : {[c.key for c in r.data.clips]}")
    print(f"  转存      : {_PUTS}")
    print(f"  结论      : {r.reasoning[0].conclusion}")
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
    # ADR-074 ① 首尾帧桥：s1 的尾帧 = s2 的图；末镜没有下一镜 ⇒ 无尾帧
    check("杯1.jpg" in _SUBMITTED[0]["tail_image_url"],
          "s1 尾帧 = **下一镜**的图（首尾帧桥，ADR-074）")
    check(_SUBMITTED[1]["tail_image_url"] == "", "末镜无下一镜 ⇒ 不下发尾帧")
    # ADR-074 ② 输入降采样：1152×2048 超输出 720×1280 ⇒ 先降采样再下发（派生件转存）
    check("/i2v_input/" in _SUBMITTED[0]["image_url"],
          "超输出分辨率的素材须**降采样后**下发（不是原图直发）")
    check(_SUBMITTED[0]["image_url"].startswith(
        "https://assets.example/dsp/scratch/t1/run-demo-8/i2v_input/"),
        "派生件落 {cachePrefix}/{runId}/i2v_input/…（工作缓存目录内）")
    check(sum(1 for k in _PUTS if "/i2v_input/" in k) == 1,
          "降采样派生件恰 1 个（只给超标的 s1 那张）")
    check(_SUBMITTED[1]["image_url"].endswith("产品/杯1.jpg"),
          "未超输出分辨率的素材**原图直用**（不重编码、不落派生件）")
    check(sum(1 for u in _FETCHED if u.endswith("杯1.jpg")) == 1,
          "同一素材只准备一次（s1 的尾帧与 s2 的图共用，不重复下载）")
    check("降采样" in (r.reasoning[0].causal_analysis or ""),
          "结论须如实说明做过输入降采样")
    check("首尾帧桥" in r.reasoning[0].conclusion, "结论须如实说明用了首尾帧桥")
    check("dsp/scratch/t1/run-demo-8/clips/s1.mp4" in _PUTS, "直链须转存为不可变 key")
    check(r.metrics.degraded is False, "全出片不应标记降级")

    section("2) build_payload：有/无尾帧的两种提交体形状")
    cfg = load_config().dsp
    with_tail = videogen.build_payload(cfg, prompt="p", image_url="A", seconds="3",
                                       tail_image_url="B")
    without = videogen.build_payload(cfg, prompt="p", image_url="A", seconds="3")
    print(f"  有尾帧    : image={with_tail['image']} images={with_tail['images']}")
    print(f"  无尾帧    : image={without['image']} images={without['images']}")
    check(with_tail["images"] == ["A", "B"] and with_tail["image"] == "A",
          "有尾帧 ⇒ images = [首帧, 尾帧]（第 2 位 = 尾帧）")
    check(without["images"] == ["A"], "无尾帧 ⇒ images 只有首帧（与新增前一致）")
    check(videogen.parse_size("720x1280") == (720, 1280)
          and videogen.parse_size("720X1280") == (720, 1280)
          and videogen.parse_size("") == (0, 0) and videogen.parse_size("abc") == (0, 0),
          "parse_size 形状守卫（大小写不敏感；坏串 → (0,0)）")

    section("3) degrade：未配密钥 → 如实降级（不伪造片段）")
    os.environ.pop(_KEY_ENV, None)
    r = invoke("image_to_video", _ctx())
    print(f"  ok        : {r.ok}  degraded={r.metrics.degraded}")
    print(f"  reason    : {r.metrics.degradation_reason}")
    print(f"  warnings  : {r.data.warnings}")
    check(r.ok and r.metrics.degraded and r.metrics.degradation_reason == "videogen-not-configured",
          "无密钥须降级且原因明确")
    check(r.data.clips == [], "降级不得给出片段（不伪造）")
    os.environ[_KEY_ENV] = "sk-demo-key"

    section("4) degrade：缺锁定时间线（deps.timeline.edl）")
    r = invoke("image_to_video", _ctx(deps={}))
    print(f"  reason    : {r.metrics.degradation_reason}")
    check(r.metrics.degradation_reason == "missing-timeline", "缺时间线须降级")

    section("5) degrade：某镜出片失败 → 该镜回落静图（其余照常）")
    _reset()
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

    section("6) 成本闸：maxShots 限制生成镜数")
    _reset()
    r = invoke("image_to_video", _ctx(maxShots=1))
    print(f"  clips     : {[c.shotId for c in r.data.clips]}")
    print(f"  warnings  : {r.data.warnings}")
    check(len(r.data.clips) == 1 and r.data.clips[0].shotId == "s1", "maxShots=1 只生成首镜")
    check(any("maxShots" in w for w in r.data.warnings), "截断须如实说明")

    section("7) 降采样失败 → 回落原图并如实告警（画质风险，不丢镜）")
    _reset()

    def _boom(path: str) -> tuple[int, int]:
        raise media.MediaError("坏图（演示用）")

    media.probe_image_size = _boom
    try:
        r = invoke("image_to_video", _ctx())
    finally:
        media.probe_image_size = _REAL_PROBE
    print(f"  首镜下发的图: {_SUBMITTED[0]['image_url']}")
    print(f"  warnings  : {[w for w in r.data.warnings if '降采样' in w]}")
    check(_SUBMITTED[0]["image_url"] == "https://assets.example/dsp/scratch/t1/门店/门头1.jpg",
          "降采样失败 ⇒ **回落原图**下发")
    check(not any("/i2v_input/" in k for k in _PUTS), "降采样失败 ⇒ 不落派生件")
    check(any("回落原图" in w for w in r.data.warnings), "降采样失败须**如实告警**（不静默）")
    check(len(r.data.clips) == 2, "降采样失败**不丢镜**（画质风险 ≠ 成片失败）")

    section("8) 开关：关首尾帧桥 / 关输入降采样（env 覆盖）")
    os.environ["FLOWMIND_DSP_VIDEOGEN_BRIDGE_NEXT_IMAGE"] = "0"
    _reset()
    invoke("image_to_video", _ctx())
    print(f"  关桥后尾帧: {[s['tail_image_url'] for s in _SUBMITTED]}")
    check(all(s["tail_image_url"] == "" for s in _SUBMITTED), "关桥 ⇒ 全部单图下发")
    check(any("/i2v_input/" in k for k in _PUTS), "关桥不影响降采样（两者独立）")
    os.environ.pop("FLOWMIND_DSP_VIDEOGEN_BRIDGE_NEXT_IMAGE")

    os.environ["FLOWMIND_DSP_VIDEOGEN_DOWNSCALE_INPUT"] = "0"
    _reset()
    invoke("image_to_video", _ctx())
    print(f"  关降采样后首帧: {_SUBMITTED[0]['image_url']}")
    check(not any("/i2v_input/" in k for k in _PUTS), "关降采样 ⇒ 不落派生件")
    check(_SUBMITTED[0]["image_url"].endswith("门店/门头1.jpg"), "关降采样 ⇒ 原图直发")
    os.environ.pop("FLOWMIND_DSP_VIDEOGEN_DOWNSCALE_INPUT")

    print("\n✅ dsp_image_to_video_demo 全部通过")


if __name__ == "__main__":
    main()
