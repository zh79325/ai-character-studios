"""裁决型 Agent 的输出解析。

后端只读首行判定，理由在下面分节写。这条约定的代价在于：首行读错一次，整份设定的去向就
错一次，所以这里宁可报错也不猜。

硬性约束清单要拆成「项 = 值」——它进 `meta.json` 后是后续每张图的比对依据，比对按项对号。
"""

from __future__ import annotations

import pytest

from atelier.agents import parsing
from atelier.agents.parsing import VerdictError, parse_verdict

FULL = """SPEC-CHECK: CONCERNS

### 缺失维度
无

### 模糊表述
- 原文「深色鳞片」→ 应写明：深灰黑色（dark charcoal）

### art bible 冲突
- §4 主色仅限冷色 ←→ 设定「橙红腹部」

### 硬性约束清单
- 尾巴 = 2 条，彼此分离
- 眼睛 = 红色发光
- 手 = 三指利爪
"""


def test_首行裁决与分节理由一起拿到() -> None:
    verdict = parse_verdict(FULL)

    assert verdict.decision == "CONCERNS"
    assert verdict.token == "SPEC-CHECK"
    assert verdict.approved is False
    assert verdict.rejected is False
    assert verdict.sections["模糊表述"] == ("原文「深色鳞片」→ 应写明：深灰黑色（dark charcoal）",)
    assert verdict.text == FULL


def test_约束清单拆成项与值() -> None:
    """整行存进 meta.json 后没法按项比对，后续每张图的校验就无从下手。"""
    constraints = parse_verdict(FULL).constraints

    assert [(c.item, c.value) for c in constraints] == [
        ("尾巴", "2 条，彼此分离"),
        ("眼睛", "红色发光"),
        ("手", "三指利爪"),
    ]


def test_三个裁决都认() -> None:
    for decision in parsing.VERDICTS:
        assert parse_verdict(f"SPEC-CHECK: {decision}\n").decision == decision


def test_前面的空行与围栏不算首行() -> None:
    """模型爱把裁决包进 ``` 里好让 Markdown 显示得规整。"""
    verdict = parse_verdict("\n\n```\nSPEC-CHECK: APPROVE\n```\n\n### 缺失维度\n无\n")

    assert verdict.approved is True


def test_加粗与全角冒号照样认() -> None:
    assert parse_verdict("**SPEC-CHECK：REJECT**\n").decision == "REJECT"
    assert parse_verdict("spec-check: approve\n").decision == "APPROVE"


def test_藏在正文里的裁决不算() -> None:
    """「若把颜色改掉则 APPROVE」是条件句，当结论读就等于替人工放行。"""
    with pytest.raises(VerdictError, match="首行不是"):
        parse_verdict("我看了一遍设定。\n\nSPEC-CHECK: APPROVE\n")


def test_没按契约说话是报错而不是当成拒收() -> None:
    """看不出裁决是模型的格式事故，默认成 REJECT 会变成一次没有理由的驳回。"""
    with pytest.raises(VerdictError, match="不在 APPROVE/CONCERNS/REJECT"):
        parse_verdict("SPEC-CHECK: MAYBE\n")
    with pytest.raises(VerdictError, match="是空的"):
        parse_verdict("   \n\n")


def test_换成四视图的token也走同一套() -> None:
    verdict = parse_verdict("VIEW-CHECK: APPROVE\n", parsing.VIEW_CHECK)

    assert verdict.token == "VIEW-CHECK"
    with pytest.raises(VerdictError):
        parse_verdict("VIEW-CHECK: APPROVE\n", parsing.SPEC_CHECK)


def test_没有约束清单就是空的() -> None:
    """REJECT 的时候本来就抽不出清单，别硬凑。"""
    verdict = parse_verdict("SPEC-CHECK: REJECT\n\n### 缺失维度\n- 环境设定\n")

    assert verdict.constraints == ()
    assert verdict.sections["缺失维度"] == ("环境设定",)


def test_拆不出项名的行丢掉() -> None:
    """「整体看着不错」这种不可逐条判定的话留在清单里，后续校验会拿它去问一张图。"""
    verdict = parse_verdict(
        "SPEC-CHECK: APPROVE\n\n### 硬性约束清单\n- 整体看着不错\n- 尾巴 = 2 条\n"
    )

    assert [(c.item, c.value) for c in verdict.constraints] == [("尾巴", "2 条")]


def test_占位符不当条目() -> None:
    verdict = parse_verdict("SPEC-CHECK: APPROVE\n\n### 缺失维度\n<一行一条，或「无」>\n")

    assert verdict.sections["缺失维度"] == ()
