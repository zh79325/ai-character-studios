"""解析会话型 Agent 每轮输出里的结构块。

提示词里跟 Agent 约好三个块：`[对焦进度]`、`[草稿开始: 路径]…[草稿结束]`、`[项目记忆]`。
平台靠它们把自由对话变成能存下来的东西——进度进对象目录里那份会话记忆，草稿进
`artifact_drafts`，记忆在确认沉淀时进 `memory/preferences.md`。

宽进严出：模型会把块包在 ``` 里、会用半角冒号、会把模板占位符 `<…>` 原样吐回来、会写
「暂无」。这些一律容错或跳过，但绝不猜测没写的内容——解析不出草稿就是这轮没有草稿，
让用户继续聊，而不是拿半截文本去覆盖定稿。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MEMORY_KINDS = ("preference", "taboo", "fact")

PROGRESS_MARKER = "[对焦进度]"
MEMORY_MARKER = "[项目记忆]"
NAMING_MARKER = "[项目命名建议]"
"""立项对焦里的命名建议块。

不走草稿那一路：项目名与代号改的是注册表主键，得由用户在收口面板上选定后显式提交，
而不能跟 art-bible.md 一起被「沉淀」进去。
"""
CHARACTER_MEMORY_MARKER = "[角色记忆]"
"""角色型 Agent 的记忆块标记。

跟 `[项目记忆]` 解析成同一种东西：记忆归项目还是归角色，由**这场会话在跟谁对焦**决定，不由
模型选的标记决定。让模型自己挑作用域的话，它在角色会话里写一句 `[项目记忆]` 就把一条只对
这个角色成立的要求塞给了全项目，而这条会一路跟到别的角色的提示词里。
"""
CHOICE_MARKER = "[待选项]"
"""还要用户拍板的那几处分歧。

前端把它摆成选择组件，用户点完平台拼成一句话发回去。不让用户把选项文字手抄进输入框，
也不靠模型自己认「选项 B」这种指代。
"""

VERDICTS = ("APPROVE", "CONCERNS", "REJECT")
SPEC_CHECK = "SPEC-CHECK"
VIEW_CHECK = "VIEW-CHECK"
CONSTRAINTS_SECTION = "硬性约束清单"

_DRAFT_RE = re.compile(
    r"^[ \t>]*\[草稿开始[:：]\s*(?P<path>[^\]\r\n]+?)\s*\]\s*$"
    r"(?P<body>.*?)"
    r"^[ \t>]*\[草稿结束\]\s*$",
    re.DOTALL | re.MULTILINE,
)

# 一个块从它的标记行开始，到下一个标记行或文本结束为止
_BLOCK_END_RE = re.compile(
    r"^[ \t>]*\[(?:草稿开始|草稿结束|对焦进度|项目记忆|角色记忆|项目命名建议|待选项)", re.MULTILINE
)

_KEY_RE = re.compile(r"^(?P<key>已定|待定|下一步)\s*[:：]\s*(?P<value>.*)$")
_NAMING_KEY_RE = re.compile(
    r"^(?P<key>名称|名字|项目名|代号|code|理由|说明)\s*[:：]\s*(?P<value>.*)$", re.I
)
_NAMING_SPLIT_RE = re.compile(r"\s*[/｜|；;]\s*")
_CHOICE_KEY_RE = re.compile(r"^(?P<key>项|选项|推荐|多选)\s*[:：]\s*(?P<value>.*)$")
_CHOICE_SEG_RE = re.compile(r"\s*([/；;])\s*")
"""一行里各段的分隔。不含 `|`：那个留给选项之间用。"""
_CHOICE_OPT_SEPS = frozenset("|｜、")
"""选项之间的分隔。"""
_CHOICE_BRACKETS = {"（": "）", "(": ")", "《": "》", "【": "】", "「": "」", "『": "』"}
"""成对的括号。里头的分隔符在列举这一个选项的内容，不是在分下一个选项。"""
_MEMORY_RE = re.compile(rf"^(?P<kind>{'|'.join(MEMORY_KINDS)})\s*[:：]\s*(?P<value>.*)$", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s*")
_FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)")

_NONE_WORDS = frozenset({"暂无", "无", "none", "n/a", "-", "—"})

_YES_WORDS = frozenset({"是", "真", "多选", "yes", "y", "true", "1"})
"""`多选` 这一段写什么算真。其余一律当单选：读不懂的时候摆成单选只是少点几个，而把单选题
摆成多选会让用户同时选上两个互排的值。
"""

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(?P<title>.+?)\s*#*\s*$")
_CONSTRAINT_RE = re.compile(r"^(?P<item>[^=:：]{1,60}?)\s*(?:=|→|:|：)\s*(?P<value>.+)$")

ASSET_PREFIX = "ASSET-"

# 卡片首行：`ASSET-CT-001 — 赤瞳 渲染图`。分隔符容错到短横线与冒号，模型并不总能打出破折号
_ASSET_HEAD_RE = re.compile(
    r"^[ \t>]*(?P<code>ASSET-[A-Za-z0-9]{1,16}-\d{1,4})\s*(?:[—–-]|[:：])?\s*(?P<name>.*?)\s*$"
)

_ASSET_KEY_RE = re.compile(
    r"^[ \t>]*(?P<key>art\s*bible\s*锚点|类别|尺寸|格式|文件名|视觉描述|硬性约束|四视图背景色"
    r"|negative[_\s-]*prompt|prompt)\s*[:：]\s*(?P<value>.*)$",
    re.I,
)

_ASSET_KEYS = {
    "类别": "category",
    "尺寸": "size",
    "格式": "image_format",
    "文件名": "file_name",
    "视觉描述": "description",
    "硬性约束": "constraints",
    "四视图背景色": "view_background_color",
    "negativeprompt": "negative_prompt",
    "prompt": "prompt",
}

# 卡片上字段名 → 缺了该怎么告诉人。报英文字段名的话，用户得自己回去对模板
_ASSET_LABELS = {
    "category": "类别",
    "size": "尺寸",
    "file_name": "文件名",
    "view_background_color": "四视图背景色",
    "prompt": "prompt",
    "negative_prompt": "negative_prompt",
}

_SIZE_RE = re.compile(r"(?P<width>\d{2,5})\s*[x×*]\s*(?P<height>\d{2,5})", re.I)
_VIEW_BACKGROUND_RE = re.compile(r"^#(?P<hex>[0-9a-f]{6})(?:\s*[（(][^）)]{1,30}[）)])?$", re.I)
_ITEM_SPLIT_RE = re.compile(r"[,，、;；]")


def is_placeholder(text: str) -> bool:
    """模板占位符与「暂无」不算内容。

    提示词里写的是 `<一行一条，或「暂无」>`，模型照搬回来的情况很常见；把它当结论存进
    记忆，用户下次就会看到 Agent 一本正经地复述一句尖括号。

    卡片模板用的是大括号（`{宽}x{高}`），一并认下来：把 `{全局预设}` 当真值发给生图接口，
    等于拿模板当提示词烧了一次额度。
    """
    stripped = text.strip().strip("`").strip()
    if not stripped or stripped.lower() in _NONE_WORDS:
        return True
    return (stripped.startswith("<") and stripped.endswith(">")) or (
        stripped.startswith("{") and stripped.endswith("}")
    )


@dataclass(frozen=True, slots=True)
class DraftBlock:
    """一份完整产物草稿。`target_path` 是 Agent 声明的落盘位置，还要由调用方校验。"""

    target_path: str
    content: str


@dataclass(frozen=True, slots=True)
class MemoryItem:
    kind: str
    content: str


@dataclass(frozen=True, slots=True)
class Progress:
    """这一轮的对焦进度。三项都可能为空——刚开场时本来就还没有结论。"""

    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_step: str | None = None

    def is_empty(self) -> bool:
        return not (self.decisions or self.open_questions or self.next_step)


@dataclass(frozen=True, slots=True)
class NamingOption:
    """一条项目命名建议。

    `code` 可能为空：模型给的代号过不了 ASCII 那一关时只留名称，名称本身仍然是有用的
    建议，丢掉整条等于让用户再聊一遗。
    """

    name: str
    code: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ChoiceGroup:
    """一处要用户拍板的分歧：一个项、几个可选值、Agent 的推荐。

    `recommended` 只留真的是 `options` 之一的那几个：前端拿它做默认选中，对不上号的
    推荐宁可不默认选，也不能多出一个选不中的影子选项。单选题最多一个（模型写了几个就只取
    第一个），多选题几个都留。

    `multiple` 由 Agent 在块里自己声明：互排的维度（写实到卡通的位置）只能选一个，可叠加的
    维度（参考作品、要避开的元素）本来就是好几项，一律单选等于逼用户丢掉其他的。
    """

    item: str
    options: tuple[str, ...]
    recommended: tuple[str, ...] = ()
    multiple: bool = False


@dataclass(frozen=True, slots=True)
class TurnOutput:
    """一轮助手输出的解析结果，原文始终原样保留。"""

    text: str
    progress: Progress | None = None
    drafts: tuple[DraftBlock, ...] = ()
    memories: tuple[MemoryItem, ...] = ()
    naming: tuple[NamingOption, ...] = ()
    choices: tuple[ChoiceGroup, ...] = ()

    @property
    def has_draft(self) -> bool:
        return bool(self.drafts)


def _strip_fences(body: str) -> str:
    """去掉草稿正文首尾可能包着的代码围栏。

    模型常把整个块塞进 ``` 里好让 Markdown 显示得规整，但围栏不是文件内容——把它写进
    art-bible.md 会让下游解析第 6 节时多出两行噪声。只削首尾各一层，正文里的围栏
    （比如设定文档中引用的代码）原样留着。
    """
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _FENCE_RE.match(lines[0]) and len(lines) > 1 and _FENCE_RE.match(lines[-1]):
        lines = lines[1:-1]
    return "\n".join(lines).strip("\n")


def parse_drafts(text: str) -> tuple[DraftBlock, ...]:
    """抽出全部草稿块。一轮里可以有多份（art-bible.md 与 project.json 就是一对）。"""
    drafts: list[DraftBlock] = []
    for match in _DRAFT_RE.finditer(text):
        path = match.group("path").strip().strip("`")
        content = _strip_fences(match.group("body"))
        if not path or is_placeholder(path) or not content:
            continue
        drafts.append(DraftBlock(target_path=path, content=content + "\n"))
    return tuple(drafts)


def _block_body(text: str, marker: str) -> str | None:
    """取标记行之后、下一个标记行之前的内容。"""
    start = text.find(marker)
    if start < 0:
        return None
    after = start + len(marker)
    nxt = _BLOCK_END_RE.search(text, after)
    return text[after : nxt.start()] if nxt else text[after:]


def _items(raw: str) -> list[str]:
    """把「一行一条」的值切成条目，顺带去掉列表符号与占位符。"""
    items: list[str] = []
    for line in raw.splitlines():
        item = _BULLET_RE.sub("", line).strip()
        if item and not is_placeholder(item):
            items.append(item)
    return items


def parse_progress(text: str) -> Progress | None:
    """解析 `[对焦进度]`。没有这个块返回 None，与「有块但三项都空」区分开。"""
    body = _block_body(text, PROGRESS_MARKER)
    if body is None:
        return None

    buckets: dict[str, list[str]] = {"已定": [], "待定": [], "下一步": []}
    current: str | None = None
    for line in body.splitlines():
        match = _KEY_RE.match(line.strip())
        if match:
            current = match.group("key")
            buckets[current].extend(_items(match.group("value")))
            continue
        if current is not None:
            buckets[current].extend(_items(line))

    next_step = "；".join(buckets["下一步"]) or None
    return Progress(
        decisions=tuple(buckets["已定"]),
        open_questions=tuple(buckets["待定"]),
        next_step=next_step,
    )


def parse_memories(text: str) -> tuple[MemoryItem, ...]:
    """解析 `[项目记忆]` 与 `[角色记忆]`。只收认识的三类，别的行忽略。

    两个标记合起来收：作用域由会话的对焦对象定（见 `CHARACTER_MEMORY_MARKER`），这里只管
    把条目捞出来。两个块都写了就都收，顺序按它们在文里出现的先后。
    """
    bodies = [
        body
        for marker in (MEMORY_MARKER, CHARACTER_MEMORY_MARKER)
        if (body := _block_body(text, marker)) is not None
    ]
    if not bodies:
        return ()

    items: list[MemoryItem] = []
    for body in bodies:
        kind: str | None = None
        for line in body.splitlines():
            stripped = _BULLET_RE.sub("", line).strip()
            match = _MEMORY_RE.match(stripped)
            if match:
                kind = match.group("kind").lower()
                value = match.group("value").strip()
                if value and not is_placeholder(value):
                    items.append(MemoryItem(kind=kind, content=value))
                continue
            # 同一类下的后续行沿用上一个 kind：模型常把三条偏好分三行写在 preference 下面
            if kind is not None and stripped and not is_placeholder(stripped):
                items.append(MemoryItem(kind=kind, content=stripped))
    return tuple(items)


_CODE_OK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _clean_code(raw: str) -> str:
    """从一段自由文本里取出可用的代号，取不出就给空。

    代号要进路径、日志与外部 API 参数，尺度跟 `projects._validate_code` 一致；这里只负责
    把不合格的建议降级成「没给代号」，真正拦人的是提交时那一关。

    模型爱在代号后面跟一句中文尾巴（「推荐」、「简短好记」），那种只取第一个词；但尾巴里
    还带英文单词时就不猜了——`Wet Metal` 截成 `wet` 比空着更坏。
    """
    tokens = raw.strip().strip("`").strip().split()
    if not tokens:
        return ""
    if any(re.search(r"[0-9A-Za-z]", one) for one in tokens[1:]):
        return ""
    token = tokens[0].lower().strip("，,。.；;")
    return token if _CODE_OK_RE.match(token) else ""


def parse_naming(text: str) -> tuple[NamingOption, ...]:
    """解析 `[项目命名建议]`。一次可以给多条，用户在收口面板上选一条或自己重写。

    以「名称」为一条的开头：名称、代号、理由既可能写在同一行用 `/` 隔开，也可能分三行写，
    两种都收。没名称的条目直接丢，否则面板上会出现一个只有代号的空选项。
    """
    body = _block_body(text, NAMING_MARKER)
    if body is None:
        return ()

    options: list[NamingOption] = []
    current: dict[str, str] = {}

    def close() -> None:
        name = current.get("name", "").strip().strip("`").strip()
        if name and not is_placeholder(name):
            options.append(
                NamingOption(
                    name=name,
                    code=_clean_code(current.get("code", "")),
                    reason=current.get("reason", "").strip(),
                )
            )
        current.clear()

    for line in body.splitlines():
        for part in _NAMING_SPLIT_RE.split(_BULLET_RE.sub("", line).strip()):
            match = _NAMING_KEY_RE.match(part.strip())
            if match is None:
                continue
            key = match.group("key").lower()
            value = match.group("value").strip()
            if key in ("名称", "名字", "项目名"):
                close()
                current["name"] = value
            elif key in ("代号", "code"):
                current["code"] = value
            else:
                current["reason"] = value
    close()
    return tuple(options)


MIN_CHOICE_OPTIONS = 2
"""一组至少要两个选项。只给一个的话用户点它等于没做选择，那就不是分歧而是结论。"""

MAX_CHOICE_GROUPS = 4
"""一轮最多摆四组。立项要定的维度十几个，一次全摊出来用户无从下手；多给的在这里截掉，
剩下的等这一批拍完再问。截而不报错：前四组本身是能用的，因为多了两组就一个不给反而更坏。
"""


def _choice_segments(line: str) -> list[str]:
    """把一行切成「项 / 选项 / 多选 / 推荐」几段。

    只在分隔符后面真的跟着段名时才断开：选项文字里本来就常带斜杠（「写实/仿真」），无条件
    切会把一个选项劈成两个。
    """
    pieces = _CHOICE_SEG_RE.split(line)
    segments = pieces[:1]
    for sep, part in zip(pieces[1::2], pieces[2::2], strict=False):
        if _CHOICE_KEY_RE.match(part.strip()):
            segments.append(part)
        elif segments:
            segments[-1] = f"{segments[-1]}{sep}{part}"
    return segments


def _split_options(raw: str) -> list[str]:
    """把一段选项文字切开，括号里的分隔符不算。

    选项文字本来就带成对括号（「《我的世界：地下城》（方块风格、光照、色彩基准）」），里头的
    顿号是在列举这一个选项的内容。无条件切会把一个选项劈成好几个，面板上就摆出一堆
    看不懂的碎片，推荐值也再也对不上。括号没合上就当它一直开着：宁可少切，不能切错。
    """
    parts: list[str] = []
    buf: list[str] = []
    stack: list[str] = []
    for char in raw:
        if char in _CHOICE_BRACKETS:
            stack.append(_CHOICE_BRACKETS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char in _CHOICE_OPT_SEPS and not stack:
            parts.append("".join(buf))
            buf.clear()
            continue
        buf.append(char)
    parts.append("".join(buf))
    return [one.strip() for one in parts]


def parse_choices(text: str) -> tuple[ChoiceGroup, ...]:
    """解析 `[待选项]`。一次最多收 `MAX_CHOICE_GROUPS` 组，用户在面板上一次点完。

    以「项」为一组的开头，各段既可能写在同一行用 `/` 隔开，也可能分行写。选项之间用 `|`
    而不是 `/`：选项文字里本来就常带斜杠（「3:7 写实/风格化」），两边用同一个分隔符就会把一个
    选项切成两个。顿号也算选项分隔，但括号里的不算：那是这一个选项自己的列举。

    `多选: 是` 的那几组可以同时拍好几个值，推荐也就能给好几个（`推荐: A | B`）。
    """
    body = _block_body(text, CHOICE_MARKER)
    if body is None:
        return ()

    groups: list[ChoiceGroup] = []
    current: dict[str, str] = {}

    def close() -> None:
        item = current.get("item", "").strip().strip("`").strip()
        raw = current.get("options", "")
        options = (
            ()
            if is_placeholder(raw)
            else tuple(
                one
                for one in (part.strip("`").strip() for part in _split_options(raw))
                if one and not is_placeholder(one)
            )
        )
        recommended_raw = current.get("recommended", "")
        multiple = current.get("multiple", "").strip().strip("`").strip().lower() in _YES_WORDS
        current.clear()
        if not item or is_placeholder(item) or len(options) < MIN_CHOICE_OPTIONS:
            return
        # 去重保序：同一个值写两遍会在面板上预选成两行
        recommended = tuple(
            dict.fromkeys(
                one
                for one in (part.strip("`").strip() for part in _split_options(recommended_raw))
                if one in options
            )
        )
        groups.append(
            ChoiceGroup(
                item=item,
                options=options,
                recommended=recommended if multiple else recommended[:1],
                multiple=multiple,
            )
        )

    for line in body.splitlines():
        for part in _choice_segments(_BULLET_RE.sub("", line).strip()):
            match = _CHOICE_KEY_RE.match(part.strip())
            if match is None:
                continue
            key, value = match.group("key"), match.group("value").strip()
            if key == "项":
                close()
                current["item"] = value
            elif key == "选项":
                current["options"] = value
            elif key == "多选":
                current["multiple"] = value
            else:
                current["recommended"] = value
    close()
    return tuple(groups[:MAX_CHOICE_GROUPS])


def parse_turn(text: str) -> TurnOutput:
    """解析一轮助手输出。原文原样带回，前端展示的仍是 Agent 说的话。"""
    return TurnOutput(
        text=text,
        progress=parse_progress(text),
        drafts=parse_drafts(text),
        memories=parse_memories(text),
        naming=parse_naming(text),
        choices=parse_choices(text),
    )


# --------------------------------------------------------------------------- #
# 素材规格卡片
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """一张图的规格卡片：既是生图入参，也是事后校图的依据。

    原文一并留着（`text`）：字段拆开后方便程序用，但人回头排查“为什么这张图不对”时看的
    是卡片本身。抽取过的结构与原文不一致时，原文才是证据。
    """

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
        """这张卡片哪几项不能用，都齐了返回空元组。

        不在这里报错而是把缺口列出来：一张卡片可能只少了尺寸，其余内容都能用，直接丢掉
        等于让用户重新聊一遍。
        """
        missing: list[str] = []
        if not self.category:
            missing.append(_ASSET_LABELS["category"])
        if self.width <= 0 or self.height <= 0:
            missing.append(_ASSET_LABELS["size"])
        if not self.file_name or "/" in self.file_name or "." not in self.file_name:
            missing.append(_ASSET_LABELS["file_name"])
        if self.category == "character" and not self.view_background_color:
            missing.append(_ASSET_LABELS["view_background_color"])
        if not self.prompt:
            missing.append(_ASSET_LABELS["prompt"])
        if not self.negative_prompt:
            missing.append(_ASSET_LABELS["negative_prompt"])
        return tuple(missing)

    def as_dict(self) -> dict[str, Any]:
        """落 meta.json 与 `generations.asset_spec` 的形态，字段名与模板对得上。"""
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


def _asset_field(key: str) -> str | None:
    flat = re.sub(r"[\s_-]", "", key).lower()
    if flat.startswith("artbible"):
        return "anchors"
    return _ASSET_KEYS.get(flat)


def _size_of(raw: str) -> tuple[int, int]:
    match = _SIZE_RE.search(raw)
    if match is None:
        return 0, 0
    return int(match.group("width")), int(match.group("height"))


def _split_items(raw: str) -> tuple[str, ...]:
    items = [one.strip() for one in _ITEM_SPLIT_RE.split(raw)]
    return tuple(one for one in items if one and not is_placeholder(one))


def normalize_view_background_color(raw: str) -> str:
    """将卡片里的纯色背景标准化为 `#RRGGBB`；透明或自由文本一律视为缺失。"""
    value = raw.strip().strip("`").strip()
    match = _VIEW_BACKGROUND_RE.fullmatch(value)
    return f"#{match.group('hex').upper()}" if match else ""


def _build_spec(fields: Mapping[str, str], text: str) -> AssetSpec | None:
    def value(key: str) -> str:
        raw = fields.get(key, "").strip().strip("`").strip()
        return "" if is_placeholder(raw) else raw

    width, height = _size_of(value("size"))
    spec = AssetSpec(
        code=fields["code"],
        name=value("name"),
        category=value("category").lower(),
        width=width,
        height=height,
        image_format=value("image_format").lstrip(".").lower(),
        file_name=value("file_name"),
        description=value("description"),
        anchors=value("anchors"),
        constraints=_split_items(value("constraints")),
        view_background_color=normalize_view_background_color(value("view_background_color")),
        prompt=value("prompt"),
        negative_prompt=value("negative_prompt"),
        text=text,
    )
    # 既没 prompt 也没文件名，说明这行 ASSET-xxx 只是正文里提了一句编号，不是卡片
    return spec if spec.prompt or spec.file_name else None


def parse_asset_specs(text: str) -> tuple[AssetSpec, ...]:
    """抽出全部素材规格卡片。四视图那一步一次就是四张。

    允许续行：prompt 往往长得被模型自己折成好几行，只取第一行会把尾巴上的画质与风格层丢
    掉。但**空行断开续行**：提示词工程师被允许在全部卡片之后另起一段提问题，那段话不能被接
    到 negative_prompt 后面当成禁止词发出去。
    """
    cards: list[AssetSpec] = []
    fields: dict[str, str] | None = None
    lines: list[str] = []
    field: str | None = None

    def close() -> None:
        nonlocal fields, lines, field
        if fields is not None:
            spec = _build_spec(fields, "\n".join(lines).strip())
            if spec is not None:
                cards.append(spec)
        fields, lines, field = None, [], None

    for raw in text.splitlines():
        head = _ASSET_HEAD_RE.match(raw) if ASSET_PREFIX in raw else None
        if head is not None:
            close()
            fields = {
                "code": head.group("code"),
                "name": head.group("name").strip().strip("—–-").strip(),
            }
            lines = [raw.strip()]
            continue
        if fields is None:
            continue
        if not raw.strip():
            field = None
            continue

        match = _ASSET_KEY_RE.match(raw)
        if match is not None:
            name = _asset_field(match.group("key"))
            if name is None:
                continue
            field = name
            fields[name] = match.group("value").strip()
            lines.append(raw.strip())
            continue
        if field is not None:
            fields[field] = f"{fields[field]} {raw.strip()}".strip()
            lines.append(raw.strip())

    close()
    return tuple(cards)


# --------------------------------------------------------------------------- #
# 裁决型 Agent
# --------------------------------------------------------------------------- #


class VerdictError(ValueError):
    """裁决回答的首行不是约定的 token。"""


@dataclass(frozen=True, slots=True)
class Constraint:
    """一条可逐张图对着回答「符合 / 不符合」的硬约束。"""

    item: str
    value: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """一次自动裁决。`text` 是全文，进 `task_events` 供回溯。"""

    token: str
    decision: str
    sections: dict[str, tuple[str, ...]]
    constraints: tuple[Constraint, ...] = ()
    text: str = ""

    @property
    def approved(self) -> bool:
        """只表示审校没发现问题，放不放行仍然是人工门禁的事。"""
        return self.decision == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.decision == "REJECT"


def _verdict_line(text: str, token: str) -> str:
    """拿首行。只跳前面的空行与围栏，不往正文里找。

    提示词里跟它说好了「不得把 token 藏在段落里」。要是这里宽容到满文搜，它写一段「若修好则
    APPROVE」也会被当成通过，那是把条件句当结论读。
    """
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip()
        if not stripped or _FENCE_RE.match(stripped):
            continue
        return stripped
    raise VerdictError(f"{token} 的回答是空的")


def parse_verdict(text: str, token: str = SPEC_CHECK) -> Verdict:
    """解析裁决型 Agent 的回答：首行 `{token}: APPROVE|CONCERNS|REJECT`，理由在下面分节写。

    首行不合规就报错而不是当成 REJECT：看不出裁决是模型没按契约说话，该重试或转人工；默认
    成 REJECT 会把一次格式事故变成一次对设定的否定，用户拿到的是一份没有理由的驳回。
    """
    first = _verdict_line(text, token)
    match = re.match(rf"^{re.escape(token)}\s*[:：]\s*(?P<decision>[A-Za-z]+)", first, re.I)
    if match is None:
        raise VerdictError(f"首行不是 {token} 裁决，而是 {first[:60]!r}")
    decision = match.group("decision").upper()
    if decision not in VERDICTS:
        raise VerdictError(f"{token} 的裁决 {decision!r} 不在 {'/'.join(VERDICTS)} 里")

    sections = _sections(text)
    return Verdict(
        token=token,
        decision=decision,
        sections=sections,
        constraints=_constraints(sections),
        text=text,
    )


def _sections(text: str) -> dict[str, tuple[str, ...]]:
    """把 `### 标题` 下的条目按标题归堆。标题原文当键：模型会改措辞，匹配由谁用谁包含。"""
    buckets: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            current = heading.group("title").strip().strip("*").strip()
            buckets.setdefault(current, [])
            continue
        if current is None:
            continue
        item = _BULLET_RE.sub("", line).strip().strip("*").strip()
        if item and not is_placeholder(item):
            buckets[current].append(item)
    return {title: tuple(items) for title, items in buckets.items()}


def _constraints(sections: dict[str, tuple[str, ...]]) -> tuple[Constraint, ...]:
    """从硬性约束那一节抽出 `项 = 值`。

    拆成两截而不是存整行：这份清单进 `meta.json` 后要被后续每张图逐条比对，比对得按项
    对得上号。拆不出项名的行直接丢：拆不出就是不可逐条判定，存下去也没人能用。
    """
    title = next((key for key in sections if CONSTRAINTS_SECTION in key), None)
    if title is None:
        return ()
    picked: list[Constraint] = []
    for line in sections[title]:
        match = _CONSTRAINT_RE.match(line)
        if match is None:
            continue
        item = match.group("item").strip().strip("`").strip()
        value = match.group("value").strip()
        if item and value:
            picked.append(Constraint(item=item, value=value))
    return tuple(picked)
