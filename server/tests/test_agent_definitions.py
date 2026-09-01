"""工程级 Agent 定义的校验与加载。

九份 md 是工程资产，任何一份写坏都必须在这里炸掉，而不是等到跑会话时才发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.agents.definitions import (
    AgentDefinitionError,
    get_agent,
    load_registry,
    parse_agent_file,
)
from atelier.settings import get_settings

EXPECTED_AGENTS = {
    "game_designer",
    "image_i2i",
    "image_t2i",
    "model3d",
    "prompt_smith",
    "spec_reviewer",
    "spec_writer",
    "video_gen",
    "vision_reviewer",
}

GOOD_BODY = """你是测试用 Agent。

### 职责
干活。

### 输出格式
Markdown。

### 绝不可做
瞎编。
"""

GOOD_META = """---
agent_code: {code}
capability: text
role: 测试
max_turns: 3
conversational: false
memory_scope: none
context_budget: 1000
output_contract: markdown_spec
---
"""


def _write(tmp_path: Path, code: str, meta: str | None = None, body: str = GOOD_BODY) -> Path:
    path = tmp_path / f"{code}.md"
    head = GOOD_META.format(code=code) if meta is None else meta
    path.write_text(head + "\n" + body, encoding="utf-8")
    return path


def test_prompts_live_in_package_not_repo_root() -> None:
    """提示词随代码包走，不在仓库根的 seeds/ 下。"""
    settings = get_settings()
    assert settings.agent_prompts_dir.is_dir()
    assert settings.prompts_dir.parent.name == "atelier"
    assert not (settings.seeds_dir / "agents").exists()


def test_load_registry_covers_all_agents() -> None:
    registry = load_registry()
    assert set(registry) == EXPECTED_AGENTS


def test_get_agent_returns_definition() -> None:
    agent = get_agent("game_designer")
    assert agent.conversational is True
    assert agent.memory_scope == "project"
    assert agent.max_output_tokens is None
    assert agent.system_prompt.startswith("你是")


def test_get_agent_rejects_unknown_code() -> None:
    with pytest.raises(AgentDefinitionError, match="未定义的 agent_code"):
        get_agent("no_such_agent")


def test_reviewers_declare_verdict_contract() -> None:
    """裁决类 Agent 必须走 verdict 契约，后端只读首行。"""
    for code in ("spec_reviewer", "vision_reviewer"):
        assert get_agent(code).output_contract == "verdict"


def test_写设定的那个岗位就叫角色设计师() -> None:
    """岗位名会直接进提示词，行业里不存在的叫法会拉偏模型的自我定位。"""
    agent = get_agent("spec_writer")
    assert agent.role == "角色设计师"
    assert agent.max_output_tokens == 16384
    assert agent.system_prompt.startswith("你是这个项目的角色设计师（Character Designer）")


def test_角色设计的分歧必须走待选项抽屉() -> None:
    """角色页与立项页共用 ChoicePicker，提示词必须要求写出后端能解析的协议块。"""
    prompt = get_agent("spec_writer").system_prompt
    assert "[待选项]" in prompt
    assert "需要拍板的一律输出" in prompt
    assert "选项之间用 `|` 分隔" in prompt


def test_parse_ok(tmp_path: Path) -> None:
    definition = parse_agent_file(_write(tmp_path, "demo"))
    assert definition.agent_code == "demo"
    assert definition.allow_tools == []
    assert definition.max_output_tokens is None
    assert len(definition.source_hash) == 64


def test_reject_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "demo.md"
    path.write_text(GOOD_BODY, encoding="utf-8")
    with pytest.raises(AgentDefinitionError, match="缺少 YAML frontmatter"):
        parse_agent_file(path)


def test_reject_code_filename_mismatch(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo", meta=GOOD_META.format(code="other"))
    with pytest.raises(AgentDefinitionError, match="与文件名不一致"):
        parse_agent_file(path)


def test_reject_bad_capability(tmp_path: Path) -> None:
    meta = GOOD_META.format(code="demo").replace("capability: text", "capability: telepathy")
    with pytest.raises(AgentDefinitionError, match="capability"):
        parse_agent_file(_write(tmp_path, "demo", meta=meta))


def test_reject_bad_max_output_tokens(tmp_path: Path) -> None:
    meta = GOOD_META.format(code="demo").replace(
        "context_budget: 1000", "context_budget: 1000\nmax_output_tokens: 0"
    )
    with pytest.raises(AgentDefinitionError, match="max_output_tokens 必须是正整数"):
        parse_agent_file(_write(tmp_path, "demo", meta=meta))


def test_reject_missing_section(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("### 绝不可做\n瞎编。\n", "")
    with pytest.raises(AgentDefinitionError, match="绝不可做"):
        parse_agent_file(_write(tmp_path, "demo", body=body))


def test_reject_body_not_starting_with_persona(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("你是测试用 Agent。", "本 Agent 负责测试。")
    with pytest.raises(AgentDefinitionError, match="你是"):
        parse_agent_file(_write(tmp_path, "demo", body=body))
