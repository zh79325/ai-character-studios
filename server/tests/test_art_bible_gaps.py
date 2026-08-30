"""art bible 的完整度提醒。

这份文档的六节是下游真的会按节去抽的东西：第 6 节进 negative prompt，其余几节被
`prompt_smith` 拼进 prompt、被 `vision_reviewer` 当判定标准。所以「还留着待填」不是排版
问题而是会一路传下去的空洞，得在用户按「确认沉淀」之前就说出来。

给的是提醒不是禁止：写一半先存下来、回头接着聊是正当用法。
"""

from __future__ import annotations

from atelier.assets import layout, projects
from atelier.settings import get_settings


def bible(*sections: str) -> str:
    return "# 项目 视觉规范\n\n" + "\n\n".join(sections) + "\n"


SECTIONS = (
    "## 1 视觉身份一句话\n\n冷光下的湿滑金属。",
    "## 2 氛围与光照\n\n| 项 | 规则 |\n|---|---|\n| 情绪目标 | 冷峻 |",
    "## 3 形状语言\n\n- 剪影以长斜面为主",
    "## 4 色彩系统\n\n| 色 | 十六进制 | 语义 |\n|---|---|---|\n| 主色 | `#12324a` | 环境金属 |",
    "## 5 资产标准\n\n| 项 | 规则 |\n|---|---|\n| 渲染图尺寸 | 2048×2048 |",
    "## 6 风格禁止项\n\n- 蒸汽朋克齿轮",
)

FULL = bible(*SECTIONS)


def test_六节都填齐了就没有提醒() -> None:
    assert projects.art_bible_gaps(FULL) == []


def test_空文档一句话说清() -> None:
    assert projects.art_bible_gaps("   ") == ["这份 art bible 还是空的"]


def test_模板原样交上来六节全报待填() -> None:
    """模板里每节都是「待填」，照原样沉淀等于把占位符送进每一张图的 prompt。"""
    template = get_settings().templates_dir / layout.ART_BIBLE
    gaps = projects.art_bible_gaps(template.read_text(encoding="utf-8"))

    assert len(gaps) == len(projects.ART_BIBLE_SECTIONS)
    assert all("待填" in gap for gap in gaps)


def test_缺一节就点名缺哪一节() -> None:
    without_colors = bible(*[s for s in SECTIONS if "色彩系统" not in s])

    assert projects.art_bible_gaps(without_colors) == ["缺「4 色彩系统」一节"]


def test_只有标题没有内容也算没填() -> None:
    """表格只剩表头与分隔线时，看着有东西其实一条规则都没有。"""
    hollow = bible(
        *[s if "氛围" not in s else "## 2 氛围与光照\n\n<!-- 待写 -->\n|---|---|" for s in SECTIONS]
    )

    assert projects.art_bible_gaps(hollow) == ["「2 氛围与光照」下面是空的"]


def test_留着一处待填就报那一节() -> None:
    half = FULL.replace("冷峻", "待填")

    assert projects.art_bible_gaps(half) == ["「2 氛围与光照」里还留着「待填」"]


def test_换了文件名不影响判定() -> None:
    """节的标题里带别的字（编号换成中文、后面加副标题）也要认出来。"""
    renamed = FULL.replace("## 1 视觉身份一句话", "## 1 视觉身份一句话（不可动摇）")

    assert projects.art_bible_gaps(renamed) == []
