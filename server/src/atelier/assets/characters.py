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

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.assets import archive, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character
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

SPEC_SUFFIX = "角色设定.md"

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


def create(project: Session, ref: ProjectRef, name: str) -> Character:
    """建一个角色：铺目录、登记库行，状态从 S0 起步。

    目录名过一遍 `safe_dir_name`，库里的 `name` 留用户写的原文——用户认的是「赤瞳双尾兽」，
    路径合不合法是平台自己的事。
    """
    display = name.strip()
    if not display:
        raise Conflict("角色名不能为空")
    require_project_ready(ref)
    if by_name(project, display) is not None:
        raise Conflict(f"角色「{display}」已经有了")

    dir_name = f"characters/{layout.safe_dir_name(display)}"
    asset_dir = ref.dir / dir_name
    if asset_dir.exists():
        raise Conflict(f"目录 {dir_name} 已经在磁盘上了，用扫描把它认领进来")
    layout.ensure_asset_dirs(asset_dir)

    character = Character(id=projects.asset_id(ref.code, dir_name), name=display, dir_name=dir_name)
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
            "hard_constraints": len(_constraint_list(character)),
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


def _constraint_list(character: Character) -> list[dict[str, str]]:
    items = character.hard_constraints.get("items") if character.hard_constraints else None
    return [one for one in items if isinstance(one, dict)] if isinstance(items, list) else []


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
        "gate_spec_confirmed_at": (
            character.gate_spec_confirmed_at.isoformat()
            if character.gate_spec_confirmed_at is not None
            else None
        ),
        "hard_constraints": _constraint_list(character),
    }
    archive.merge_meta(meta_path(ref, character), {"character": snapshot})
