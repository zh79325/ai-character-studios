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
    payload: dict[str, object] = {"name": name, "code": code}
    if dir_path is not None:
        payload["dir_path"] = str(dir_path)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
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
    assert body["current"] is None
    assert body["default_root"] == str(projects_root)


def test_create_lands_in_the_default_root_and_becomes_current(
    client: TestClient, projects_root: Path
) -> None:
    """刚建完就是要用它，不必再点一次切换。"""
    body = create(client, "赤瞳系列", "chitong")

    assert body["current"] == "chitong"
    row = summary(body, "chitong")
    assert row["dir_path"] == str(projects_root / "赤瞳系列")
    assert row["managed"] is True
    assert layout.is_project_dir(projects_root / "赤瞳系列")


def test_create_accepts_any_directory(client: TestClient, tmp_path: Path) -> None:
    """项目不必待在仓库里：前端从系统文件对话框拿到的绝对路径直接收。"""
    target = tmp_path / "外置盘" / "我的项目"

    body = create(client, "我的项目", "mine", target)

    row = summary(body, "mine")
    assert row["dir_path"] == str(target)
    assert row["managed"] is False
    assert (target / ".atelier" / "project.db").is_file()


def test_create_with_a_taken_code_is_409(client: TestClient, tmp_path: Path) -> None:
    create(client, "第一个", "dup", tmp_path / "a")

    response = client.post(
        "/api/projects", json={"name": "第二个", "code": "dup", "dir_path": str(tmp_path / "b")}
    )

    assert response.status_code == 409
    assert "dup" in response.json()["detail"]


def test_create_with_an_impossible_name_is_400(client: TestClient) -> None:
    """名字要当目录名用，斜杠这类字符在布局层就被拦下，是入参问题。"""
    response = client.post("/api/projects", json={"name": "a/b", "code": "slash"})

    assert response.status_code == 400


def test_import_mounts_a_directory_from_anywhere(client: TestClient, tmp_path: Path) -> None:
    """换机器、外置盘、同事拷来的目录都走这里。"""
    create(client, "项目", "p1", tmp_path / "src")
    client.delete("/api/projects/p1")
    assert client.get("/api/projects").json()["projects"] == []

    response = client.post("/api/projects/import", json={"dir_path": str(tmp_path / "src")})

    assert response.status_code == 200
    body = response.json()
    assert body["current"] == "p1"
    assert summary(body, "p1")["dir_path"] == str(tmp_path / "src")


def test_import_of_a_plain_directory_is_404(client: TestClient, tmp_path: Path) -> None:
    plain = tmp_path / "普通目录"
    plain.mkdir()

    assert client.post("/api/projects/import", json={"dir_path": str(plain)}).status_code == 404


def test_switch_changes_the_current_project(client: TestClient) -> None:
    create(client, "第一个", "p1")
    create(client, "第二个", "p2")

    body = client.put("/api/projects/current", json={"code": "p1"}).json()

    assert body["current"] == "p1"
    assert summary(body, "p1")["is_current"] is True
    assert summary(body, "p2")["is_current"] is False
    assert client.get("/api/projects/current").json()["code"] == "p1"


def test_switching_to_an_unknown_project_is_404(client: TestClient) -> None:
    assert client.put("/api/projects/current", json={"code": "nope"}).status_code == 404


def test_delete_only_removes_the_index_entry(client: TestClient, tmp_path: Path) -> None:
    """项目目录是用户的资产，移出只动本机索引。"""
    create(client, "项目", "p1", tmp_path / "src")

    body = client.delete("/api/projects/p1").json()

    assert body["projects"] == []
    assert body["current"] is None
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


def test_health_reports_the_current_project(client: TestClient) -> None:
    """前端启动时靠它决定是进工作台还是引导去新建项目。"""
    assert client.get("/api/health").json()["current_project"] is None

    create(client, "项目", "p1")
    body = client.get("/api/health").json()

    assert body["current_project"] == "p1"
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
    assert json.loads(raw_path.read_text(encoding="utf-8"))["我的备注"] == "下周交付"
    # 目录名不跟着改名走，否则所有已存的相对路径都得重算
    assert Path(client.get("/api/projects/current").json()["dir_path"]) == project_dir
    assert summary(client.get("/api/projects").json(), "p1")["name"] == "改了名"


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
    assert client.get("/api/projects").json()["current"] == "p2"


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
