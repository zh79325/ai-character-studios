"""项目管理接口。

「新建」与「导入」是同一件事的两半，所以放在一个路由文件里：新建 = 铺骨架 + 登记，导入
= 只登记。项目目录可以在磁盘任意位置，接口一律收发绝对路径（前端从系统文件对话框拿到
的就是绝对路径），库里也只存绝对路径，口径只有一种。

配置读写全部直通磁盘上的 `project.json`：它是唯一真相，库里不留副本，因此不存在「表和
文件不一致」这种要对账的状态。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, status
from sqlalchemy.orm import Session

from atelier.api.characters import character_out
from atelier.api.deps import CurrentProject, RuntimeDb
from atelier.api.schemas import (
    ArtBibleIn,
    ArtBibleOut,
    CharacterOut,
    ProjectConfigOut,
    ProjectConfigPatch,
    ProjectCreateIn,
    ProjectImportIn,
    ProjectListOut,
    ProjectSummaryOut,
    ProjectSwitchIn,
    ScanResultOut,
)
from atelier.assets import projects
from atelier.assets.projects import ProjectRef
from atelier.settings import get_settings

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _summary_out(item: projects.ProjectSummary) -> ProjectSummaryOut:
    return ProjectSummaryOut(
        code=item.code,
        name=item.name,
        dir_path=item.dir_path,
        managed=item.managed,
        missing=item.missing,
        is_current=item.is_current,
        last_opened_at=item.last_opened_at.isoformat() if item.last_opened_at else None,
    )


def _list_out(session: Session) -> ProjectListOut:
    return ProjectListOut(
        projects=[_summary_out(item) for item in projects.list_projects(session)],
        current=projects.current_code(session),
        default_root=str(get_settings().assets_dir),
    )


@router.get("", response_model=ProjectListOut)
def list_projects(session: RuntimeDb, sync: bool = False) -> ProjectListOut:
    """本机的项目列表。

    `sync=true` 时先扫一遍默认项目根，把用户手动拷进 `assets/` 的项目目录认领进来；别处
    的项目不扫，得显式导入——平台不去遍历用户的磁盘。
    """
    if sync:
        projects.sync_default_root(session)
    return _list_out(session)


@router.post("", response_model=ProjectListOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreateIn, session: RuntimeDb) -> ProjectListOut:
    """新建项目并切过去——刚建完就是要用它，不必再点一次切换。"""
    ref = projects.create_project(
        session,
        name=payload.name,
        code=payload.code,
        dir_path=Path(payload.dir_path) if payload.dir_path else None,
        style=projects.ProjectStyle.model_validate(payload.style.model_dump())
        if payload.style
        else None,
        defaults=projects.ProjectDefaults.model_validate(payload.defaults.model_dump())
        if payload.defaults
        else None,
        review_mode=payload.review_mode,
    )
    projects.open_project(session, ref.code)
    return _list_out(session)


@router.post("/import", response_model=ProjectListOut)
def import_project(payload: ProjectImportIn, session: RuntimeDb) -> ProjectListOut:
    """挂上一个已有的项目目录（换机器、外置盘、同事拷来的都走这里），并切过去。"""
    ref = projects.import_project(session, Path(payload.dir_path))
    projects.open_project(session, ref.code)
    return _list_out(session)


@router.put("/current", response_model=ProjectListOut)
def switch_project(payload: ProjectSwitchIn, session: RuntimeDb) -> ProjectListOut:
    """切换当前项目。切的同时把目标项目的库补到 head。

    code 走请求体而不是路径：路径上还有 `/current/config`、`/current/art-bible` 这些具名
    子资源，再来一个 `/current/{code}` 就会互相抢匹配。
    """
    projects.open_project(session, payload.code)
    return _list_out(session)


@router.delete("/{code}", response_model=ProjectListOut)
def forget_project(code: str, session: RuntimeDb) -> ProjectListOut:
    """从本机移出项目，**不动磁盘上的任何文件**。项目目录是用户的资产。"""
    projects.forget(session, code)
    return _list_out(session)


@router.get("/current/config", response_model=ProjectConfigOut)
def read_current_config(ref: CurrentProject) -> ProjectConfigOut:
    return ProjectConfigOut.model_validate(projects.read_config(ref.dir).model_dump())


@router.put("/current/config", response_model=ProjectConfigOut)
def update_current_config(
    payload: ProjectConfigPatch, ref: CurrentProject, session: RuntimeDb
) -> ProjectConfigOut:
    """改项目配置：只覆盖表单交上来的字段，其余（含用户手写的额外键）原样留着。"""
    config = projects.read_config(ref.dir)
    patch = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "style" in patch:
        config.style = projects.ProjectStyle.model_validate(patch.pop("style"))
    if "defaults" in patch:
        config.defaults = projects.ProjectDefaults.model_validate(patch.pop("defaults"))
    for key, value in patch.items():
        setattr(config, key, value)
    projects.write_config(ref.dir, config)

    # 目录名不跟着改名（改目录会让所有已存的相对路径失效），只同步索引里的显示名
    projects.rename_in_registry(session, ref.code, config.name)
    return ProjectConfigOut.model_validate(config.model_dump())


@router.get("/current/art-bible", response_model=ArtBibleOut)
def read_art_bible(ref: CurrentProject) -> ArtBibleOut:
    content = projects.read_art_bible(ref)
    return ArtBibleOut(
        path=str(projects.art_bible_path(ref)),
        content=content,
        forbidden=projects.forbidden_terms(content),
    )


@router.put("/current/art-bible", response_model=ArtBibleOut)
def write_art_bible(payload: ArtBibleIn, ref: CurrentProject) -> ArtBibleOut:
    """整篇覆盖保存。art bible 是视觉真相，编辑器给的就是全文，不做行级合并。"""
    projects.write_art_bible(ref, payload.content)
    return ArtBibleOut(
        path=str(projects.art_bible_path(ref)),
        content=payload.content,
        forbidden=projects.forbidden_terms(payload.content),
    )


@router.post("/current/scan", response_model=ScanResultOut)
def scan_project(ref: CurrentProject) -> ScanResultOut:
    """扫 `characters/` 目录同步进项目库：用户直接拷进来的素材靠这个被认领。"""
    result = projects.scan_characters(ref)
    return ScanResultOut(added=result.added, missing=result.missing, total=result.total)


@router.get("/current/characters", response_model=list[CharacterOut])
def list_characters(ref: CurrentProject) -> list[CharacterOut]:
    """当前项目的人物素材。换项目就是换库，列表天然隔离。"""
    return [character_out(row) for row in projects.character_rows(ref)]


@router.get("/current", response_model=ProjectSummaryOut)
def read_current(ref: CurrentProject, session: RuntimeDb) -> ProjectSummaryOut:
    """当前项目是谁。没选过时依赖层直接 404，前端据此引导去新建或导入。"""
    return _current_summary(session, ref)


def _current_summary(session: Session, ref: ProjectRef) -> ProjectSummaryOut:
    for item in projects.list_projects(session):
        if item.code == ref.code:
            return _summary_out(item)
    # 走不到：ref 是从注册表解析出来的
    return ProjectSummaryOut(
        code=ref.code,
        name=ref.name,
        dir_path=str(ref.dir),
        managed=False,
        missing=False,
        is_current=True,
    )
