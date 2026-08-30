"""项目与素材的磁盘布局。

一条项目目录的自洽约定：目录里有 `project.json` 就是一个项目，它自带的运行库固定在
`.atelier/project.db`。整个目录可以放在磁盘任意位置、可以整份拷走换台机器挂上，因为
项目内一切引用都写成**相对项目目录**的路径，绝对路径只出现在本机的注册表里。

```
{项目目录}/
├── project.json          # 项目配置的唯一真相
├── art-bible.md          # 视觉真相
├── .atelier/             # 项目自己的运行数据（不进 Git）
│   ├── .gitignore        # 内容就是 *，防止被用户的仓库收进去
│   └── project.db
├── templates/            # 可选，项目级模版覆盖全局
└── characters/ equipment/ maps/ scenes/
    └── {素材名}/{images,models,animations,tmp}/
```
"""

from __future__ import annotations

from pathlib import Path

PROJECT_JSON = "project.json"
ART_BIBLE = "art-bible.md"
DATA_DIR = ".atelier"
PROJECT_DB = "project.db"
TEMPLATES_DIR = "templates"

ASSET_SUBDIRS = ("images", "models", "animations", "tmp")
GITKEEP = ".gitkeep"

CATEGORY_DIRS = ("characters", "equipment", "maps", "scenes")
"""新建项目时铺的维度目录。与 config.db 的 `asset_categories.dir_name` 同名，但建目录
不查库：项目目录必须能在没有任何库的情况下由模板铺出来，否则「拷到另一台机器打开」就
多了一个前置条件。本期只有 characters 会真的跑工作流，其余仅占位。"""

_BAD_CHARS = frozenset('/\\:*?"<>|')
_RESERVED = frozenset({".", "..", DATA_DIR})


class LayoutError(ValueError):
    """目录名或路径不符合约定。"""


def safe_dir_name(name: str) -> str:
    """把用户给的项目名/素材名当目录名用之前必须过这一关。

    只做校验不做改写：偷偷替换字符会让「用户看到的名字」和「磁盘上的名字」对不上，
    后面找不到目录时更难排查。
    """
    cleaned = name.strip()
    if not cleaned:
        raise LayoutError("名称不能为空")
    if cleaned in _RESERVED or cleaned.startswith("."):
        raise LayoutError(f"名称 {name!r} 不能作为目录名")
    bad = sorted(_BAD_CHARS & set(cleaned))
    if bad:
        raise LayoutError(f"名称里不能出现 {' '.join(bad)}")
    if len(cleaned) > 100:
        raise LayoutError("名称过长（超过 100 字）")
    return cleaned


def project_json_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_JSON


def data_dir(project_dir: Path) -> Path:
    return project_dir / DATA_DIR


def project_db_path(project_dir: Path) -> Path:
    """项目库始终在项目目录下，跟着目录走。"""
    return data_dir(project_dir) / PROJECT_DB


def art_bible_path(project_dir: Path, file_name: str = ART_BIBLE) -> Path:
    return resolve_inside(project_dir, file_name)


def is_project_dir(path: Path) -> bool:
    return project_json_path(path).is_file()


def ensure_data_dir(project_dir: Path) -> Path:
    """建好 `.atelier/` 并放一个自我忽略的 .gitignore。

    用户很可能把项目目录纳入自己的 Git 仓库（素材本来就该进版本管理），但运行库、WAL
    与缓存不该进。与其指望用户自己写规则，不如项目目录自带这一条。

    规则里留了 `!.gitignore`：否则 `*` 会把规则文件本身也忽略掉，用户把项目推给同事后
    那边就没有这条忽略，运行库会被一起提交进去。
    """
    target = data_dir(project_dir)
    target.mkdir(parents=True, exist_ok=True)
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# 项目自己的运行库与缓存，随时可由项目目录重建，不进版本管理\n*\n!.gitignore\n",
            encoding="utf-8",
        )
    return target


def ensure_asset_dirs(asset_dir: Path) -> Path:
    """一个素材目录下的四个固定子目录，空目录放 .gitkeep 以便进 Git。"""
    asset_dir.mkdir(parents=True, exist_ok=True)
    for name in ASSET_SUBDIRS:
        sub = asset_dir / name
        sub.mkdir(exist_ok=True)
        touch_gitkeep(sub)
    return asset_dir


def touch_gitkeep(directory: Path) -> None:
    keep = directory / GITKEEP
    if not any(directory.iterdir()) and not keep.exists():
        keep.touch()


def relative_to(project_dir: Path, path: Path) -> str:
    """项目内路径一律以相对形式存库，目录搬走后仍然有效。"""
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError as exc:
        raise LayoutError(f"{path} 不在项目目录 {project_dir} 内") from exc


def resolve_inside(project_dir: Path, relative: str) -> Path:
    """把库里存的相对路径还原成绝对路径，顺手拦住 `../` 穿越。"""
    if not relative:
        raise LayoutError("路径不能为空")
    candidate = (project_dir / relative).resolve()
    root = project_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise LayoutError(f"路径 {relative!r} 越出了项目目录")
    return candidate
