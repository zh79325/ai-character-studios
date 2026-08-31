"""一场会话里的多 Agent 编排：阶段表与指派协议。

**本版只有骨架**：阶段表、判阶段、判这一轮谁说话，以及一段写清将来怎么接的协议说明。没有
执行器，没有解析，谁都还没被指派过——真进角色设计阶段时按下面的接线图往里填。

## 目标形态

一个角色一场会话（`target_kind="character"` + `target_ref=角色 id`），会话行上的
`agent_code` 是这场的**主 Agent**：它负责判断现在处在哪一阶段、这一阶段还差什么、什么
时候该把活派给谁。用户始终只跟这一场会话说话，不用在页面之间跳。

以角色为例，一场会话里跑完三段：

1. 设定阶段：主 Agent 跟用户对话，把角色设定写成草稿，用户确认后落盘（`gate_spec`）。
2. 渲染图阶段：主 Agent 读懂用户要什么，指派 `image_t2i` 生图；图回来后接着聊修改意见，
   把已生成的那张当参考图指派 `image_i2i` 调效果，直到用户定稿（`gate_render`）。
3. 四视图阶段：同理指派 `image_i2i` 出四张、`vision_reviewer` 审校。

## 谁管阶段：状态机管，模型不管

阶段跳转与人工门禁一律由 `Character.state` 与 `gate_*_confirmed_at` 把关（
`assets/characters.py` 里那套 `advance` / `require_state` / `confirm_*`）。主 Agent
只能在**当前阶段之内**指派子 Agent，不能自己宣布进了下一阶段——阶段是后续每一步的凭据，
让模型改凭据等于没有凭据。

## 指派协议怎么接（`DISPATCH_PROTOCOL`）

主 Agent 在回答里输出一个动作块声明要调谁、拿什么入参。接线时按顺序补这四处：

1. `prompts/agents/{主 Agent}.md`：把 `DISPATCH_PROTOCOL` 的格式写进提示词，并申明只准
   指派 `Stage.crew` 里列的那几个。
2. `agents/parsing.py`：仿 `parse_choices()` 加一个 `parse_dispatch()`，把动作块解析成
   `(agent_code, params)`；同时把动作块的原文从展示文本里剔掉（前端 `visibleText`
   已经按 `[标记]` 剥了一层，新标记要一起加进去）。
3. 本文件加执行器 `run(project, runtime, ref, conversation, order)`：校验
   `order.agent_code in stage.crew` → 调既有能力（生图走 `assets/render.py`、
   `assets/views.py` 那两条现成链路，它们已经会把产物登记进 `generations`）→ 把结果写成
   一条 assistant 消息：`agent_code=子 Agent`、`attachments=[{"kind": "image",
   "path": 相对项目目录, "generation_id": ...}]`。
4. `agents/conversation.py` 的 `send()`：解析出动作块就跑执行器，跑完把产物摘要接着喂回
   主 Agent 再要一轮点评（同一场会话内的第二次模型调用，注意 `BUS` 的增量要按轮清，否则
   新订上来的流会被上一轮的 `turn` 收掉）。

前端不用改结构：消息已经带 `agent_code`（气泡按它取称谓）与 `attachments`（有图就在气泡
下面铺缩略图），`components/chat/MessageList.tsx` 那两个分支现在就留着。
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier.assets import characters as character_assets
from atelier.db.project_models import Character, Conversation

DISPATCH_PROTOCOL = """[指派开始]
agent: image_t2i
说明: 一句话讲清为什么现在派它
参数:
- prompt: 给子 Agent 的正向提示词
- 参考图: 相对项目目录的路径，没有就省掉这一行
[指派结束]"""
"""主 Agent 声明指派的文本块。一轮最多一个块；解析与执行都还没接。"""


@dataclass(frozen=True)
class Stage:
    """一个阶段：谁主持、能派谁、推进它要哪一道人工确认。"""

    code: str
    label: str
    director: str
    """这一阶段的主 Agent。现在三段都是 `spec_writer`——它是唯一的角色会话型 Agent；
    将来会换成一个专职编排的 `character_director`，届时只改这张表。"""
    crew: tuple[str, ...]
    """允许被指派的子 Agent。执行器要拿它当白名单，模型报了别的就拒掉。"""
    gate_field: str
    """推进这一阶段需要人工确认的列名，空串=这一阶段没有人工门禁。"""


STAGES: tuple[Stage, ...] = (
    Stage(
        code="spec",
        label="设定对焦",
        director="spec_writer",
        crew=("spec_reviewer",),
        gate_field="gate_spec_confirmed_at",
    ),
    Stage(
        code="render",
        label="渲染图对焦",
        director="spec_writer",
        crew=("prompt_smith", "image_t2i", "image_i2i"),
        gate_field="gate_render_confirmed_at",
    ),
    Stage(
        code="views",
        label="四视图对焦",
        director="spec_writer",
        crew=("prompt_smith", "image_i2i", "vision_reviewer"),
        gate_field="",
    ),
)
"""角色流程在会话里的三段，数据照 `seeds/workflow_defs.json` 的 `character_v1` 抄。

建模及之后（S6-S9）不进会话，仍走各自的任务接口。
"""

_BY_CODE = {one.code: one for one in STAGES}


def stage(code: str) -> Stage:
    """按 code 取阶段。code 不认识直接 KeyError：调用方只会传本文件里的常量。"""
    return _BY_CODE[code]


def stage_of(character: Character) -> str:
    """这个角色当下在哪一阶段。

    看的是两道人工门禁的既成事实，而不是 `state` 的字面值：门禁确认过就算过了那一段，之后
    无论重生几张图都还在同一段里。`state` 只用来兜住「四视图都确认完了」这种已经走出会话
    范围的情况。
    """
    if character.gate_render_confirmed_at is not None or character_assets.at_least(
        character, character_assets.VIEWS_CONFIRMED
    ):
        return "views"
    if character.gate_spec_confirmed_at is not None:
        return "render"
    return "spec"


def actor_for(conversation: Conversation) -> str:
    """这一轮该由谁说话。

    现在恒是会话行上的主 Agent。将来主 Agent 指派了子 Agent，这一轮的发言人就是那个子
    Agent，由执行器把它的 `agent_code` 写进消息行——`send()` 只管从这里取，不自己判断。
    """
    return conversation.agent_code
