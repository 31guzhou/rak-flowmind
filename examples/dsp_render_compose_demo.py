"""dsp_render_compose 技能演示 —— 合成渲染（MediaKit；唯一渲染 / DAG 汇点）。

运行：PYTHONPATH=<repo>/src conda run -n flowmind python examples/dsp_render_compose_demo.py

展示：
1. discover() 自动字段发现
2. happy path：EDL → track[][] → 提交渲染 → **成片转存**为不可变 key（§7.3②）
3. error：缺渲染密钥 → 明确报错（abort 步，不伪造成片）
4. error：EDL 各镜均无图 → 轨道为空，如实报错
5. **运镜不在这里施加**（ADR-076）：运镜已随 I2V 生成落地，本步**不得**再施加（否则同一运动两次）
   ——含一条**结构性哨兵**：``_dsp_mediakit`` 不再暴露 ``apply_camera_motion``

mock 方式：替换 ``_dsp_mediakit.render``（模块级薄适配层）
与 ``_dsp_storage`` 的 presign/fetch/put_file，不需要网络 / MediaKit 密钥 / 对象存储。
密钥用假值经 env 注入。
"""

from __future__ import annotations

import importlib
import os

import flowmind.skills  # noqa: F401  触发 @skill 注册
from flowmind.discover import field_names
from flowmind.skill import invoke
from flowmind.skills import _dsp_mediakit as mediakit
from flowmind.skills import _dsp_storage as storage

_KEY_ENV = "DSP_MEDIAKIT_API_KEY"
_BODY: dict = {}
_PUTS: list[str] = []    # 所有 put_file 的 key 序列（运镜退役后只有成片）


def section(title: str) -> None:
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _install_fakes() -> None:
    def render(cfg, body):
        _BODY.clear()
        _BODY.update(body)
        return {"task_id": "mediakit-task-1", "video_url": "https://mediakit.example/tmp/final.mp4?sig=x",
                "duration": 9.0, "resolution": "1080p"}

    def fetch_to_file(url: str, dest_path: str, timeout_s: float | None = None) -> str:
        _BODY["_fetched"] = url
        return dest_path

    def put_file(path: str, key: str, content_type: str | None = None) -> str:
        _PUTS.append(key)
        _BODY["_stored"] = key
        return key

    def presign_get(key: str, ttl_s: int | None = None) -> str:
        return f"https://assets.example/{key}"

    mediakit.render = render
    storage.fetch_to_file = fetch_to_file
    storage.put_file = put_file
    storage.presign_get = presign_get


def _edl() -> dict:
    return {"timeline": {"edl": {
        "version": 1, "totalMs": 9000,
        "tracks": [
            {"kind": "image", "segments": [
                {"atMs": 0, "durationMs": 3000, "sourceKey": "dsp/scratch/t1/门店/门头1.jpg"},
                {"atMs": 3000, "durationMs": 6000, "sourceKey": "dsp/scratch/t1/产品/杯1.jpg"},
            ]},
            {"kind": "vo", "segments": [{"atMs": 0, "durationMs": 9000, "sourceKey": ""}]},
        ],
        "shots": [{"id": "s1", "atMs": 0, "durationMs": 3000,
                   "imageSourceKey": "dsp/scratch/t1/门店/门头1.jpg"}],
    }}}


def _edl_motion() -> dict:
    """带**运镜方案**的 EDL：2 镜，s1 带 push_in 方案（render **不应**因此做任何事），s2 无方案。"""
    return {"timeline": {"edl": {
        "version": 1, "totalMs": 7000,
        "tracks": [
            {"kind": "image", "segments": [
                {"atMs": 0, "durationMs": 3000, "sourceKey": "dsp/scratch/t1/门店/门头1.jpg"},
                {"atMs": 3000, "durationMs": 4000, "sourceKey": "dsp/scratch/t1/产品/杯1.jpg"},
            ]},
            {"kind": "vo", "segments": [{"atMs": 0, "durationMs": 7000, "sourceKey": ""}]},
        ],
        "shots": [
            {"id": "s1", "atMs": 0, "durationMs": 3000,
             "imageSourceKey": "dsp/scratch/t1/门店/门头1.jpg",
             "motion": {"type": "push_in", "intensity": "subtle", "reason": "留白充足",
                        "positive": "", "negative": ""}},
            {"id": "s2", "atMs": 3000, "durationMs": 4000,
             "imageSourceKey": "dsp/scratch/t1/产品/杯1.jpg"},
        ],
    }}}


def _ctx(**over) -> dict:
    base = {
        "mode": "content", "platform": "douyin",
        "cachePrefix": "dsp/scratch/t1", "outputPrefix": "dsp/output/t1",
        "runId": "run-demo-7", "stepId": "render", "idempotencyKey": "run-demo-7:render",
        "shots": [], "materials": [],
        "deps": _edl() | {
            "tts": {"audioKey": "dsp/scratch/t1/run-demo-7/audio/voice.mp3"},
            "subtitle": {"srtKey": "dsp/scratch/t1/run-demo-7/subtitle/captions.srt"},
            "cover": {"coverKey": "dsp/scratch/t1/门店/门头1.jpg", "atMs": 0},
            "cart": {"stickers": [{"atMs": 0, "durationMs": 9000,
                                   "productRef": {"kind": "manual", "value": "sku-123"}}]},
        },
    }
    base.update(over)
    return base


def main() -> None:
    section("0) discover('render_compose') —— Agent 自查字段")
    for path, names in field_names("render_compose").items():
        print(f"  {path}: {names}")

    section("1) Happy path：EDL → track[][] → 渲染 → 成片转存")
    os.environ[_KEY_ENV] = "demo-mediakit-key"
    _install_fakes()
    r = invoke("render_compose", _ctx())
    check(r.ok, "happy path 应 ok")
    d = r.data
    print(f"  storedKey : {d.storedKey}")
    print(f"  duration  : {d.durationMs}ms  分辨率: {d.width}×{d.height}")
    print(f"  轨道数    : {len(_BODY['track'])}（图片主轨 / 音轨 / 字幕轨）")
    print("  轨内元素  :")
    for idx, track in enumerate(_BODY["track"]):
        print(f"    [{idx}] " + ", ".join(e["type"] for e in track))
    print(f"  output    : {_BODY['output']}（封面走 output.cover 抽帧）")
    print(f"  AIGC 元数据: {_BODY.get('extra_param', {}).get('upload', {}).get('aigc_meta')}")
    print(f"  warnings  : {d.warnings}")
    check(d.storedKey == "dsp/output/t1/run-demo-7/final.mp4", "成片须落成片目录")
    check(_BODY["_stored"] == d.storedKey, "24h 签名 URL 必须转存为不可变 key（§7.3②）")
    check(_BODY["output"]["cover"]["cover_time"] == [0], "封面走 output.cover 抽帧")
    check(len(_BODY["track"][0]) == 2, "图片主轨两段")
    check(any("平台组件" in w for w in d.warnings), "商品链接不烧进画面须如实说明")

    section("2) error：缺渲染密钥（abort 步）→ 明确报错，不伪造成片")
    os.environ.pop(_KEY_ENV, None)
    r = invoke("render_compose", _ctx())
    print(f"  ok        : {r.ok}")
    print(f"  error.msg : {(r.error.message if r.error else '')[:80]}")
    check(r.ok is False, "无密钥必须失败（成片字段保持空）")
    os.environ[_KEY_ENV] = "demo-mediakit-key"

    section("3) error：EDL 各镜均无图 → 轨道为空")
    empty = {"timeline": {"edl": {"version": 1, "totalMs": 9000, "tracks": [
        {"kind": "image", "segments": [{"atMs": 0, "durationMs": 9000, "sourceKey": ""}]}]}}}
    r = invoke("render_compose", _ctx(deps=empty))
    print(f"  ok        : {r.ok}")
    print(f"  error.msg : {(r.error.message if r.error else '')[:60]}")
    check(r.ok is False and "轨道为空" in (r.error.message if r.error else ""), "空轨道须如实报错")

    section("4a) 有片段的镜头走 video 元素，无片段的回落 image（运镜**不在此施加**）")
    _BODY.clear()
    _PUTS.clear()
    r = invoke("render_compose", _ctx(deps=_edl_motion() | {
        "video": {"clips": [{"shotId": "s1", "key": "dsp/scratch/t1/run-demo-7/clips/s1.mp4",
                             "atMs": 0, "durationMs": 3000}]},
        "tts": {"audioKey": "dsp/scratch/t1/run-demo-7/audio/voice.mp3"},
    }))
    check(r.ok, "有片段应渲染成功")
    kinds = [e["type"] for e in _BODY["track"][0]]
    print(f"  主轨元素  : {kinds}（s1 有片段=video / s2 无片段=image 回落）")
    print(f"  put_file  : {_PUTS}（运镜退役 ⇒ 只有成片，没有 motion/*.mp4）")
    print(f"  warnings  : {r.data.warnings}")
    check(kinds == ["video", "image"], "有片段的镜头走 video 元素，无片段的回落 image")
    check(_PUTS == ["dsp/output/t1/run-demo-7/final.mp4"], "只转存成片——不再有运镜产物")
    check(not any("运镜" in w for w in r.data.warnings), "不再因运镜产生告警（本步已不参与运镜）")

    section("4b) 结构性哨兵：运镜施加路径**整块退役**（防重复施加）")
    # 用 importlib 取模块：main() 后面还有一处**局部** import 同名（转场表那节），
    # 直接用模块级 `mediakit` 会被 Python 当成局部变量而在此处未绑定。
    _mk = importlib.import_module("flowmind.skills._dsp_mediakit")
    check(not hasattr(_mk, "apply_camera_motion"),
          "MediaKit 客户端不再暴露 apply_camera_motion（ADR-076）")
    check(not hasattr(_mk, "motion_style_for") and not hasattr(_mk, "MOTION_STYLES"),
          "旧运镜词表/映射一并退役（新适配表在 _dsp_video_gen.MOTION_ENGINE）")
    _BODY.clear()
    r = invoke("render_compose", _ctx(deps=_edl_motion()))
    check(r.ok, "带运镜方案的 EDL 仍可静图成片")
    check([e["type"] for e in _BODY["track"][0]] == ["image", "image"], "无片段全部回落静图")

    section("4c) 配音实测总长 ≠ 锁定时间线 → 自检告警（ADR-069 新不变量）")
    _BODY.clear()
    deps = _edl_motion()
    deps["tts"] = {"audioKey": "dsp/scratch/t1/run-demo-7/audio/voice.mp3", "durationMs": 9999}
    r = invoke("render_compose", _ctx(deps=deps))
    check(r.ok, "不一致仍可渲染（如实告警，不静默重算）")
    print(f"  warnings  : {r.data.warnings}")
    check(any("不一致" in w for w in r.data.warnings),
          "EDL 与配音不同源须如实告警（时间线应即音频总长）")

    section("4d) 转场（ADR-070）：厂商资源未登记 → **不加 extra**、如实回落硬切")
    _BODY.clear()
    deps = _edl_motion()
    # s1 末尾标一个转场（符号名）；当前 TRANSITION_VENDOR 为空表 ⇒ 应回落硬切并告警
    deps["timeline"]["edl"]["tracks"][0]["segments"][0]["transition"] = "dissolve"
    r = invoke("render_compose", _ctx(deps=deps))
    check(r.ok, "未登记转场不应让渲染失败（回落硬切即可）")
    print(f"  warnings  : {r.data.warnings}")
    first = _BODY["track"][0][0]
    check(not any(f.get("type") == "transition" for f in (first.get("extra") or [])),
          "未登记的转场**不得**编 ID 编 extra（如实回落硬切）")
    check(any("dissolve" in w and "未登记" in w and "硬切" in w for w in r.data.warnings),
          "未登记转场须如实告警（绝不编造厂商 ID）")

    section("4e) 转场：登记后（模拟填表）→ 编成厂商 extra 滤镜，加在**前一个**元素上")
    from flowmind.skills import _dsp_mediakit as mediakit
    _BODY.clear()
    mediakit.TRANSITION_VENDOR["dissolve"] = {"id": "vcr//test-dissolve", "mode": "non-overlap", "durationMs": 400}
    try:
        deps = _edl_motion()
        deps["timeline"]["edl"]["tracks"][0]["segments"][0]["transition"] = "dissolve"
        r = invoke("render_compose", _ctx(deps=deps))
        check(r.ok, "登记后应正常渲染")
        first = _BODY["track"][0][0]
        extra = first.get("extra") or []
        check(extra == [{"type": "transition", "source": "vcr//test-dissolve", "duration": 400}],
              f"转场应编成 extra 滤镜加在前一个元素上，实际 {extra}")
        check(not any("未登记" in w for w in r.data.warnings), "登记后不应再报「未登记」")
    finally:
        mediakit.TRANSITION_VENDOR.pop("dissolve", None)  # 表是进程级的，用完恢复为空

    print("\n✅ dsp_render_compose_demo 全部通过")


if __name__ == "__main__":
    main()
