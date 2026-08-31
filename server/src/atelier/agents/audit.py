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
# 写在正文中间的 base64 也要拦：用户直接粘一个 data URL 进消息是完全可能的
_DATA_URL = re.compile(r"data:([\w.+/-]*);base64,([A-Za-z0-9+/=]+)")


def _asset_note(mime: str, encoded: str) -> str:
    """只留 MIME、字节数与摘要，让同一张图在不同记录里能对上号。"""
    try:
        raw = base64.b64decode(encoded, validate=False)
    except ValueError:
        raw = encoded.encode()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"[data URL omitted: mime={mime or 'unknown'}, bytes={len(raw)}, sha256={digest}]"


def _safe_value(value: Any) -> Any:
    """复制可序列化值，同时移除凭证和 data URL 正文。"""
    if isinstance(value, Mapping):
        return {
            str(key): "***" if str(key).lower() in _SECRET_KEYS else _safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_safe_value(item) for item in value]
    if isinstance(value, str) and ";base64," in value:
        return _DATA_URL.sub(lambda hit: _asset_note(hit.group(1), hit.group(2)), value)
    if isinstance(value, Path):
        return str(value)
    return value


def _code_block(content: str, language: str) -> str:
    """选择不会与正文中的反引号冲突的 Markdown fence。"""
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{content}\n{fence}\n"


def _text_of(part: Any) -> str | None:
    """取多模态片段里的纯文本；不是文本片段就返回空。"""
    if isinstance(part, str):
        return part
    if isinstance(part, Mapping) and part.get("type") in (None, "text"):
        text = part.get("text")
        return text if isinstance(text, str) else None
    return None


def _part_digest(index: int, part: Any) -> str:
    """非文本片段只留一行统计：图片、音频整段落盘会把审计文件撑爆。"""
    kind = str(part.get("type") or "unknown") if isinstance(part, Mapping) else type(part).__name__
    raw = json.dumps(part, ensure_ascii=False, default=str)
    line = f"- 片段 {index}：{kind}，原文 {len(raw)} 字（已省略）"
    # data URL 已经被 _safe_value 洗成摘要，抬到行上比让人去 JSON 里扬得快
    assets = re.findall(r"\[data URL omitted: ([^\]]*)\]", json.dumps(_safe_value(part)))
    if assets:
        line += "，" + "；".join(assets)
    return line + "\n"


def _content_body(content: Any) -> str:
    """文本原样呈现，其余模态只统计：审计要能看清说了什么，不是存一份请求副本。"""
    if isinstance(content, str):
        return _code_block(_safe_value(content), "text")
    if not isinstance(content, Sequence):
        return _code_block(json.dumps(_safe_value(content), ensure_ascii=False), "json")

    pieces: list[str] = []
    for index, part in enumerate(content, start=1):
        text = _text_of(part)
        if text is None:
            pieces.append(_part_digest(index, part))
        else:
            pieces.append(_code_block(_safe_value(text), "text"))
    return "\n".join(pieces)


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
        body = "".join(
            self._message_section(index, message) for index, message in enumerate(messages, start=1)
        )
        section = (
            f"\n## 调用 {self._calls}：{purpose}\n\n"
            f"- Provider：{candidate.label}\n"
            f"- Model：{candidate.model_id}\n\n"
            f"### Request\n{body}"
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

    def _message_section(self, index: int, message: Mapping[str, Any]) -> str:
        """一条消息一节：小标题报 role，正文原样放，读的时候不用先在脑子里解 JSON。"""
        rest = dict(message)
        role = str(rest.pop("role", "unknown"))
        section = f"\n#### {self._calls}.{index} {role}\n\n{_content_body(rest.pop('content', ''))}"
        if rest:
            extra = json.dumps(_safe_value(rest), ensure_ascii=False, indent=2)
            section += f"\n其他字段：\n\n{_code_block(extra, 'json')}"
        return section

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
