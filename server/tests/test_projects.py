"""项目层：一个目录就是一个项目。

这里盯的是「项目可以放在磁盘任意位置、整份拷走还是同一个项目」这条约束落到底：配置真相在
`project.json`、运行库在项目目录的 `.atelier/` 下、库里只留一张索引表。所以用例大量在
tmp_path 里造目录、搬目录、删目录，看平台是否始终以磁盘为准。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.assets import layout
from atelier.assets import projects as projects_mod
from atelier.db.project_models import Character
from atelier.db.runtime_models import ProjectRegistry
from atelier.db.session import project_session
from atelier.errors import Conflict, NotFound

pytestmark = pytest.mark.usefixtures("projects_root")


def make_character(ref: projects_mod.ProjectRef, name: str) -> Path:
    """像用户那样直接往 characters/ 里拷一个角色目录进去（带 marker 才算角色）。"""
    target = ref.dir / "characters" / name
    target.mkdir(parents=True)
    (target / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    layout.write_model_marker(target, name)
    return target


def registry(session: Session, code: str) -> ProjectRegistry:
    row = session.get(ProjectRegistry, code)
    assert row is not None
    return row


# --------------------------------------------------------------------------- #
# 新建
# --------------------------------------------------------------------------- #


def test_new_project_is_self_contained(session: Session, projects_root: Path) -> None:
    """新建完这个目录里就该什么都不缺：配置、视觉规范、维度目录、自己的运行库。"""
    ref = projects_mod.create_project(session, name="赤瞳系列", code="chitong")

    assert ref.dir == projects_root / "赤瞳系列"
    assert layout.is_project_dir(ref.dir)
    assert ref.db_path == ref.dir / ".atelier" / "project.db"
    assert ref.db_path.is_file()
    assert (layout.data_dir(ref.dir) / ".gitignore").is_file()
    for name in layout.CATEGORY_DIRS:
        assert (ref.dir / name).is_dir()


def test_new_project_lays_out_the_consensus_dirs(session: Session) -> None:
    """记忆与项目级提示词落在目录里、进 Git，所以空着也得铺出来带上 .gitkeep。

    接手的人 clone 下来要看得出这两处是干什么的、可以手写；也要确认它们没被 .gitignore 挡掉。
    """
    ref = projects_mod.create_project(session, name="赤瞳系列", code="chitong")

    for path in (layout.memory_dir(ref.dir), ref.dir / layout.PROMPTS_DIR):
        assert (path / ".gitkeep").is_file()
    ignored = (ref.dir / ".gitignore").read_text(encoding="utf-8")
    assert layout.MEMORY_DIR not in ignored
    assert layout.PROMPTS_DIR not in ignored


def test_new_project_json_carries_the_workflow_state(session: Session) -> None:
    """立项推到哪一步是项目自己的事，跟着 project.json 走而不是留在本机那个可删的库里。"""
    ref = projects_mod.create_project(session, name="赤瞳系列", code="chitong")

    raw = json.loads(layout.project_json_path(ref.dir).read_text(encoding="utf-8"))
    assert raw["state"] == projects_mod.DEFAULT_STATE
    assert projects_mod.read_config(ref.dir).state == projects_mod.DEFAULT_STATE


def test_importing_a_bare_project_dir_gets_its_data_dir(session: Session, tmp_path: Path) -> None:
    """同事拷来的目录里只有素材与配置（`.atelier/` 本来就不进 Git），挂上去要自己补齐。"""
    origin = projects_mod.create_project(session, name="项目", code="p1", dir_path=tmp_path / "a")
    bare = tmp_path / "b"
    shutil.copytree(origin.dir, bare, ignore=shutil.ignore_patterns(".atelier"))
    projects_mod.forget(session, "p1")
    shutil.rmtree(origin.dir)
    assert not layout.data_dir(bare).exists()

    ref = projects_mod.import_project(session, bare)

    assert ref.db_path.is_file()
    assert (layout.data_dir(bare) / ".gitignore").is_file()


def test_new_project_gets_the_art_bible_template_with_its_name(session: Session) -> None:
    ref = projects_mod.create_project(session, name="赤瞳系列", code="chitong")

    content = projects_mod.read_art_bible(ref)
    assert content.startswith("# 赤瞳系列 视觉规范")
    assert "{项目名}" not in content


def test_project_can_live_anywhere_on_disk(session: Session, tmp_path: Path) -> None:
    """项目不必待在仓库里——这是本期架构的要点，指哪儿建哪儿。"""
    elsewhere = tmp_path / "外置盘" / "某个项目"

    ref = projects_mod.create_project(
        session, name="别处的项目", code="elsewhere", dir_path=elsewhere
    )

    assert ref.dir == elsewhere.resolve()
    assert ref.db_path.is_file()
    row = registry(session, "elsewhere")
    assert row.dir_path == str(elsewhere.resolve())
    assert row.managed is False  # 不在默认根下，平台只是挂着它


def test_project_under_default_root_is_marked_managed(session: Session) -> None:
    ref = projects_mod.create_project(session, name="赤瞳系列", code="chitong")

    assert registry(session, ref.code).managed is True


def test_creating_on_an_existing_project_points_at_import(session: Session, tmp_path: Path) -> None:
    """已经是项目的目录不在新建这里悄悄兼容，得走导入，否则会覆盖别人的配置。"""
    ref = projects_mod.create_project(
        session, name="原项目", code="origin", dir_path=tmp_path / "p"
    )

    with pytest.raises(Conflict, match="导入"):
        projects_mod.create_project(session, name="新项目", code="fresh", dir_path=ref.dir)


def test_creating_in_a_dir_that_already_has_files_is_fine(session: Session, tmp_path: Path) -> None:
    """参考图、旧稿往往先丢进目录再立项，为此逼用户另建一个空目录只是添乱。"""
    busy = tmp_path / "已有东西"
    busy.mkdir()
    (busy / "我的照片.png").touch()

    ref = projects_mod.create_project(session, name="入驻", code="busy", dir_path=busy)

    assert ref.dir == busy
    assert (busy / "我的照片.png").is_file()  # 原有的东西一个不动


def test_creating_where_an_art_bible_sits_is_refused(session: Session, tmp_path: Path) -> None:
    """`art-bible.md` 在就说明这块地已归另一个项目，铺下去会盖掉它。"""
    taken = tmp_path / "别人的项目"
    taken.mkdir()
    (taken / "art-bible.md").write_text("# 别人的视觉真相\n", encoding="utf-8")

    with pytest.raises(Conflict, match="art-bible.md"):
        projects_mod.create_project(session, name="占用", code="taken", dir_path=taken)


def test_inspect_dir_reports_what_holds_the_ground(session: Session, tmp_path: Path) -> None:
    """新建前先问一句：占着的是什么、是不是一整个项目，界面靠它拟确认框的词。"""
    ref = projects_mod.create_project(session, name="项目", code="p1", dir_path=tmp_path / "p")

    taken = projects_mod.inspect_dir(ref.dir)
    fresh = projects_mod.inspect_dir(tmp_path / "还不存在")

    assert taken.occupied is True
    assert taken.is_project is True
    assert taken.marks == ("project.json", "art-bible.md")
    assert fresh.occupied is False
    assert fresh.marks == ()


def test_bootstrap_overwrite_swaps_the_identity_and_rebuilds_the_db(
    session: Session, tmp_path: Path
) -> None:
    """覆盖后这块地归新项目：库是重建的（旧角色不跟过来），素材文件一个不动。"""
    old = projects_mod.create_project(session, name="旧项目", code="old", dir_path=tmp_path / "p")
    make_character(old, "旧角色")
    with project_session(old.db_path) as db:
        db.add(Character(id="c1", name="旧角色", dir_name="旧角色"))

    fresh = projects_mod.bootstrap_project(session, old.dir, overwrite=True)

    assert fresh.code.startswith(projects_mod.DRAFT_CODE_PREFIX)
    assert projects_mod.read_config(old.dir).code == fresh.code
    assert fresh.db_path.is_file()  # 库文件删掉了还得能重建（建库的幂等缓存曾把这步跳掉）
    with project_session(fresh.db_path) as db:
        assert db.scalars(select(Character)).all() == []
    assert not (old.dir / "art-bible.md").exists()  # 旧项目的视觉真相让位
    assert (old.dir / "characters" / "旧角色" / "旧角色.md").is_file()  # 用户的文件不动
    assert session.get(ProjectRegistry, "old") is None  # 同一个目录不能挂着两条索引


def test_bootstrap_without_overwrite_keeps_the_old_project(
    session: Session, tmp_path: Path
) -> None:
    """没点过头就不能动别人的东西。"""
    old = projects_mod.create_project(session, name="旧项目", code="old", dir_path=tmp_path / "p")

    with pytest.raises(Conflict, match="导入"):
        projects_mod.bootstrap_project(session, old.dir)

    assert projects_mod.read_config(old.dir).code == "old"


def test_duplicate_code_is_refused(session: Session, tmp_path: Path) -> None:
    projects_mod.create_project(session, name="第一个", code="dup", dir_path=tmp_path / "a")

    with pytest.raises(Conflict, match="代号"):
        projects_mod.create_project(session, name="第二个", code="dup", dir_path=tmp_path / "b")


@pytest.mark.parametrize("code", ["", "  ", "有中文", "a b", "x/y", "c" * 65])
def test_bad_codes_are_refused(session: Session, code: str, tmp_path: Path) -> None:
    """代号会进路径与外部 API 参数，中文虽然能过 `isalnum()` 但不能收。"""
    with pytest.raises(Conflict):
        projects_mod.create_project(session, name="项目", code=code, dir_path=tmp_path / "x")


def test_code_is_normalised_to_lower_case(session: Session, tmp_path: Path) -> None:
    """code 会进路径、进 prompt、进日志，大小写混用等于埋两个身份。"""
    ref = projects_mod.create_project(session, name="项目", code="ChiTong", dir_path=tmp_path / "x")

    assert ref.code == "chitong"
    assert projects_mod.read_config(ref.dir).code == "chitong"


# --------------------------------------------------------------------------- #
# 配置：project.json 是唯一真相
# --------------------------------------------------------------------------- #


def test_two_projects_keep_their_own_config(session: Session) -> None:
    """A5 的验收点之一：两个项目各自独立配置，互不串味。"""
    one = projects_mod.create_project(
        session,
        name="赤瞳系列",
        code="chitong",
        style=projects_mod.ProjectStyle(art_style="国风水墨"),
    )
    two = projects_mod.create_project(
        session,
        name="蒸汽都市",
        code="steam",
        style=projects_mod.ProjectStyle(art_style="蒸汽朋克"),
    )

    config = projects_mod.read_config(one.dir)
    config.defaults.image_size = 1024
    projects_mod.write_config(one.dir, config)

    assert projects_mod.read_config(one.dir).style.art_style == "国风水墨"
    assert projects_mod.read_config(one.dir).defaults.image_size == 1024
    assert projects_mod.read_config(two.dir).style.art_style == "蒸汽朋克"
    assert projects_mod.read_config(two.dir).defaults.image_size == 2048


def test_config_round_trip_keeps_unknown_keys(session: Session) -> None:
    """用户手写进 project.json 的东西，平台不认识也不能丢。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    path = layout.project_json_path(ref.dir)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["我的备注"] = "下周交付"
    raw["style"]["我的风格键"] = "冷色"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    config = projects_mod.read_config(ref.dir)
    config.name = "改了名"
    projects_mod.write_config(ref.dir, config)

    back = json.loads(path.read_text(encoding="utf-8"))
    assert back["我的备注"] == "下周交付"
    assert back["style"]["我的风格键"] == "冷色"
    assert back["name"] == "改了名"


def test_a_broken_config_makes_the_project_unusable(session: Session) -> None:
    """配置读不出来就整个项目不可用，不做「尽力解析」——猜出来的配置会静默画错。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    layout.project_json_path(ref.dir).write_text("{ 这不是 json", encoding="utf-8")

    with pytest.raises(Conflict):
        projects_mod.read_config(ref.dir)


def test_reading_config_of_a_plain_dir_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFound):
        projects_mod.read_config(tmp_path)


def test_rename_only_touches_the_display_name(session: Session) -> None:
    """改名不搬目录：目录一改，所有已存的相对路径都得跟着重算。"""
    ref = projects_mod.create_project(session, name="老名字", code="p1")

    projects_mod.rename_in_registry(session, "p1", "新名字")

    row = registry(session, "p1")
    assert row.name == "新名字"
    assert Path(row.dir_path) == ref.dir


# --------------------------------------------------------------------------- #
# 导入与索引
# --------------------------------------------------------------------------- #


def test_import_picks_up_a_project_from_anywhere(session: Session, tmp_path: Path) -> None:
    """换机器就是这条路径：目录已经在磁盘上，指一下就挂上来。"""
    ref = projects_mod.create_project(session, name="项目", code="p1", dir_path=tmp_path / "src")
    make_character(ref, "chitong_beast")
    projects_mod.scan_characters(ref)
    projects_mod.forget(session, "p1")
    assert session.get(ProjectRegistry, "p1") is None

    again = projects_mod.import_project(session, ref.dir)

    assert again.dir == ref.dir
    assert [row["name"] for row in projects_mod.character_rows(again)] == ["chitong_beast"]


def test_forget_leaves_the_files_alone(session: Session, tmp_path: Path) -> None:
    """项目目录是用户的资产，移出只动索引。"""
    ref = projects_mod.create_project(session, name="项目", code="p1", dir_path=tmp_path / "src")

    projects_mod.forget(session, "p1")

    assert layout.is_project_dir(ref.dir)
    assert ref.db_path.is_file()


def test_forget_closes_the_opened_project(session: Session) -> None:
    ref = projects_mod.create_project(session, name="项目", code="p1")
    projects_mod.open_project(session, ref.code)

    projects_mod.forget(session, "p1")

    assert projects_mod.opened_code() is None
    assert projects_mod.opened(session) is None


def test_forgetting_an_unknown_project_is_not_found(session: Session) -> None:
    with pytest.raises(NotFound):
        projects_mod.forget(session, "nope")


def test_import_needs_a_real_project_dir(session: Session, tmp_path: Path) -> None:
    with pytest.raises(NotFound):
        projects_mod.import_project(session, tmp_path / "不存在")

    plain = tmp_path / "普通目录"
    plain.mkdir()
    with pytest.raises(NotFound):
        projects_mod.import_project(session, plain)


def test_import_refuses_a_code_already_used_elsewhere(session: Session, tmp_path: Path) -> None:
    """两个不同目录顶着同一个 code，只能让用户去改 project.json，平台不替他选。"""
    projects_mod.create_project(session, name="第一个", code="p1", dir_path=tmp_path / "a")
    other = projects_mod.create_project(session, name="第二个", code="p2", dir_path=tmp_path / "b")
    config = projects_mod.read_config(other.dir)
    config.code = "p1"
    projects_mod.write_config(other.dir, config)

    with pytest.raises(Conflict, match="project.json"):
        projects_mod.import_project(session, other.dir)


def test_moving_a_project_dir_is_just_a_reimport(session: Session, tmp_path: Path) -> None:
    """用户在 Finder 里把项目搬走之后，重新导入一次就该认新位置。

    旧索引还顶着同一个 code，但它指的位置已经不是项目了，不该拦住真正的那个项目。
    """
    ref = projects_mod.create_project(session, name="项目", code="p1", dir_path=tmp_path / "old")
    projects_mod.open_project(session, "p1")
    moved = tmp_path / "new"
    shutil.move(str(ref.dir), str(moved))

    with pytest.raises(NotFound):
        projects_mod.resolve(session, "p1")
    assert registry(session, "p1").missing is True

    again = projects_mod.import_project(session, moved)

    assert again.dir == moved.resolve()
    row = registry(session, "p1")
    assert row.missing is False
    assert Path(row.dir_path) == moved.resolve()


def test_a_copied_project_with_a_new_code_keeps_its_assets(
    session: Session, tmp_path: Path
) -> None:
    """「拿那个项目当起点开新项目」= 拷目录 + 改 code，素材应当留着。"""
    origin = projects_mod.create_project(
        session, name="原项目", code="origin", dir_path=tmp_path / "a"
    )
    make_character(origin, "chitong_beast")
    projects_mod.scan_characters(origin)

    clone_dir = tmp_path / "b"
    shutil.copytree(origin.dir, clone_dir)
    config = projects_mod.read_config(clone_dir)
    config.code = "clone"
    config.name = "副本项目"
    projects_mod.write_config(clone_dir, config)

    clone = projects_mod.import_project(session, clone_dir)

    assert [row["name"] for row in projects_mod.character_rows(clone)] == ["chitong_beast"]
    assert [row["name"] for row in projects_mod.character_rows(origin)] == ["chitong_beast"]


def test_registry_drops_a_stale_row_pointing_at_the_same_dir(
    session: Session, tmp_path: Path
) -> None:
    """同一个目录被两个 code 指着，说明有一条索引过期了；以磁盘上的 code 为准。"""
    ref = projects_mod.create_project(session, name="项目", code="old", dir_path=tmp_path / "p")
    config = projects_mod.read_config(ref.dir)
    config.code = "new"
    projects_mod.write_config(ref.dir, config)

    projects_mod.import_project(session, ref.dir)

    assert session.get(ProjectRegistry, "old") is None
    assert session.get(ProjectRegistry, "new") is not None


def test_list_projects_reports_a_missing_directory(session: Session, tmp_path: Path) -> None:
    """外置盘没挂上就是这个状态：列表里还在，但标着不可用。"""
    projects_mod.create_project(session, name="在的", code="here", dir_path=tmp_path / "here")
    gone = projects_mod.create_project(
        session, name="不在的", code="gone", dir_path=tmp_path / "gone"
    )
    shutil.rmtree(gone.dir)

    items = {item.code: item for item in projects_mod.list_projects(session)}

    assert items["here"].missing is False
    assert items["gone"].missing is True


def test_resolve_follows_a_name_changed_on_disk(session: Session, tmp_path: Path) -> None:
    ref = projects_mod.create_project(session, name="老名字", code="p1", dir_path=tmp_path / "p")
    config = projects_mod.read_config(ref.dir)
    config.name = "手改的名字"
    projects_mod.write_config(ref.dir, config)

    resolved = projects_mod.resolve(session, "p1")

    assert resolved.name == "手改的名字"
    assert registry(session, "p1").name == "手改的名字"


def test_sync_default_root_claims_manually_copied_projects(
    session: Session, projects_root: Path, tmp_path: Path
) -> None:
    """用户直接把项目目录拖进默认根，扫一遍就该认领。"""
    ref = projects_mod.create_project(
        session, name="外面的", code="outside", dir_path=tmp_path / "out"
    )
    projects_mod.forget(session, "outside")
    shutil.move(str(ref.dir), str(projects_root / "外面的"))
    (projects_root / "不是项目的目录").mkdir()

    added = projects_mod.sync_default_root(session)

    assert added == ["outside"]
    assert projects_mod.sync_default_root(session) == []  # 再扫不重复认领


def test_opening_another_project_moves_over(session: Session) -> None:
    one = projects_mod.create_project(session, name="第一个", code="p1")
    two = projects_mod.create_project(session, name="第二个", code="p2")

    projects_mod.open_project(session, one.code)
    assert projects_mod.opened(session) == one

    projects_mod.open_project(session, two.code)
    assert projects_mod.opened(session) == two
    assert registry(session, "p2").last_opened_at is not None


def test_opened_is_none_when_the_project_vanished(session: Session, tmp_path: Path) -> None:
    """打开的项目所在的盘拔了，不该让整个应用起不来，只是回到「没打开项目」。"""
    ref = projects_mod.create_project(session, name="项目", code="p1", dir_path=tmp_path / "p")
    projects_mod.open_project(session, ref.code)
    shutil.rmtree(ref.dir)

    assert projects_mod.opened(session) is None


def test_the_opened_project_does_not_outlive_the_process(session: Session) -> None:
    """打开哪个项目不入库：后端重启就是没打开，开工从用户点「打开」开始。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    projects_mod.open_project(session, ref.code)

    projects_mod.close_project()  # 等同于换一个进程

    assert projects_mod.opened_code() is None
    assert projects_mod.resolve(session, "p1").dir == ref.dir  # 项目本身一点不少


# --------------------------------------------------------------------------- #
# art bible
# --------------------------------------------------------------------------- #


def test_a_fresh_art_bible_yields_no_forbidden_terms(session: Session) -> None:
    """模板里全是「待填」，把它送进 negative prompt 等于往每张图上泼一句无意义的中文。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")

    assert projects_mod.forbidden_terms(projects_mod.read_art_bible(ref)) == []


def test_forbidden_section_feeds_the_negative_prompt(session: Session) -> None:
    """A5 的验收点之一：art bible 的禁止项能进 negative。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")

    projects_mod.write_art_bible(
        ref,
        "\n".join(
            [
                "# 项目 视觉规范",
                "## 3 形状语言",
                "- 硬边为主",  # 别的小节的条目不算禁止项
                "## 6 风格禁止项",
                "<!-- 一行一条 -->",
                "- 赛博霓虹",
                "- `低饱和灰调`",
                "- 待填",
                "## 7 参考",
                "- 某部电影",
            ]
        ),
    )

    assert projects_mod.forbidden_terms(projects_mod.read_art_bible(ref)) == [
        "赛博霓虹",
        "低饱和灰调",
    ]


def test_art_bible_can_be_renamed_in_the_config(session: Session) -> None:
    """art_bible 是配置项，用户改了文件名就该跟着走。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    config = projects_mod.read_config(ref.dir)
    config.art_bible = "docs/视觉规范.md"
    projects_mod.write_config(ref.dir, config)
    (ref.dir / "docs").mkdir()

    projects_mod.write_art_bible(ref, "# 换了位置\n")

    assert projects_mod.art_bible_path(ref) == (ref.dir / "docs" / "视觉规范.md").resolve()
    assert projects_mod.read_art_bible(ref) == "# 换了位置\n"


def test_missing_art_bible_reads_as_empty(session: Session) -> None:
    ref = projects_mod.create_project(session, name="项目", code="p1")
    projects_mod.art_bible_path(ref).unlink()

    assert projects_mod.read_art_bible(ref) == ""


# --------------------------------------------------------------------------- #
# 目录扫描：磁盘是素材的真相
# --------------------------------------------------------------------------- #


def test_scan_claims_directories_copied_in_by_hand(session: Session) -> None:
    ref = projects_mod.create_project(session, name="项目", code="p1")
    asset = make_character(ref, "chitong_beast")

    result = projects_mod.scan_characters(ref)

    assert result.added == ["chitong_beast"]
    assert result.total == 1
    row = projects_mod.character_rows(ref)[0]
    assert row["dir_name"] == "characters/chitong_beast"
    assert row["spec_path"] == "characters/chitong_beast/chitong_beast.md"
    for name in layout.ASSET_SUBDIRS:  # 顺手把四个固定子目录补齐
        assert (asset / name).is_dir()


def test_scan_is_idempotent_and_keeps_ids_stable(session: Session) -> None:
    """重扫不能生出第二条记录，也不能换 id——产物路径都挂在这个 id 上。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    make_character(ref, "chitong_beast")

    first = projects_mod.scan_characters(ref)
    ids = [row["id"] for row in projects_mod.character_rows(ref)]
    second = projects_mod.scan_characters(ref)

    assert first.added == ["chitong_beast"]
    assert second.added == []
    assert second.total == 1
    assert [row["id"] for row in projects_mod.character_rows(ref)] == ids


def test_scan_reports_but_never_deletes(session: Session) -> None:
    """库里有磁盘没有：可能只是目录还没从别处拷过来，不能替用户删记录。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    asset = make_character(ref, "chitong_beast")
    projects_mod.scan_characters(ref)
    shutil.rmtree(asset)

    result = projects_mod.scan_characters(ref)

    assert result.missing == ["chitong_beast"]
    assert len(projects_mod.character_rows(ref)) == 1


def test_scan_ignores_hidden_dirs_and_loose_files(session: Session) -> None:
    ref = projects_mod.create_project(session, name="项目", code="p1")
    (ref.dir / "characters" / ".DS_Store").touch()
    (ref.dir / "characters" / ".缓存").mkdir()
    (ref.dir / "characters" / "随手放的.png").touch()

    assert projects_mod.scan_characters(ref).added == []


def test_scan_claims_characters_nested_in_groups(session: Session) -> None:
    """角色按文件夹分层，层级任意深：扫描递归按 marker 认领，不把中间的分组文件夹当角色。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    boss = ref.dir / "characters" / "boss角色" / "赤瞳"
    boss.mkdir(parents=True)
    layout.write_model_marker(boss, "赤瞳")
    (ref.dir / "characters" / "玩家角色").mkdir()  # 空分组，不应被当角色

    result = projects_mod.scan_characters(ref)

    assert result.added == ["赤瞳"]
    row = projects_mod.character_rows(ref)[0]
    assert row["dir_name"] == "characters/boss角色/赤瞳"


def test_list_groups_reads_folders_from_disk(session: Session) -> None:
    """分组只是文件夹，含空分组；角色目录与 asset 子目录不算分组。"""
    ref = projects_mod.create_project(session, name="项目", code="p1")
    hero = ref.dir / "characters" / "玩家角色" / "赤瞳"
    hero.mkdir(parents=True)
    layout.ensure_asset_dirs(hero)
    layout.write_model_marker(hero, "赤瞳")
    (ref.dir / "characters" / "boss角色" / "精英").mkdir(parents=True)  # 多级空分组

    assert projects_mod.list_groups(ref) == ["boss角色", "boss角色/精英", "玩家角色"]


def test_create_group_makes_an_empty_folder(session: Session) -> None:
    ref = projects_mod.create_project(session, name="项目", code="p1")

    rel = projects_mod.create_group(ref, "boss角色/精英")

    assert rel == "boss角色/精英"
    assert (ref.dir / "characters" / "boss角色" / "精英").is_dir()
    assert projects_mod.list_groups(ref) == ["boss角色", "boss角色/精英"]


def test_assets_are_isolated_between_projects(session: Session) -> None:
    """A5 的验收点之一：切项目后素材列表隔离——两个项目根本不是同一个库。"""
    one = projects_mod.create_project(session, name="第一个", code="p1")
    two = projects_mod.create_project(session, name="第二个", code="p2")
    make_character(one, "chitong_beast")
    make_character(two, "steam_golem")
    projects_mod.scan_characters(one)
    projects_mod.scan_characters(two)

    assert [row["name"] for row in projects_mod.character_rows(one)] == ["chitong_beast"]
    assert [row["name"] for row in projects_mod.character_rows(two)] == ["steam_golem"]

    with project_session(one.db_path) as db:
        assert db.scalars(select(Character.name)).all() == ["chitong_beast"]
