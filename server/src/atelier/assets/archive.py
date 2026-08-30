"""确认沉淀：把草稿写到定稿位，旧定稿退位进 `tmp/`。

这是**唯一的落盘入口**。会话过程中的产物只在 `artifact_drafts` 里，用户不点确认，磁盘上
一个字节都不变——创作过程反复推翻是常态，让每轮都改工作区会把用户自己的 Git 历史搅烂。

三条硬规则：

1. **先校 `based_on_hash`**：草稿是基于哪一版定稿改的，写回去时那一版必须还在。不一致
   说明中途有人（另一个会话、用户手改、git checkout）动过文件，此时覆盖等于静默丢掉
   别人的修改，只能拒绝并让用户重新读取。
2. **旧定稿先退位再写新的**：退位到同级 `tmp/`，带版本号与时间戳。原地覆盖一旦写坏就
   什么都不剩了。
3. **`project.json` 是合并不是覆盖**：Agent 只产出 `style` / `defaults` 这几段，整份
   写回去会把 `code`、`name` 和用户手写的键抹掉。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from atelier.assets import layout, projects
from atelier.assets.projects import ProjectRef
from atelier.errors import Conflict

_log = structlog.get_logger(__name__)

META_JSON = "meta.json"

MERGED_CONFIG_KEYS = ("style", "defaults", "review_mode", "pose_template")
"""`project.json` 里允许 Agent 改的键。`code` 与 `art_bible` 是平台的账，它说了不算。"""


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """一次沉淀的结果，直接回给前端展示「写到哪儿、旧版去哪儿了」。"""

    target_path: str
    content_hash: str
    previous_hash: str
    previous_path: str | None
    """旧定稿退位后的相对路径；原先没有文件则为 None。"""


def file_hash(path: Path) -> str:
    """定稿文件的内容 hash。文件不存在返回空串，表示「还没有定稿」。

    按字节算而不是按解析后的内容算：用户手改一个空格也算改过，宁可多报一次冲突，也不能
    在「看起来等价」上做判断——等价与否是人的判断，不是平台的。
    """
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_hash(ref: ProjectRef, target_path: str, based_on_hash: str) -> str:
    """校验草稿的基线仍是当前定稿，返回当前 hash。不一致抛 Conflict。"""
    target = layout.resolve_inside(ref.dir, target_path)
    current = file_hash(target)
    if (based_on_hash or "") != current:
        raise Conflict(
            f"{target_path} 在这次对话期间被改过了（草稿基于 "
            f"{based_on_hash[:8] or '空文件'}，磁盘上现在是 {current[:8] or '空文件'}）。"
            "先重新读取当前定稿再继续，避免覆盖别处的修改。"
        )
    return current


def _write_atomic(path: Path, content: str) -> None:
    """临时文件 + `os.replace`，理由同 project.json：宁可写不成，不能写一半。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _retire(ref: ProjectRef, target: Path, now: datetime) -> str | None:
    """把现有定稿移进同级 `tmp/`，返回它的新相对路径。"""
    if not target.is_file():
        return None
    retired = layout.next_version_path(target, now)
    os.replace(target, retired)
    return ref.relative(retired)


def _merge_project_json(ref: ProjectRef, content: str) -> str:
    """把 Agent 给的 project.json 片段并进现有配置，返回合并后的整份文本。

    Agent 输出的是建议值而不是完整配置，所以这里逐键合并：认识的键覆盖，`style` 与
    `defaults` 内部也是逐键覆盖（用户手写在里面的额外键要留着），其余一概不动。
    """
    try:
        patch = json.loads(content)
    except json.JSONDecodeError as exc:
        raise Conflict(f"草稿里的 project.json 不是合法 JSON：{exc}") from exc
    if not isinstance(patch, dict):
        raise Conflict("草稿里的 project.json 顶层必须是对象")

    config = projects.read_config(ref.dir)
    merged: dict[str, Any] = config.model_dump(mode="json")
    for key in MERGED_CONFIG_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key in ("style", "defaults") and isinstance(value, dict):
            base = merged.get(key)
            merged[key] = {**base, **value} if isinstance(base, dict) else value
        else:
            merged[key] = value

    try:
        updated = projects.ProjectConfig.model_validate(merged)
    except ValidationError as exc:
        # 合并后站不住脚就是一次拒收，不是平台出错：Agent 往 `review_mode` 里写了一个
        # 枚举外的值这种事，得拿人话告诉用户，而不是介面上弹一个 500。
        raise Conflict(
            f"草稿里的 {layout.PROJECT_JSON} 合并后不合法：{exc.errors()[0]['msg']}"
        ) from exc
    return json.dumps(updated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def config_patch_warnings(ref: ProjectRef, content: str) -> list[str]:
    """这份 `project.json` 草稿里有哪几处沉下去不会生效。

    在确认之前就说：不认识的键合并时是静默丢掉的，用户会以为那一行建议已经生效。
    """
    try:
        patch = json.loads(content)
    except json.JSONDecodeError as exc:
        return [f"不是合法 JSON（第 {exc.lineno} 行：{exc.msg}），沉淀会被拒"]
    if not isinstance(patch, dict):
        return ["顶层必须是对象，沉淀会被拒"]

    warnings: list[str] = []
    ignored = [key for key in patch if key not in MERGED_CONFIG_KEYS]
    if ignored:
        warnings.append(f"「{'、'.join(ignored)}」不在平台允许 Agent 改的键里，沉淀时会被忽略")
    try:
        _merge_project_json(ref, content)
    except Conflict as exc:
        warnings.append(str(exc))
    return warnings


def _meta_dir(ref: ProjectRef, target: Path) -> Path | None:
    """该定稿的 `meta.json` 该写在哪儿：素材目录下。

    素材目录形如 `{项目}/characters/{角色}/`，`meta.json` 是这个素材的工作流台账。项目根
    上的 `art-bible.md` 与 `project.json` 没有这样的台账——它们本身就是项目的真相，再旁
    边放一份记录只会多一处可能不一致的副本，所以项目级沉淀只记 `task_events`。
    """
    project_dir = ref.dir.resolve()
    relative = target.resolve().relative_to(project_dir).parts
    if len(relative) < 3 or relative[0] not in layout.CATEGORY_DIRS:
        return None
    return project_dir / relative[0] / relative[1]


def read_meta(path: Path) -> dict[str, Any]:
    """读一份 `meta.json`。读不出来就当空的。

    它是可从库与目录重建的台账，不是真相；为一份坏掉的台账把用户的沉淀拦下来不值得。
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _log.warning("meta_json_unreadable", path=str(path))
        return {}
    return loaded if isinstance(loaded, dict) else {}


def merge_meta(path: Path, patch: Mapping[str, Any]) -> None:
    """把几个顶层键并进 `meta.json`，其余键原样留着。

    逐键并而不是整份覆盖：同一份台账里同时记着沉淀历史、角色状态、各阶段参数快照，写状态
    的那一方不该把历史抹掉。
    """
    meta = read_meta(path)
    meta.update(patch)
    _write_atomic(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def _record_meta(
    meta_dir: Path, *, result: ArchiveResult, conversation_id: str, now: datetime
) -> None:
    """把这次沉淀追加进素材的 `meta.json`。"""
    path = meta_dir / META_JSON
    meta = read_meta(path)

    history = meta.get("artifacts")
    entries = list(history) if isinstance(history, list) else []
    entries.append(
        {
            "target_path": result.target_path,
            "content_hash": result.content_hash,
            "previous_hash": result.previous_hash,
            "previous_path": result.previous_path,
            "conversation_id": conversation_id,
            "committed_at": now.isoformat(),
        }
    )
    merge_meta(path, {"artifacts": entries})


def commit_draft(
    ref: ProjectRef,
    *,
    target_path: str,
    content: str,
    based_on_hash: str,
    conversation_id: str,
    now: datetime | None = None,
) -> ArchiveResult:
    """把一份草稿写成定稿。基线不符即拒绝，写之前先让旧版退位。"""
    moment = now or _now()
    target = layout.resolve_inside(ref.dir, target_path)
    relative = ref.relative(target)
    previous_hash = check_hash(ref, relative, based_on_hash)

    text = _merge_project_json(ref, content) if target.name == layout.PROJECT_JSON else content
    previous_path = _retire(ref, target, moment)
    _write_atomic(target, text)

    result = ArchiveResult(
        target_path=relative,
        content_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
        previous_hash=previous_hash,
        previous_path=previous_path,
    )

    meta_dir = _meta_dir(ref, target)
    if meta_dir is not None:
        _record_meta(meta_dir, result=result, conversation_id=conversation_id, now=moment)

    _log.info(
        "artifact_committed",
        project=ref.code,
        target=result.target_path,
        previous=result.previous_path,
        conversation=conversation_id,
    )
    return result
