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


def test_命名建议一行一条() -> None:
    text = """想了几个名字：

[项目命名建议]
- 名称: 赤瞳系列 / 代号: chitong / 理由: 主角的红瞳是最强记忆点
- 名称: 湿滑金属 / 代号: wet_metal / 理由: 直指美术方向
"""
    options = parsing.parse_naming(text)

    assert [(o.name, o.code) for o in options] == [
        ("赤瞳系列", "chitong"),
        ("湿滑金属", "wet_metal"),
    ]
    assert options[0].reason == "主角的红瞳是最强记忆点"


def test_命名建议分行写也收() -> None:
    text = """[项目命名建议]
名称：赤瞳系列
代号：chitong
说明：红瞳是记忆点
"""
    (option,) = parsing.parse_naming(text)

    assert (option.name, option.code, option.reason) == ("赤瞳系列", "chitong", "红瞳是记忆点")


def test_不合法的代号降级成没给() -> None:
    """代号要进目录、日志与外部 API 参数，模型给中文或大写时当没给，让用户自己填。"""
    text = """[项目命名建议]
- 名称: 赤瞳 / 代号: 赤瞳 / 理由: 一
- 名称: 湿滑金属 / 代号: Wet Metal / 理由: 二
- 名称: 临时代号 / 代号: draft-abc / 理由: 三
"""
    options = parsing.parse_naming(text)

    assert [o.code for o in options] == ["", "", "draft-abc"]  # draft- 由提交那一关拦


def test_没名称的条目丢掉() -> None:
    """面板上不该出现一个只有代号的空选项。"""
    text = "[项目命名建议]\n- 代号: chitong / 理由: 只给了代号\n- 名称: <中文项目名>\n"

    assert parsing.parse_naming(text) == ()


def test_命名块到下一个标记就结束() -> None:
    text = """[项目命名建议]
- 名称: 赤瞳系列 / 代号: chitong

[对焦进度]
已定：名称: 这行是进度不是命名
下一步：等你选
"""
    options = parsing.parse_naming(text)

    assert [o.name for o in options] == ["赤瞳系列"]


def test_没有命名块返回空() -> None:
    assert parsing.parse_naming("名称这个词出现在正文里也不算") == ()


def test_待选项一次给好几组() -> None:
    text = """这些是我的建议。

[待选项]
- 项: 写实到卡通的位置 / 选项: 3:7 偏卡通 | 5:5 折中 | 7:3 偏写实 / 推荐: 3:7 偏卡通
- 项: 面数预算 / 选项: 8k | 15k | 30k / 推荐: 15k
"""
    groups = parsing.parse_choices(text)

    assert [(g.item, g.options, g.recommended, g.multiple) for g in groups] == [
        ("写实到卡通的位置", ("3:7 偏卡通", "5:5 折中", "7:3 偏写实"), ("3:7 偏卡通",), False),
        ("面数预算", ("8k", "15k", "30k"), ("15k",), False),
    ]


def test_待选项分行写也收() -> None:
    text = """[待选项]
项：色彩基调
选项：明亮白日｜霓虹夜景
推荐：霓虹夜景
"""
    (group,) = parsing.parse_choices(text)

    assert (group.item, group.options, group.recommended) == (
        "色彩基调",
        ("明亮白日", "霓虹夜景"),
        ("霓虹夜景",),
    )


def test_选项文字里的斜杠不当分隔() -> None:
    """选项之间只认 `|`：一行里的 `/` 是段分隔，切进选项就会把一个选项劈成两个。"""
    (group,) = parsing.parse_choices("[待选项]\n- 项: 比例 / 选项: 写实/仿真 | 夸张/Q 版\n")

    assert group.options == ("写实/仿真", "夸张/Q 版")


def test_只有一个选项的组丢掉() -> None:
    """摆成选择组件却只有一个能点的，等于逼用户按同意。"""
    text = "[待选项]\n- 项: 只有一个 / 选项: 就这个\n- 项: 没选项 / 选项: <A | B>\n"

    assert parsing.parse_choices(text) == ()


def test_对不上号的推荐当没给() -> None:
    """预选不中的推荐会让用户以为默认值丢了。"""
    (group,) = parsing.parse_choices("[待选项]\n- 项: 光照 / 选项: 冷白 | 暖黄 / 推荐: 中性\n")

    assert group.recommended == ()


def test_多选的那几组推荐能给好几个() -> None:
    """可叠加的维度本来就是好几项，只能预选一个等于逼用户丢掉其他的。"""
    text = """[待选项]
- 项: 参考作品锚点 / 选项: 银翼 | 攻壳 | 2077 / 多选: 是 / 推荐: 银翼 | 2077
- 项: 主光方向 / 选项: 逆光 | 侧逆光 / 多选: 否 / 推荐: 侧逆光
"""
    first, second = parsing.parse_choices(text)

    assert (first.multiple, first.recommended) == (True, ("银翼", "2077"))
    assert (second.multiple, second.recommended) == (False, ("侧逆光",))


def test_单选组的推荐只取第一个() -> None:
    """互排的维度预选两个，用户看到的就是两个重写彼此的结论。"""
    (group,) = parsing.parse_choices("[待选项]\n- 项: 色温 / 选项: 冷 | 暖 / 推荐: 冷 | 暖\n")

    assert (group.multiple, group.recommended) == (False, ("冷",))


def test_没写多选就是单选() -> None:
    """读不到声明时摆成多选，用户会同时选上两个互排的值。"""
    (group,) = parsing.parse_choices("[待选项]\n- 项: 比例 / 选项: 6头身 | 7头身\n")

    assert group.multiple is False


def test_待选项块到下一个标记就结束() -> None:
    text = """[待选项]
- 项: 色调 / 选项: 冷 | 暖

[对焦进度]
已定：项: 这行是进度不是选项
下一步：等你点
"""
    groups = parsing.parse_choices(text)

    assert [g.item for g in groups] == ["色调"]


def test_没有待选项块返回空() -> None:
    assert parsing.parse_choices("选项这个词出现在正文里也不算") == ()


def test_一轮只摆前四组() -> None:
    """模型一口气列十几项时只留前四项，剩下的等这一批拍完再问。"""
    lines = "\n".join(f"- 项: 第{i} 项 / 选项: A | B" for i in range(1, 7))
    groups = parsing.parse_choices(f"[待选项]\n{lines}\n")

    assert [g.item for g in groups] == ["第1 项", "第2 项", "第3 项", "第4 项"]


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

[项目命名建议]
- 名称: 赤瞳系列 / 代号: chitong / 理由: 记忆点
"""
    turn = parsing.parse_turn(text)

    assert turn.text == text
    assert turn.has_draft
    assert turn.progress is not None
    assert turn.progress.decisions == ("题材定了",)
    assert [i.content for i in turn.memories] == ["冷色调"]
    assert [o.code for o in turn.naming] == ["chitong"]
