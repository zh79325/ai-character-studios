"""裁决型 Agent 的统一 Action payload 解析。

裁决必须来自严格 JSON 中的 `payload.verdict`，正文只用于向用户解释结论。
硬性约束清单以结构化的「item/value」保存，供后续每张图逐项比对。
"""

from __future__ import annotations

import pytest

from atelier.agents import parsing
from atelier.agents.parsing import ProtocolError, VerdictError, parse_verdict
from tests.conftest import action_reply

FULL = action_reply(
    "设定存在两处需要关注。",
    reason="设定审校完成",
    payload={
        "verdict": {
            "token": "SPEC-CHECK",
            "decision": "CONCERNS",
            "sections": {
                "缺失维度": [],
                "模糊表述": ["原文「深色鳞片」→ 应写明：深灰黑色（dark charcoal）"],
                "art bible 冲突": ["§4 主色仅限冷色 ←→ 设定「橙红腹部」"],
            },
            "constraints": [
                {"item": "尾巴", "value": "2 条，彼此分离"},
                {"item": "眼睛", "value": "红色发光"},
                {"item": "手", "value": "三指利爪"},
            ],
        }
    },
)


def test_Action裁决与结构化分节一起拿到() -> None:
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
        text = action_reply(
            payload={
                "verdict": {
                    "token": "SPEC-CHECK",
                    "decision": decision,
                    "sections": {},
                    "constraints": [],
                }
            }
        )
        assert parse_verdict(text).decision == decision


def test_Action块前的可读正文不影响裁决() -> None:
    verdict = parse_verdict(
        action_reply(
            "正文可以解释结论。",
            payload={
                "verdict": {
                    "token": "SPEC-CHECK",
                    "decision": "APPROVE",
                    "sections": {"缺失维度": []},
                    "constraints": [],
                }
            },
        )
    )

    assert verdict.approved is True


def test_裁决枚举大小写严格() -> None:
    text = action_reply(
        payload={
            "verdict": {
                "token": "SPEC-CHECK",
                "decision": "approve",
                "sections": {},
                "constraints": [],
            }
        }
    )
    with pytest.raises(VerdictError, match="decision 非法"):
        parse_verdict(text)


def test_正文里的旧裁决文本不算结构化结果() -> None:
    """正文即使提到旧 token，也必须以 payload.verdict 为准。"""
    text = action_reply("我看了一遍设定。\nSPEC-CHECK: APPROVE")
    with pytest.raises(VerdictError, match="缺少 SPEC-CHECK"):
        parse_verdict(text)


def test_没按契约说话会报协议错误() -> None:
    """看不出裁决是模型的格式事故，不能默认成 REJECT。"""
    with pytest.raises(ProtocolError, match="Action"):
        parse_verdict("SPEC-CHECK: MAYBE\n")
    with pytest.raises(ProtocolError, match="Action"):
        parse_verdict("   \n\n")


def test_换成四视图的token也走同一套() -> None:
    text = action_reply(
        payload={
            "verdict": {
                "token": "VIEW-CHECK",
                "decision": "APPROVE",
                "sections": {},
                "constraints": [],
            }
        }
    )
    verdict = parse_verdict(text, parsing.VIEW_CHECK)

    assert verdict.token == "VIEW-CHECK"
    with pytest.raises(VerdictError):
        parse_verdict(text, parsing.SPEC_CHECK)


def test_没有约束清单就是空的() -> None:
    """REJECT 的时候允许没有可抽取的硬性约束。"""
    text = action_reply(
        payload={
            "verdict": {
                "token": "SPEC-CHECK",
                "decision": "REJECT",
                "sections": {"缺失维度": ["环境设定"]},
                "constraints": [],
            }
        }
    )
    verdict = parse_verdict(text)

    assert verdict.constraints == ()
    assert verdict.sections["缺失维度"] == ("环境设定",)


def test_约束缺少项名会报错() -> None:
    text = action_reply(
        payload={
            "verdict": {
                "token": "SPEC-CHECK",
                "decision": "APPROVE",
                "sections": {},
                "constraints": [{"item": "", "value": "整体看着不错"}],
            }
        }
    )
    with pytest.raises(VerdictError, match="非空字符串"):
        parse_verdict(text)


def test_分节内容按字符串数组原样保留() -> None:
    text = action_reply(
        payload={
            "verdict": {
                "token": "SPEC-CHECK",
                "decision": "APPROVE",
                "sections": {"缺失维度": ["无"]},
                "constraints": [],
            }
        }
    )
    verdict = parse_verdict(text)

    assert verdict.sections["缺失维度"] == ("无",)
