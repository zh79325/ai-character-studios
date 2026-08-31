"""会话 LLM 调用审计：请求先落盘，结果回到同一 Markdown 后追加。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from atelier.providers.base import Candidate
    from atelier.providers.text_chat import ChatReply

_SECRET_KEYS = frozenset({"api_key", "authorization", "access_token", "secret", "token"})


def _safe_value(value: Any) -> Any:
    """复制可序列化值，同时移除凭证和 data URL 正文。"""
    if isinstance(value, Mapping):
        return {
            str(key): "***" if str(key).lower() in _SECRET_KEYS else _safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_safe_value(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        header, encoded = value.split(",", 1)
        mime = header.removeprefix("data:").removesuffix(";base64")
        try:
            raw = base64.b64decode(encoded, validate=False)
        except ValueError:
            raw = encoded.encode()
        digest = hashlib.sha256(raw).hexdigest()
        return f"[data URL omitted: mime={mime}, bytes={len(raw)}, sha256={digest}]"
    if isinstance(value, Path):
        return str(value)
    return value


def _code_block(content: str, language: str) -> str:
    """选择不会与正文中的反引号冲突的 Markdown fence。"""
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{content}\n{fence}\n"


@dataclass(slots=True)
class TurnAudit:
    """一轮会话的审计文件；一轮内的折叠与主回答按调用顺序追加。"""

    path: Path
    conversation_id: str
    turn_no: int
    target: str
    agent_code: str
    started_at: datetime
    _calls: int = field(default=0, init=False)

    @classmethod
    def create(
        cls,
        target_dir: Path,
        *,
        conversation_id: str,
        turn_no: int,
        target: str,
        agent_code: str,
        now: datetime | None = None,
    ) -> TurnAudit:
        started = (now or datetime.now().astimezone()).astimezone()
        name = f"{started:%Y%m%d-%H}-turn-{turn_no}.md"
        return cls(
            path=target_dir / "tmp" / "conversation" / name,
            conversation_id=conversation_id,
            turn_no=turn_no,
            target=target,
            agent_code=agent_code,
            started_at=started,
        )

    def write_request(
        self,
        purpose: str,
        candidate: Candidate,
        messages: Sequence[Mapping[str, Any]],
    ) -> None:
        """在调用 LLM 前写入实际消息；第一次写同时创建文件头。"""
        self._calls += 1
        payload = json.dumps(_safe_value(messages), ensure_ascii=False, indent=2)
        section = (
            f"\n## 调用 {self._calls}：{purpose}\n\n"
            f"- Provider：{candidate.label}\n"
            f"- Model：{candidate.model_id}\n\n"
            "### Request\n\n"
            f"{_code_block(payload, 'json')}"
        )
        if self._calls == 1:
            header = (
                "# LLM 对话审计\n\n"
                f"- 会话：{self.conversation_id}\n"
                f"- 轮次：{self.turn_no}\n"
                f"- 时间：{self.started_at.isoformat()}\n"
                f"- 目标：{self.target}\n"
                f"- Agent：{self.agent_code}\n"
            )
            self._write(header + section, exclusive=True)
        else:
            self._write(section)

    def write_response(self, reply: ChatReply) -> None:
        """模型完整返回后追加正文与用量。"""
        usage = (
            f"- Prompt tokens：{reply.prompt_tokens}\n"
            f"- Completion tokens：{reply.completion_tokens}\n"
            f"- Total tokens：{reply.total_tokens}\n"
            f"- Latency：{reply.latency_ms} ms\n"
            f"- Finish reason：{reply.finish_reason}\n"
        )
        self._write(f"\n### Response\n\n{_code_block(reply.content, 'markdown')}\n{usage}")

    def write_error(self, error: Exception, partial_response: str = "") -> None:
        """调用失败或被中断时追加错误；已有流式片段一并保留。"""
        content = "\n### Error\n\n" + _code_block(str(error), "text")
        if partial_response:
            content += "\n### Partial Response\n\n" + _code_block(partial_response, "markdown")
        self._write(content)

    def _write(self, content: str, *, exclusive: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "x" if exclusive else "a"
        with self.path.open(mode, encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
