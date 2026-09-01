"""角色工作流的状态机与门禁。

状态是这条流水线的唯一次序凭据：每一步都得说清「凭什么现在能做这一步」。所以状态推进只走
这里的函数，谁都不直接改 `characters.state`——散落的赋值最后会让「过了门禁没有」这件事说不
清楚。

两道人工门禁（设定、渲染图）不由 Agent 决定。`spec_reviewer` 的 `APPROVE` 只是「审校没发
现问题」，放行仍要人按一下：自动裁决替人拍板，等于把责任推给一个看不见全局的模型。

门禁与状态推进都往 `task_events` 写一条，人工的选择与理由一并记下。事后要回答「这份定稿当
时凭什么过的」，只有这条时间线答得上。
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from atelier.assets import archive, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import (
    ArtifactDraft,
    Character,
    Conversation,
    Generation,
    Message,
    Task,
    TaskEvent,
    TaskStep,
)
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict, NotFound

_log = structlog.get_logger(__name__)

STATES: tuple[tuple[str, str], ...] = (
    ("S0_spec_drafting", "设定对焦中"),
    ("S1_spec_confirmed", "设定已确认"),
    ("S2_render_generated", "渲染图已生成"),
    ("S3_render_confirmed", "渲染图已定稿"),
    ("S4_views_generated", "四视图已生成"),
    ("S5_views_confirmed", "四视图已确认"),
    ("S6_model_generated", "模型已生成"),
    ("S7_rigged", "已绑骨"),
    ("S8_animated", "已出动作"),
    ("S9_archived", "已归档"),
)

SPEC_DRAFTING = STATES[0][0]
SPEC_CONFIRMED = STATES[1][0]
RENDER_GENERATED = STATES[2][0]
RENDER_CONFIRMED = STATES[3][0]
VIEWS_GENERATED = STATES[4][0]
VIEWS_CONFIRMED = STATES[5][0]

SPEC_SUFFIX = "角色设定.md"
RENDER_SUFFIX = "渲染图"
RENDER_DIR = "images"
VIEWS_DIR = RENDER_DIR
ASSET_CATEGORY = "character"
"""文件名的类别段。定稿名统一是 `{类别}_{角色}_{变体}.{ext}`，四张视图的名字由平台拼
（卡片只给了渲染图那一张的文件名），所以类别段得在代码里有一个口径。"""

_ORDER = {code: index for index, (code, _) in enumerate(STATES)}
_LABELS = dict(STATES)


def rank(state: str) -> int:
    """状态在流水线上的位次。不认识的状态是数据坏了，不能当成「还没开始」放过去。"""
    if state not in _ORDER:
        raise Conflict(f"角色状态 {state!r} 不是平台认识的状态")
    return _ORDER[state]


def label(state: str) -> str:
    return _LABELS.get(state, state)


def at_least(character: Character, state: str) -> bool:
    return rank(character.state) >= rank(state)


def require_state(character: Character, minimum: str, *, action: str) -> None:
    """门禁守卫：没到该到的状态就把这一步拦下来（API 层变成 409）。

    话说给人听：只报「现在是哪一步、还差哪一步」，用户据此知道该先去按哪个按钮，而不是收到
    一句状态码。
    """
    if at_least(character, minimum):
        return
    raise Conflict(
        f"{character.name} 现在是「{label(character.state)}」，"
        f"要到「{label(minimum)}」之后才能{action}"
    )


# --------------------------------------------------------------------------- #
# 项目就绪（P1）
# --------------------------------------------------------------------------- #


def project_gaps(ref: ProjectRef) -> list[str]:
    """项目还差什么才算能开工做角色。

    只卡住「art bible 完全没写」这一档，不卡「写得不全」：角色设定要以 art bible 为风格锚
    点，一份空文档做锚点等于没有锚点，后面每张图都会跑偏。但缺一两节还能一边做一边补，卡
    死它就是拿平台的洁癖挡住用户干活。
    """
    text = projects.read_art_bible(ref)
    gaps = projects.art_bible_gaps(text)
    if not text.strip():
        return ["项目的 art bible 还是空的，先跟设计师聊出视觉规范再建角色"]
    if len(gaps) == len(projects.ART_BIBLE_SECTIONS):
        return ["项目的 art bible 还是模板原样，先跟设计师聊出视觉规范再建角色"]
    return []


def require_project_ready(ref: ProjectRef) -> None:
    gaps = project_gaps(ref)
    if gaps:
        raise Conflict(gaps[0])


# --------------------------------------------------------------------------- #
# 增删查
# --------------------------------------------------------------------------- #


def get(project: Session, character_id: str) -> Character:
    character = project.get(Character, character_id)
    if character is None:
        raise NotFound(f"角色 {character_id} 不存在")
    return character


def by_name(project: Session, name: str) -> Character | None:
    return project.scalar(select(Character).where(Character.name == name))


def spec_target(character: Character) -> str:
    """设定文档该往哪儿落。

    已经沉过一次就认 `spec_path`：用户可能把文件改名或携到子目录了，拿默认名覆回去等
    于在旁边另开一份，两份设定同时存在时没人说得清后续的图按的是哪份。
    """
    # dir_name 已经是相对项目目录的路径（`characters/赤瞳`），别再补一层维度目录
    return character.spec_path or f"{character.dir_name}/{character.name}{SPEC_SUFFIX}"


def create(
    project: Session,
    ref: ProjectRef,
    name: str,
    group: str = "",
    overwrite: bool = False,
) -> Character:
    """建一个角色：铺目录、写 marker、登记库行，状态从 S0 起步。

    角色按磁盘文件夹分层组织（`group` 可多级），身份是 `.model.json` 中的随机 ID；目录路径
    `dir_name` 只表示当前位置。同一分组内同名会占用同一目录，跨分组允许同名。目录名过
    `safe_dir_name`，库里的 `name` 留用户写的原文。

    目标目录已存在时：`overwrite` 为假就拒；为真就删旧目录（含已生成素材）及该目录下的旧
    记录再重建。每次新建都生成随机 ID，并写入 `.model.json` 作为不随目录移动的身份。
    """
    display = name.strip()
    if not display:
        raise Conflict("角色名不能为空")
    require_project_ready(ref)

    group = layout.safe_rel_path(group)
    seg = layout.safe_dir_name(display)
    dir_name = "/".join(["characters", *([group] if group else []), seg])
    asset_dir = ref.dir / dir_name

    if asset_dir.exists():
        if not overwrite:
            raise Conflict(f"角色「{display}」在该分组已存在")
        _remove_existing(project, ref, dir_name)

    character_id = projects.new_asset_id()
    layout.ensure_asset_dirs(asset_dir)
    layout.write_model_marker(asset_dir, display, character_id)

    character = Character(id=character_id, name=display, dir_name=dir_name)
    project.add(character)
    record_event(
        project,
        character.id,
        "character_created",
        f"建角色 {display}",
        {"dir_name": dir_name, "state": character.state},
    )
    project.commit()
    _log.info("character_created", id=character.id, name=display, dir=dir_name)
    return character


def _delete_records(project: Session, character: Character) -> None:
    """清掉角色及其过程数据。"""
    conversation_ids = list(
        project.scalars(
            select(Conversation.id).where(
                Conversation.target_kind == "character",
                Conversation.target_ref == character.id,
            )
        )
    )
    if conversation_ids:
        project.execute(
            delete(ArtifactDraft).where(ArtifactDraft.conversation_id.in_(conversation_ids))
        )
        project.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        project.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))

    task_ids = list(
        project.scalars(
            select(Task.id).where(Task.target_kind == "character", Task.target_ref == character.id)
        )
    )
    if task_ids:
        project.execute(delete(TaskStep).where(TaskStep.task_id.in_(task_ids)))
        project.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)))
        project.execute(delete(Task).where(Task.id.in_(task_ids)))

    project.execute(
        delete(Generation).where(
            Generation.target_kind == "character", Generation.target_ref == character.id
        )
    )
    project.execute(delete(TaskEvent).where(TaskEvent.task_id == character.id))
    project.delete(character)
    project.flush()


def remove_missing(project: Session, ref: ProjectRef, character_id: str) -> None:
    """删除 marker 已不存在或已经指向其他身份的角色记录。"""
    character = get(project, character_id)
    marker = layout.read_model_marker(ref.absolute(character.dir_name))
    marker_id = layout.model_marker_id(marker)
    if marker_id is None and layout.is_character_dir(ref.absolute(character.dir_name)):
        raise Conflict(f"{character.name} 的角色目录仍存在，不能删除数据库记录")
    if marker_id == character.id:
        raise Conflict(f"{character.name} 的角色目录仍存在，不能删除数据库记录")
    _delete_records(project, character)
    project.commit()
    _log.info("missing_character_removed", id=character_id, dir=character.dir_name)


def _remove_existing(project: Session, ref: ProjectRef, dir_name: str) -> None:
    """覆盖时把旧角色目录、库行及关联过程数据一并清掉，不单独提交。"""
    asset_dir = ref.dir / dir_name
    if asset_dir.exists():
        shutil.rmtree(asset_dir)
    stale = list(project.scalars(select(Character).where(Character.dir_name == dir_name)))
    for character in stale:
        _delete_records(project, character)


# --------------------------------------------------------------------------- #
# 门禁 1：设定确认
# --------------------------------------------------------------------------- #


def confirm_spec(
    project: Session,
    ref: ProjectRef,
    character: Character,
    *,
    note: str = "",
) -> Character:
    """人工确认设定，推到 S1。

    要求设定文档已经沉淀过：门禁确认的是磁盘上那一份，库里躺着的草稿不算——`spec_path` 空
    着就说明用户还没按过「确认沉淀」，这时候放行会让后续每一步都拿不到设定原文。
    """
    if character.spec_path is None:
        raise Conflict(f"{character.name} 还没有沉淀设定文档，先在设定会话里确认沉淀")
    spec = ref.absolute(character.spec_path)
    if not spec.is_file():
        raise Conflict(f"设定文档 {character.spec_path} 不在磁盘上了，重新沉淀一份再确认")
    if at_least(character, SPEC_CONFIRMED):
        raise Conflict(f"{character.name} 的设定已经确认过了")

    character.state = SPEC_CONFIRMED
    character.gate_spec_confirmed_at = datetime.now(UTC)
    record_event(
        project,
        character.id,
        "gate_spec_confirmed",
        note or f"人工确认 {character.name} 的设定",
        {
            "spec_path": character.spec_path,
            "state": character.state,
            "note": note,
            "hard_constraints": len(hard_constraints(character)),
        },
    )
    sync_meta(ref, character)
    project.commit()
    _log.info("gate_spec_confirmed", id=character.id, spec=character.spec_path)
    return character


def reject_spec(project: Session, character: Character, *, note: str) -> Character:
    """门禁驳回：状态停在原地，理由记下来。

    不改状态是要点——驳回不是一个新阶段，是「这一步还没过」。把理由写进事件，下一轮设定会
    话能看见上次为什么没过。
    """
    reason = note.strip()
    if not reason:
        raise Conflict("驳回要写清哪里不行，否则下一轮改不动")
    record_event(
        project,
        character.id,
        "gate_spec_rejected",
        reason,
        {"state": character.state, "spec_path": character.spec_path},
        level="warning",
    )
    project.commit()
    _log.info("gate_spec_rejected", id=character.id)
    return character


# --------------------------------------------------------------------------- #
# 门禁 2：渲染图定稿
# --------------------------------------------------------------------------- #


def render_target(character: Character, file_name: str = "") -> str:
    """渲染图的定稿位。

    优先用卡片里给的文件名——卡片是这一步的规格，文件名也是规格的一部分，平台另起一个名字
    会让卡片与磁盘对不上。卡片没给就退回默认名，图总得有地方落。
    """
    stem = file_name.strip() or f"{character.name}_{RENDER_SUFFIX}.png"
    return f"{character.dir_name}/{RENDER_DIR}/{layout.safe_dir_name(stem)}"


def confirm_render(
    project: Session,
    ref: ProjectRef,
    character: Character,
    *,
    render_path: str,
    note: str = "",
) -> Character:
    """人工采用渲染图，推到 S3。

    校的是磁盘上确实有这张图：门禁确认的是「我看到的这一张」，文件不在就说明归档那一步没
    成，此时放行会让四视图拿着一个不存在的参考图开工。
    """
    require_state(character, RENDER_GENERATED, action="定稿渲染图")
    if at_least(character, RENDER_CONFIRMED):
        raise Conflict(f"{character.name} 的渲染图已经定稿过了")
    if not render_path:
        raise Conflict("没有指定要采用哪张渲染图")
    if not ref.absolute(render_path).is_file():
        raise Conflict(f"渲染图 {render_path} 不在磁盘上了，重新生成一张再定稿")

    character.state = RENDER_CONFIRMED
    character.gate_render_confirmed_at = datetime.now(UTC)
    character.render_path = render_path
    record_event(
        project,
        character.id,
        "gate_render_confirmed",
        note or f"人工采用 {character.name} 的渲染图",
        {"render_path": render_path, "state": character.state, "note": note},
    )
    sync_meta(ref, character)
    project.commit()
    _log.info("gate_render_confirmed", id=character.id, render=render_path)
    return character


def reject_render(project: Session, character: Character, *, note: str) -> Character:
    """渲染图驳回：状态停在 S2，理由记下来给下一轮重生用。"""
    reason = note.strip()
    if not reason:
        raise Conflict("驳回要写清哪里不行，否则下一轮改不动")
    record_event(
        project,
        character.id,
        "gate_render_rejected",
        reason,
        {"state": character.state},
        level="warning",
    )
    project.commit()
    _log.info("gate_render_rejected", id=character.id)
    return character


# --------------------------------------------------------------------------- #
# S4/S5：四视图
# --------------------------------------------------------------------------- #


def views_target(character: Character, variant: str, suffix: str = ".png") -> str:
    """某一个视角的定稿位。

    名字里不带版本也不带时间戳：下游引用的是「这个角色的正面图」，带版本号的话每次换定稿
    都要改一遍引用。想知道它是哪一版，看 `meta.json` 里这一条的 `source_path`。
    """
    stem = f"{ASSET_CATEGORY}_{character.name}_{variant}{suffix}"
    return f"{character.dir_name}/{VIEWS_DIR}/{layout.safe_dir_name(stem)}"


def view_paths(character: Character) -> dict[str, str]:
    """已定稿的四张视图：`{变体: 相对路径}`。还没定稿就是空字典。

    台账里每个变体那一行 `is_final` 才是原始事实，这里存的是结论副本——理由跟 `render_path`
    一样：建模、绑骨每一步都要拿它当输入，每次去台账里筛一遍不如把结论存在角色行上。
    """
    stored = character.params.get("views") if character.params else None
    if not isinstance(stored, dict):
        return {}
    return {str(key): str(value) for key, value in stored.items() if isinstance(value, str)}


def confirm_views(
    project: Session,
    ref: ProjectRef,
    character: Character,
    *,
    paths: Mapping[str, str],
    note: str = "",
) -> Character:
    """四张视图定稿，推到 S5。

    这一步不是第三道人工门禁，而是「人选输入」：建模只吃定稿位上的那四张，所以得有人指明让
    哪几张上位。四个视角一个都不能少：缺一张就会让建模拿三张去猜第四个面，而猜出来的那一
    面要到绑骨之后才看得出不对。
    """
    require_state(character, VIEWS_GENERATED, action="定稿四视图")
    if at_least(character, VIEWS_CONFIRMED):
        raise Conflict(f"{character.name} 的四视图已经定稿过了")
    if not paths:
        raise Conflict("没有指定要采用哪几张视图")
    missing = [relative for relative in paths.values() if not ref.absolute(relative).is_file()]
    if missing:
        raise Conflict(f"视图 {missing[0]} 不在磁盘上了，重新生一批再定稿")

    character.state = VIEWS_CONFIRMED
    character.params = {**character.params, "views": dict(paths)}
    record_event(
        project,
        character.id,
        "views_confirmed",
        note or f"人工采用 {character.name} 的四视图",
        {"views": dict(paths), "state": character.state, "note": note},
    )
    sync_meta(ref, character)
    project.commit()
    _log.info("views_confirmed", id=character.id, views=len(paths))
    return character


def advance(project: Session, ref: ProjectRef, character: Character, state: str) -> Character:
    """把状态推到下一步。只允许往前，且只能一步一步走。

    往回退与跳级都拒掉：状态是后续每一步的凭据，能任意改写的凭据等于没有凭据。要重做某一步
    就重跑那一步（它自己会把产物换掉），不是把状态拨回去。
    """
    target = rank(state)
    current = rank(character.state)
    if target <= current:
        raise Conflict(
            f"{character.name} 已经是「{label(character.state)}」，不能退回「{label(state)}」"
        )
    if target > current + 1:
        raise Conflict(f"从「{label(character.state)}」跳到「{label(state)}」中间还有步骤没做")

    character.state = state
    record_event(
        project,
        character.id,
        "state_advanced",
        f"{character.name} 进入「{label(state)}」",
        {"state": state},
    )
    sync_meta(ref, character)
    project.commit()
    return character


# --------------------------------------------------------------------------- #
# meta.json 双写
# --------------------------------------------------------------------------- #


def _moment(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def hard_constraints(character: Character) -> list[dict[str, str]]:
    """硬性约束清单（`spec_reviewer` 抽出来的可数项）。

    开成公共的：四视图的背面图要把附属结构的数量强制注入 prompt，拿的就是这份清单。
    """
    items = character.hard_constraints.get("items") if character.hard_constraints else None
    if not isinstance(items, list):
        return []
    return [
        {str(key): str(value) for key, value in one.items()}
        for one in items
        if isinstance(one, dict)
    ]


def meta_path(ref: ProjectRef, character: Character) -> Path:
    return ref.absolute(character.dir_name) / archive.META_JSON


def sync_meta(ref: ProjectRef, character: Character) -> None:
    """状态与门禁时间写进素材目录的 `meta.json`。

    库是可查询副本，目录是真相：项目目录整体拷到另一台机器时库也跟着走，但断电或库损坏后
    要能从 `meta.json` 认回「这个角色走到哪一步了」。
    """
    snapshot: dict[str, Any] = {
        "id": character.id,
        "name": character.name,
        "state": character.state,
        "spec_path": character.spec_path,
        "render_path": character.render_path,
        "views": view_paths(character),
        "gate_spec_confirmed_at": _moment(character.gate_spec_confirmed_at),
        "gate_render_confirmed_at": _moment(character.gate_render_confirmed_at),
        "hard_constraints": hard_constraints(character),
    }
    archive.merge_meta(meta_path(ref, character), {"character": snapshot})
