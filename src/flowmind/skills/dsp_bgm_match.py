"""bgm_match 技能：背景音乐——**如实降级**（契约 §3.3）。

契约 §3.3：
- 依赖 ``timeline``（按**锁定总时长**选曲，与有没有配音无关）；降级 **degrade**；
  受「背景音乐」开关控制。
- 出参 ``{bgmKey, ducking:{enabled,db}, durationMs}``；无合适曲目 → ``degraded=true``
  + 原因（成片**空轨继续**）。

**现状（ADR-126）**：本仓仍**没有曲库 / 选曲接口**（《豆包语音》只提供 TTS；
MediaKit 只有音量 / 闪避参数，无曲库）。与 2026-10-12 的占位实现的差别是**状态语义**：

- 未配置（``FLOWMIND_DSP_BGM_TRACK_KEY`` 为空）⇒ ``data.degraded=true`` +
  ``reason=not_configured``，运行器把 ``bgm`` 步**标 degraded**，任务整体如实呈现「静音回退」；
  **不再**出现「技能 degraded、运行器却记 ok」的假成功。
- 运维把一首**已入库**的曲子 key 配进来 ⇒ 先 ``storage.exists`` 校验它真的在对象存储里，
  再交给 render 混音；缺对象 ⇒ 仍是 degraded（``configured_but_missing``），不编造。
- 不做「猜一个曲目 / 随机取一首」：没有授权与曲库元数据时，选曲结果无法复核。

拿到选曲接口后只需替换 ``_pick_track`` 一处，DAG / 开关 / 出参形状 / 降级语义均不变。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from flowmind.config import load_config
from flowmind.contracts import SkillOutput
from flowmind.skill import skill
from flowmind.skills import _dsp_common as common
from flowmind.skills import _dsp_storage as storage
from flowmind.skills._dsp_common import DspBaseInput

_VERSION = "0.2.0"

_UNAVAILABLE = "BGM 选曲能力未接通"
_DEFAULT_DUCKING_DB = -12


class BgmMatchInput(DspBaseInput):
    """BGM 入参（公共上下文 + 可选情绪/风格提示）。"""
    mood: str = Field(default="", description="情绪 / 风格（可空；选曲接通后由它驱动）")


class Ducking(BaseModel):
    """闪避参数（配音起时压低 BGM）。"""
    enabled: bool = True
    db: int = _DEFAULT_DUCKING_DB


class BgmMatchData(BaseModel):
    """出参：BGM key（未接通时为空）+ 闪避参数 + 时长 + **如实状态**。"""
    bgmKey: str = Field(default="", description="BGM 的云存储 key；未接通时为空（成片空轨）")
    ducking: Ducking = Field(default_factory=Ducking)
    durationMs: int = 0
    degraded: bool = Field(
        default=False,
        description="**本步是否降级**（true ⇒ 运行器把 bgm 步标 degraded；不冒充成功）")
    reason: str = Field(default="", description="降级原因码：not_configured / configured_but_missing / ''")
    note: str = Field(default="", description="给人看的说明（不含凭证 / 预签名 URL）")
    warnings: list[str] = Field(default_factory=list)


def _pick_track(inp: BgmMatchInput, total_ms: int) -> tuple[str, str, str]:
    """选曲 → ``(key, reason, note)``；``key`` 为空表示降级。

    当前只有「配置单曲」这一条可复核的路径；没有曲库时如实返回 ``not_configured``。
    """
    cfg = load_config().dsp
    key = str(getattr(cfg, "bgm_track_key", "") or "").strip()
    if not key:
        return "", "not_configured", _UNAVAILABLE
    try:
        exists = storage.exists(key)
    except storage.DspStorageError as exc:
        return "", "configured_but_missing", f"BGM 对象 {key} 无法校验：{exc}"
    if not exists:
        return "", "configured_but_missing", f"BGM 对象 {key} 不在对象存储里——不静默回退到别的曲子"
    return key, "", ""


@skill(id="bgm_match", name="背景音乐", version=_VERSION,
       category="抖音成片", tags=["dsp", "bgm"])
def bgm_match(inp: BgmMatchInput) -> SkillOutput[BgmMatchData]:
    """按锁定总时长 / 情绪选曲 + 闪避参数；无选曲能力 → **如实降级为空轨**。"""
    edl = common.dep_edl(inp.deps)
    total_ms = edl.totalMs if edl else 0
    bgm_key, reason, note = _pick_track(inp, total_ms)

    if not bgm_key:
        return SkillOutput(
            data=BgmMatchData(
                bgmKey="", ducking=Ducking(), durationMs=total_ms,
                degraded=True, reason=reason, note=note, warnings=[note]),
            reasoning=[common.chain(
                f"背景音乐未匹配：{note}（成片空轨继续）",
                causal_analysis=(
                    f"参考时长 = 锁定时间线 totalMs={total_ms}ms；"
                    "本仓没有曲库 / 选曲接口，只支持配置一首已入库的曲子"
                    "（FLOWMIND_DSP_BGM_TRACK_KEY）；未配置时如实降级。"
                ),
                risk_note=(
                    "本节点属 degrade：失败不阻塞成片，但 bgm 步会标 **degraded**，"
                    "任务整体如实呈现「静音回退」——不把未接通说成成功。"
                ),
            )],
            confidence=0.0, degraded=True, degradation_reason=note or _UNAVAILABLE,
        )

    return SkillOutput(
        data=BgmMatchData(bgmKey=bgm_key, ducking=Ducking(), durationMs=total_ms,
                          degraded=False, reason="", note=f"BGM {bgm_key}"),
        reasoning=[common.chain(
            f"背景音乐匹配完成：{bgm_key}（{total_ms}ms）",
            causal_analysis=(
                f"按锁定总时长 {total_ms}ms 与情绪「{inp.mood or '未指定'}」选曲；"
                "曲目来自运维配置且已校验对象存在。"),
            risk_note="闪避参数在 render_compose 侧按配音轨生效。",
        )],
        confidence=0.9, sample_size=1,
    )
