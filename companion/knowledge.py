"""明日方舟静态知识：术语表 / 基建干员技能简表 / 常见关卡代号。

设计边界（不可违反）
--------------------
1. **纯数据 + 纯函数查询**：零 I/O、零网络、零 async、**零第三方依赖**
   （只用 ``re`` / ``dataclasses`` / ``collections.abc`` 这几样标准库）。
   这条不只是洁癖：本模块被 ``companion.logic`` 与 ``companion.service`` 同时依赖，
   一旦它开始读文件/联网，"离线也能给出肉鸽建议"这条产品承诺就当场失效。
2. **不编造数值**（本文件的立身之本）。星级/职业这类低风险事实可以写；
   理智消耗、掉落表、基建产能加成、招募概率一律**不填**：
   写成 ``None`` / 空 tuple 并标 ``# TODO_VERIFY``，让"不知道"是一个**能说出来**的值。
   填一个看起来像真的数字，比留空危险得多 —— 用户会照着它做决策。
3. **``verified`` 字段**：一条事实的 ``verified`` 为 ``False`` 时，它只作为
   "结构占位"，任何面向用户的建议都**不得**把它当事实陈述。查询函数
   （:meth:`KnowledgeBase.base_operators_for` 等）默认只返回 ``verified=True`` 的条目。
4. 术语解释**只做概念性概括**，不写数值、不写版本相关内容（版本一改就全错）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "OperatorFact",
    "StageFact",
    "StageAdvice",
    "BaseSkillFact",
    "GlossaryTerm",
    "KnowledgeBase",
    "BASE_ROOMS",
]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OperatorFact:
    """一条干员事实。

    ``rarity`` 用 0 表示"未核实"（**不用 None 以外的哨兵**：0 不在合法星级 1–6 里，
    误用会立刻在渲染时暴露成一个明显不对的"稀有度 0"）。``roles`` 是**职业**（近卫/狙击/…），
    不是"定位标签"——后者版本一变就漂移。
    """

    name: str
    rarity: int = 0
    roles: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True, slots=True)
class StageFact:
    """一条关卡事实。

    ``sanity`` 为 ``None`` = **未核实**，绝不用 0 或估计值占位：
    0 会被渲染成"这关不花理智"，是一个具体且错误的断言。
    ``drops`` 同理，空 tuple = 未核实（不是"什么都不掉"）。
    """

    code: str
    chapter: str = ""
    sanity: int | None = None       # TODO_VERIFY：具体理智消耗未逐关核实
    drops: tuple[str, ...] = ()     # TODO_VERIFY：掉落表未核实，留空
    note: str = ""
    verified: bool = False


@dataclass(frozen=True, slots=True)
class StageAdvice:
    """``analyze_text()`` 的返回值：**只含结论与提示**，不含设备动作。"""

    stage: str
    ocr_ok: bool
    highlights: tuple[str, ...] = ()
    concern_reply: str = ""


@dataclass(frozen=True, slots=True)
class BaseSkillFact:
    """基建技能简表的一行：**只说"这个干员的基建技能在哪个房间有用"**。

    ``bonus`` 留空是刻意的：产能加成是具体数值（+25% / +30% 之类），
    未核实就写进去等于编造。``verified=False`` 的行不会被
    :meth:`KnowledgeBase.base_operators_for` 返回。
    """

    operator: str
    rooms: tuple[str, ...] = ()
    bonus: str = ""          # TODO_VERIFY：具体加成数值未核实
    verified: bool = False


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    """术语表一行。``aliases`` 供 ``extract_terms()`` 做同义命中。"""

    term: str
    meaning: str
    aliases: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 静态数据
# ---------------------------------------------------------------------------
BASE_ROOMS: Final[tuple[str, ...]] = (
    "控制中枢",
    "制造站",
    "贸易站",
    "发电站",
    "宿舍",
    "办公室",
    "会客室",
    "加工站",
    "训练室",
)
"""基建房间的**全集**（顺序按游戏内排版习惯，不承载语义）。"""


_OPERATORS: Final[tuple[OperatorFact, ...]] = (
    # --- 低风险事实：名字 / 星级 / 职业。这三样是"看一眼卡面就知道"的，且极少变。
    OperatorFact("阿米娅", rarity=5, roles=("术师",), verified=True),
    OperatorFact("凯尔希", rarity=6, roles=("医疗",), verified=True),
    OperatorFact("能天使", rarity=6, roles=("狙击",), verified=True),
    OperatorFact("银灰", rarity=6, roles=("近卫",), verified=True),
    OperatorFact("艾雅法拉", rarity=6, roles=("术师",), verified=True),
    OperatorFact("塞雷娅", rarity=6, roles=("重装",), verified=True),
    OperatorFact("夜莺", rarity=6, roles=("医疗",), verified=True),
    OperatorFact("星熊", rarity=6, roles=("重装",), verified=True),
    OperatorFact("德克萨斯", rarity=5, roles=("先锋",), verified=True),
    OperatorFact("白面鸮", rarity=5, roles=("医疗",), verified=True),
    OperatorFact("桃金娘", rarity=4, roles=("先锋",), verified=True),
    OperatorFact("陈", rarity=6, roles=("近卫",), verified=True),
    OperatorFact("史尔特尔", rarity=6, roles=("近卫",), verified=True),
    OperatorFact("泥岩", rarity=6, roles=("重装",), verified=True),
)


_STAGES: Final[tuple[StageFact, ...]] = (
    # 章节归属是低风险事实（代号前缀就是章号）；**理智与掉落全部留空**。
    StageFact("1-7", chapter="1"),
    StageFact("1-12", chapter="1"),
    StageFact("2-5", chapter="2"),
    StageFact("3-8", chapter="3"),
    StageFact("4-4", chapter="4"),
    StageFact("5-3", chapter="5"),
    StageFact("6-5", chapter="6"),
    StageFact("7-4", chapter="7"),
    StageFact("9-6", chapter="9"),
    # 资源关（代号前缀不是章号，单独给 chapter 字样，避免被当成主线章节）。
    StageFact("CE-5", chapter="资源关"),
    StageFact("CE-6", chapter="资源关"),
    StageFact("LS-5", chapter="资源关"),
    StageFact("LS-6", chapter="资源关"),
    StageFact("AP-5", chapter="资源关"),
    StageFact("SK-5", chapter="资源关"),
    StageFact("CA-5", chapter="资源关"),
    StageFact("PR-A-2", chapter="芯片关"),
    StageFact("PR-B-2", chapter="芯片关"),
    StageFact("PR-C-2", chapter="芯片关"),
    StageFact("PR-D-2", chapter="芯片关"),
)


_BASE_SKILLS: Final[tuple[BaseSkillFact, ...]] = (
    # 只放"我有把握"的两条；其余**故意空缺**并在 base_operators_for 里说明原因。
    # 宁可少给，也不要给错 —— 基建排班排错一次要用户花理智去改。
    BaseSkillFact("能天使", rooms=("贸易站",), verified=True),
    BaseSkillFact("德克萨斯", rooms=("贸易站",), verified=True),
    # TODO_VERIFY：以下房间的推荐干员与技能加成未逐条核实，留空占位（不填名字=不给错名字）。
    #   制造站 / 发电站 / 宿舍 / 办公室 / 会客室 / 加工站 / 控制中枢
)


_GLOSSARY: Final[tuple[GlossaryTerm, ...]] = (
    GlossaryTerm("理智", "打关卡要花的行动资源，会随时间自然恢复，也能用药剂补。", ("sanity",)),
    GlossaryTerm("龙门币", "通用货币，升级、精英化、基建都要用。", ("LMD",)),
    GlossaryTerm("源石", "付费货币，可用于兑换抽卡资源或补给。", ("Originium", "至纯源石")),
    GlossaryTerm("合成玉", "抽卡货币，可由源石兑换或从日常产出。", ("玉",)),
    GlossaryTerm("干员", "可编入队伍的作战单位，分不同职业与星级。", ("operator", "角色")),
    GlossaryTerm("精英化", "提升干员等级上限、解锁技能与立绘的阶段。", ("精一", "精二", "E1", "E2")),
    GlossaryTerm("潜能", "重复获得同一干员带来的强化，影响少量属性与费用。", ("潜",)),
    GlossaryTerm("技能专精", "把干员技能练到更高专精等级，改变技能的强度或形态。", ("专精", "mastery")),
    GlossaryTerm("模组", "给干员追加属性与特性的养成系统。", ("module",)),
    GlossaryTerm("基建", "罗德岛内的生产与后勤系统（制造站、贸易站、宿舍等）。", ("base",)),
    GlossaryTerm("公开招募", "用招募许可随机获得干员的系统，可出现资深/高级资深标签。", ("公招",)),
    GlossaryTerm("剿灭作战", "按周计奖励的常驻大规模作战。", ("剿灭",)),
    GlossaryTerm("集成战略", "俗称肉鸽的关卡内构筑模式，每局独立成长。", ("肉鸽", "roguelike", "IS")),
    GlossaryTerm("危机合约", "周期性高难挑战，靠词条叠加难度。", ("合约", "CC")),
    GlossaryTerm("悖论模拟", "干员专属的固定关卡，通关给专属奖励。", ("悖论",)),
    GlossaryTerm("凭证", "各类商店使用的兑换券，分不同等级。", ("绿票", "黄票")),
)


# ---------------------------------------------------------------------------
# 关卡代号识别
# ---------------------------------------------------------------------------
# 主线：1-7 / 12-20 / EX-8 之类。前导断言用"非数字"而不用 \b：
# OCR 出来的文本经常是 "关卡1-7" 这种中日韩字符紧贴数字的形态，\b 在那里不成立。
_MAIN_STAGE: Final = re.compile(r"(?<!\d)(\d{1,2})\s*[-–—]\s*(\d{1,2})(?!\d)")
# 资源关：CE-5 / LS-6 / AP-5 / SK-5 / CA-5；芯片关：PR-A-2
_RES_STAGE: Final = re.compile(
    r"(?<![A-Za-z])(CE|LS|AP|SK|CA)\s*[-–—]\s*(\d{1,2})(?!\d)", re.IGNORECASE
)
_CHIP_STAGE: Final = re.compile(
    r"(?<![A-Za-z])(PR)\s*[-–—]\s*([A-D])\s*[-–—]\s*(\d{1,2})(?!\d)", re.IGNORECASE
)

_SETTLEMENT_KEYWORDS: Final[tuple[str, ...]] = (
    "行动结束",
    "作战结束",
    "任务完成",
    "作战完成",
    "已获得",
    "获得报酬",
    "理智",
)


class KnowledgeBase:
    """冻结知识表 + 纯查询函数。全部同步，可直接单测，**不需要事件循环**。

    构造参数全部可选，且默认值来自本模块的静态表：这让"造一个只含一条事实的 KB"
    在用例里是一行代码，不必 reload 模块、也不必 monkeypatch 全局。
    """

    __slots__ = ("_operators", "_stages", "_base_skills", "_glossary", "_high_value")

    def __init__(
        self,
        *,
        operators: Mapping[str, OperatorFact] | None = None,
        stages: Mapping[str, StageFact] | None = None,
        base_skills: Sequence[BaseSkillFact] | None = None,
        glossary: Sequence[GlossaryTerm] | None = None,
        high_value_operators: Iterable[str] = (),
    ) -> None:
        if operators is None:
            operators = {fact.name: fact for fact in _OPERATORS}
        if stages is None:
            stages = {fact.code.upper(): fact for fact in _STAGES}
        self._operators: dict[str, OperatorFact] = dict(operators)
        self._stages: dict[str, StageFact] = dict(stages)
        self._base_skills: tuple[BaseSkillFact, ...] = tuple(
            _BASE_SKILLS if base_skills is None else base_skills
        )
        self._glossary: tuple[GlossaryTerm, ...] = tuple(
            _GLOSSARY if glossary is None else glossary
        )
        self._high_value: frozenset[str] = frozenset(high_value_operators)

    @classmethod
    def load_default(cls) -> "KnowledgeBase":
        """内置表的实例。**不读磁盘**——"load" 只是命名沿用，没有 I/O。"""
        return cls()

    # --- 单条查询 ---
    def stage(self, code: str) -> StageFact | None:
        """按代号查关卡。大小写、前后空格都容忍（OCR 输出常见 ``" 1-7 "``）。"""
        key = (code or "").strip().upper()
        if not key:
            return None
        fact = self._stages.get(key)
        if fact is not None:
            return fact
        # 再试一次"把分隔符归一化"的形态：OCR 常把 '-' 认成 '–' 或空格。
        return self._stages.get(re.sub(r"\s*[-–—]\s*", "-", key))

    def operator(self, name: str) -> OperatorFact | None:
        key = (name or "").strip()
        return self._operators.get(key) if key else None

    def term(self, name: str) -> GlossaryTerm | None:
        """按术语名或别名查。别名匹配**不区分大小写**（``sanity`` / ``Sanity``）。"""
        key = (name or "").strip()
        if not key:
            return None
        lowered = key.lower()
        for item in self._glossary:
            if item.term == key or any(alias.lower() == lowered for alias in item.aliases):
                return item
        return None

    def terms(self) -> tuple[GlossaryTerm, ...]:
        return self._glossary

    def operators(self) -> tuple[OperatorFact, ...]:
        return tuple(self._operators.values())

    def stages(self) -> tuple[StageFact, ...]:
        return tuple(self._stages.values())

    # --- 文本识别（纯字符串，无 OCR、无正则回溯风险） ---
    def extract_stage(self, ocr_text: str) -> str | None:
        """从 OCR 文本里抠出关卡代号；抠不到返回 ``None``（**不猜**）。

        识别顺序：芯片关（三段式，最长）→ 资源关 → 主线。
        顺序不可换：``PR-A-2`` 先被主线正则吃掉的话会变成 ``A-2``。
        """
        if not ocr_text:
            return None
        text = str(ocr_text)
        match = _CHIP_STAGE.search(text)
        if match:
            return f"{match.group(1).upper()}-{match.group(2).upper()}-{match.group(3)}"
        match = _RES_STAGE.search(text)
        if match:
            return f"{match.group(1).upper()}-{match.group(2)}"
        match = _MAIN_STAGE.search(text)
        if match:
            return f"{int(match.group(1))}-{int(match.group(2))}"
        return None

    def looks_like_settlement(self, ocr_text: str) -> bool:
        """这段文本像不像"关卡结算页"。

        判据是关键词命中，**不是**因果推断：命中只说明"值得再截一张确认"，
        不说明"这局打完了"。调用方不得据此做任何自动动作。
        """
        if not ocr_text:
            return False
        text = str(ocr_text)
        return any(keyword in text for keyword in _SETTLEMENT_KEYWORDS)

    # --- 基建简表 ---
    def base_operators_for(self, room: str) -> tuple[BaseSkillFact, ...]:
        """某房间"已知有效"的干员。**只返回 ``verified=True`` 的条目**。

        未核实的房间返回空 tuple —— 空是对的：它表达"我这条知识没有覆盖到"，
        而给一个错名字会让用户排错班、白花理智。
        """
        wanted = (room or "").strip()
        if not wanted:
            return ()
        return tuple(
            fact
            for fact in self._base_skills
            if fact.verified and wanted in fact.rooms
        )

    def base_rooms(self) -> tuple[str, ...]:
        return BASE_ROOMS

    # --- 肉鸽建议（启发式，最多 3 条，**不给数值**） ---
    def roguelike_advice(
        self,
        *,
        theme: str = "",
        roster: Sequence[str] = (),
        hope: int | None = None,
        layer: int | None = None,
    ) -> tuple[str, ...]:
        """集成战略的启发式建议。

        ``theme`` / ``hope`` / ``layer`` 都是**可选输入**：缺了就不给对应那条建议，
        而不是拿默认值编一条。所有条目都是"怎么想"，不是"多少个"——
        数值阈值随主题与版本变化，写死就是编造。
        """
        tips: list[str] = []
        theme_text = (theme or "").strip()
        if theme_text:
            tips.append(f"这一局是「{theme_text}」主题喵，构筑和干员取舍按这个主题的规则来。")
        else:
            tips.append("没告诉我是哪个主题喵，先按常规开局走，把稳的输出位留住。")

        roster_names = tuple(name for name in (roster or ()) if str(name).strip())
        if roster_names:
            # 只做"有没有重复职业"这种纯计数的观察，不评价具体干员强弱（那需要版本数据）。
            tip = f"队伍里已经有 {len(roster_names)} 个干员喵，先看有没有缺口，再决定招谁。"
            tips.append(tip)
        else:
            tips.append("干员名单还空着喵，报给我你现在手上的，我按缺口帮你挑。")

        if hope is not None and hope <= 0:
            tips.append("希望见底了喵，接下来优先保全阵容，别再随便招人。")
        elif layer is not None and layer >= 4:
            tips.append("层数往上走了喵，出去之前先把治疗和稳定输出补齐。")
        else:
            tips.append("先攒资源、少冒险喵，肉鸽里活着比多打一格重要。")

        return tuple(tips[:3])

    # --- 综合分析（无设备依赖） ---
    def analyze_text(
        self,
        text: str = "",
        *,
        stage: str = "",
        concern: str = "",
    ) -> StageAdvice:
        """把"一段文本 + 用户给的关卡/顾虑"变成结构化建议。**纯函数**。

        ``stage`` 显式给了就优先用它；否则尝试从 ``text`` 里抠。
        抠不到时 ``stage`` 留空、``ocr_ok=False`` —— 这是事实，不是失败。
        """
        resolved = (stage or "").strip()
        if not resolved:
            resolved = self.extract_stage(text) or ""

        highlights: list[str] = []
        fact = self.stage(resolved) if resolved else None
        if fact is not None:
            chapter = fact.chapter or "未知章节"
            highlights.append(f"{fact.code}：{chapter}的关卡喵。")
            if fact.sanity is None:
                # 说"我不知道"而不是给个数字。这条文案本身就是"不编造"的落地形态。
                highlights.append("这关具体要多少理智我还没核对过喵，以游戏里显示的为准。")
            if not fact.drops:
                highlights.append("掉落表我这边还没核实喵，别照我说的刷。")
        elif resolved:
            highlights.append(f"{resolved} 这个代号我这边没有记录喵，可能是新关卡或我看错了。")
        else:
            highlights.append("还没认出是哪一关喵，给我代号（比如 1-7）我就能说得更准。")

        concern_reply = self._reply_concern(concern)
        return StageAdvice(
            stage=resolved,
            ocr_ok=bool(resolved),
            highlights=tuple(highlights),
            concern_reply=concern_reply,
        )

    def _reply_concern(self, concern: str) -> str:
        """把用户的顾虑回一句**不承诺动作**的话。

        ``concern`` 是玩家可控文本（可能带指令），这里**只按关键词归类**，
        原文一律不回显 —— 回显等于把上游文本原样搬进模型上下文。
        """
        text = (concern or "").strip()
        if not text:
            return ""
        if any(keyword in text for keyword in ("打不过", "过不去", "卡住", "翻车")):
            return "卡关的话，先看是哪一类问题：漏怪就补输出、撑不住就补治疗或重装喵。"
        if any(keyword in text for keyword in ("刷", "素材", "掉落", "理智")):
            return "刷素材我可以帮你对代号，但掉落率我这边没有核实过的数据喵。"
        if any(keyword in text for keyword in ("干员", "练谁", "精二", "专精")):
            return "练谁这件事我按职业缺口给方向，具体强度榜我不敢乱排喵。"
        return "收到喵，我先按你给的关卡和队伍缺口来说。"


def default_knowledge() -> KnowledgeBase:
    """模块级别名，便于 ``CompanionLogic()`` 在不传参时拿到内置表。"""
    return KnowledgeBase.load_default()
