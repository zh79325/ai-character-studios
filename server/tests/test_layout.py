"""磁盘布局约定。

这层没有数据库，全是路径规则，但它是「项目能整份拷到别处」的地基：项目内引用一律存相对
路径、还原时不许越出项目目录。所以这里盯的主要是边界情况，不是 happy path。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atelier.assets import layout


def test_project_dir_is_recognised_by_its_config_file(tmp_path: Path) -> None:
    """「目录里有 project.json 就是项目」是全局唯一的判定，导入功能整个建在它上面。"""
    assert not layout.is_project_dir(tmp_path)
    layout.project_json_path(tmp_path).write_text("{}", encoding="utf-8")
    assert layout.is_project_dir(tmp_path)


def test_project_db_always_sits_inside_the_project(tmp_path: Path) -> None:
    """项目库跟着项目目录走，不在仓库里，也不在用户主目录里。"""
    assert layout.project_db_path(tmp_path) == tmp_path / ".atelier" / "project.db"


@pytest.mark.parametrize("name", ["赤瞳系列", "chitong-2", "带 空格 的名字"])
def test_safe_dir_name_keeps_what_it_gets(name: str) -> None:
    """只校验不改写：偷偷换字符会让用户看到的名字和磁盘上的对不上。"""
    assert layout.safe_dir_name(f"  {name}  ") == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "..",
        ".atelier",  # 会和项目自己的数据目录抢位置
        ".hidden",
        "a/b",
        "c:d",
        "问号?",
        "x" * 101,
    ],
)
def test_safe_dir_name_rejects_what_cannot_be_a_directory(name: str) -> None:
    with pytest.raises(layout.LayoutError):
        layout.safe_dir_name(name)


def test_relative_to_stores_posix_style_paths(tmp_path: Path) -> None:
    """存库统一用 / 分隔：同一个项目目录在 Windows 上挂起来也得能对上。"""
    target = tmp_path / "characters" / "chitong_beast" / "images" / "front.png"
    target.parent.mkdir(parents=True)
    target.touch()

    assert layout.relative_to(tmp_path, target) == "characters/chitong_beast/images/front.png"


def test_relative_to_refuses_paths_outside_the_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "别人的目录"
    outside.mkdir(exist_ok=True)

    with pytest.raises(layout.LayoutError):
        layout.relative_to(tmp_path, outside)


def test_resolve_inside_round_trips_a_stored_path(tmp_path: Path) -> None:
    target = tmp_path / "characters" / "chitong_beast"
    target.mkdir(parents=True)

    stored = layout.relative_to(tmp_path, target)
    assert layout.resolve_inside(tmp_path, stored) == target.resolve()


@pytest.mark.parametrize("relative", ["", "../secrets.txt", "characters/../../etc/passwd"])
def test_resolve_inside_blocks_traversal(tmp_path: Path, relative: str) -> None:
    """库里的相对路径将来会被前端和 Agent 写，穿越必须在这一层拦死。"""
    with pytest.raises(layout.LayoutError):
        layout.resolve_inside(tmp_path, relative)


def test_data_dir_ignores_itself(tmp_path: Path) -> None:
    """用户常把项目目录纳进自己的 Git，运行库不该跟着进去，所以项目自带这条忽略。"""
    data = layout.ensure_data_dir(tmp_path)

    assert data.is_dir()
    rules = (data / ".gitignore").read_text(encoding="utf-8").split()
    # 只写 `*` 的话规则文件把自己也忽略了，用户推给同事那边就没有这条忽略
    assert "*" in rules and "!.gitignore" in rules


def test_ensure_data_dir_does_not_touch_an_existing_gitignore(tmp_path: Path) -> None:
    layout.ensure_data_dir(tmp_path)
    gitignore = layout.data_dir(tmp_path) / ".gitignore"
    gitignore.write_text("*\n!keep-me\n", encoding="utf-8")

    layout.ensure_data_dir(tmp_path)
    assert "!keep-me" in gitignore.read_text(encoding="utf-8")


def test_asset_dirs_are_the_four_fixed_subdirs(tmp_path: Path) -> None:
    asset = layout.ensure_asset_dirs(tmp_path / "characters" / "chitong_beast")

    assert sorted(p.name for p in asset.iterdir()) == sorted(layout.ASSET_SUBDIRS)
    for name in layout.ASSET_SUBDIRS:
        assert (asset / name / layout.GITKEEP).is_file()


def test_ensure_asset_dirs_is_idempotent(tmp_path: Path) -> None:
    """用户拷进来的素材目录会被反复扫，补齐子目录不能踩坏已有文件。"""
    asset = tmp_path / "characters" / "chitong_beast"
    (asset / "images").mkdir(parents=True)
    (asset / "images" / "front.png").touch()

    layout.ensure_asset_dirs(asset)
    layout.ensure_asset_dirs(asset)

    assert (asset / "images" / "front.png").is_file()
    # 目录非空就不放 .gitkeep，Git 本来就能收下有内容的目录
    assert not (asset / "images" / layout.GITKEEP).exists()


def test_art_bible_path_defaults_to_the_conventional_name(tmp_path: Path) -> None:
    assert layout.art_bible_path(tmp_path) == (tmp_path / layout.ART_BIBLE).resolve()
    assert (
        layout.art_bible_path(tmp_path, "docs/视觉规范.md")
        == (tmp_path / "docs" / "视觉规范.md").resolve()
    )


def test_art_bible_path_cannot_point_outside(tmp_path: Path) -> None:
    """art_bible 是 project.json 里的字段，用户能手改，越界得拦住。"""
    with pytest.raises(layout.LayoutError):
        layout.art_bible_path(tmp_path, "../art-bible.md")


def test_一个素材只有一个tmp(tmp_path: Path) -> None:
    """定稿在 images/ 下，历史却要落到素材的 tmp/ ——每个子目录各开一个 tmp/ 会让人找不到旧版。"""
    asset = layout.ensure_asset_dirs(tmp_path / "characters" / "赤瞳")
    final = asset / "images" / "赤瞳_渲染图.png"

    assert layout.history_dir(final) == asset / "tmp"
    # 已经在 tmp/ 里的文件就地退位，不再往上跳一级
    assert layout.history_dir(asset / "tmp" / "候选.png") == asset / "tmp"


def test_退位命名带版本号与时间戳(tmp_path: Path) -> None:
    asset = layout.ensure_asset_dirs(tmp_path / "characters" / "赤瞳")
    final = asset / "images" / "赤瞳_渲染图.png"

    first = layout.next_version_path(final, datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC))
    first.touch()
    second = layout.next_version_path(final, datetime(2026, 8, 30, 12, 5, 0, tzinfo=UTC))

    assert first.name == "赤瞳_渲染图_v1_20260830-120000.png"
    assert second.name == "赤瞳_渲染图_v2_20260830-120500.png"
    assert second.parent == asset / "tmp"


def test_候选与退位版本共用编号(tmp_path: Path) -> None:
    """两边都在 tmp/ 里，各排一套号的话文件名会撞。"""
    asset = layout.ensure_asset_dirs(tmp_path / "characters" / "赤瞳")
    now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

    first = layout.next_tmp_path(asset, "赤瞳_渲染图", ".png", now)
    first.touch()
    second = layout.next_tmp_path(asset, "赤瞳_渲染图", ".png", now)

    assert first.name == "赤瞳_渲染图_v1_20260830-120000.png"
    assert second.name == "赤瞳_渲染图_v2_20260830-120000.png"
    assert layout.tmp_dir(asset) == asset / "tmp"


def test_git规则把二进制素材指向lfs(tmp_path: Path) -> None:
    gitignore, gitattributes = layout.ensure_git_files(tmp_path)

    assert "tmp/" in gitignore.read_text(encoding="utf-8")
    text = gitattributes.read_text(encoding="utf-8")
    assert "*.png filter=lfs" in text
    assert "*.glb filter=lfs" in text
    assert "*.db binary" in text  # 项目库是二进制，但没必要占 LFS 额度


def test_已有的git规则不覆盖(tmp_path: Path) -> None:
    """用户可能已经按自己的仓库习惯改过这两份。"""
    (tmp_path / ".gitignore").write_text("我自己写的\n", encoding="utf-8")

    gitignore, gitattributes = layout.ensure_git_files(tmp_path)

    assert gitignore.read_text(encoding="utf-8") == "我自己写的\n"
    assert gitattributes.is_file()
