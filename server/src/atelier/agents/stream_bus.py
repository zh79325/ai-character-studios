"""会话流的进程内广播。

后端由 Electron 拉起，只有一个进程，所以流式增量不必落库中转：POST 那一路边收边发布，
SSE 那一路按游标取。落库反而更糟——一次回答几百个增量，写库写日志都是白花的 I/O，而
这些碎片的价值只在「正在生成」这几秒，回答完整后原文已经在 `messages` 里了。

代价是重启即失效：客户端重连时拿不到旧增量。这是可接受的，因为完整回答从库里读得到，
前端只需在流断开后刷一次消息列表。

每个会话一段环形缓冲，超出上限丢最老的——保住内存上限，不因为一个没人看的会话把进程
撑大。发布与读取都加锁：发布方是跑对话的工作线程，读取方是 SSE 的事件循环。

缓冲里只留当下这一轮：开工第一步先 `reset`。让上一轮的东西留着，新订上来的那条流会把
上一轮重放一遍，而末尾那条 `turn` 一到就把流收了——这一轮的字一个也出不来，前端只能
干转圈到 POST 返回。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

MAX_EVENTS = 500
"""单个会话保留的增量条数。够覆盖一次长回答，看不完就靠回答结束后的原文。"""

DELTA = "delta"
TURN = "turn"
ERROR = "error"
COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """一条待推的事件。seq 在单个会话内自增，前端用它做断线续传的游标。"""

    seq: int
    event: str
    data: Any


class ConversationBus:
    """会话 id → 事件缓冲。"""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._lock = threading.Lock()
        self._buffers: dict[str, deque[StreamEvent]] = {}
        self._seq: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._max_events = max_events

    def publish(self, conversation_id: str, event: str, data: Any = None) -> StreamEvent:
        with self._lock:
            seq = self._seq.get(conversation_id, 0) + 1
            self._seq[conversation_id] = seq
            buffer = self._buffers.setdefault(conversation_id, deque(maxlen=self._max_events))
            item = StreamEvent(seq=seq, event=event, data=data)
            buffer.append(item)
            return item

    def since(self, conversation_id: str, after_seq: int) -> list[StreamEvent]:
        """取游标之后的事件。缓冲已滚掉的那些取不回来，只能少不能错。"""
        with self._lock:
            buffer = self._buffers.get(conversation_id)
            return [e for e in buffer if e.seq > after_seq] if buffer else []

    def latest_seq(self, conversation_id: str) -> int:
        with self._lock:
            return self._seq.get(conversation_id, 0)

    def reset(self, conversation_id: str) -> None:
        """清掉上一轮，序号接着数。

        序号不归零：`Last-Event-ID` 拿的就是它，归零后重连的客户端会拿着一个比当下大的
        游标进来，这一轮的增量就全被当成看过的跳掉了。
        """
        with self._lock:
            self._buffers.pop(conversation_id, None)

    def drop(self, conversation_id: str) -> None:
        """会话结束（沉淀或丢弃）后清掉缓冲。"""
        with self._lock:
            self._buffers.pop(conversation_id, None)
            self._seq.pop(conversation_id, None)
            self._cancelled.discard(conversation_id)

    def request_cancel(self, conversation_id: str) -> None:
        """掐掉正在跑的那一轮。

        模型那边没有「取消」接口，能停的只有我们自己这头：标一下，下一段增量到的时候发布那里抛
        出去，整条 HTTP 流跟着关，剩下的字就不再生了。没开流的那一轮掐不断，只能等它自己回完。
        """
        with self._lock:
            self._cancelled.add(conversation_id)

    def cancel_requested(self, conversation_id: str) -> bool:
        with self._lock:
            return conversation_id in self._cancelled

    def clear_cancel(self, conversation_id: str) -> None:
        """清掉中断标记。下一轮开工前必须清，否则上一轮的决定会把新那一轮一进门就掐了。"""
        with self._lock:
            self._cancelled.discard(conversation_id)

    def clear(self) -> None:
        with self._lock:
            self._buffers.clear()
            self._seq.clear()
            self._cancelled.clear()


BUS = ConversationBus()
"""进程级单例。测试里用 `BUS.clear()` 隔离，不必替换实例。"""
