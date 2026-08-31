"""角色状态机与门禁 1。

状态是整条流水线的次序凭据：能任意改写的凭据等于没有凭据，所以这里把「不许退回、不许跳
级、未过门禁不许往下」这几条钉住。

门禁确认的是磁盘上那一份设定，不是库里的草稿——放行一次，后面每张图都拿这份当标准。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from atelier.assets import characters, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db import task_events
from atelier.errors import Conflict, NotFound

PARTIAL_BIBLE = """# 视觉规范

## 1 视觉身份一句话
冷光金属 + 赛博霓虹。

## 2 氛围与光照

## 3 形状语言

## 4 色彩系统

## 5 资产标准

## 6 风格禁止项
"""


def ready(ref: ProjectRef) -> None:
    """把项目推到能建角色的档：art bible 至少有一节写了东西。"""
    projects.write_art_bible(ref, PARTIAL_BIBLE)


def make(project_db: Session, ref: ProjectRef, name: str = "赤瞳") -> characters.Character:
    ready(ref)
    return characters.create(project_db, ref, name)


def spec_on_disk(ref: ProjectRef, character: characters.Character) -> Path:
    """沉一份设定文档到磁盘并挂到库行上，模拟用户按过「确认沉淀」。"""
    relative = f"{character.dir_name}/{character.name}角色设定.md"
    path = ref.absolute(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {character.name}\n双尾、红瞳。\n", encoding="utf-8")
    character.spec_path = relative
    return path


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


def test_不认识的状态是数据坏了而不是还没开始() -> None:
    with pytest.raises(Conflict, match="不是平台认识的状态"):
        characters.rank("S99_随手写的")


def test_状态只能往前一步一步走(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character)
    characters.confirm_spec(project_db, project, character)

    with pytest.raises(Conflict, match="中间还有步骤没做"):
        characters.advance(project_db, project, character, "S4_views_generated")
    with pytest.raises(Conflict, match="不能退回"):
        characters.advance(project_db, project, character, characters.SPEC_DRAFTING)

    characters.advance(project_db, project, character, "S2_render_generated")
    assert character.state == "S2_render_generated"


def test_没到该到的状态就把这一步拦下来(project: ProjectRef, project_db: Session) -> None:
    """API 层把它变成 409：用户看到的是「还差哪一步」，不是一个状态码。"""
    character = make(project_db, project)

    with pytest.raises(Conflict, match="设定对焦中.*设定已确认.*才能出渲染图"):
        characters.require_state(character, characters.SPEC_CONFIRMED, action="出渲染图")

    spec_on_disk(project, character)
    characters.confirm_spec(project_db, project, character)
    characters.require_state(character, characters.SPEC_CONFIRMED, action="出渲染图")


# --------------------------------------------------------------------------- #
# P1 守卫
# --------------------------------------------------------------------------- #


def test_art_bible_没写过就不给建角色(project: ProjectRef, project_db: Session) -> None:
    """角色设定拿 art bible 当风格锚点，空文档做锚点等于没有锚点，后面每张图都会跑偏。"""
    projects.write_art_bible(project, "")

    with pytest.raises(Conflict, match="还是空的"):
        characters.create(project_db, project, "赤瞳")


def test_模板原样也算没写(project: ProjectRef, project_db: Session) -> None:
    """新建项目落下来的就是模板；满篇「待填」会被原样拼进每一张图的 prompt。"""
    with pytest.raises(Conflict, match="模板原样"):
        characters.create(project_db, project, "赤瞳")


def test_写了一节就放行(project: ProjectRef) -> None:
    """缺几节能一边做一边补，卡死它是拿平台的洁癖挡住用户干活。"""
    ready(project)

    assert characters.project_gaps(project) == []


# --------------------------------------------------------------------------- #
# 建角色
# --------------------------------------------------------------------------- #


def test_建角色铺好四个子目录并记一条事件(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project, "赤瞳双尾兽")

    asset_dir = project.absolute(character.dir_name)
    assert sorted(one.name for one in asset_dir.iterdir() if one.is_dir()) == [
        "animations",
        "images",
        "models",
        "tmp",
    ]
    assert character.state == characters.SPEC_DRAFTING
    assert character.name == "赤瞳双尾兽"
    assert [one.event for one in task_events.history(project_db, character.id)] == [
        "character_created"
    ]


def test_同组重名默认不覆盖(project: ProjectRef, project_db: Session) -> None:
    """同一分组（同一父目录）内同名才算重名，非覆盖就拦下来。"""
    make(project_db, project)

    with pytest.raises(Conflict, match="在该分组已存在"):
        characters.create(project_db, project, "赤瞳")


def test_跨分组允许同名(project: ProjectRef, project_db: Session) -> None:
    """角色身份是相对路径，不同分组下同名各自成立。"""
    ready(project)
    hero = characters.create(project_db, project, "赤瞳", group="玩家角色")
    boss = characters.create(project_db, project, "赤瞳", group="boss角色")

    assert hero.dir_name == "characters/玩家角色/赤瞳"
    assert boss.dir_name == "characters/boss角色/赤瞳"
    assert hero.id != boss.id


def test_建在分组下并写了marker(project: ProjectRef, project_db: Session) -> None:
    """分组可多级；建角色后目录里落 `.model.json`，扫描据此认它是角色。"""
    ready(project)
    character = characters.create(project_db, project, "赤瞳", group="boss角色/精英")

    assert character.dir_name == "characters/boss角色/精英/赤瞳"
    asset_dir = project.absolute(character.dir_name)
    assert layout.is_character_dir(asset_dir)
    assert layout.read_model_marker(asset_dir)["name"] == "赤瞳"


def test_覆盖删旧重建_id不变且旧事件清掉(project: ProjectRef, project_db: Session) -> None:
    """覆盖 = 删旧目录（含素材）+ 删旧库行与其 task_events 再重建；dir_name 不变故 id 不变。"""
    ready(project)
    old = characters.create(project_db, project, "赤瞳")
    old_id = old.id
    (project.absolute(old.dir_name) / "images" / "旧图.png").write_bytes(b"old")
    task_events.record(project_db, old.id, "随手一条", "旧事件")
    project_db.commit()

    fresh = characters.create(project_db, project, "赤瞳", overwrite=True)

    assert fresh.id == old_id  # dir_name 没变，id 由它派生
    assert not (project.absolute(fresh.dir_name) / "images" / "旧图.png").exists()
    assert [one.event for one in task_events.history(project_db, fresh.id)] == [
        "character_created"
    ]


def test_取不到的角色是找不到而不是空(project_db: Session) -> None:
    with pytest.raises(NotFound):
        characters.get(project_db, "不存在的-id")


# --------------------------------------------------------------------------- #
# 门禁 1
# --------------------------------------------------------------------------- #


def test_没沉淀过设定就确认不了(project: ProjectRef, project_db: Session) -> None:
    """库里躺着的草稿不算：放行会让后续每一步都拿不到设定原文。"""
    character = make(project_db, project)

    with pytest.raises(Conflict, match="还没有沉淀设定文档"):
        characters.confirm_spec(project_db, project, character)


def test_设定文档不在磁盘上就确认不了(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character).unlink()

    with pytest.raises(Conflict, match="不在磁盘上了"):
        characters.confirm_spec(project_db, project, character)


def test_确认过一次就不再确认(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character)
    characters.confirm_spec(project_db, project, character)

    with pytest.raises(Conflict, match="已经确认过了"):
        characters.confirm_spec(project_db, project, character)


def test_确认后状态时间与台账一起落下来(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character)

    characters.confirm_spec(project_db, project, character, note="按用户的意思放行")

    assert character.state == characters.SPEC_CONFIRMED
    assert character.gate_spec_confirmed_at is not None
    meta = json.loads(characters.meta_path(project, character).read_text(encoding="utf-8"))
    assert meta["character"]["state"] == characters.SPEC_CONFIRMED
    assert meta["character"]["spec_path"] == character.spec_path
    assert meta["character"]["gate_spec_confirmed_at"]
    event = task_events.history(project_db, character.id)[-1]
    assert event.event == "gate_spec_confirmed"
    assert event.message == "按用户的意思放行"


def test_写状态不会抹掉台账里的沉淀历史(project: ProjectRef, project_db: Session) -> None:
    """同一份 meta.json 里同时记着沉淀历史与角色状态，写状态的一方不该踩掉另一边。"""
    character = make(project_db, project)
    path = characters.meta_path(project, character)
    path.write_text(
        json.dumps({"artifacts": [{"target_path": "旧的.md"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    spec_on_disk(project, character)

    characters.confirm_spec(project_db, project, character)

    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["artifacts"] == [{"target_path": "旧的.md"}]
    assert meta["character"]["state"] == characters.SPEC_CONFIRMED


def test_驳回不动状态只留理由(project: ProjectRef, project_db: Session) -> None:
    """驳回不是一个新阶段，是「这一步还没过」；理由留给下一轮设定会话看。"""
    character = make(project_db, project)
    spec_on_disk(project, character)

    characters.reject_spec(project_db, character, note="腹部配色跟 art bible 的冷色约束冲突")

    assert character.state == characters.SPEC_DRAFTING
    assert character.gate_spec_confirmed_at is None
    event = task_events.history(project_db, character.id)[-1]
    assert event.event == "gate_spec_rejected"
    assert event.level == "warning"
    assert "冷色约束" in event.message


def test_驳回不写理由等于下一轮改不动(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project)

    with pytest.raises(Conflict, match="要写清哪里不行"):
        characters.reject_spec(project_db, character, note="   ")
