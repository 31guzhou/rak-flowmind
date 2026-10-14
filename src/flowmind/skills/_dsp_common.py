"""dsp 成片技能套件的**公共层**：公共上下文模型 + EDL 中间表示 + 目录/key 约定。

契约真源（cross-dashboard 侧）：
``docs/architecture/2026-10-11-dsp-backend-skill-contract.md``
- §2.2 入参 = 公共上下文 + 运行器注入（``runId`` / ``stepId`` / ``idempotencyKey`` / ``deps``）
- §2.3 出参 = SkillResult 信封 + 产物 key（``data.key`` / ``storedKey`` / ``artifactKey`` 任一）
- §2.4 目录约定（全云、零本地）：中间产物落 ``cachePrefix``，成片落 ``outputPrefix``，
  一律 ``/{runId}/`` 一级命名空间
- §2.7 时间单位一律 ms；``timeline_build`` 锁定的 ``totalMs`` 是**唯一时间基准**

设计约束：
- **时长真源 = 配音逐句实测**（2026-10-13 改判，ADR-069）：DAG 是 ``tts → timeline``——
  ``tts`` 先出音轨与逐句时间戳，``timeline`` 再按它锁时间线（``_dsp_align`` 把句子归属到镜头）。
  ``shots[].durationMs`` 因此**降级为「无配音」时的估算兜底**。**旧「甲案·时间线前移」已推翻**
  （那要求配音适配已锁定的画面，而厂商不保证该精度——详见 ADR-069）。
- **镜头由文案自己定**（2026-10-13 改判，ADR-070；**六个内置模板已整体删除**）：不再有「固定镜头位」，
  镜头的「有几个、切在哪」由文案说了算——① 文案按**视觉信息点**写成镜头单元（一镜一信息点，镜数由内容定）；
  ② ``timeline`` 在实测轴上做**节奏收口**（``_dsp_align.consolidate_shots``：过短的镜并入邻镜、
  过长的镜在其内部**最近的合法边界**一分为二——**先句边界，句内下沉到词边界**）；③ 切点**永远落在
  实测 token（句 / 词）边界**上。比例兜底的权重不再是下发的
  ``weight``（该字段已删），改由 ``timeline`` 从该镜文案的**可念字数**自算。
- **转场是 EDL 段的字段**（ADR-070）：``segments[i].transition`` = 第 i 镜**末尾**、桥接 i→i+1 的转场
  （符号名，对齐厂商「加到相邻两元素的前一个」）。厂商资源 ID 走 ``_dsp_mediakit.TRANSITION_VENDOR``
  单点表——**表为空即未登记**，``render_compose`` **如实回落硬切并告警**，**绝不编造 ID**。
  只允许**非交叠**转场（音轨是一整条实测音频、时间线严格连续，交叠会移动画面或改总时长 ⇒ 音画失同步）。
- EDL 的 ``vo`` 轨仍只声明位次（``[0, totalMs)``），``sourceKey`` 由 ``render_compose``
  从 ``deps.tts.audioKey`` 挂上；但 ``totalMs`` 现在**就是音频长度**（末尾静默并入末镜）。
- 图片源（2026-10-12 重构）：素材池是**图片**，主轨是 ``image``（不是 ``video``）——
  每段 = 一张图 + 绝对区间，**无源内 trim**（``inMs``/``outMs`` 不存在）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flowmind.contracts import Evidence, ReasoningChain, RuleHit

DspMode = Literal["content", "commerce"]


def _join(*parts: str) -> str:
    """以 ``/`` 连接 key 片段，折叠多余斜杠（对象 key 的规范形式）。"""
    segs = [p.strip("/") for p in parts if p is not None and str(p).strip("/")]
    return "/".join(segs)


def edl_key(cache_prefix: str, run_id: str) -> str:
    """锁定 EDL 快照的 key（§2.4：``{cachePrefix}/{runId}/{stepId}/…``）。"""
    return _join(cache_prefix, run_id, "timeline", "edl.json")


def audio_key(cache_prefix: str, run_id: str, ext: str = "mp3") -> str:
    """口播音轨的 key（§2.4 的「音频夹」）。"""
    return _join(cache_prefix, run_id, "audio", f"voice.{ext}")


def subtitle_key(cache_prefix: str, run_id: str) -> str:
    """字幕 SRT 的 key（§2.4 的「字幕文件夹」）。"""
    return _join(cache_prefix, run_id, "subtitle", "captions.srt")


def words_subtitle_key(cache_prefix: str, run_id: str) -> str:
    """**词级** SRT 的 key（ADR-073）——与句级 SRT 并列落同一字幕夹。

    用途：**导出 / 二次剪辑**（平台跟读字幕）。**不参与烧制**——烧制仍用句级 SRT
    （既有轨数约束不变，见契约 §3.4）。厂商未给词级 token 时本文件不生成。
    """
    return _join(cache_prefix, run_id, "subtitle", "captions.words.srt")


def final_key(output_prefix: str, run_id: str, ext: str = "mp4") -> str:
    """成片的 key（§2.4；本仓就取这个 key 当成品）。"""
    return _join(output_prefix, run_id, f"final.{ext}")


def step_key(cache_prefix: str, run_id: str, step_id: str, filename: str) -> str:
    """其余中间产物的 key（``{cachePrefix}/{runId}/{stepId}/…``）。"""
    return _join(cache_prefix, run_id, step_id, filename)


# ── 入参模型（§2.2）─────────────────────────────────────────────

# 运镜**符号**词表（与前端 `lib/content/dsp-motion.ts` 的 `DSP_MOTION_TYPES` **同源同值**）：
# 那边是产物侧真源（并带符号 → 引擎表达的单点适配表），本侧只**消费**——改一处必改另一处，
# 同「转场资源 ID 两处同源」的纪律。故意**不做** pydantic 枚举校验：词表外只让适配器**如实回落**
# （见 `_dsp_video_gen.motion_expression`），不因一个逐镜参数把整步调用打挂。
MOTION_TYPES: tuple[str, ...] = (
    "none", "push_in", "pull_out", "pan_left", "pan_right",
    "tilt_up", "tilt_down", "zoom_in", "zoom_out", "orbit",
)
MOTION_INTENSITIES: tuple[str, ...] = ("minimal", "subtle", "standard", "strong")


class DspShotMotion(BaseModel):
    """单镜的**运镜方案**（ADR-076「运镜导演」）。

    符号词表与引擎适配真源在前端 `lib/content/dsp-motion.ts`（**同源同值**：那边的
    `DSP_MOTION_TYPES` 与本文件 `MOTION_TYPES` 是同一份符号集，改一处必改另一处——
    与转场资源 ID「两处同源同值」同一纪律）。本侧只**消费**：把符号经适配器
    （`_dsp_video_gen.motion_expression`）转成该引擎的表达。

    **缺省（None / 上游没给）= 摄影机固定**，不是「没有这个字段」。
    """

    type: str = Field(..., description="运镜符号：none / push_in / pull_out / pan_left / pan_right / "
                                       "tilt_up / tilt_down / zoom_in / zoom_out / orbit")
    intensity: str = Field(..., description="幅度：minimal（极轻，『不宜明显运镜』时的落法）/ subtle / standard / strong")
    direction: str | None = Field(default=None, description="方向：toward_subject / away_from_subject / "
                                                           "left_to_right / right_to_left / none")
    target: str | None = Field(default=None, description="视觉焦点（如 coffee_cup）")
    reason: str = Field(default="", description="**判断依据**（为什么这么运镜 / 为什么不宜明显运镜）")
    positive: str = Field(default="", description="Agent 给引擎的**正向**运动描述（空则适配器出确定性短语）")
    negative: str = Field(default="", description="给引擎的**负向**约束（防变形/改标签/闪烁/突然变焦/新增物体）")
    locked: bool = Field(default=False, description="人工锁定：重跑与重分析**不得覆盖**（导演员会跳过该镜）")


class DspSwitches(BaseModel):
    """本单的**开关整组**（ADR-076 起随公共 args 下发，不再只靠「哪些节点在场」表达）。

    为什么要有它：`motion` **不 gate 任何节点**（运镜是逐镜参数不是工序），后端无从「数节点」得知
    运镜开没开——而 `image_to_video` 必须知道（关 ⇒ 逐镜下**固定机位**条款；开 ⇒ 下发逐镜方案）。
    缺省按各开关的缺省给（tts/bgm/video 开、motion 关）。
    """

    tts: bool = True
    bgm: bool = True
    video: bool = True
    motion: bool = False


class DspShot(BaseModel):
    """单个**镜头单元**（ADR-070：文案按视觉信息点切出来的一段）+ 逐句文案 + 需要的**图片**类目。"""

    # 线上字段名按契约 §2.2 是 ``copy``；但 ``copy`` 与 ``BaseModel.copy`` 撞名
    # （pydantic v2 会告警），故属性名用 ``copy_text`` + 别名 ``copy``（populate_by_name
    # 让两种写法都可用；本仓代码统一走 ``.copy_text``）。
    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str = ""
    needKind: str = Field(default="", description="该镜需要的图片类目（与 materials[].category 比对）")
    durationMs: int = Field(
        ..., ge=1,
        description="镜头**估算**时长（ms，前端按文案字数算）——ADR-069：仅「无配音」时的兜底；"
                    "有配音时真实时长由 `timeline` 按逐句实测锁定，本值被覆盖")
    copy_text: str = Field(default="", alias="copy")
    visualPrompt: str = Field(
        default="",
        description="该镜的**画面提示词**（ADR-073；与 `copy` 口播文案**分开**）——喂图生视频模型的"
                    "**画面描述**（主体 + 动作 + 环境光线 + 景别机位 + 风格），**不是台词**。"
                    "空串 = 未给 ⇒ 下游回落用 `copy` 出 prompt 并**如实告警**（绝不假装有画面提示词）")
    clipKeys: list[str] = Field(default_factory=list, description="指名的图片 key（优先于 needKind 选图）")
    motion: DspShotMotion | None = Field(
        default=None,
        description="该镜的**运镜方案**（ADR-076）。**缺省 = 摄影机固定**；`locked: true` = 人工锁定。"
                    "由前端「运镜导演」**看图**产出（旧布尔 `cameraMotion` 已退役）")
    transitionAfter: str | None = Field(
        default=None,
        description="该镜**末尾**的转场覆盖（ADR-070；符号名，如 `dissolve`）——缺省 = 硬切（不写字段）。"
                    "只允许非交叠转场；厂商资源未登记时 render 侧如实回落硬切")


class DspMaterial(BaseModel):
    """素材池条目（**图片**：jpg/jpeg/png/webp）。"""
    kind: str = "image"
    key: str
    category: str = ""
    width: int | None = None
    height: int | None = None
    durationMs: int = 0


class DspBaseInput(BaseModel):
    """所有 dsp 技能的**公共入参**（§2.2 公共上下文 + 运行器注入）。

    各技能在自己的 Input 模型里继承本类并追加本步专有字段（如 cart 的 ``productRef``）。
    """

    # ── 公共上下文（运行器每步下发同一份）──
    mode: DspMode = Field(..., description="content | commerce（决定 cart 是否在编排里）")
    platform: str = "douyin"
    targetSec: int = Field(
        default=0,
        description="**目标总时长**（秒，ADR-070——用户直设，不再是模板属性）。真实时长由配音实测 + "
                    "收口规则决定；本字段只作参考留档")
    profileId: str | None = None
    shots: list[DspShot] = Field(default_factory=list)
    materials: list[DspMaterial] = Field(default_factory=list)
    materialsNote: str = Field(default="", description="素材池被截断/未识别类目时的如实说明（逐字透出）")
    productInfo: str = Field(default="", description="仅带货：出文案的商品信息单行事实文本")
    cachePrefix: str = Field(..., description="工作缓存目录（云存储 prefix）")
    outputPrefix: str = Field(..., description="成片目录（云存储 prefix）")
    switches: DspSwitches = Field(
        default_factory=DspSwitches,
        description="本单**开关整组**（ADR-076，随公共 args 下发）。`motion` 不 gate 任何节点，"
                    "故只能这样传——`image_to_video` 靠它决定「关 ⇒ 固定机位条款」还是「开 ⇒ 下发运镜方案」")

    # ── 运行器注入（每步都有）──
    runId: str = Field(..., description="本单运行 id（对象 key 的命名空间）")
    stepId: str = Field(..., description="本步骤 id（timeline/tts/bgm/…）")
    idempotencyKey: str = Field(default="", description="幂等键 {runId}:{stepId}（重复调用不得重复计费）")
    deps: dict[str, Any] = Field(default_factory=dict, description="上游产物（按 dependsOn 注入；没跑的上游为 null）")


# ── EDL 中间表示（§3.2 / §2.7）──────────────────────────────────

class DspEdlSegment(BaseModel):
    """EDL 段。图片轨：一张图 + 绝对区间（**无源内 trim**）。"""
    atMs: int
    durationMs: int
    sourceKey: str = ""
    transition: str | None = Field(
        default=None,
        description="**转场**（符号名，ADR-070）——语义 = **本段末尾、桥接下一段**（对齐厂商「加到相邻两元素的"
                    "前一个」）。取值见契约的转场词表；**硬切 = 不写该字段**。厂商资源 ID 未登记时 render 侧"
                    "如实回落硬切并告警；只允许**非交叠**转场")


class DspEdlTrack(BaseModel):
    """EDL 轨：``image`` 主轨 + ``vo``/``bgm``/``sfx`` 音轨。"""
    kind: Literal["image", "vo", "bgm", "sfx"]
    segments: list[DspEdlSegment] = Field(default_factory=list)


class DspEdlShot(BaseModel):
    """EDL 里的**终版镜头**（渲染侧按它定位每镜）。

    ADR-070：镜头在 ``timeline`` 里经**收口**（并/拆）后会重排 id（``s1..sK``），故终版镜头的
    ``id`` 与下发的 ``shot.id`` **不一定一一对应**——因此把**该镜的文案**（与类目）一并落进 EDL，
    下游（I2V 的提示词、封面、诊断）一律读这里，**不再回查 ``inp.shots``**（否则并/拆后必然错配）。
    """
    id: str
    atMs: int
    durationMs: int
    imageSourceKey: str = ""
    copy: str = Field(default="", description="**该终版镜头**的口播文案（并镜=拼接、拆镜=该半段）；下游据此出提示词")
    visualPrompt: str = Field(
        default="",
        description="**该终版镜头**的**画面提示词**（ADR-073；并镜=按行拼接、拆镜=两半共用）。"
                    "`image_to_video` 优先用它出 prompt，缺则回落 `copy` 并如实告警")
    needKind: str = Field(default="", description="该终版镜头需要的图片类目（并镜取其一，仅供诊断）")
    motion: DspShotMotion | None = Field(
        default=None,
        description="**该终版镜头**的运镜方案（ADR-076；并镜取**主导镜**、拆镜两半共用）。"
                    "`image_to_video` 据此经适配器并入 prompt；None = 摄影机固定。"
                    "**放在 EDL 而非回查 inp.shots**：并/拆会重排 id，回查必然错配（同 copy/visualPrompt 的理由）")


class DspEdl(BaseModel):
    """锁定时间线：``totalMs`` 是**唯一时间基准**，此后不得再改（唯一写者 = timeline）。"""
    version: int = 1
    totalMs: int
    tracks: list[DspEdlTrack] = Field(default_factory=list)
    shots: list[DspEdlShot] = Field(default_factory=list)


# ── deps（上游产物）读取 ─────────────────────────────────────────

def dep(deps: dict[str, Any], step: str) -> dict[str, Any] | None:
    """取某上游步骤的产物载荷（未跑 / null 即 None）。"""
    value = deps.get(step)
    return value if isinstance(value, dict) else None


def dep_edl(deps: dict[str, Any]) -> DspEdl | None:
    """取 ``deps.timeline.edl`` 并解析成 EDL（缺 / 形状不对即 None）。"""
    timeline = dep(deps, "timeline")
    if not timeline:
        return None
    raw = timeline.get("edl")
    if not isinstance(raw, dict):
        return None
    try:
        return DspEdl.model_validate(raw)
    except Exception:  # noqa: BLE001  形状不对 → 交给调用方如实报错
        return None


# ── 时间线校验（与 cross-dashboard 的 validateTimedParams 同口径）──

def check_in_timeline(total_ms: int, params: list[tuple[int, str]]) -> list[str]:
    """校验参数时间点 ∈ ``[0, totalMs]``；返回违规说明（空 = 合规）。

    **不静默夹取**——越界即如实列出（字幕 / 贴片 / 封面位都走这里）。
    """
    bad: list[str] = []
    if total_ms <= 0:
        bad.append(f"EDL totalMs 必须 > 0（当前 {total_ms}）")
    for at_ms, label in params:
        if at_ms < 0 or at_ms > total_ms:
            bad.append(f"{label} 时间点 {at_ms}ms 越界 [0, {total_ms}]")
    return bad


# ── 推理链 ──────────────────────────────────────────────────────

def chain(
    conclusion: str,
    *,
    causal_analysis: str = "",
    risk_note: str = "",
    rules: list[RuleHit] | None = None,
    evidence: list[Evidence] | None = None,
    confidence: float = 1.0,
) -> ReasoningChain:
    """四段式推理链的轻量构造器（结论 / 触发规则 / 数据证据 / 因果与风险）。"""
    return ReasoningChain(
        conclusion=conclusion,
        triggered_rules=rules or [],
        evidence=evidence or [],
        causal_analysis=causal_analysis or conclusion,
        risk_note=risk_note,
        confidence=confidence,
    )
