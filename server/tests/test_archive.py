"""确认沉淀：落盘、旧版退位、project.json 合并、基线校验、素材台账。

这是唯一会改用户工作区的地方，所以每条规则都得钉住：写歪一次就是把用户手写的内容覆盖没
了，而 `tmp/` 里那份是唯一的退路。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier.assets import archive, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.errors import Conflict


def write_final(ref: ProjectRef, relative: str, text: str) -> Path:
    path = ref.absolute(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit(
    ref: ProjectRef, relative: str, content: str, *, based_on: str | None = None
) -> archive.ArchiveResult:
    baseline = archive.file_hash(ref.absolute(relative)) if based_on is None else based_on
    return archive.commit_draft(
        ref,
        target_path=relative,
        content=content,
        based_on_hash=baseline,
        conversation_id="conv-1",
    )


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #


def test_首次沉淀直接落盘且没有旧版(project: ProjectRef) -> None:
    relative = "characters/chitong/赤瞳角色设定.md"

    result = commit(project, relative, "# 赤瞳\n冷光金属。\n")

    assert project.absolute(relative).read_text(encoding="utf-8") == "# 赤瞳\n冷光金属。\n"
    assert result.previous_path is None
    assert result.previous_hash == ""
    assert result.target_path == relative


def test_旧定稿退位到同级tmp而不是被覆盖(project: ProjectRef) -> None:
    """覆盖式写入等于删掉用户上一版的成果，`tmp/` 是唯一的退路。"""
    write_final(project, "art-bible.md", "# 第一版\n")

    result = commit(project, "art-bible.md", "# 第二版\n")

    assert project.absolute("art-bible.md").read_text(encoding="utf-8") == "# 第二版\n"
    assert result.previous_path is not None
    retired = project.absolute(result.previous_path)
    assert retired.read_text(encoding="utf-8") == "# 第一版\n"
    assert retired.parent.name == layout.TMP_DIR
    assert retired.parent.parent == project.dir.resolve()
    assert "_v1_" in retired.name


def test_素材的旧版进素材自己的tmp(project: ProjectRef) -> None:
    relative = "characters/chitong/赤瞳角色设定.md"
    write_final(project, relative, "# v1\n")

    result = commit(project, relative, "# v2\n")

    assert result.previous_path is not None
    retired = project.absolute(result.previous_path)
    assert retired.parent == project.absolute("characters/chitong") / layout.TMP_DIR


def test_版本号连着涨(project: ProjectRef) -> None:
    write_final(project, "art-bible.md", "# v1\n")

    first = commit(project, "art-bible.md", "# v2\n")
    second = commit(project, "art-bible.md", "# v3\n")

    assert first.previous_path is not None and "_v1_" in first.previous_path
    assert second.previous_path is not None and "_v2_" in second.previous_path
    assert len(list((project.dir / layout.TMP_DIR).glob("art-bible_v*.md"))) == 2


def test_越界路径落不了盘(project: ProjectRef) -> None:
    with pytest.raises(ValueError):
        commit(project, "../别人的项目/art-bible.md", "# 偷家\n")


# --------------------------------------------------------------------------- #
# 基线校验
# --------------------------------------------------------------------------- #


def test_定稿在对话期间被改过就拒绝沉淀(project: ProjectRef) -> None:
    """草稿是基于旧版改的，直接写下去就把中间那次修改吞掉了。"""
    write_final(project, "art-bible.md", "# 原版\n")
    baseline = archive.file_hash(project.absolute("art-bible.md"))
    write_final(project, "art-bible.md", "# 用户手改过\n")

    with pytest.raises(Conflict, match="被改过"):
        commit(project, "art-bible.md", "# 草稿\n", based_on=baseline)

    assert project.absolute("art-bible.md").read_text(encoding="utf-8") == "# 用户手改过\n"


def test_以为是新文件结果已经有了也拒绝(project: ProjectRef) -> None:
    """另一个会话先沉淀了同一个文件，这时空基线不再成立。"""
    write_final(project, "art-bible.md", "# 别的会话先写了\n")

    with pytest.raises(Conflict):
        commit(project, "art-bible.md", "# 我的草稿\n", based_on="")


def test_文件不存在时空基线就是对的(project: ProjectRef) -> None:
    assert archive.check_hash(project, "characters/chitong/赤瞳角色设定.md", "") == ""


# --------------------------------------------------------------------------- #
# project.json 合并
# --------------------------------------------------------------------------- #


def test_配置草稿是合并不是整份覆盖(project: ProjectRef) -> None:
    """Agent 只出建议值。整写会把 code、name 和用户手写的键一起抹掉。"""
    config_path = project.absolute(layout.PROJECT_JSON)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["my_own_key"] = "别动我"
    raw.setdefault("style", {})["手写风格键"] = "留着"
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    commit(
        project,
        layout.PROJECT_JSON,
        json.dumps({"style": {"art_style": "国风写实"}, "review_mode": "lean"}),
    )

    merged = json.loads(config_path.read_text(encoding="utf-8"))
    assert merged["code"] == project.code
    assert merged["name"] == project.name
    assert merged["my_own_key"] == "别动我"
    assert merged["style"]["art_style"] == "国风写实"
    assert merged["style"]["手写风格键"] == "留着"
    assert merged["review_mode"] == "lean"
    # 合并后仍是配置层认得的样子
    assert projects.read_config(project.dir).style.art_style == "国风写实"


def test_配置草稿不许改项目身份(project: ProjectRef) -> None:
    commit(project, layout.PROJECT_JSON, json.dumps({"code": "hijacked", "name": "改名"}))

    assert projects.read_config(project.dir).code == project.code


def test_配置草稿不是合法json就拒绝(project: ProjectRef) -> None:
    with pytest.raises(Conflict, match="JSON"):
        commit(project, layout.PROJECT_JSON, "{这不是 json")


def test_配置草稿顶层必须是对象(project: ProjectRef) -> None:
    with pytest.raises(Conflict, match="顶层"):
        commit(project, layout.PROJECT_JSON, "[1, 2, 3]")


def test_枚举外的值是拒收而不是服务器出错(project: ProjectRef) -> None:
    """模型往 review_mode 里写一个自己发明的词很常见，那是一次拒收，不该在介面上弹 500。"""
    with pytest.raises(Conflict, match="合并后不合法"):
        commit(project, layout.PROJECT_JSON, json.dumps({"review_mode": "随便看看"}))


# --------------------------------------------------------------------------- #
# 配置草稿的事前提醒
# --------------------------------------------------------------------------- #


def test_改得动的键不报提醒(project: ProjectRef) -> None:
    patch = json.dumps({"style": {"art_style": "国风写实"}, "defaults": {"image_size": 1024}})

    assert archive.config_patch_warnings(project, patch) == []


def test_不认识的键要先说一声(project: ProjectRef) -> None:
    """合并时它是静默丢掉的，不说用户会以为那一行建议已经生效。"""
    patch = json.dumps({"style": {"art_style": "国风"}, "code": "hijacked", "自创键": 1})

    warnings = archive.config_patch_warnings(project, patch)

    assert len(warnings) == 1
    assert "code" in warnings[0] and "自创键" in warnings[0]
    assert "忽略" in warnings[0]


def test_沉下去会被拒的草稿提前就能看出来(project: ProjectRef) -> None:
    assert "JSON" in archive.config_patch_warnings(project, "{这不是 json")[0]
    assert (
        "合并后不合法"
        in archive.config_patch_warnings(project, json.dumps({"review_mode": "随便"}))[0]
    )


# --------------------------------------------------------------------------- #
# 素材台账
# --------------------------------------------------------------------------- #


def test_素材目录下写meta台账(project: ProjectRef) -> None:
    relative = "characters/chitong/赤瞳角色设定.md"

    commit(project, relative, "# v1\n")
    second = commit(project, relative, "# v2\n")

    meta = json.loads(
        (project.absolute("characters/chitong") / archive.META_JSON).read_text(encoding="utf-8")
    )
    entries = meta["artifacts"]
    assert len(entries) == 2
    assert entries[0]["target_path"] == relative
    assert entries[1]["previous_path"] == second.previous_path
    assert entries[1]["conversation_id"] == "conv-1"


def test_项目根的定稿不写台账(project: ProjectRef) -> None:
    """art-bible.md 与 project.json 本身就是项目的真相，旁边再放一份记录只会多一处不一致。"""
    commit(project, "art-bible.md", "# 规范\n")

    assert not (project.dir / archive.META_JSON).exists()


def test_台账坏了不拦住沉淀(project: ProjectRef) -> None:
    """meta.json 是可重建的台账，不是真相。"""
    asset = project.absolute("characters/chitong")
    asset.mkdir(parents=True, exist_ok=True)
    (asset / archive.META_JSON).write_text("坏掉的内容", encoding="utf-8")

    commit(project, "characters/chitong/赤瞳角色设定.md", "# v1\n")

    meta = json.loads((asset / archive.META_JSON).read_text(encoding="utf-8"))
    assert len(meta["artifacts"]) == 1


def test_角色目录不存在时顺手建出来(project: ProjectRef) -> None:
    """新角色的第一份设定文档来时目录还没建，沉淀不该因此失败。"""
    result = commit(project, "characters/新角色/新角色角色设定.md", "# v1\n")

    assert project.absolute(result.target_path).is_file()
