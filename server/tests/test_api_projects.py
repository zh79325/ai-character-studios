"""项目管理接口。

这层的看点不是 CRUD 而是三件事：项目目录可以在磁盘任意位置、`?project=` 只查不切、配置
读写直通磁盘上的 `project.json`（库里没有副本，所以不存在对账）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier.assets import layout
from atelier.assets import projects as projects_mod

pytestmark = pytest.mark.usefixtures("projects_root")


def create(client: TestClient, name: str, code: str, dir_path: Path | None = None) -> dict:
    """走完整的两段式立项：先占下目录，再定名字与代号。

    立项本来要在这两步之间跟 Agent 对焦，但除了对焦本身，别的用例只关心「有个立好的项目」。
    """
    if dir_path is None:
        dir_path = Path(client.get("/api/projects").json()["default_root"]) / name
    response = client.post("/api/projects/bootstrap", json={"dir_path": str(dir_path)})
    assert response.status_code == 201, response.text

    response = client.post("/api/projects/current/finalize", json={"name": name, "code": code})
    assert response.status_code == 200, response.text
    return response.json()


def summary(body: dict, code: str) -> dict:
    return next(item for item in body["projects"] if item["code"] == code)


# --------------------------------------------------------------------------- #
# 列表、新建、导入、切换
# --------------------------------------------------------------------------- #


def test_empty_install_lists_nothing_but_tells_where_to_put_things(
    client: TestClient, projects_root: Path
) -> None:
    body = client.get("/api/projects").json()

    assert body["projects"] == []
    assert body["opened"] is None
    assert body["default_root"] == str(projects_root)


def test_bootstrap_opens_a_drafting_project_without_the_skeleton(
    client: TestClient, tmp_path: Path
) -> None:
    """选完目录就能开始对焦，但此时还不该铺一堆写着「待填」的模板。"""
    target = tmp_path / "外置盘" / "待命名"

    response = client.post("/api/projects/bootstrap", json={"dir_path": str(target)})

    assert response.status_code == 201, response.text
    body = response.json()
    row = summary(body, body["opened"])
    assert row["stage"] == "drafting"
    assert row["name"] == "待命名"  # 暂时借目录名
    assert layout.is_project_dir(target)
    assert (target / ".atelier" / "project.db").is_file()  # 会话得有地方存
    assert not (target / "characters").exists()
    assert not (target / ".gitignore").exists()


def test_bootstrap_takes_a_directory_that_already_has_files(
    client: TestClient, tmp_path: Path
) -> None:
    """用户常先把参考图丢进目录再来立项，拦下来只会逼他多建一个空目录。"""
    target = tmp_path / "已经攒了料"
    target.mkdir()
    (target / "参考图.png").touch()

    response = client.post("/api/projects/bootstrap", json={"dir_path": str(target)})

    assert response.status_code == 201, response.text
    assert (target / "参考图.png").is_file()  # 原有的东西一个不动


def test_bootstrap_refuses_a_directory_that_is_already_a_project(
    client: TestClient, tmp_path: Path
) -> None:
    """已经是项目的目录该走导入，不在这里悄悄兼容。"""
    create(client, "项目", "p1", tmp_path / "src")

    response = client.post("/api/projects/bootstrap", json={"dir_path": str(tmp_path / "src")})

    assert response.status_code == 409


def test_dir_state_tells_the_ui_whether_the_directory_is_taken(
    client: TestClient, tmp_path: Path
) -> None:
    """界面靠它决定要不要先问一句「覆盖吗」，而不是先撞一个 409 再猜。"""
    create(client, "项目", "p1", tmp_path / "src")

    taken = client.get("/api/projects/dir-state", params={"dir_path": str(tmp_path / "src")})
    empty = client.get("/api/projects/dir-state", params={"dir_path": str(tmp_path / "新的")})

    assert taken.json() == {
        "occupied": True,
        "marks": ["project.json", "art-bible.md"],
        "is_project": True,
    }
    assert empty.json() == {"occupied": False, "marks": [], "is_project": False}


def test_bootstrap_overwrites_the_old_project_once_confirmed(
    client: TestClient, tmp_path: Path
) -> None:
    """用户点了覆盖就照办：旧项目的身份让位，他自己丢进去的素材文件一个不动。"""
    target = tmp_path / "src"
    create(client, "旧项目", "old", target)
    (target / "characters" / "参考图.png").touch()

    response = client.post(
        "/api/projects/bootstrap", json={"dir_path": str(target), "overwrite": True}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert summary(body, body["opened"])["stage"] == "drafting"
    assert [item["code"] for item in body["projects"]] == [body["opened"]]  # 旧那条索引退场
    assert (target / ".atelier" / "project.db").is_file()  # 库删干净了还得重新建出来
    assert (target / "characters" / "参考图.png").is_file()


def test_finalize_lands_in_the_default_root_and_opens_it(
    client: TestClient, projects_root: Path
) -> None:
    """刚立完项就是要用它，不必再点一次打开。"""
    body = create(client, "赤瞳系列", "chitong")

    assert body["opened"] == "chitong"
    assert [item["code"] for item in body["projects"]] == ["chitong"]  # 临时代号那条已经退场
    row = summary(body, "chitong")
    assert row["dir_path"] == str(projects_root / "赤瞳系列")
    assert row["managed"] is True
    assert row["stage"] == "ready"
    assert layout.is_project_dir(projects_root / "赤瞳系列")


def test_finalize_lays_out_the_skeleton_and_the_git_rules(
    client: TestClient, tmp_path: Path
) -> None:
    """项目不必待在仓库里；素材目录与 git 规则都是立项收口时才铺的。"""
    target = tmp_path / "外置盘" / "我的项目"

    body = create(client, "我的项目", "mine", target)

    row = summary(body, "mine")
    assert row["dir_path"] == str(target)
    assert row["managed"] is False
    assert (target / "characters").is_dir()
    assert (target / "art-bible.md").is_file()
    # 图片不走 LFS 会把用户的仓库撑爆
    assert "tmp/" in (target / ".gitignore").read_text(encoding="utf-8")
    assert "filter=lfs" in (target / ".gitattributes").read_text(encoding="utf-8")


def test_finalize_keeps_what_the_focusing_already_settled(
    client: TestClient, tmp_path: Path
) -> None:
    """对焦阶段沉淀下来的 art bible 不能被模板盖掉。"""
    target = tmp_path / "待命名"
    client.post("/api/projects/bootstrap", json={"dir_path": str(target)})
    client.put("/api/projects/current/art-bible", json={"content": "# 聊出来的\n"})

    create_response = client.post(
        "/api/projects/current/finalize", json={"name": "我的项目", "code": "mine"}
    )

    assert create_response.status_code == 200, create_response.text
    assert (target / "art-bible.md").read_text(encoding="utf-8") == "# 聊出来的\n"


def test_a_project_json_without_stage_reads_as_ready(client: TestClient, tmp_path: Path) -> None:
    """老项目的 `project.json` 里没有 stage，读出来该是已立项而不是被拉回对焦页。"""
    create(client, "项目", "p1", tmp_path / "src")
    raw_path = layout.project_json_path(tmp_path / "src")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    del raw["stage"]
    raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    assert summary(client.get("/api/projects").json(), "p1")["stage"] == "ready"


def test_finalize_with_a_taken_code_is_409(client: TestClient, tmp_path: Path) -> None:
    create(client, "第一个", "dup", tmp_path / "a")
    client.post("/api/projects/bootstrap", json={"dir_path": str(tmp_path / "b")})

    response = client.post("/api/projects/current/finalize", json={"name": "第二个", "code": "dup"})

    assert response.status_code == 409
    assert "dup" in response.json()["detail"]


def test_finalize_with_an_impossible_name_is_400(client: TestClient, tmp_path: Path) -> None:
    """名字要当目录名用，斜杠这类字符在布局层就被拦下，是入参问题。"""
    client.post("/api/projects/bootstrap", json={"dir_path": str(tmp_path / "a")})

    response = client.post("/api/projects/current/finalize", json={"name": "a/b", "code": "slash"})

    assert response.status_code == 400


def test_import_mounts_a_directory_from_anywhere(client: TestClient, tmp_path: Path) -> None:
    """换机器、外置盘、同事拷来的目录都走这里。"""
    create(client, "项目", "p1", tmp_path / "src")
    client.delete("/api/projects/p1")
    assert client.get("/api/projects").json()["projects"] == []

    response = client.post("/api/projects/import", json={"dir_path": str(tmp_path / "src")})

    assert response.status_code == 200
    body = response.json()
    assert body["opened"] == "p1"
    assert summary(body, "p1")["dir_path"] == str(tmp_path / "src")


def test_import_of_a_plain_directory_is_404(client: TestClient, tmp_path: Path) -> None:
    plain = tmp_path / "普通目录"
    plain.mkdir()

    assert client.post("/api/projects/import", json={"dir_path": str(plain)}).status_code == 404


def test_opening_another_project_moves_over(client: TestClient) -> None:
    create(client, "第一个", "p1")
    create(client, "第二个", "p2")

    body = client.put("/api/projects/current", json={"code": "p1"}).json()

    assert body["opened"] == "p1"
    assert client.get("/api/projects/current").json()["code"] == "p1"


def test_switching_to_an_unknown_project_is_404(client: TestClient) -> None:
    assert client.put("/api/projects/current", json={"code": "nope"}).status_code == 404


def test_delete_only_removes_the_index_entry(client: TestClient, tmp_path: Path) -> None:
    """项目目录是用户的资产，移出只动本机索引。"""
    create(client, "项目", "p1", tmp_path / "src")

    body = client.delete("/api/projects/p1").json()

    assert body["projects"] == []
    assert body["opened"] is None
    assert layout.is_project_dir(tmp_path / "src")


def test_sync_claims_projects_dropped_into_the_default_root(
    client: TestClient, session: Session, projects_root: Path, tmp_path: Path
) -> None:
    """用户直接把项目目录拖进默认根，列表带上 sync 就该认领。"""
    ref = projects_mod.create_project(
        session, name="外面的", code="outside", dir_path=tmp_path / "o"
    )
    session.commit()
    client.delete("/api/projects/outside")
    ref.dir.rename(projects_root / "外面的")

    assert client.get("/api/projects").json()["projects"] == []
    body = client.get("/api/projects", params={"sync": True}).json()
    assert summary(body, "outside")["dir_path"] == str(projects_root / "外面的")


def test_health_reports_the_opened_project(client: TestClient) -> None:
    """前端启动时靠它决定是进工作台还是引导去新建项目。"""
    assert client.get("/api/health").json()["opened_project"] is None

    create(client, "项目", "p1")
    body = client.get("/api/health").json()

    assert body["opened_project"] == "p1"
    assert body["project_db"].endswith("/.atelier/project.db")


# --------------------------------------------------------------------------- #
# 没选项目时
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    ["/api/projects/current", "/api/projects/current/config", "/api/projects/current/art-bible"],
)
def test_project_scoped_reads_need_a_project(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert "项目" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def test_config_patch_only_touches_what_the_form_sent(client: TestClient) -> None:
    """表单没提的字段（含用户手写的额外键）必须原样留着。"""
    create(client, "项目", "p1")
    project_dir = Path(client.get("/api/projects/current").json()["dir_path"])
    raw_path = layout.project_json_path(project_dir)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["我的备注"] = "下周交付"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    body = client.put(
        "/api/projects/current/config",
        json={"name": "改了名", "style": {"art_style": "国风水墨"}},
    ).json()

    assert body["name"] == "改了名"
    assert body["style"]["art_style"] == "国风水墨"
    assert body["defaults"]["image_size"] == 2048  # 没提到，保持缺省
    assert body["review_mode"] == "lean"
    assert body["conversation_audit"] is False
    assert json.loads(raw_path.read_text(encoding="utf-8"))["我的备注"] == "下周交付"
    # 目录名不跟着改名走，否则所有已存的相对路径都得重算
    assert Path(client.get("/api/projects/current").json()["dir_path"]) == project_dir
    assert summary(client.get("/api/projects").json(), "p1")["name"] == "改了名"


def test_config_can_toggle_conversation_audit(client: TestClient) -> None:
    create(client, "项目", "p1")

    enabled = client.put("/api/projects/current/config", json={"conversation_audit": True}).json()
    assert enabled["conversation_audit"] is True

    project_dir = Path(client.get("/api/projects/current").json()["dir_path"])
    assert projects_mod.read_config(project_dir).conversation_audit is True

    disabled = client.put("/api/projects/current/config", json={"conversation_audit": False}).json()
    assert disabled["conversation_audit"] is False


def test_config_is_read_from_disk_not_from_a_copy(client: TestClient) -> None:
    """`project.json` 是唯一真相：用户在编辑器里手改，接口下一次就该读到。"""
    create(client, "项目", "p1")
    project_dir = Path(client.get("/api/projects/current").json()["dir_path"])
    config = projects_mod.read_config(project_dir)
    config.defaults.image_size = 1024
    projects_mod.write_config(project_dir, config)

    body = client.get("/api/projects/current/config").json()

    assert body["defaults"]["image_size"] == 1024


def test_two_projects_keep_separate_configs(client: TestClient) -> None:
    """A5 的验收点：两个项目各自独立配置。"""
    create(client, "第一个", "p1")
    client.put("/api/projects/current/config", json={"style": {"art_style": "国风水墨"}})
    create(client, "第二个", "p2")
    client.put("/api/projects/current/config", json={"style": {"art_style": "蒸汽朋克"}})

    assert (
        client.get("/api/projects/current/config", params={"project": "p1"}).json()["style"][
            "art_style"
        ]
        == "国风水墨"
    )
    assert client.get("/api/projects/current/config").json()["style"]["art_style"] == "蒸汽朋克"


def test_the_project_query_param_looks_without_switching(client: TestClient) -> None:
    """带 `?project=` 查一眼别的项目，不该把用户的当前项目换掉。"""
    create(client, "第一个", "p1")
    create(client, "第二个", "p2")

    assert (
        client.get("/api/projects/current/config", params={"project": "p1"}).json()["code"] == "p1"
    )
    assert client.get("/api/projects").json()["opened"] == "p2"


def test_an_unknown_project_query_param_is_404(client: TestClient) -> None:
    create(client, "项目", "p1")

    assert client.get("/api/projects/current/config", params={"project": "nope"}).status_code == 404


# --------------------------------------------------------------------------- #
# art bible
# --------------------------------------------------------------------------- #


def test_art_bible_starts_from_the_template(client: TestClient) -> None:
    create(client, "赤瞳系列", "chitong")

    body = client.get("/api/projects/current/art-bible").json()

    assert body["content"].startswith("# 赤瞳系列 视觉规范")
    assert body["path"].endswith("art-bible.md")
    assert body["forbidden"] == []  # 模板里只有「待填」，不该送进 negative prompt


def test_art_bible_save_exposes_the_forbidden_terms(client: TestClient) -> None:
    """A5 的验收点：art bible 的禁止项能进 negative。"""
    create(client, "项目", "p1")
    content = "# 项目 视觉规范\n\n## 6 风格禁止项\n\n- 赛博霓虹\n- 塑料光泽\n"

    saved = client.put("/api/projects/current/art-bible", json={"content": content}).json()

    assert saved["forbidden"] == ["赛博霓虹", "塑料光泽"]
    assert client.get("/api/projects/current/art-bible").json()["content"] == content
    assert Path(saved["path"]).read_text(encoding="utf-8") == content


# --------------------------------------------------------------------------- #
# 素材
# --------------------------------------------------------------------------- #


def make_character(client: TestClient, name: str, project: str | None = None) -> Path:
    params = {"project": project} if project else None
    project_dir = Path(client.get("/api/projects/current", params=params).json()["dir_path"])
    target = project_dir / "characters" / name
    target.mkdir(parents=True)
    (target / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    (target / ".model.json").write_text(f'{{"schema": 1, "name": "{name}"}}', encoding="utf-8")
    return target


def test_scan_claims_what_the_user_copied_in(client: TestClient) -> None:
    create(client, "项目", "p1")
    make_character(client, "chitong_beast")

    result = client.post("/api/projects/current/scan").json()

    assert result == {"added": ["chitong_beast"], "missing": [], "total": 1}
    rows = client.get("/api/projects/current/characters").json()
    assert [row["name"] for row in rows] == ["chitong_beast"]
    assert rows[0]["dir_name"] == "characters/chitong_beast"
    assert rows[0]["state"]


def test_characters_are_isolated_per_project(client: TestClient) -> None:
    """A5 的验收点：切项目后素材列表隔离。"""
    create(client, "第一个", "p1")
    make_character(client, "chitong_beast")
    client.post("/api/projects/current/scan")

    create(client, "第二个", "p2")
    make_character(client, "steam_golem")
    client.post("/api/projects/current/scan")

    assert [row["name"] for row in client.get("/api/projects/current/characters").json()] == [
        "steam_golem"
    ]
    first = client.get("/api/projects/current/characters", params={"project": "p1"}).json()
    assert [row["name"] for row in first] == ["chitong_beast"]
