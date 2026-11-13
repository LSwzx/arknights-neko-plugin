"""陪玩决策层（本轮只读）。

职责边界
--------
* **只做决策，不做设备动作**：:meth:`CompanionLogic.decide` 是纯函数级别的
  同步方法（除 ``knowledge`` 查询外无副作用），``service`` 拿它产出的
  :class:`AdviceBundle` 去组织文案。
* **写任务的拒绝点在这里，而且在 adapter 之前**：``execute_automation`` 先按
  ``kind`` 判白名单，再谈"能不能做"。v1 里连只读三类也**不真执行**——回一个
  ``not_implemented`` 的明确结果，绝不把请求转发给 adapter。
  这条顺序不能换：一旦"先转发再看结果"，写任务就已经进了调用链。
* ``execute_automation`` **不 raise**（除编程错误）：拒绝要能被调用方当成
  正常返回值处理，否则"用户在聊天里说了句 Fight"会变成一条异常日志而不是一句人话。

与 ``docs/01-architecture.md`` §3.6 的差异（已回写文档）
--------------------------------------------------------
文档里 ``AutomationRequest`` 只是文字描述、``execute_automation(plan: object)`` 恒定
``NotImplementedError``。按任务书要求，这里升级为**结构化请求 + 结构化结果**：
只有这样才能断言"写任务在进 adapter 之前就被拒了"，而不是靠"抛了个异常"反推。
（拒绝**不 raise**：用户/模型说了句 ``Fight`` 是正常输入，要回一句人话。）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .knowledge import KnowledgeBase, StageAdvice, default_knowledge

__all__ = [
    "PlayerContext",
    "AdviceBundle",
    "CompanionLogic",
    "AutomationRequest",
    "AutomationOutcome",
    "AUTOMATION_READONLY_KINDS",
    "execute_automation",
]


AUTOMATION_READONLY_KINDS: Final[frozenset[str]] = frozenset({
    "screencap", "operbox", "depot",
})
"""v1 **唯一**接受的自动化种类。

``screencap`` 对应 MAA 的只读截图；``operbox`` / ``depot`` 对应干员 box 与仓库的
**识别**（MAA 内部会打开对应页面浏览，不触发战斗）。

**这份名单是"允许被考虑"的白名单，不是"会被执行"的名单**：v1 里三类都不真跑
（见 :func:`execute_automation`）。名单之外一律默认拒绝——默认拒绝，而不是默认放行，
是因为放行的代价是"代打"，拒绝的代价只是"用户多问一句"。
"""

WRITE_TASK_KINDS: Final[frozenset[str]] = frozenset({
    # 显式列出，只为让拒绝日志能说清"你点的是写任务"，不用来判白名单。
    # 注意：Depot / OperBox **不在**这里 —— 它们是只读识别（见上面的白名单）。
    "Fight", "Infrast", "Recruit", "Mall", "Award", "Roguelike", "Copilot",
    "StartUp", "CloseDown",
})
"""写任务的**示例清单**（MAA 的 task type 词表）。

它**不参与判定**：判定是"不在 ``AUTOMATION_READONLY_KINDS`` 里就拒绝"。
存在意义只有两个：① 拒绝时日志能区分"用户点了代打"与"用户拼错了一个只读种类"；
② 让读代码的人一眼看到边界在哪。**不要**改成"在写清单里才拒绝"。"""


# ---------------------------------------------------------------------------
# 决策输入 / 输出
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlayerContext:
    """一次决策的全部输入。**只读**，每个字段都有安全的空值。"""

    stage: str = ""
    concern: str = ""
    roster: tuple[str, ...] = ()
    hope: int | None = None
    layer: int | None = None
    ocr_text: str = ""


@dataclass(frozen=True, slots=True)
class AdviceBundle:
    """决策输出：一句总结 + 若干条提示 + 建议下一个调的 entry。"""

    summary: str
    tips: tuple[str, ...] = ()
    suggested_entry: str | None = None


# ---------------------------------------------------------------------------
# 自动化请求 / 结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AutomationRequest:
    """一次自动化请求。

    ``kind`` 只允许 ``screencap`` / ``operbox`` / ``depot``（见
    :data:`AUTOMATION_READONLY_KINDS`）。校验**不在构造期抛错**：用户/模型传进来一个
    ``"Fight"`` 是**正常业务输入**，要回一句人话，而不是炸一个 ``ValueError``
    让 entry 变成 ``Err("内部错误")``。真正的拒绝发生在 :func:`execute_automation`。

    ``params`` 用 ``Mapping`` + ``field(default_factory=dict)``：不可变默认值，
    不会出现"两个请求共享同一个 dict"这种经典坑。
    """

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    """自动化请求的结果。**拒绝不是异常**，是一个可渲染的返回值。"""

    ok: bool
    kind: str
    rejected: bool
    reason: str
    """稳定原因码：``not_implemented`` / ``task_not_allowed`` / ``invalid_request``。"""
    error_code: str | None = None
    """稳定错误码字符串，与 ``maa.errors.ErrorCode`` 的取值同词表
    （``"guard.task_not_allowed"``）。

    **存字符串而不是异常对象**：§1 的模块边界规定 ``logic`` 不得 import ``maa.*`` ——
    决策层一旦认识引擎异常，"纯决策"就变成了"半个业务层"。需要异常对象的地方是
    service（它本来就 import ``maa.errors``），拿这个码去查同一张文案表即可。
    """
    detail: str = ""
    """给日志的一句话。**不进聊天**。"""

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好形态；``error`` 只出 ``code``，不出 message（含路径）。"""
        return {
            "ok": self.ok,
            "kind": self.kind,
            "rejected": self.rejected,
            "reason": self.reason,
            "error_code": self.error_code,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# 决策器
# ---------------------------------------------------------------------------
class CompanionLogic:
    """决策层。**本轮只读**：不发起任何设备动作。"""

    __slots__ = ("_knowledge",)

    def __init__(self, knowledge: KnowledgeBase | None = None) -> None:
        # 允许不传：默认走内置静态表。传参的意义是用例可以塞一份只含一条事实的表。
        self._knowledge = knowledge if knowledge is not None else default_knowledge()

    @property
    def knowledge(self) -> KnowledgeBase:
        """只读暴露知识表，供 service 的纯文本入口（肉鸽建议）复用。

        不加这个属性的话，service 就得自己再持一份 ``KnowledgeBase``——两份实例
        迟早不同步（一处加表、另一处忘了），而"知识表只有一个真相"是这张表存在的意义。
        """
        return self._knowledge

    def decide(self, ctx: PlayerContext) -> AdviceBundle:
        """把玩家上下文变成建议。**纯同步、无 I/O、无设备动作**。

        优先级（顺序即产品语义）：显式关卡 > 文本里抠出的关卡 > 无关卡。
        """
        if not isinstance(ctx, PlayerContext):  # 防御：调用方传 dict 是常见手误
            ctx = PlayerContext()

        advice: StageAdvice = self._knowledge.analyze_text(
            ctx.ocr_text or "", stage=ctx.stage or "", concern=ctx.concern or ""
        )

        tips: list[str] = list(advice.highlights)
        if advice.concern_reply:
            tips.append(advice.concern_reply)

        # 队伍：只做"报了没报"的区分，不评价强弱（那需要版本数据，本模块不持有）。
        if ctx.roster:
            tips.append(f"队伍里记下了 {len(ctx.roster)} 个干员喵。")

        stage = advice.stage
        if stage:
            summary = f"看的是 {stage} 这一关喵。" if advice.ocr_ok else f"暂且按 {stage} 来说喵。"
            suggested = "arknights_analyze_stage"
        else:
            summary = "还没确定是哪一关喵，先说说你卡在什么地方。"
            suggested = "arknights_capture"

        return AdviceBundle(summary=summary, tips=tuple(tips[:4]), suggested_entry=suggested)


# ---------------------------------------------------------------------------
# 自动化入口（v1 占位：只读三类也**不执行**）
# ---------------------------------------------------------------------------
async def execute_automation(req: AutomationRequest) -> AutomationOutcome:
    """v1 占位：**只**接受只读三类，其余一律拒绝；绝不调用任何写任务。

    三层判定，顺序不可换：

      1. 形状：``kind`` 不是非空字符串 → ``invalid_request``（连"是什么任务"都不知道）；
      2. 白名单：``kind.strip().lower()`` 不在 :data:`AUTOMATION_READONLY_KINDS`
         → ``task_not_allowed`` + ``error_code="guard.task_not_allowed"``（**在这里就返回**，
         函数体里没有任何 adapter 引用，所以"进 adapter 之前被拒"是结构上成立的，
         不是靠自觉）；
      3. 即使在前两层通过，本版本也返回 ``not_implemented`` —— 本轮的设计边界是
         "只读陪玩"，真正的只读任务执行链路（``append_readonly_task`` +
         结果回收）还没接，接了才是越界。

    **本函数不 raise**：拒绝是一个返回值，调用方据此出人话。
    """
    if not isinstance(req, AutomationRequest):
        return AutomationOutcome(
            ok=False,
            kind="",
            rejected=True,
            reason="invalid_request",
            detail=f"expected AutomationRequest, got {type(req).__name__}",
        )

    raw_kind = req.kind if isinstance(req.kind, str) else ""
    kind = raw_kind.strip()
    if not kind:
        return AutomationOutcome(
            ok=False, kind="", rejected=True, reason="invalid_request",
            detail="kind is empty",
        )

    normalized = kind.lower()
    if normalized not in AUTOMATION_READONLY_KINDS:
        return AutomationOutcome(
            ok=False,
            kind=kind,
            rejected=True,
            reason="task_not_allowed",
            error_code="guard.task_not_allowed",
            detail=(
                f"write/unknown task kind rejected before adapter: kind={kind!r} "
                f"known_write={kind in WRITE_TASK_KINDS}"
            ),
        )

    # 只读三类的合法请求：本版本仍不执行。
    return AutomationOutcome(
        ok=False,
        kind=normalized,
        rejected=False,
        reason="not_implemented",
        detail="v1 is read-only companion: automation execution is intentionally unwired",
    )
