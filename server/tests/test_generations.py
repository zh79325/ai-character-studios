"""产物台账：登记、候选排序与定稿唯一。

要钉的是「定稿只有一个」和「换定稿不丢历史」：门禁上人连生了几张才挑一张，事后要能回答
「采用的是哪一次调用、当时的 prompt 是什么」。旧定稿落回候选而不是删行——它的文件退位后
还在 `tmp/` 里，台账跟着留住才对得上。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from atelier.assets import generations


def add(project: Session, path: str, *, target: str = "CHAR-1") -> str:
    row = generations.record(
        project,
        target_ref=target,
        stage=generations.RENDER,
        file_path=path,
        file_hash=f"hash-{path}",
        asset_spec={"prompt": path},
    )
    project.commit()
    return row.id


def test_刚生成的一律不是定稿(project_db: Session) -> None:
    """产物落在 tmp/ 里，定稿位上还是旧的那张，标成定稿会让台账比磁盘超前一步。"""
    row_id = add(project_db, "characters/赤瞳/tmp/a.png")

    row = generations.get(project_db, row_id)

    assert row is not None
    assert row.is_final is False
    assert row.source == "generated"
    assert row.asset_spec == {"prompt": "characters/赤瞳/tmp/a.png"}


def test_候选新的在前(project_db: Session) -> None:
    first = add(project_db, "characters/赤瞳/tmp/a.png")
    second = add(project_db, "characters/赤瞳/tmp/b.png")

    rows = generations.candidates(project_db, target_ref="CHAR-1", stage=generations.RENDER)

    assert [one.id for one in rows][0] in (second, first)
    assert len(rows) == 2
    assert generations.latest(project_db, target_ref="CHAR-1", stage=generations.RENDER) is not None


def test_别的角色的产物不串进来(project_db: Session) -> None:
    add(project_db, "characters/赤瞳/tmp/a.png")
    add(project_db, "characters/青瞳/tmp/a.png", target="CHAR-2")

    rows = generations.candidates(project_db, target_ref="CHAR-2", stage=generations.RENDER)

    assert [one.file_path for one in rows] == ["characters/青瞳/tmp/a.png"]


def test_定稿只能有一个(project_db: Session) -> None:
    first = add(project_db, "characters/赤瞳/tmp/a.png")
    second = add(project_db, "characters/赤瞳/tmp/b.png")

    one = generations.get(project_db, first)
    two = generations.get(project_db, second)
    assert one is not None and two is not None
    generations.mark_final(
        project_db, one, file_path="characters/赤瞳/images/final.png", file_hash="h1"
    )
    project_db.commit()
    generations.mark_final(
        project_db, two, file_path="characters/赤瞳/images/final.png", file_hash="h2"
    )
    project_db.commit()

    final = generations.final(project_db, target_ref="CHAR-1", stage=generations.RENDER)
    assert final is not None
    assert final.id == second
    # 旧定稿落回候选而不是删掉：它的文件退位后还在 tmp/ 里
    assert generations.get(project_db, first) is not None
    assert one.is_final is False


def test_定稿的路径改成定稿位(project_db: Session) -> None:
    """指向 tmp/ 的定稿行迟早会因为清理目录而悬空。"""
    row_id = add(project_db, "characters/赤瞳/tmp/a.png")
    row = generations.get(project_db, row_id)
    assert row is not None

    generations.mark_final(
        project_db, row, file_path="characters/赤瞳/images/赤瞳_渲染图.png", file_hash="h"
    )

    assert row.file_path == "characters/赤瞳/images/赤瞳_渲染图.png"
    assert row.file_hash == "h"
    assert row.is_final is True


def test_没定稿时返回空(project_db: Session) -> None:
    add(project_db, "characters/赤瞳/tmp/a.png")

    assert generations.final(project_db, target_ref="CHAR-1", stage=generations.RENDER) is None
