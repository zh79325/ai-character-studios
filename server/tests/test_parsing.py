"""统一 Action JSON 契约与领域 payload 解析。"""

from __future__ import annotations

import json

import pytest

from atelier.agents import parsing


def action_reply(
    text: str = "正文",
    *,
    action: str = "done",
    target_agent: str | None = None,
    reason: str = "本轮完成",
    payload: dict[str, object] | None = None,
) -> str:
    body = json.dumps(
        {
            "action": action,
            "target_agent": target_agent,
            "reason": reason,
            "payload": payload or {},
        },
        ensure_ascii=False,
        indent=2,
    )
    return f"{text}\n\n{parsing.ACTION_START}\n{body}\n{parsing.ACTION_END}"


def test_完整Action与领域数据一次解析() -> None:
    text = action_reply(
        "方案已整理。",
        action="ask_user",
        reason="需要确认方案",
        payload={
            "progress": {
                "decisions": ["题材是赛博朋克"],
                "open_questions": ["面数预算"],
                "next_step": "确认预算",
            },
            "choices": [
                {
                    "item": "面数预算",
                    "options": ["8k", "15k"],
                    "recommended": ["15k"],
                    "multiple": False,
                }
            ],
            "drafts": [{"target_path": "art-bible.md", "content": "# 规范"}],
            "memories": [{"scope": "project", "kind": "preference", "content": "喜欢冷色调"}],
            "naming": [{"name": "赤瞳", "code": "red-eye", "reason": "记忆点"}],
        },
    )

    turn = parsing.parse_turn(text)

    assert turn.text == text
    assert turn.action.action is parsing.ActionType.ASK_USER
    assert turn.action.target_agent is None
    assert turn.progress is not None
    assert turn.progress.decisions == ("题材是赛博朋克",)
    assert turn.progress.open_questions == ("面数预算",)
    assert turn.progress.next_step == "确认预算"
    assert turn.choices[0].recommended == ("15k",)
    assert turn.drafts[0].content == "# 规范\n"
    assert turn.memories[0].scope == "project"
    assert turn.naming[0].code == "red-eye"


def test_handoff使用独立Agent枚举() -> None:
    parsed = parsing.parse_action(
        action_reply(
            action="handoff",
            target_agent="spec_writer",
            reason="交给角色设计师",
        )
    )

    assert parsed.action is parsing.ActionType.HANDOFF
    assert parsed.target_agent is parsing.AgentCode.SPEC_WRITER


@pytest.mark.parametrize(
    "text",
    [
        "只有正文",
        f"{parsing.ACTION_START}\n{{}}\n{parsing.ACTION_END}\n块后还有字",
        (
            f"{parsing.ACTION_START}\n{{}}\n{parsing.ACTION_END}\n"
            f"{parsing.ACTION_START}\n{{}}\n{parsing.ACTION_END}"
        ),
    ],
)
def test_Action必须唯一且位于末尾(text: str) -> None:
    with pytest.raises(parsing.ProtocolError):
        parsing.parse_action(text)


def test_Action中间必须是严格JSON() -> None:
    text = f"{parsing.ACTION_START}\n{{'action': 'done',}}\n{parsing.ACTION_END}"

    with pytest.raises(parsing.ProtocolError, match="合法 JSON"):
        parsing.parse_action(text)


@pytest.mark.parametrize(
    ("fragment", "error"),
    [
        ('"action": "done", "action": "blocked"', "重复字段"),
        ('"action": NaN', "非法常量"),
    ],
)
def test_Action拒绝JSON扩展语法(fragment: str, error: str) -> None:
    body = "{" + fragment + ', "target_agent": null, "reason": "完成", "payload": {}}'
    text = f"{parsing.ACTION_START}\n{body}\n{parsing.ACTION_END}"

    with pytest.raises(parsing.ProtocolError, match=error):
        parsing.parse_action(text)


def test_Action的reason必须是非空单句() -> None:
    with pytest.raises(parsing.ProtocolError, match="单句"):
        parsing.parse_action(action_reply(reason="先完成。再交付"))


def test_Action拒绝未知字段() -> None:
    text = action_reply()
    text = text.replace('"payload": {}', '"payload": {},\n  "extra": true')

    with pytest.raises(parsing.ProtocolError, match="未声明字段"):
        parsing.parse_action(text)


def test_payload拒绝未知字段() -> None:
    with pytest.raises(parsing.ProtocolError, match="未声明字段"):
        parsing.parse_action(action_reply(payload={"other": []}))


def test_handoff必须有合法目标且其他动作目标必须为空() -> None:
    with pytest.raises(parsing.ProtocolError, match="target_agent"):
        parsing.parse_action(action_reply(action="handoff"))
    with pytest.raises(parsing.ProtocolError, match="target_agent"):
        parsing.parse_action(action_reply(action="handoff", target_agent="unknown"))
    with pytest.raises(parsing.ProtocolError, match="必须是 null"):
        parsing.parse_action(action_reply(target_agent="spec_writer"))


def test_choices必须配合ask_user() -> None:
    choices = [
        {
            "item": "色温",
            "options": ["冷", "暖"],
            "recommended": ["冷"],
            "multiple": False,
        }
    ]

    with pytest.raises(parsing.ProtocolError, match="ask_user"):
        parsing.parse_action(action_reply(payload={"choices": choices}))


def test_choices字段严格校验() -> None:
    bad = [
        {
            "item": "色温",
            "options": ["冷"],
            "recommended": ["冷"],
            "multiple": False,
        }
    ]
    with pytest.raises(parsing.ProtocolError, match="至少要有两个"):
        parsing.parse_action(action_reply(action="ask_user", payload={"choices": bad}))


def test_memory_scope与类别严格校验() -> None:
    with pytest.raises(parsing.ProtocolError, match="scope 非法"):
        parsing.parse_action(
            action_reply(
                payload={"memories": [{"scope": "global", "kind": "fact", "content": "事实"}]}
            )
        )


def test_result状态必须与Action一致() -> None:
    result = {"status": "success", "artifacts": [], "error": None}
    with pytest.raises(parsing.ProtocolError, match="action 必须是 done"):
        parsing.parse_action(action_reply(action="blocked", payload={"result": result}))


def test_素材规格从payload解析并标准化() -> None:
    spec_payload = {
        "code": "ASSET-CT-001",
        "name": "赤瞳 渲染图",
        "category": "character",
        "size": "2048x2048",
        "format": "png",
        "file_name": "character_赤瞳_渲染图.png",
        "description": "一只双尾兽。",
        "anchors": "§1 冷调工业写实",
        "constraints": ["双尾数量=2", "瞳色=赤红"],
        "view_background_color": "#12abEf（高反差蓝绿）",
        "prompt": "standing pose, TWO distinct tails",
        "negative_prompt": "watermark",
    }
    text = action_reply(payload={"asset_specs": [spec_payload]})

    (spec,) = parsing.parse_asset_specs(text)

    assert spec.code == "ASSET-CT-001"
    assert (spec.width, spec.height) == (2048, 2048)
    assert spec.constraints == ("双尾数量=2", "瞳色=赤红")
    assert spec.view_background_color == "#12ABEF"
    assert spec.gaps() == ()
    assert spec.as_dict()["card"] == text


def test_素材规格缺业务值交给领域校验() -> None:
    payload = {
        "code": "ASSET-CT-002",
        "name": "赤瞳",
        "category": "character",
        "size": "",
        "format": "png",
        "file_name": "images/赤瞳.png",
        "description": "",
        "anchors": "",
        "constraints": [],
        "view_background_color": "transparent",
        "prompt": "standing pose",
        "negative_prompt": "",
    }

    (spec,) = parsing.parse_asset_specs(action_reply(payload={"asset_specs": [payload]}))

    assert set(spec.gaps()) == {"尺寸", "文件名", "四视图背景色", "negative_prompt"}


def test_裁决从payload解析() -> None:
    text = action_reply(
        payload={
            "verdict": {
                "token": "SPEC-CHECK",
                "decision": "REJECT",
                "sections": {"缺失维度": ["环境设定"]},
                "constraints": [{"item": "尾巴", "value": "2 条"}],
            }
        }
    )

    verdict = parsing.parse_verdict(text, parsing.SPEC_CHECK)

    assert verdict.rejected
    assert verdict.sections["缺失维度"] == ("环境设定",)
    assert verdict.constraints[0].item == "尾巴"


def test_裁决token不匹配会失败() -> None:
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
    with pytest.raises(parsing.VerdictError, match="期待 SPEC-CHECK"):
        parsing.parse_verdict(text, parsing.SPEC_CHECK)


def test_程序生成Action可被反向解析() -> None:
    text = parsing.with_action(
        "完成。",
        parsing.ActionType.DONE,
        reason="生成成功",
        payload={"result": {"status": "success", "artifacts": [], "error": None}},
    )

    parsed = parsing.parse_action(text)

    assert parsed.action is parsing.ActionType.DONE
    assert parsed.reason == "生成成功"


def test_隐藏完整与流式Action块() -> None:
    complete = action_reply("用户正文")
    partial = f'用户正文\n\n{parsing.ACTION_START}\n{{"action":'

    assert parsing.strip_protocol_blocks(complete) == "用户正文"
    assert parsing.strip_protocol_blocks(partial) == "用户正文"
