"""项目层：一个目录就是一个项目。

项目的真相全在它自己的目录里——`project.json`（配置）、`art-bible.md`（视觉规范）、
素材目录、以及 `.atelier/project.db`（这个项目的运行库）。目录可以放在磁盘任意位置，
整份拷到另一台机器、挂上去就还是那个项目。

全局 `runtime.db` 里只有一张 `project_registry`：本机登记过哪些项目、它们在哪。这张表
是索引不是真相，删了不影响项目本身（重新导入一次就回来），所以任何时候都以磁盘上的
`project.json` 为准，冲突时改库不改文件。请求所操作的项目始终由 URL 中的 code 指定。

「新建」与「导入」因此是同一件事的两半：新建 = 铺目录骨架 + 登记；导入 = 只登记。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.assets import layout
from atelier.db.migrate import upgrade_project
from atelier.db.project_models import Character, Generation
from atelier.db.runtime_models import ProjectRegistry
from atelier.db.session import dispose_project_engine, project_session
from atelier.errors import Conflict, NotFound
from atelier.settings import get_settings

FORBIDDEN_SECTION = "风格禁止项"
PLACEHOLDER = "待填"

ReviewMode = Literal["full", "lean", "solo"]

Stage = Literal["drafting", "ready"]
"""项目所处阶段。`drafting` = 还在跟 Agent 对焦、名字与骨架都没定；`ready` = 已立项。

这个字段不在注册表里而在 `project.json` 里：项目搬到别的机器上得能看出它是否立过项。
老项目的 json 里没这个键，读出来就是 `ready`。"""

DEFAULT_STATE = "P0_project_shaping"
"""新项目的工作流起点。"""

DRAFT_CODE_PREFIX = "draft-"
"""立项期的临时代号前缀。会话得存在项目库里，所以对话开始前就得先有个项目身份，
而真正的代号要等对焦完才定。"""

DRAFT_NAME = "未命名项目"


# --------------------------------------------------------------------------- #
# project.json
# --------------------------------------------------------------------------- #


class ProjectStyle(BaseModel):
    """风格基调，`prompt_smith` 会把它追加进本项目所有素材的 prompt。

    写清了 art bible 之后这里只是缓存摘要，冲突以 art bible 为准。
    """

    model_config = ConfigDict(extra="allow")

    art_style: str = ""
    mood: str = ""
    palette: str = ""
    quality: str = ""


class ProjectDefaults(BaseModel):
    """生产参数的项目级中间层：素材级不填就取这里，这里没有才用代码内置缺省。"""

    model_config = ConfigDict(extra="allow")

    image_size: int = 2048
    texture_resolution: str = "2k"
    enable_pbr: bool = True
    target_polycount: int = 30000
    pose_mode: str = "t-pose"
    height_meters: float = 1.7


class ProjectConfig(BaseModel):
    """`project.json` 的结构。

    `extra="allow"`：用户手写进去的字段（临时备注、自己的工具用的键）读进来还要原样写
    回去，平台不认识不等于可以丢。
    """

    model_config = ConfigDict(extra="allow")

    code: str
    name: str
    style: ProjectStyle = Field(default_factory=ProjectStyle)
    defaults: ProjectDefaults = Field(default_factory=ProjectDefaults)
    pose_template: str | None = None
    """相对项目目录的姿势模版；为空则回落全局 `templates/人物姿势模版.jpg`。"""
    art_bible: str = layout.ART_BIBLE
    review_mode: ReviewMode = "lean"
    conversation_audit: bool = False
    stage: Stage = "ready"
    state: str = DEFAULT_STATE
    """立项工作流推到哪一步。跟 `stage` 不是一回事：`stage` 是「名字定下来了没」，它是整套
    阶段门禁里的位置。现在只存不读，但它得跟项目目录进 Git——接手的人要知道上一个人停在哪儿。"""


def read_config(project_dir: Path) -> ProjectConfig:
    """读并校验 `project.json`。它坏了就整个项目不可用，不做「尽力解析」。"""
    path = layout.project_json_path(project_dir)
    if not path.is_file():
        raise NotFound(f"{project_dir} 不是项目目录（缺 {layout.PROJECT_JSON}）")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Conflict(f"{path} 读不出来：{exc}") from exc
    try:
        return ProjectConfig.model_validate(raw)
    except ValidationError as exc:
        raise Conflict(f"{path} 内容不合法：{exc.errors()[0]['msg']}") from exc


def write_config(project_dir: Path, config: ProjectConfig) -> None:
    """写回 `project.json`。

    先写临时文件再 `os.replace`：这文件是项目配置的唯一真相，宁可写不成，不能写一半——
    半个 JSON 会让项目下次打不开。
    """
    path = layout.project_json_path(project_dir)
    text = json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# 项目引用
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """一个已定位到磁盘的项目。库路径由目录推出来，不单独存，免得两处不一致。"""

    code: str
    name: str
    dir: Path

    @property
    def db_path(self) -> Path:
        return layout.project_db_path(self.dir)

    def relative(self, path: Path) -> str:
        return layout.relative_to(self.dir, path)

    def absolute(self, relative: str) -> Path:
        return layout.resolve_inside(self.dir, relative)


_migrated: set[Path] = set()
_migrate_lock = threading.Lock()


def ensure_schema(ref: ProjectRef) -> None:
    """把项目自带的运行目录备齐：`.atelier/` 在、忽略规则在、库补到当前结构。

    新建与导入走的是同一条路：导入别人拷来的目录时那里本来就没有 `.atelier/`，而建库本身
    只会顶出目录、不会顶出忽略规则，所以两件事得绑在一起做。

    每次打开项目都调，但一个进程内同一个库只真跑一次：alembic 自己是幂等的，可它每次
    要开连接读 version 表，切项目频繁时没必要。项目换机器、换版本后第一次打开就靠它
    补齐新表。
    """
    key = ref.db_path.resolve()
    if key in _migrated:
        return
    with _migrate_lock:
        if key in _migrated:
            return
        layout.ensure_data_dir(ref.dir)
        upgrade_project(key)
        _migrated.add(key)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def _register(runtime: Session, ref: ProjectRef, *, managed: bool) -> ProjectRegistry:
    """登记或更新一条索引。同 code 换了目录就认新目录（用户搬了项目）。"""
    dir_path = str(ref.dir.resolve())
    row = runtime.get(ProjectRegistry, ref.code)
    clash = runtime.scalars(
        select(ProjectRegistry).where(
            ProjectRegistry.dir_path == dir_path, ProjectRegistry.code != ref.code
        )
    ).first()
    if clash is not None:
        # 同一个目录被两个 code 指着，说明其中一条索引已经过期了；以磁盘上的 code 为准
        runtime.delete(clash)
        runtime.flush()
    if row is None:
        row = ProjectRegistry(code=ref.code, name=ref.name, dir_path=dir_path, managed=managed)
        runtime.add(row)
    else:
        row.name = ref.name
        row.dir_path = dir_path
        row.managed = managed
    row.missing = False
    runtime.flush()
    return row


def resolve(runtime: Session, code: str) -> ProjectRef:
    """按 code 找到项目，并确认它在磁盘上还在。"""
    row = runtime.get(ProjectRegistry, code)
    if row is None:
        raise NotFound(f"项目 {code} 没有登记在本机")
    project_dir = Path(row.dir_path)
    if not layout.is_project_dir(project_dir):
        row.missing = True
        raise NotFound(f"项目 {code} 的目录不见了：{row.dir_path}")
    config = read_config(project_dir)
    if row.name != config.name or row.missing:
        # 用户直接改了 project.json 的名字，索引跟上
        row.name = config.name
        row.missing = False
    return ProjectRef(code=config.code, name=config.name, dir=project_dir)


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    code: str
    name: str
    dir_path: str
    managed: bool
    missing: bool
    stage: Stage = "ready"


def stage_of(project_dir: Path) -> Stage:
    """读项目所处阶段。读不出来（目录不在、json 坏了）当 `ready`：删不了、导不进才是真问题，
    在列表上把它标成「立项中」只会把用户往对焦页引。"""
    try:
        return read_config(project_dir).stage
    except (NotFound, Conflict):
        return "ready"


def list_projects(runtime: Session) -> list[ProjectSummary]:
    """列出本机所有项目，顺手校准 missing 标记（外置盘没挂就是这个状态）。"""
    rows = runtime.scalars(select(ProjectRegistry).order_by(ProjectRegistry.name)).all()
    out: list[ProjectSummary] = []
    for row in rows:
        project_dir = Path(row.dir_path)
        missing = not layout.is_project_dir(project_dir)
        if row.missing != missing:
            row.missing = missing
        out.append(
            ProjectSummary(
                code=row.code,
                name=row.name,
                dir_path=row.dir_path,
                managed=row.managed,
                missing=missing,
                stage="ready" if missing else stage_of(project_dir),
            )
        )
    return out


def sync_default_root(runtime: Session) -> list[str]:
    """扫默认项目根（仓库 `assets/`），把手动放进去的项目目录登记上。

    只扫这一层、只扫默认根：别处的项目由用户显式导入，平台不去遍历用户的磁盘。
    """
    root = get_settings().assets_dir
    if not root.is_dir():
        return []
    added: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not layout.is_project_dir(child):
            continue
        config = read_config(child)
        known = runtime.get(ProjectRegistry, config.code)
        ref = ProjectRef(code=config.code, name=config.name, dir=child)
        _register(runtime, ref, managed=True)
        if known is None:
            ensure_schema(ref)
            added.append(config.code)
    return added


def rename_in_registry(runtime: Session, code: str, name: str) -> None:
    """项目改了显示名，同步索引。

    目录名不跟着改：库里所有素材路径都是相对项目目录的，改目录名本身不会弄坏它们，
    但用户可能已经把这个目录纳入自己的 Git 仓库、或在别处引用了它，静悄改名得不偿失。
    想改目录名就在 Finder 里改完再导入一次。
    """
    row = runtime.get(ProjectRegistry, code)
    if row is None:
        raise NotFound(f"项目 {code} 没有登记在本机")
    row.name = name


def forget(runtime: Session, code: str) -> None:
    """从本机索引里移出，不删磁盘上的任何东西。

    项目目录是用户的资产，删除只能由用户在 Finder 里做。先放开库句柄，否则目录被占着，
    用户接着去搬它会留下 `-wal` / `-shm` 残骸。
    """
    row = runtime.get(ProjectRegistry, code)
    if row is None:
        raise NotFound(f"项目 {code} 没有登记在本机")
    dispose_project_engine(layout.project_db_path(Path(row.dir_path)))
    runtime.delete(row)
    runtime.flush()


# --------------------------------------------------------------------------- #
# 新建与导入
# --------------------------------------------------------------------------- #


def _write_skeleton(project_dir: Path, config: ProjectConfig) -> None:
    """铺目录骨架：维度目录 + 共识目录 + art bible + project.json。

    `.atelier/` 不在这里铺——它由 `ensure_schema` 负责，导入别人的目录时同样要补，两个入口
    共用一份实现。
    """
    settings = get_settings()
    project_dir.mkdir(parents=True, exist_ok=True)

    for name in layout.CATEGORY_DIRS:
        sub = project_dir / name
        sub.mkdir(exist_ok=True)
        if name != "characters":
            readme = sub / "README.md"
            if not readme.exists():
                readme.write_text(_placeholder_readme(name), encoding="utf-8")
        layout.touch_gitkeep(sub)

    # 共识目录先铺出来：空目录带 .gitkeep 才进得了 Git，否则接手的人 clone 下来看不出
    # 这两处是干什么的，也不知道可以手写
    for path in (layout.memory_dir(project_dir), project_dir / layout.PROMPTS_DIR):
        path.mkdir(exist_ok=True)
        layout.touch_gitkeep(path)

    art_bible = project_dir / config.art_bible
    if not art_bible.exists():
        template = settings.templates_dir / layout.ART_BIBLE
        skeleton = (
            template.read_text(encoding="utf-8")
            if template.is_file()
            else f"# {config.name} 视觉规范（Art Bible）\n"
        )
        art_bible.write_text(skeleton.replace("{项目名}", config.name), encoding="utf-8")

    write_config(project_dir, config)


def _placeholder_readme(dir_name: str) -> str:
    return (
        f"# {dir_name}\n\n"
        "本期（阶段一至阶段三）只实现 character 工作流，本目录仅建结构占位。\n\n"
        f"后续素材按 `{dir_name}/{{素材名}}/` 组织，内部与 character 同构：\n"
        "定稿放第一层，`images/`、`models/`，中间产物进 `tmp/`。\n"
    )


def _project_marks(path: Path) -> list[str]:
    """目录里已经属于某个项目的标志文件。

    非空不算占用：用户往往先把参考图、旧稿、策划文档丢进目录再来立项，为这个逼他另建
    一个空目录只是添乱（还有 Finder 进去看一眼就留下的 `.DS_Store`）。真会撞车的只有项目自己
    的两份真相：`project.json` 与 `art-bible.md`——它们在就说明这块地已经归另一个项目，
    铺下去会盖掉别人的东西。
    """
    if not path.is_dir():
        return []
    return [name for name in (layout.PROJECT_JSON, layout.ART_BIBLE) if (path / name).is_file()]


@dataclass(frozen=True, slots=True)
class DirState:
    """一个候选目录现在是什么状况。新建之前先问它，好在覆盖之前让用户点头。"""

    dir: Path
    marks: tuple[str, ...]
    """目录里已经属于某个项目的那几个文件名。"""
    is_project: bool

    @property
    def occupied(self) -> bool:
        return bool(self.marks)


def inspect_dir(dir_path: Path) -> DirState:
    """这块地能不能直接建。占着就报出占着的是什么，让界面把话说清再问要不要覆盖。"""
    target = dir_path.expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise Conflict(f"{target} 不是目录")
    return DirState(
        dir=target,
        marks=tuple(_project_marks(target)),
        is_project=layout.is_project_dir(target),
    )


def _clear_project(target: Path) -> None:
    """把这块地上一个项目的身份抹掉：`project.json`、`art-bible.md` 与 `.atelier/`。

    只清项目自己的那几样，素材文件一个不动——用户点的是「这个目录归新项目」，不是「把我的
    图删了」。`.atelier/` 得连库一起删，否则新项目会接着用旧项目的会话与素材记录。
    """
    db_path = layout.project_db_path(target)
    dispose_project_engine(db_path)
    # 库文件马上要被删掉，建库那步不能再被跳过
    _migrated.discard(db_path.resolve())
    for name in (layout.PROJECT_JSON, layout.ART_BIBLE):
        (target / name).unlink(missing_ok=True)
    shutil.rmtree(layout.data_dir(target), ignore_errors=True)


def bootstrap_project(runtime: Session, dir_path: Path, *, overwrite: bool = False) -> ProjectRef:
    """开个立项中的空壳项目：只写 `project.json` 与项目库，不铺骨架。

    素材目录、art bible、git 规则都等 `finalize_project`——对焦还没开始就铺一堆写着「待填」
    的模板，用户下一步得先去删它们。

    代号先给个临时的：会话存在项目库里，没有项目身份就无处开会话，而名字与代号要等聊完
    才由用户定。

    目录已经归另一个项目时默认拒收（界面据此问一句要不要覆盖）；`overwrite=True` 就是用户
    点过头了，先抹掉旧项目的身份再建。
    """
    target = dir_path.expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise Conflict(f"{target} 不是目录")
    marks = _project_marks(target)
    if marks and overwrite:
        _clear_project(target)
        marks = []
    if layout.is_project_dir(target):
        raise Conflict(f"{target} 已经是一个项目，请用导入")
    if marks:
        raise Conflict(f"{target} 里已经有 {'、'.join(marks)}，换个目录或走导入")

    code = f"{DRAFT_CODE_PREFIX}{secrets.token_hex(3)}"
    try:
        name = layout.safe_dir_name(target.name)
    except layout.LayoutError:
        name = DRAFT_NAME

    target.mkdir(parents=True, exist_ok=True)
    config = ProjectConfig(code=code, name=name, stage="drafting")
    write_config(target, config)

    ref = ProjectRef(code=code, name=name, dir=target)
    ensure_schema(ref)
    _register(runtime, ref, managed=_under_default_root(target))
    return ref


def finalize_project(runtime: Session, ref: ProjectRef, *, name: str, code: str) -> ProjectRef:
    """用户确认了名字与代号，现在才把项目真正立起来。

    改代号 = 换注册表主键，但项目目录与库都不动：库里路径全是相对项目目录的，代号的真相
    在 `project.json` 里。art bible 只在缺失时才从模板补——对焦里已经沉淀过的那份不能被
    模板盖掉。
    """
    safe_name = layout.safe_dir_name(name)
    safe_code = _validate_code(code)
    if safe_code.startswith(DRAFT_CODE_PREFIX):
        raise Conflict(f"项目代号不能以 {DRAFT_CODE_PREFIX} 开头")

    clash = runtime.get(ProjectRegistry, safe_code)
    if clash is not None and clash.code != ref.code:
        raise Conflict(f"项目代号 {safe_code} 已被占用")

    config = read_config(ref.dir)
    config.code = safe_code
    config.name = safe_name
    config.stage = "ready"

    fresh = ProjectRef(code=safe_code, name=safe_name, dir=ref.dir)
    _write_skeleton(ref.dir, config)
    layout.ensure_git_files(ref.dir)
    ensure_schema(fresh)
    _register(runtime, fresh, managed=_under_default_root(ref.dir))
    return fresh


def create_project(
    runtime: Session,
    *,
    name: str,
    code: str,
    dir_path: Path | None = None,
    style: ProjectStyle | None = None,
    defaults: ProjectDefaults | None = None,
    review_mode: ReviewMode = "lean",
) -> ProjectRef:
    """新建项目。

    `dir_path` 不给就落在默认项目根（仓库 `assets/{项目名}`）；给了就放在用户指定的任意
    位置，两种情况在库里都记绝对路径，口径只有一种。目标目录已经是个项目就报冲突——
    「导入」是另一个入口，不在这里悄悄兼容。
    """
    safe_name = layout.safe_dir_name(name)
    safe_code = _validate_code(code)
    target = (dir_path or (get_settings().assets_dir / safe_name)).expanduser().resolve()

    if layout.is_project_dir(target):
        raise Conflict(f"{target} 已经是一个项目，请用导入")
    marks = _project_marks(target)
    if marks:
        raise Conflict(f"{target} 里已经有 {'、'.join(marks)}，换个目录或走导入")
    if runtime.get(ProjectRegistry, safe_code) is not None:
        raise Conflict(f"项目代号 {safe_code} 已被占用")

    config = ProjectConfig(
        code=safe_code,
        name=safe_name,
        style=style or ProjectStyle(),
        defaults=defaults or ProjectDefaults(),
        review_mode=review_mode,
    )
    _write_skeleton(target, config)
    layout.ensure_git_files(target)

    ref = ProjectRef(code=safe_code, name=safe_name, dir=target)
    ensure_schema(ref)
    _register(runtime, ref, managed=_under_default_root(target))
    return ref


def import_project(runtime: Session, dir_path: Path) -> ProjectRef:
    """把磁盘上已有的项目目录挂到本机。

    这就是「项目能放在任意位置」的另一半：换机器、换硬盘、从同事那儿拷来一份，都是指一
    下目录的事。库不存在会被建出来、落后会被补齐，都由 `ensure_schema` 兜住。
    """
    target = dir_path.expanduser().resolve()
    if not target.is_dir():
        raise NotFound(f"目录不存在：{target}")
    config = read_config(target)

    existing = runtime.get(ProjectRegistry, config.code)
    if existing is not None:
        registered = Path(existing.dir_path).resolve()
        if registered != target and layout.is_project_dir(registered):
            raise Conflict(
                f"项目代号 {config.code} 已指向 {existing.dir_path}，"
                "改一下 project.json 里的 code 再导入"
            )
        # 原来登记的位置已经不是项目了（用户在 Finder 里搬走或改了名），那条索引过期了，认新目录

    ref = ProjectRef(code=config.code, name=config.name, dir=target)
    ensure_schema(ref)
    _register(runtime, ref, managed=_under_default_root(target))
    return ref


def _under_default_root(path: Path) -> bool:
    root = get_settings().assets_dir.resolve()
    return root == path.parent


def _validate_code(code: str) -> str:
    """项目代号只收 ASCII：它会进路径、进 prompt、进日志和外部 API 的参数。

    中文过得了 `isalnum()`，但一旦出现在这些地方，编码问题会在离这里很远的位置才炸。
    """
    cleaned = code.strip().lower()
    if not cleaned:
        raise Conflict("项目代号不能为空")
    if not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in cleaned):
        raise Conflict("项目代号只能用英文字母、数字、- 和 _")
    if len(cleaned) > 64:
        raise Conflict("项目代号过长（超过 64 字符）")
    return cleaned


# --------------------------------------------------------------------------- #
# art bible
# --------------------------------------------------------------------------- #


def art_bible_path(ref: ProjectRef, config: ProjectConfig | None = None) -> Path:
    cfg = config or read_config(ref.dir)
    return layout.art_bible_path(ref.dir, cfg.art_bible)


def read_art_bible(ref: ProjectRef) -> str:
    path = art_bible_path(ref)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def write_art_bible(ref: ProjectRef, content: str) -> None:
    """整篇覆盖写。原子替换的理由同 project.json：这是视觉真相，不能写一半。"""
    path = art_bible_path(ref)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def forbidden_terms(text: str) -> list[str]:
    """抽出 art bible「风格禁止项」一节的条目，供生图时拼进 negative_prompt。

    只认这一节的无序列表项，`待填` 与注释跳过：模板里本来就带占位符，把 `待填` 送进
    negative prompt 等于往每张图上泼一句无意义的中文。
    """
    terms: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            inside = FORBIDDEN_SECTION in line
            continue
        if not inside or not line.startswith(("-", "*")):
            continue
        item = line[1:].strip().strip("`")
        if item and item != PLACEHOLDER:
            terms.append(item)
    return terms


ART_BIBLE_SECTIONS: tuple[tuple[int, str], ...] = (
    (1, "视觉身份一句话"),
    (2, "氛围与光照"),
    (3, "形状语言"),
    (4, "色彩系统"),
    (5, "资产标准"),
    (6, FORBIDDEN_SECTION),
)
"""一份能用的 art bible 必须有的六节。下游真的按节抽内容，缺一节就是一处抽不到。"""


def _section_bodies(text: str) -> dict[str, list[str]]:
    """按二级标题切段，键是标题行原文。"""
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        if raw.strip().startswith("##"):
            current = raw.strip()
            bodies[current] = []
            continue
        if current is not None:
            bodies[current].append(raw)
    return bodies


def _has_content(lines: list[str]) -> bool:
    """这一节除了注释与表格分隔线之外还剩下东西吗。"""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("<!--") or set(line) <= set("|-: "):
            continue
        return True
    return False


def art_bible_gaps(text: str) -> list[str]:
    """这份 art bible 还差哪几处，一条一句人话。

    给的是提醒而不是禁止：写到一半先沉下去、回头接着聊是正当的用法，但留着 `待填`
    的那几节会直接被 `prompt_smith` 拼进 prompt、被 `vision_reviewer` 当标准用，用户得在
    按确认之前就看见这件事。
    """
    if not text.strip():
        return ["这份 art bible 还是空的"]

    bodies = _section_bodies(text)
    gaps: list[str] = []
    for number, name in ART_BIBLE_SECTIONS:
        heading = next((key for key in bodies if name in key), None)
        if heading is None:
            gaps.append(f"缺「{number} {name}」一节")
        elif not _has_content(bodies[heading]):
            gaps.append(f"「{number} {name}」下面是空的")
        elif PLACEHOLDER in "\n".join(bodies[heading]):
            gaps.append(f"「{number} {name}」里还留着「{PLACEHOLDER}」")
    return gaps


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MissingCharacter:
    id: str
    name: str
    dir_name: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """一次目录扫描的结果，前端照这个报「同步了什么」。"""

    added: list[str]
    missing: list[MissingCharacter]
    total: int


def new_asset_id() -> str:
    """生成与目录、项目无关的角色身份；持久化后只认 marker 中的值。"""
    return f"CHAR-{secrets.token_hex(10)}"


def _moved_asset_path(value: str | None, old_dir: str, new_dir: str) -> str | None:
    if value == old_dir:
        return new_dir
    prefix = f"{old_dir}/"
    return f"{new_dir}/{value[len(prefix) :]}" if value and value.startswith(prefix) else value


def _move_character(session: Session, character: Character, dir_name: str) -> None:
    """marker 跟着目录移动时，按持久 ID 更新所有直接保存的素材路径。"""
    old_dir = character.dir_name
    character.dir_name = dir_name
    character.spec_path = _moved_asset_path(character.spec_path, old_dir, dir_name)
    character.render_path = _moved_asset_path(character.render_path, old_dir, dir_name)
    generations = session.scalars(
        select(Generation).where(
            Generation.target_kind == "character", Generation.target_ref == character.id
        )
    )
    for generation in generations:
        generation.file_path = _moved_asset_path(generation.file_path, old_dir, dir_name) or ""


def scan_characters(ref: ProjectRef) -> ScanResult:
    """按 `.model.json` 中的随机 ID 对账角色目录与数据库。

    marker 跟着目录移动，因此扫描会更新已有角色的路径；旧 marker 没有 ID 时优先沿用同路径
    数据库记录的 ID，否则生成新 ID 并回写。数据库中的旧 ID 没有对应 marker 时只报告缺失，
    不阻止同一路径登记一个全新的角色。
    """
    root = ref.dir / "characters"
    dirs = (
        sorted(p for p in root.rglob("*") if p.is_dir() and layout.is_character_dir(p))
        if root.is_dir()
        else []
    )
    added: list[str] = []

    with project_session(ref.db_path) as session:
        rows = list(session.scalars(select(Character)).all())
        by_id = {row.id: row for row in rows}
        by_dir: dict[str, list[Character]] = {}
        for row in rows:
            by_dir.setdefault(row.dir_name, []).append(row)

        entries: list[tuple[Path, str, str, str]] = []
        for path in dirs:
            rel = layout.relative_to(ref.dir, path)
            marker = layout.read_model_marker(path)
            name = str(marker.get("name") or path.name)
            character_id = layout.model_marker_id(marker)
            if character_id is None:
                candidates = by_dir.get(rel, [])
                existing = max(candidates, key=lambda row: row.updated_at) if candidates else None
                character_id = existing.id if existing is not None else new_asset_id()
                layout.write_model_marker(path, name, character_id)
            entries.append((path, rel, name, character_id))

        owners: dict[str, str] = {}
        for _, rel, _, character_id in entries:
            current = by_id.get(character_id)
            if character_id not in owners or (current is not None and current.dir_name == rel):
                owners[character_id] = rel

        on_disk_ids: set[str] = set()
        for path, rel, name, character_id in entries:
            if owners[character_id] != rel:
                character_id = new_asset_id()
                layout.write_model_marker(path, name, character_id)

            character = by_id.get(character_id)
            if character is not None:
                if character.dir_name != rel:
                    _move_character(session, character, rel)
            else:
                layout.ensure_asset_dirs(path)
                character = Character(
                    id=character_id,
                    name=name,
                    dir_name=rel,
                    spec_path=_find_spec(path, ref.dir),
                )
                session.add(character)
                by_id[character_id] = character
                added.append(name)
            on_disk_ids.add(character_id)

        missing = sorted(
            (
                MissingCharacter(id=row.id, name=row.name, dir_name=row.dir_name)
                for row in rows
                if row.id not in on_disk_ids
            ),
            key=lambda row: row.dir_name,
        )
        total = len(rows) + len(added)

    return ScanResult(added=added, missing=missing, total=total)


def list_groups(ref: ProjectRef) -> list[str]:
    """`characters/` 下的所有分组目录（相对 `characters/` 的路径，含空分组）。

    分组只是普通文件夹，磁盘是它们存在与否的真相（空分组没有库行可依），所以直接读盘。
    排掉三类不是分组的目录：角色目录（带 marker）、角色内的 asset 子目录、. 开头的隐藏目录。
    """
    root = ref.dir / "characters"
    if not root.is_dir():
        return []
    groups: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_dir() or path.name.startswith(".") or path.name in layout.ASSET_SUBDIRS:
            continue
        if layout.is_character_dir(path):
            continue
        rel = path.relative_to(root)
        if any(part in layout.ASSET_SUBDIRS for part in rel.parts):
            continue
        ancestors = (root.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts)))
        if any(layout.is_character_dir(parent) for parent in ancestors):
            continue
        groups.add(rel.as_posix())
    return sorted(groups)


def create_group(ref: ProjectRef, path: str) -> str:
    """在 `characters/<path>` 下建一个空分组文件夹，返回相对 `characters/` 的路径。"""
    rel = layout.safe_rel_path(path)
    if not rel:
        raise Conflict("分组名不能为空")
    (ref.dir / "characters" / rel).mkdir(parents=True, exist_ok=True)
    return rel


def _find_spec(asset_dir: Path, project_dir: Path) -> str | None:
    """素材目录第一层的 md 就是设定稿（定稿放第一层是既定约定）。"""
    for path in sorted(asset_dir.glob("*.md")):
        return layout.relative_to(project_dir, path)
    return None


def character_row(row: Character) -> dict[str, Any]:
    """一行角色给到 API 的样子。

    列表与详情共用这一份：两处各拼一遍的话，加字段时总会只加到其中一边。
    """
    constraints = row.hard_constraints.get("items") if row.hard_constraints else None
    stored = row.params.get("views") if row.params else None
    adopted = stored if isinstance(stored, dict) else {}
    return {
        "id": row.id,
        "name": row.name,
        "dir_name": row.dir_name,
        "state": row.state,
        "spec_path": row.spec_path,
        "render_path": row.render_path,
        "view_paths": {
            str(key): str(value) for key, value in adopted.items() if isinstance(value, str)
        },
        "hard_constraints": [one for one in constraints or [] if isinstance(one, dict)],
        "gate_spec_confirmed_at": (
            row.gate_spec_confirmed_at.isoformat()
            if row.gate_spec_confirmed_at is not None
            else None
        ),
        "gate_render_confirmed_at": (
            row.gate_render_confirmed_at.isoformat()
            if row.gate_render_confirmed_at is not None
            else None
        ),
        "updated_at": row.updated_at.isoformat(),
    }


def character_rows(ref: ProjectRef) -> list[dict[str, Any]]:
    """当前项目的素材列表。切项目后天然隔离——库都不是同一个。"""
    with project_session(ref.db_path) as session:
        rows = session.scalars(select(Character).order_by(Character.created_at)).all()
        return [character_row(row) for row in rows]
