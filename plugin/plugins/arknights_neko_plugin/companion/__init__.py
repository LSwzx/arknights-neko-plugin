"""陪玩业务层。

**薄包**：只做三件事 —— 导出对外的三个名字、说明 import 方向、不在这里 import 宿主。

为什么 ``__init__`` 里只 re-export 三个名字
------------------------------------------
``service`` 依赖 ``logic``、``logic`` 依赖 ``knowledge``；如果把 ``knowledge``
的静态表也在这里 re-export，调用方就会图省事 ``from companion import BASE_ROOMS``——
那条路径绕过了 ``CompanionLogic``，知识表就从"一处真相"变成"两处取用"。
所以对外只暴露三个真正会被 entry 用的名字：

* :class:`CompanionService` —— 唯一业务出口；
* :class:`AutomationRequest` —— 自动化请求的形状（v1 只读白名单）；
* :func:`execute_automation` —— 自动化入口（v1 占位，写任务在 adapter 之前就被拒）。

**本文件不 import 宿主 SDK**：``companion`` 必须能脱离宿主单测（只用标准库 + ``maa.errors``）。
"""

from __future__ import annotations

from .logic import AutomationRequest, execute_automation
from .service import CompanionService

__all__ = [
    "CompanionService",
    "AutomationRequest",
    "execute_automation",
]
