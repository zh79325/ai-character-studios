"""跨层共用的领域异常。

放在包根而不是某个模块里：projects（磁盘层）与 provider_ops（API 层）都要抛「不存在」
「冲突」，谁都不该 import 对方，磁盘层更不该 import api。HTTP 状态码的映射统一在
`main.py` 的异常处理器里，抛的人不关心自己会变成几号响应。
"""

from __future__ import annotations


class NotFound(LookupError):
    """要读写的对象不存在。→ 404"""


class Conflict(ValueError):
    """与现有数据冲突：重复的 code、已存在的目录、别人已经改过的版本。→ 409"""
