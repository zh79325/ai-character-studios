"""项目与素材的磁盘布局。

一条项目目录的自洽约定：目录里有 `project.json` 就是一个项目，它自带的运行库固定在
`.atelier/project.db`。整个目录可以放在磁盘任意位置、可以整份拷走换台机器挂上，因为
项目内一切引用都写成**相对项目目录**的路径，绝对路径只出现在本机的注册表里。

```
{项目目录}/
├── project.json          # 项目配置的唯一真相
├── art-bible.md          # 视觉真相
├── .gitignore            # 忽略过程产物
├── .gitattributes        # 素材走 Git LFS
├── .atelier/             # 项目自己的运行数据（不进 Git）
│   ├── .gitignore        # 内容就是 *，防止被用户的仓库收进去
│   └── project.db
├── templates/            # 可选，项目级模版覆盖全局
├── tmp/                  # 项目根定稿的历史版本
└── characters/ equipment/ maps/ scenes/
    └── {素材名}/{images,models,animations,tmp}/
```
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

PROJECT_JSON = "project.json"
ART_BIBLE = "art-bible.md"
DATA_DIR = ".atelier"
PROJECT_DB = "project.db"
TEMPLATES_DIR = "templates"
TMP_DIR = "tmp"

ASSET_SUBDIRS = ("images", "models", "animations", TMP_DIR)
GITKEEP = ".gitkeep"
MODEL_JSON = ".model.json"
"""角色目录的判定 marker。目录里有它才算一个角色目录：分组只是普通文件夹，靠有没有这个
文件把「角色」从「分组」里区分出来，扫描据此递归认领。"""

_log = structlog.get_logger(__name__)

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


def safe_rel_path(group: str) -> str:
    """把用户给的分组路径（可多级，如 `boss角色/精英`）逐段过 `safe_dir_name`。

    空串代表「根分组」，原样返回。返回用 `/` 拼回的相对路径，仍不做改写只做校验。
    """
    cleaned = group.strip().strip("/")
    if not cleaned:
        return ""
    return "/".join(safe_dir_name(seg) for seg in cleaned.split("/") if seg.strip())


def is_character_dir(path: Path) -> bool:
    """目录里有 marker 才算角色目录，否则只是分组文件夹。"""
    return (path / MODEL_JSON).is_file()


def write_model_marker(asset_dir: Path, name: str) -> Path:
    """在角色目录里落 marker。判定角色目录只看它在不在，内容仅供人肉排查。"""
    target = asset_dir / MODEL_JSON
    payload = {"schema": 1, "name": name, "created_at": datetime.now(UTC).isoformat()}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def read_model_marker(path: Path) -> dict[str, Any]:
    """读角色目录的 marker，读不出来就当空的——它不是真相，只是判定与展示用。"""
    target = path / MODEL_JSON
    if not target.is_file():
        return {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _log.warning("model_json_unreadable", path=str(target))
        return {}
    return loaded if isinstance(loaded, dict) else {}


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


GITIGNORE = """\
# 过程产物与退位的历史版本，随时可由定稿重跑出来
tmp/
*.tmp

# 系统垃圾文件
.DS_Store
Thumbs.db

# 编辑器本机配置
.idea/
"""

GITATTRIBUTES = """\
* text=auto eol=lf

# Git LFS —— 二进制素材
*.png filter=lfs diff=lfs merge=lfs -text
*.jpg filter=lfs diff=lfs merge=lfs -text
*.jpeg filter=lfs diff=lfs merge=lfs -text
*.webp filter=lfs diff=lfs merge=lfs -text
*.glb filter=lfs diff=lfs merge=lfs -text
*.fbx filter=lfs diff=lfs merge=lfs -text
*.usdz filter=lfs diff=lfs merge=lfs -text
*.mp4 filter=lfs diff=lfs merge=lfs -text

# SQLite 库按二进制处理但不进 LFS
*.db binary
"""


def ensure_git_files(project_dir: Path) -> tuple[Path, Path]:
    """给项目目录铺上 `.gitignore` 与 `.gitattributes`。

    素材目录几乎一定会被纳入 Git，而图片模型不走 LFS 会把仓库撑爆。已存在的不覆盖：
    用户可能已经按自己的仓库习惯改过。
    """
    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE, encoding="utf-8")
    gitattributes = project_dir / ".gitattributes"
    if not gitattributes.exists():
        gitattributes.write_text(GITATTRIBUTES, encoding="utf-8")
    return gitignore, gitattributes


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


def tmp_dir(asset_dir: Path) -> Path:
    """过程产物与历史版本的归处，逗手建好。"""
    target = asset_dir / TMP_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def history_dir(final_path: Path) -> Path:
    """某个定稿文件的历史版本该放哪儿：这个素材的 `tmp/`。

    一条规则管三种位置——素材目录下的设定文档进 `{素材}/tmp/`，项目根上的
    `art-bible.md` `project.json` 进 `{项目}/tmp/`，而 `images/` `models/`
    `animations/` 里的定稿往上跳一级、仍然回到素材的 `tmp/`。

    图片不在旁边另开一个 `images/tmp/`：一个素材只应该有一个 `tmp/`，分成三处的话
    “上一版在哪里”这个问题得看类别才能答。历史版本跟着素材走，整份目录拷到别处仍然
    自洽。已经躺在 `tmp/` 里的文件就地退位，否则会套出 `tmp/tmp/`。
    """
    parent = final_path.parent
    if parent.name == TMP_DIR:
        parent.mkdir(parents=True, exist_ok=True)
        return parent
    if parent.name in ASSET_SUBDIRS:
        parent = parent.parent
    return tmp_dir(parent)


_VERSION_RE = re.compile(r"_v(\d+)_\d{8}-\d{6}$")


def _versioned(directory: Path, stem: str, suffix: str, now: datetime) -> Path:
    """`{stem}_v{N}_{时间戳}{suffix}`，N 比目录里已有的最大号大一。"""
    used = 0
    for existing in directory.glob(f"{stem}_v*{suffix}"):
        match = _VERSION_RE.search(existing.stem)
        if match:
            used = max(used, int(match.group(1)))
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return directory / f"{stem}_v{used + 1}_{stamp}{suffix}"


def next_version_path(final_path: Path, now: datetime) -> Path:
    """给要退位的旧定稿起个名：`{名}_v{N}_{时间戳}{后缀}`。

    版本号数的是同一文件已退位过几次，而不是全目录的文件数——同一个素材目录里既有设定
    文档也有图片模型，混着数会让版本号看起来跳号。时间戳保证同一秒外的排序稳定，也让
    「哪个更新」不必依赖文件 mtime。
    """
    return _versioned(history_dir(final_path), final_path.stem, final_path.suffix, now)


def next_tmp_path(asset_dir: Path, stem: str, suffix: str, now: datetime) -> Path:
    """`tmp/` 里下一个候选产物的名字。

    与退位版本共用同一套编号：两者最终躺在同一个 `tmp/` 里，各数一套会出现两个 v2。
    """
    return _versioned(tmp_dir(asset_dir), stem, suffix, now)
