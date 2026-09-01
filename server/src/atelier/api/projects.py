"""项目管理接口。

新建项目分两步：`POST /bootstrap` 只占下目录与项目库（此后就能开会话对焦），
`POST /{project_code}/finalize` 在用户确认名字与代号后铺骨架、git 规则与 art bible。
导入则是只登记。项目目录可以在磁盘任意位置，接口一律收发绝对路径（前端从系统文件对话框
拿到的就是绝对路径），库里也只存绝对路径，口径只有一种。

配置读写全部直通磁盘上的 `project.json`：它是唯一真相，库里不留副本，因此不存在「表和
文件不一致」这种要对账的状态。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, status
from sqlalchemy.orm import Session

from atelier.api.characters import character_out
from atelier.api.deps import CurrentProject, RuntimeDb
from atelier.api.schemas import (
    ArtBibleIn,
    ArtBibleOut,
    CharacterOut,
    GroupCreateIn,
    ProjectBootstrapIn,
    ProjectConfigOut,
    ProjectConfigPatch,
    ProjectDirStateOut,
    ProjectFinalizeIn,
    ProjectImportIn,
    ProjectListOut,
    ProjectSummaryOut,
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
        stage=item.stage,
    )


def _list_out(session: Session) -> ProjectListOut:
    return ProjectListOut(
        projects=[_summary_out(item) for item in projects.list_projects(session)],
        default_root=str(get_settings().assets_dir),
    )


def _summary_for(session: Session, ref: ProjectRef) -> ProjectSummaryOut:
    for item in projects.list_projects(session):
        if item.code == ref.code:
            return _summary_out(item)
    # 走不到：ref 是刚从注册表解析或登记出来的
    return ProjectSummaryOut(
        code=ref.code,
        name=ref.name,
        dir_path=str(ref.dir),
        managed=False,
        missing=False,
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


@router.get("/dir-state", response_model=ProjectDirStateOut)
def inspect_dir(dir_path: str = Query(min_length=1)) -> ProjectDirStateOut:
    """选完目录先问一句：这块地是不是已经归另一个项目。

    占着就报出占着的是哪几个文件，好让界面在覆盖之前把代价说清。
    """
    state = projects.inspect_dir(Path(dir_path))
    return ProjectDirStateOut(
        occupied=state.occupied, marks=list(state.marks), is_project=state.is_project
    )


@router.post("/bootstrap", response_model=ProjectSummaryOut, status_code=status.HTTP_201_CREATED)
def bootstrap_project(payload: ProjectBootstrapIn, session: RuntimeDb) -> ProjectSummaryOut:
    """选完目录就创建一个立项中的项目，接下来由前端进入其项目 URL 对焦。

    此时只有 `project.json` 与项目库，名字暂时用目录名、代号是临时的。

    目录已经归另一个项目时报 409；用户对着确认框点了覆盖就带 `overwrite=true` 再来一次。
    """
    ref = projects.bootstrap_project(session, Path(payload.dir_path), overwrite=payload.overwrite)
    return _summary_for(session, ref)


@router.post("/{project_code}/finalize", response_model=ProjectSummaryOut)
def finalize_project(
    payload: ProjectFinalizeIn, ref: CurrentProject, session: RuntimeDb
) -> ProjectSummaryOut:
    """立项收口：定下名字与代号，铺素材目录、`.gitignore`、`.gitattributes` 与 art bible。

    已立项的项目重复调也安全：目录与文件都是「缺了才补」。
    """
    fresh = projects.finalize_project(session, ref, name=payload.name, code=payload.code)
    return _summary_for(session, fresh)


@router.post("/import", response_model=ProjectSummaryOut)
def import_project(payload: ProjectImportIn, session: RuntimeDb) -> ProjectSummaryOut:
    """挂上一个已有的项目目录（换机器、外置盘、同事拷来的都走这里）。"""
    ref = projects.import_project(session, Path(payload.dir_path))
    return _summary_for(session, ref)


@router.delete("/{project_code}", status_code=status.HTTP_204_NO_CONTENT)
def forget_project(project_code: str, session: RuntimeDb) -> None:
    """从本机移出项目，**不动磁盘上的任何文件**。项目目录是用户的资产。"""
    projects.forget(session, project_code)


@router.get("/{project_code}/config", response_model=ProjectConfigOut)
def read_project_config(ref: CurrentProject) -> ProjectConfigOut:
    return ProjectConfigOut.model_validate(projects.read_config(ref.dir).model_dump())


@router.put("/{project_code}/config", response_model=ProjectConfigOut)
def update_project_config(
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


@router.get("/{project_code}/art-bible", response_model=ArtBibleOut)
def read_art_bible(ref: CurrentProject) -> ArtBibleOut:
    content = projects.read_art_bible(ref)
    return ArtBibleOut(
        path=str(projects.art_bible_path(ref)),
        content=content,
        forbidden=projects.forbidden_terms(content),
    )


@router.put("/{project_code}/art-bible", response_model=ArtBibleOut)
def write_art_bible(payload: ArtBibleIn, ref: CurrentProject) -> ArtBibleOut:
    """整篇覆盖保存。art bible 是视觉真相，编辑器给的就是全文，不做行级合并。"""
    projects.write_art_bible(ref, payload.content)
    return ArtBibleOut(
        path=str(projects.art_bible_path(ref)),
        content=payload.content,
        forbidden=projects.forbidden_terms(payload.content),
    )


@router.post("/{project_code}/scan", response_model=ScanResultOut)
def scan_project(ref: CurrentProject) -> ScanResultOut:
    """扫 `characters/` 目录同步进项目库：用户直接拷进来的素材靠这个被认领。"""
    result = projects.scan_characters(ref)
    return ScanResultOut(added=result.added, missing=result.missing, total=result.total)


@router.get("/{project_code}/groups", response_model=list[str])
def list_groups(ref: CurrentProject) -> list[str]:
    """当前项目 `characters/` 下的分组目录（含空分组）。分组只是文件夹，直接读盘。"""
    return projects.list_groups(ref)


@router.post(
    "/{project_code}/groups", response_model=list[str], status_code=status.HTTP_201_CREATED
)
def create_group(payload: GroupCreateIn, ref: CurrentProject) -> list[str]:
    """在当前项目建一个空分组文件夹，建完回最新分组列表。"""
    projects.create_group(ref, payload.path)
    return projects.list_groups(ref)


@router.get("/{project_code}/characters", response_model=list[CharacterOut])
def list_characters(ref: CurrentProject) -> list[CharacterOut]:
    """当前项目的人物素材。换项目就是换库，列表天然隔离。"""
    return [character_out(row) for row in projects.character_rows(ref)]


@router.get("/{project_code}", response_model=ProjectSummaryOut)
def read_project(ref: CurrentProject, session: RuntimeDb) -> ProjectSummaryOut:
    """返回 URL 指定项目的摘要；未登记或目录缺失时依赖层直接返回 404。"""
    return _summary_for(session, ref)
