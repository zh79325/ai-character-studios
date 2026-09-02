"""统一解析所有 Agent 每轮末尾的 Action JSON 契约。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ACTION_START = "<-------- ACTION-START------->"
ACTION_END = "<-------- ACTION-END------->"


class ActionType(StrEnum):
    ASK_USER = "ask_user"
    HANDOFF = "handoff"
    DONE = "done"
    BLOCKED = "blocked"


class AgentCode(StrEnum):
    STUDIO_DIRECTOR = "studio_director"
    GAME_DESIGNER = "game_designer"
    SPEC_WRITER = "spec_writer"
    SPEC_REVIEWER = "spec_reviewer"
    PROMPT_SMITH = "prompt_smith"
    IMAGE_T2I = "image_t2i"
    IMAGE_I2I = "image_i2i"
    VISION_REVIEWER = "vision_reviewer"
    MODEL3D = "model3d"
    VIDEO_GEN = "video_gen"


ACTION_TYPES = tuple(item.value for item in ActionType)
AGENT_CODES = tuple(item.value for item in AgentCode)
ACTION_PAYLOAD_KEYS = frozenset(
    {"choices", "progress", "drafts", "memories", "naming", "asset_specs", "verdict", "result"}
)
MEMORY_KINDS = ("preference", "taboo", "fact")
VERDICTS = ("APPROVE", "CONCERNS", "REJECT")
SPEC_CHECK = "SPEC-CHECK"
VIEW_CHECK = "VIEW-CHECK"
MIN_CHOICE_OPTIONS = 2
MAX_CHOICE_GROUPS = 4

_ACTION_RE = re.compile(
    rf"^[ \t]*{re.escape(ACTION_START)}[ \t]*\r?\n(?P<body>.*?)\r?\n"
    rf"^[ \t]*{re.escape(ACTION_END)}[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_CODE_OK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SIZE_RE = re.compile(r"(?P<width>\d{2,5})\s*[x×*]\s*(?P<height>\d{2,5})", re.I)
_VIEW_BACKGROUND_RE = re.compile(r"^#(?P<hex>[0-9a-f]{6})(?:\s*[（(][^）)]{1,30}[）)])?$", re.I)
_SENTENCE_BREAK_RE = re.compile(r"[。！？!?](?=\s*\S)")


class ProtocolError(ValueError):
    """Agent 输出不符合统一 Action JSON 契约。"""


class VerdictError(ProtocolError):
    """Action payload 中的裁决不符合约定。"""


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    action: ActionType
    target_agent: AgentCode | None
    reason: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class DraftBlock:
    target_path: str
    content: str


@dataclass(frozen=True, slots=True)
class MemoryItem:
    kind: str
    content: str
    scope: str = "project"


@dataclass(frozen=True, slots=True)
class Progress:
    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_step: str | None = None

    def is_empty(self) -> bool:
        return not (self.decisions or self.open_questions or self.next_step)


@dataclass(frozen=True, slots=True)
class NamingOption:
    name: str
    code: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ChoiceGroup:
    item: str
    options: tuple[str, ...]
    recommended: tuple[str, ...] = ()
    multiple: bool = False


@dataclass(frozen=True, slots=True)
class TurnOutput:
    text: str
    action: ActionEnvelope
    progress: Progress | None = None
    drafts: tuple[DraftBlock, ...] = ()
    memories: tuple[MemoryItem, ...] = ()
    naming: tuple[NamingOption, ...] = ()
    choices: tuple[ChoiceGroup, ...] = ()

    @property
    def has_draft(self) -> bool:
        return bool(self.drafts)


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """一张图的规格卡片。"""

    code: str
    name: str = ""
    category: str = ""
    width: int = 0
    height: int = 0
    image_format: str = ""
    file_name: str = ""
    description: str = ""
    anchors: str = ""
    constraints: tuple[str, ...] = ()
    view_background_color: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    text: str = ""

    def gaps(self) -> tuple[str, ...]:
        labels = {
            "category": "类别",
            "size": "尺寸",
            "file_name": "文件名",
            "view_background_color": "四视图背景色",
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
        }
        missing: list[str] = []
        if not self.category:
            missing.append(labels["category"])
        if self.width <= 0 or self.height <= 0:
            missing.append(labels["size"])
        if not self.file_name or "/" in self.file_name or "." not in self.file_name:
            missing.append(labels["file_name"])
        if self.category == "character" and not self.view_background_color:
            missing.append(labels["view_background_color"])
        if not self.prompt:
            missing.append(labels["prompt"])
        if not self.negative_prompt:
            missing.append(labels["negative_prompt"])
        return tuple(missing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "category": self.category,
            "size": f"{self.width}x{self.height}",
            "format": self.image_format,
            "file_name": self.file_name,
            "description": self.description,
            "anchors": self.anchors,
            "constraints": list(self.constraints),
            "view_background_color": self.view_background_color,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "card": self.text,
        }


@dataclass(frozen=True, slots=True)
class Constraint:
    item: str
    value: str


@dataclass(frozen=True, slots=True)
class Verdict:
    token: str
    decision: str
    sections: dict[str, tuple[str, ...]]
    constraints: tuple[Constraint, ...] = ()
    text: str = ""

    @property
    def approved(self) -> bool:
        return self.decision == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.decision == "REJECT"


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"Action JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProtocolError(f"Action JSON 包含非法常量：{value}")


def _strict_keys(value: dict[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        raise ProtocolError(f"{label} 包含未声明字段：{sorted(unknown)}")


def _required_keys(value: dict[str, Any], required: set[str], label: str) -> None:
    missing = required - set(value)
    if missing:
        raise ProtocolError(f"{label} 缺少字段：{sorted(missing)}")


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProtocolError(f"{label} 必须是字符串数组")
    return tuple(item.strip() for item in value if item.strip())


def _clean_code(raw: str) -> str:
    value = raw.strip().lower()
    return value if _CODE_OK_RE.fullmatch(value) else ""


def normalize_view_background_color(raw: str) -> str:
    value = raw.strip().strip("`").strip()
    match = _VIEW_BACKGROUND_RE.fullmatch(value)
    return f"#{match.group('hex').upper()}" if match else ""


def _size_of(raw: str) -> tuple[int, int]:
    match = _SIZE_RE.fullmatch(raw.strip())
    return (int(match.group("width")), int(match.group("height"))) if match else (0, 0)


def _payload_choices(payload: dict[str, object]) -> tuple[ChoiceGroup, ...]:
    raw = payload.get("choices", [])
    if not isinstance(raw, list):
        raise ProtocolError("payload.choices 必须是数组")
    if len(raw) > MAX_CHOICE_GROUPS:
        raise ProtocolError(f"payload.choices 最多 {MAX_CHOICE_GROUPS} 组")
    groups: list[ChoiceGroup] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ProtocolError(f"payload.choices[{index}] 必须是对象")
        keys = {"item", "options", "recommended", "multiple"}
        _strict_keys(item, keys, f"choices[{index}]")
        _required_keys(item, keys, f"choices[{index}]")
        name = item["item"]
        options = tuple(dict.fromkeys(_string_list(item["options"], f"choices[{index}].options")))
        recommended = tuple(
            dict.fromkeys(_string_list(item["recommended"], f"choices[{index}].recommended"))
        )
        multiple = item["multiple"]
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError(f"choices[{index}].item 必须是非空字符串")
        if len(options) < MIN_CHOICE_OPTIONS:
            raise ProtocolError(f"choices[{index}] 至少要有两个不同选项")
        if not isinstance(multiple, bool):
            raise ProtocolError(f"choices[{index}].multiple 必须是布尔值")
        if any(value not in options for value in recommended):
            raise ProtocolError(f"choices[{index}].recommended 必须来自 options")
        if not multiple and len(recommended) > 1:
            raise ProtocolError(f"choices[{index}] 是单选，recommended 最多一个")
        groups.append(ChoiceGroup(name.strip(), options, recommended, multiple))
    return tuple(groups)


def _payload_progress(payload: dict[str, object]) -> Progress | None:
    raw = payload.get("progress")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError("payload.progress 必须是对象")
    keys = {"decisions", "open_questions", "next_step"}
    _strict_keys(raw, keys, "payload.progress")
    _required_keys(raw, keys, "payload.progress")
    next_step = raw["next_step"]
    if next_step is not None and not isinstance(next_step, str):
        raise ProtocolError("payload.progress.next_step 必须是字符串或 null")
    return Progress(
        decisions=_string_list(raw["decisions"], "payload.progress.decisions"),
        open_questions=_string_list(raw["open_questions"], "payload.progress.open_questions"),
        next_step=next_step.strip() if isinstance(next_step, str) and next_step.strip() else None,
    )


def _payload_drafts(payload: dict[str, object]) -> tuple[DraftBlock, ...]:
    raw = payload.get("drafts", [])
    if not isinstance(raw, list):
        raise ProtocolError("payload.drafts 必须是数组")
    drafts: list[DraftBlock] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ProtocolError(f"payload.drafts[{index}] 必须是对象")
        keys = {"target_path", "content"}
        _strict_keys(item, keys, f"drafts[{index}]")
        _required_keys(item, keys, f"drafts[{index}]")
        path, content = item["target_path"], item["content"]
        if not isinstance(path, str) or not path.strip():
            raise ProtocolError(f"drafts[{index}].target_path 必须是非空字符串")
        if not isinstance(content, str) or not content.strip():
            raise ProtocolError(f"drafts[{index}].content 必须是非空字符串")
        drafts.append(DraftBlock(path.strip(), content.rstrip() + "\n"))
    return tuple(drafts)


def _payload_memories(payload: dict[str, object]) -> tuple[MemoryItem, ...]:
    raw = payload.get("memories", [])
    if not isinstance(raw, list):
        raise ProtocolError("payload.memories 必须是数组")
    memories: list[MemoryItem] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ProtocolError(f"payload.memories[{index}] 必须是对象")
        keys = {"scope", "kind", "content"}
        _strict_keys(item, keys, f"memories[{index}]")
        _required_keys(item, keys, f"memories[{index}]")
        scope, kind, content = item["scope"], item["kind"], item["content"]
        if scope not in {"project", "character"}:
            raise ProtocolError(f"memories[{index}].scope 非法")
        if kind not in MEMORY_KINDS:
            raise ProtocolError(f"memories[{index}].kind 非法")
        if not isinstance(content, str) or not content.strip():
            raise ProtocolError(f"memories[{index}].content 必须是非空字符串")
        memories.append(MemoryItem(str(kind), content.strip(), str(scope)))
    return tuple(memories)


def _payload_naming(payload: dict[str, object]) -> tuple[NamingOption, ...]:
    raw = payload.get("naming", [])
    if not isinstance(raw, list):
        raise ProtocolError("payload.naming 必须是数组")
    naming: list[NamingOption] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ProtocolError(f"payload.naming[{index}] 必须是对象")
        keys = {"name", "code", "reason"}
        _strict_keys(item, keys, f"naming[{index}]")
        _required_keys(item, keys, f"naming[{index}]")
        name, code, reason = item["name"], item["code"], item["reason"]
        if not all(isinstance(value, str) for value in (name, code, reason)) or not name.strip():
            raise ProtocolError(f"naming[{index}] 的 name/code/reason 必须是字符串且 name 非空")
        naming.append(NamingOption(name.strip(), _clean_code(code), reason.strip()))
    return tuple(naming)


def _validate_asset_specs(payload: dict[str, object]) -> None:
    raw = payload.get("asset_specs", [])
    if not isinstance(raw, list):
        raise ProtocolError("payload.asset_specs 必须是数组")
    keys = {
        "code",
        "name",
        "category",
        "size",
        "format",
        "file_name",
        "description",
        "anchors",
        "constraints",
        "view_background_color",
        "prompt",
        "negative_prompt",
    }
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ProtocolError(f"asset_specs[{index}] 必须是对象")
        _strict_keys(item, keys, f"asset_specs[{index}]")
        _required_keys(item, keys, f"asset_specs[{index}]")
        if not all(isinstance(item[key], str) for key in keys - {"constraints"}):
            raise ProtocolError(f"asset_specs[{index}] 的文本字段必须是字符串")
        if not item["code"].strip():
            raise ProtocolError(f"asset_specs[{index}].code 必须是非空字符串")
        _string_list(item["constraints"], f"asset_specs[{index}].constraints")


def _payload_verdict(
    payload: dict[str, object], text: str, token: str | None = None
) -> Verdict | None:
    raw = payload.get("verdict")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise VerdictError("payload.verdict 必须是对象")
    keys = {"token", "decision", "sections", "constraints"}
    _strict_keys(raw, keys, "payload.verdict")
    _required_keys(raw, keys, "payload.verdict")
    actual_token, decision = raw["token"], raw["decision"]
    if actual_token not in {SPEC_CHECK, VIEW_CHECK}:
        raise VerdictError("payload.verdict.token 非法")
    if token is not None and actual_token != token:
        raise VerdictError(f"期待 {token}，实际是 {actual_token}")
    if decision not in VERDICTS:
        raise VerdictError("payload.verdict.decision 非法")
    sections_raw = raw["sections"]
    if not isinstance(sections_raw, dict):
        raise VerdictError("payload.verdict.sections 必须是对象")
    sections: dict[str, tuple[str, ...]] = {}
    for name, items in sections_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise VerdictError("payload.verdict.sections 的键必须是非空字符串")
        sections[name.strip()] = _string_list(items, f"payload.verdict.sections.{name}")
    constraints_raw = raw["constraints"]
    if not isinstance(constraints_raw, list):
        raise VerdictError("payload.verdict.constraints 必须是数组")
    constraints: list[Constraint] = []
    for index, item in enumerate(constraints_raw, 1):
        if not isinstance(item, dict):
            raise VerdictError(f"verdict.constraints[{index}] 必须是对象")
        keys = {"item", "value"}
        _strict_keys(item, keys, f"verdict.constraints[{index}]")
        _required_keys(item, keys, f"verdict.constraints[{index}]")
        if not all(isinstance(item[key], str) and item[key].strip() for key in keys):
            raise VerdictError(f"verdict.constraints[{index}] 的 item/value 必须是非空字符串")
        constraints.append(Constraint(item["item"].strip(), item["value"].strip()))
    return Verdict(str(actual_token), str(decision), sections, tuple(constraints), text)


def _validate_result(payload: dict[str, object], action: ActionType) -> None:
    raw = payload.get("result")
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ProtocolError("payload.result 必须是对象")
    keys = {"status", "artifacts", "error"}
    _strict_keys(raw, keys, "payload.result")
    _required_keys(raw, keys, "payload.result")
    status, artifacts, error = raw["status"], raw["artifacts"], raw["error"]
    if status not in {"success", "failed"}:
        raise ProtocolError("payload.result.status 只能是 success 或 failed")
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ProtocolError("payload.result.artifacts 必须是对象数组")
    if error is not None and not isinstance(error, str):
        raise ProtocolError("payload.result.error 必须是字符串或 null")
    expected = ActionType.DONE if status == "success" else ActionType.BLOCKED
    if action != expected:
        raise ProtocolError(f"result.status={status} 时 action 必须是 {expected.value}")


def parse_action(text: str) -> ActionEnvelope:
    matches = list(_ACTION_RE.finditer(text))
    if text.count(ACTION_START) != 1 or text.count(ACTION_END) != 1 or len(matches) != 1:
        raise ProtocolError("每轮必须且只能输出一个完整 Action 块")
    match = matches[0]
    if text[match.end() :].strip():
        raise ProtocolError("Action 块必须是整条回复的最后内容")
    try:
        raw = json.loads(
            match.group("body"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Action 块不是合法 JSON：{exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("Action JSON 顶层必须是对象")
    keys = {"action", "target_agent", "reason", "payload"}
    _strict_keys(raw, keys, "Action")
    _required_keys(raw, keys, "Action")
    try:
        action = ActionType(raw["action"])
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"Action 动作 {raw['action']!r} 非法") from exc
    target_raw, reason, payload = raw["target_agent"], raw["reason"], raw["payload"]
    if not isinstance(reason, str) or not reason.strip():
        raise ProtocolError("Action reason 必须是非空字符串")
    clean_reason = reason.strip()
    if "\n" in clean_reason or "\r" in clean_reason or _SENTENCE_BREAK_RE.search(clean_reason):
        raise ProtocolError("Action reason 必须是单句")
    if not isinstance(payload, dict):
        raise ProtocolError("Action payload 必须是对象")
    _strict_keys(payload, ACTION_PAYLOAD_KEYS, "Action payload")
    target: AgentCode | None = None
    if action == ActionType.HANDOFF:
        try:
            target = AgentCode(target_raw)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"handoff 的 target_agent {target_raw!r} 非法") from exc
    elif target_raw is not None:
        raise ProtocolError(f"{action.value} 的 target_agent 必须是 null")
    choices = _payload_choices(payload)
    if choices and action != ActionType.ASK_USER:
        raise ProtocolError("包含 choices 时 action 必须是 ask_user")
    _payload_progress(payload)
    _payload_drafts(payload)
    _payload_memories(payload)
    _payload_naming(payload)
    _validate_asset_specs(payload)
    _payload_verdict(payload, text)
    _validate_result(payload, action)
    return ActionEnvelope(action, target, clean_reason, payload)


def format_action(
    action: ActionType | str,
    *,
    reason: str,
    target_agent: AgentCode | str | None = None,
    payload: dict[str, object] | None = None,
) -> str:
    """生成程序消息使用的合法 Action 块。"""
    action_value = ActionType(action).value
    target_value = AgentCode(target_agent).value if target_agent is not None else None
    body = json.dumps(
        {
            "action": action_value,
            "target_agent": target_value,
            "reason": reason,
            "payload": payload or {},
        },
        ensure_ascii=False,
        indent=2,
    )
    return f"{ACTION_START}\n{body}\n{ACTION_END}"


def with_action(
    text: str,
    action: ActionType | str,
    *,
    reason: str,
    target_agent: AgentCode | str | None = None,
    payload: dict[str, object] | None = None,
) -> str:
    body = text.rstrip()
    block = format_action(action, reason=reason, target_agent=target_agent, payload=payload)
    return f"{body}\n\n{block}" if body else block


def strip_protocol_blocks(text: str) -> str:
    """隐藏完整或流式生成中的 Action 块。"""
    start = text.find(ACTION_START)
    if start >= 0:
        return text[:start].rstrip()
    return text.strip()


def parse_turn(text: str) -> TurnOutput:
    action = parse_action(text)
    return TurnOutput(
        text=text,
        action=action,
        progress=_payload_progress(action.payload),
        drafts=_payload_drafts(action.payload),
        memories=_payload_memories(action.payload),
        naming=_payload_naming(action.payload),
        choices=_payload_choices(action.payload),
    )


def parse_drafts(text: str) -> tuple[DraftBlock, ...]:
    return parse_turn(text).drafts


def parse_progress(text: str) -> Progress | None:
    return parse_turn(text).progress


def parse_memories(text: str) -> tuple[MemoryItem, ...]:
    return parse_turn(text).memories


def parse_naming(text: str) -> tuple[NamingOption, ...]:
    return parse_turn(text).naming


def parse_choices(text: str) -> tuple[ChoiceGroup, ...]:
    return parse_turn(text).choices


def parse_asset_specs(text: str) -> tuple[AssetSpec, ...]:
    action = parse_action(text)
    raw = action.payload.get("asset_specs", [])
    assert isinstance(raw, list)
    specs: list[AssetSpec] = []
    for item in raw:
        assert isinstance(item, dict)
        width, height = _size_of(str(item["size"]))
        specs.append(
            AssetSpec(
                code=str(item["code"]).strip(),
                name=str(item["name"]).strip(),
                category=str(item["category"]).strip().lower(),
                width=width,
                height=height,
                image_format=str(item["format"]).strip().lstrip(".").lower(),
                file_name=str(item["file_name"]).strip(),
                description=str(item["description"]).strip(),
                anchors=str(item["anchors"]).strip(),
                constraints=_string_list(item["constraints"], "asset_specs.constraints"),
                view_background_color=normalize_view_background_color(
                    str(item["view_background_color"])
                ),
                prompt=str(item["prompt"]).strip(),
                negative_prompt=str(item["negative_prompt"]).strip(),
                text=text,
            )
        )
    return tuple(specs)


def parse_verdict(text: str, token: str = SPEC_CHECK) -> Verdict:
    action = parse_action(text)
    verdict = _payload_verdict(action.payload, text, token)
    if verdict is None:
        raise VerdictError(f"Action payload 缺少 {token} 裁决")
    return verdict
