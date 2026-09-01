"""素材规格卡片的解析：字段抽取、续行、缺项与串味。

卡片是生图这一步的唯一规格，解析错一个字段，出来的图就不是用户说的那张。要钉的三件事：
**续行要拼成一行**（prompt 在模板里经常折行）、**卡片后面的提问段不能被吃进最后一个字段**、
**缺项要报缺项而不是悄悄给个空值**——空 prompt 发出去就是白烧一次额度。
"""

from __future__ import annotations

from atelier.agents.parsing import parse_asset_specs

ONE_CARD = """ASSET-CT-001 — 赤瞳 渲染图
类别：character
尺寸：2048x2048
格式：png
文件名：character_赤瞳_渲染图.png
视觉描述：一只双尾兽站在废弃电厂前，红瞳发光。
art bible 锚点：§1 冷调工业写实
硬性约束：双尾数量=2，瞳色=赤红
四视图背景色：#F2E8D5（暖米色）
prompt：standing pose, red eyes,
  TWO distinct tails, clearly separated,
  cinematic light, 8k
negative_prompt：background clutter, watermark, text
"""


def test_一张卡片各字段都对得上() -> None:
    (spec,) = parse_asset_specs(ONE_CARD)

    assert spec.code == "ASSET-CT-001"
    assert spec.name == "赤瞳 渲染图"
    assert spec.category == "character"
    assert (spec.width, spec.height) == (2048, 2048)
    assert spec.image_format == "png"
    assert spec.file_name == "character_赤瞳_渲染图.png"
    assert spec.anchors.startswith("§1")
    assert spec.constraints == ("双尾数量=2", "瞳色=赤红")
    assert spec.negative_prompt == "background clutter, watermark, text"
    assert spec.gaps() == ()


def test_折行的prompt拼成一行() -> None:
    """模板里的 prompt 很长，模型多半会折行，拼不回去就等于把层序拆断了。"""
    (spec,) = parse_asset_specs(ONE_CARD)

    assert spec.prompt == (
        "standing pose, red eyes, TWO distinct tails, clearly separated, cinematic light, 8k"
    )


def test_原文一并留着() -> None:
    """抽取过的结构与原文不一致时，原文才是证据。"""
    (spec,) = parse_asset_specs(ONE_CARD)

    assert "ASSET-CT-001" in spec.text
    assert "negative_prompt" in spec.text
    assert spec.as_dict()["card"] == spec.text


def test_卡片后面的提问段不会被吃进最后一个字段() -> None:
    """提示词允许在全部卡片之后另起一段提问，空行就是那道界。"""
    text = ONE_CARD + "\n另外，设定里没说尾巴的末端有没有毛，需要你确认一下。\n"

    (spec,) = parse_asset_specs(text)

    assert "尾巴的末端" not in spec.negative_prompt
    assert spec.negative_prompt == "background clutter, watermark, text"


def test_多张卡片各算一张() -> None:
    second = ONE_CARD.replace("ASSET-CT-001", "ASSET-CT-002").replace("渲染图", "正面视图")
    specs = parse_asset_specs(f"{ONE_CARD}\n{second}")

    assert [one.code for one in specs] == ["ASSET-CT-001", "ASSET-CT-002"]
    assert specs[1].file_name == "character_赤瞳_正面视图.png"


def test_正文里提一句编号不算卡片() -> None:
    """既没 prompt 也没文件名，说明那行只是引用了一个编号。"""
    assert parse_asset_specs("上一版 ASSET-CT-001 的比例不对，我重出一张。") == ()


def test_缺项要报出来而不是给个空值() -> None:
    """空 prompt 发出去就是白烧一次额度，还得用户自己看出问题在卡片上。"""
    text = """ASSET-CT-003 — 赤瞳 渲染图
类别：character
文件名：character_赤瞳.png
四视图背景色：#F2E8D5
prompt：standing pose
"""

    (spec,) = parse_asset_specs(text)

    assert spec.gaps() == ("尺寸", "negative_prompt")


def test_占位符当没填() -> None:
    """模型照抄模板时会留下 `{宽}x{高}` 这类占位符，认成真值等于把模板发给生图接口。"""
    text = """ASSET-CT-004 — 赤瞳 渲染图
类别：character
尺寸：{宽}x{高}
文件名：character_赤瞳.png
四视图背景色：#F2E8D5
prompt：standing pose
negative_prompt：{全局预设}
"""

    (spec,) = parse_asset_specs(text)

    assert spec.width == 0
    assert spec.negative_prompt == ""
    assert "尺寸" in spec.gaps()


def test_文件名带路径也算缺项() -> None:
    """文件名要落到素材的 images/ 下，带斜杠会把图写到别处去。"""
    text = ONE_CARD.replace(
        "文件名：character_赤瞳_渲染图.png", "文件名：images/character_赤瞳_渲染图.png"
    )

    (spec,) = parse_asset_specs(text)

    assert spec.gaps() == ("文件名",)


def test_引用块里的卡片也认() -> None:
    """模型爱用 ``` 包起来或加 > 引用，格式装饰不该让卡片解析不出来。"""
    quoted = "\n".join(f"> {line}" if line else ">" for line in ONE_CARD.splitlines())

    (spec,) = parse_asset_specs(quoted)

    assert spec.code == "ASSET-CT-001"
    assert spec.prompt.startswith("standing pose")
