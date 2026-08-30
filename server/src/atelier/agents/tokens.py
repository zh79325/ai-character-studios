"""token 估算。

不装 tokenizer：候选横跨百炼、方舟、OpenAI 兼容端点，各家分词表不同，装一个只会给出
「精确的错数」。上下文预算要的是「还能塞多少」，估多了浪费一点窗口，估少了才会截断，
所以这里刻意往高估一侧偏。

真实消耗一律以供应商响应里的 usage 为准（写进 `messages.token_count` 与 `route_logs`），
本模块只用于调用前的编排决策。
"""

from __future__ import annotations

import math

CJK_RANGES = (
    (0x3000, 0x303F),  # 中日韩标点
    (0x3040, 0x30FF),  # 假名
    (0x4E00, 0x9FFF),  # 汉字
    (0xF900, 0xFAFF),  # 兼容汉字
    (0xFF00, 0xFFEF),  # 全角字符
)

LATIN_CHARS_PER_TOKEN = 3.5
"""拉丁文本每 token 约 4 字符，取 3.5 是故意高估，宁可少塞一点也别截断。"""

MESSAGE_OVERHEAD = 4
"""每条消息的 role 与分隔符开销，各家格式不同，按 4 个 token 记。"""


def _is_cjk(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in CJK_RANGES)


def estimate_text(text: str) -> int:
    """一段文本大约多少 token。中日韩字符按一字一 token，其余按字符数折算。"""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    rest = len(text) - cjk
    return cjk + math.ceil(rest / LATIN_CHARS_PER_TOKEN)


def estimate_message(content: str) -> int:
    """一条消息占的预算，含格式开销。"""
    return estimate_text(content) + MESSAGE_OVERHEAD
