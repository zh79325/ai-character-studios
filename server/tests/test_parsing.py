"""Agent 输出的结构块解析。

这些用例的实际内容都是模型真实会吐出来的样子：把块包在 ``` 里、半角冒号、照抄模板占位
符、把三条偏好分行写在一个 `preference:` 下面。解析歪了的后果不是报错而是静默——把占位符
当结论存进记忆、拿半截文本覆盖定稿，所以每种走形都单独钉一条。
"""

from __future__ import annotations

from atelier.agents import parsing


def test_单个草稿块抽出路径与正文() -> None:
    text = """好的，按你说的改。

[草稿开始: art-bible.md]
# 视觉规范

## 1 视觉身份一句话
冷光下的湿滑金属。
[草稿结束]

还要改哪节？
"""
    (draft,) = parsing.parse_drafts(text)

    assert draft.target_path == "art-bible.md"
    assert draft.content.startswith("# 视觉规范")
    assert draft.content.endswith("\n")
    assert "[草稿结束]" not in draft.content


def test_一轮可以出两份草稿() -> None:
    text = """[草稿开始: art-bible.md]
# 规范
[草稿结束]

[草稿开始: project.json]
{"style": {"art_style": "国风"}}
[草稿结束]
"""
    drafts = parsing.parse_drafts(text)

    assert [d.target_path for d in drafts] == ["art-bible.md", "project.json"]


def test_草稿外面包着代码围栏时只削掉围栏() -> None:
    text = """[草稿开始: spec.md]
```markdown
# 设定

代码示例：

```python
print(1)
```
```
[草稿结束]
"""
    (draft,) = parsing.parse_drafts(text)

    assert draft.content.startswith("# 设定")
    # 正文里引用的围栏是内容的一部分，不能跟着削掉
    assert "```python" in draft.content


def test_半角冒号与占位符路径() -> None:
    assert parsing.parse_drafts("[草稿开始:  spec.md ]\n正文\n[草稿结束]\n")[0].target_path == (
        "spec.md"
    )
    assert parsing.parse_drafts("[草稿开始: <文件名>]\n正文\n[草稿结束]\n") == ()


def test_没有结束标记就不算草稿() -> None:
    """流式被打断、模型说到一半停了都会留下半个块，此时宁可当没有草稿。"""
    assert parsing.parse_drafts("[草稿开始: art-bible.md]\n# 只写了一半\n") == ()


def test_进度块三项都解析出来() -> None:
    text = """聊得差不多了。

[对焦进度]
已定：题材是赛博朋克
待定：面数预算
下一步：确认目标平台
"""
    progress = parsing.parse_progress(text)

    assert progress is not None
    assert progress.decisions == ("题材是赛博朋克",)
    assert progress.open_questions == ("面数预算",)
    assert progress.next_step == "确认目标平台"


def test_进度块的多行条目与列表符号() -> None:
    text = """[对焦进度]
已定：
- 题材是赛博朋克
- 第三人称
待定：
* 面数预算
下一步: 聊色彩系统
"""
    progress = parsing.parse_progress(text)

    assert progress is not None
    assert progress.decisions == ("题材是赛博朋克", "第三人称")
    assert progress.open_questions == ("面数预算",)
    assert progress.next_step == "聊色彩系统"


def test_暂无与占位符不算条目() -> None:
    text = "[对焦进度]\n已定：暂无\n待定：<一行一条>\n下一步：暂无\n"
    progress = parsing.parse_progress(text)

    assert progress is not None
    assert progress.is_empty()


def test_没有进度块与有块但空是两回事() -> None:
    """没写就是没写，不能替 Agent 补一个空进度覆盖掉上一轮的结论。"""
    assert parsing.parse_progress("就聊两句") is None


def test_进度块到下一个标记就结束() -> None:
    text = """[对焦进度]
已定：题材定了
待定：暂无

[草稿开始: art-bible.md]
已定：这行是草稿正文，不是进度
[草稿结束]
"""
    progress = parsing.parse_progress(text)

    assert progress is not None
    assert progress.decisions == ("题材定了",)


def test_记忆块按类别收条目() -> None:
    text = """[项目记忆]
preference: 喜欢冷色调
taboo: 不要蒸汽朋克齿轮
fact: 目标平台是 PC
"""
    items = parsing.parse_memories(text)

    assert [(i.kind, i.content) for i in items] == [
        ("preference", "喜欢冷色调"),
        ("taboo", "不要蒸汽朋克齿轮"),
        ("fact", "目标平台是 PC"),
    ]


def test_同一类别下的后续行沿用类别() -> None:
    text = """[项目记忆]
preference：
- 喜欢冷色调
- 讨厌高饱和
taboo：不要齿轮
"""
    items = parsing.parse_memories(text)

    assert [(i.kind, i.content) for i in items] == [
        ("preference", "喜欢冷色调"),
        ("preference", "讨厌高饱和"),
        ("taboo", "不要齿轮"),
    ]


def test_记忆块里的占位符跳过() -> None:
    text = "[项目记忆]\npreference: <用户明确表达的偏好，一行一条>\ntaboo: 暂无\nfact: PC 平台\n"

    assert [i.content for i in parsing.parse_memories(text)] == ["PC 平台"]


def test_没有记忆块返回空() -> None:
    assert parsing.parse_memories("普通回答") == ()


def test_整轮解析原文原样保留() -> None:
    text = """先说结论。

[对焦进度]
已定：题材定了
待定：暂无
下一步：聊色彩

[草稿开始: art-bible.md]
# 规范
[草稿结束]

[项目记忆]
preference: 冷色调
"""
    turn = parsing.parse_turn(text)

    assert turn.text == text
    assert turn.has_draft
    assert turn.progress is not None
    assert turn.progress.decisions == ("题材定了",)
    assert [i.content for i in turn.memories] == ["冷色调"]
