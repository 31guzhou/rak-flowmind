"""subtitle_build 技能：字幕（SRT）——只出参数**不入画**，由 render 统一烧制。

契约 §3.4：依赖 ``tts``（逐句轴）+ ``timeline``（基准）；降级 **degrade**；
受「智能配音」开关控制（**关配音即无逐句字幕**——字幕依赖配音的逐句时间戳）。

时间戳来源：``deps.tts.voSegments``（豆包语音 ``enable_subtitle`` 的句级 / 词级
时间戳）——**不估算、不 ASR 对齐**。

数据诚实（契约 §2.7）：烧制类时间点必须 ∈ ``[0, edl.totalMs]``；越界**如实报出、
不静默夹取**（与 cross-dashboard 的 ``validateTimedParams`` 同口径）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from flowmind.contracts import SkillOutput
from flowmind.skill import skill
from flowmind.skills import _dsp_common as common
from flowmind.skills import _dsp_storage as storage
from flowmind.skills._dsp_common import DspBaseInput

_VERSION = "0.1.0"

_DEFAULT_STYLE = {"fontSize": 48, "position": "bottom", "stroke": {"color": "#000", "width": 2}}


class SubtitleStyle(BaseModel):
    """字幕样式（render 侧烧制参数）。"""
    fontSize: int = 48
    position: str = "bottom"
    stroke: dict[str, object] = Field(default_factory=lambda: {"color": "#000", "width": 2})


class SubtitleBuildInput(DspBaseInput):
    """字幕入参（公共上下文 + 可选样式覆盖）。"""
    style: SubtitleStyle | None = None


class SubtitleItem(BaseModel):
    """单条字幕。"""
    atMs: int
    durationMs: int
    text: str
    style: SubtitleStyle


class SubtitleBuildData(BaseModel):
    """出参：SRT key + 逐条字幕 + 样式 + 如实告警。"""
    srtKey: str = Field(default="", description="完整 SRT 的云存储 key（降级时为空）")
    subtitles: list[SubtitleItem] = Field(default_factory=list)
    style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    warnings: list[str] = Field(default_factory=list, description="时间点越界等如实说明")


def _srt_ts(ms: int) -> str:
    """毫秒 → SRT 时间戳 ``HH:MM:SS,mmm``（负数按 0 处理，防负号写坏文件）。"""
    total = max(0, int(ms))
    hours, rem = divmod(total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt(items: list[SubtitleItem]) -> str:
    """把逐条字幕渲染成 SRT 文本（序号自 1）。"""
    blocks: list[str] = []
    for idx, item in enumerate(items, start=1):
        end = item.atMs + item.durationMs
        blocks.append(
            f"{idx}\n{_srt_ts(item.atMs)} --> {_srt_ts(end)}\n{item.text}\n",
        )
    return "\n".join(blocks)


@skill(id="subtitle_build", name="字幕 SRT", version=_VERSION,
       category="抖音成片", tags=["dsp", "subtitle"])
def subtitle_build(inp: SubtitleBuildInput) -> SkillOutput[SubtitleBuildData]:
    """依配音的逐句 / 词级时间戳出字幕参数 + SRT 文件（只出参数，渲染时统一烧制）。

    ``deps.tts.voSegments`` 缺失（未跑配音 / 配音未开字幕）→ 如实降级，不伪造字幕。
    """
    style = inp.style or SubtitleStyle()
    edl = common.dep_edl(inp.deps)
    if edl is None:
        return SkillOutput(
            data=SubtitleBuildData(style=style),
            reasoning=[common.chain(
                "字幕未生成：缺少锁定时间线（deps.timeline.edl）",
                risk_note="字幕以时间线为基准；请确认 timeline 已跑通后重试。",
            )],
            confidence=0.0, degraded=True,
            degradation_reason="missing-timeline",
        )

    tts = common.dep(inp.deps, "tts") or {}
    raw_segments = tts.get("voSegments")
    if not isinstance(raw_segments, list) or not raw_segments:
        return SkillOutput(
            data=SubtitleBuildData(style=style),
            reasoning=[common.chain(
                "字幕未生成：配音未给出逐句时间戳（deps.tts.voSegments 为空）",
                risk_note="字幕依赖配音的逐句/词级时间戳；关掉「智能配音」时本节点不应生成。",
            )],
            confidence=0.0, degraded=True,
            degradation_reason="no-voice-segments",
        )

    items: list[SubtitleItem] = []
    timed: list[tuple[int, str]] = []
    for idx, seg in enumerate(raw_segments):
        at_ms = int(seg.get("atMs", 0))
        dur_ms = int(seg.get("durationMs", 0))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        items.append(SubtitleItem(atMs=at_ms, durationMs=dur_ms, text=text, style=style))
        timed.append((at_ms, f"字幕 #{len(items)}"))
        timed.append((at_ms + dur_ms, f"字幕 #{len(items)} 结束"))

    warnings = common.check_in_timeline(edl.totalMs, timed)
    srt_key = ""
    if items:
        srt_key = common.subtitle_key(inp.cachePrefix, inp.runId)
        storage.put_text(srt_key, render_srt(items), content_type="text/plain; charset=utf-8")

    conclusion = (
        f"字幕 {len(items)} 条（SRT 落 {srt_key or '未生成'}）"
        + (f"；{len(warnings)} 处时间点越界（已如实报出，未夹取）" if warnings else "")
    )
    return SkillOutput(
        data=SubtitleBuildData(srtKey=srt_key, subtitles=items, style=style, warnings=warnings),
        reasoning=[common.chain(
            conclusion,
            causal_analysis=(
                f"由 deps.tts.voSegments（{len(raw_segments)} 段）逐句取时间戳；"
                f"基准 = 锁定时间线 totalMs={edl.totalMs}ms。"
            ),
            risk_note=(
                "存在越界时间点：请检查配音与时间线是否同源（字幕不夹取，越界即如实报出）。"
                if warnings else
                "字幕只出参数不入画，最终由 render_compose 一次性烧制（字幕轨独占、≤10 条）。"
            ),
        )],
        confidence=0.9 if not warnings else 0.5,
        sample_size=len(items),
    )
