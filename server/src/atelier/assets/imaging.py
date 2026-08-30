"""出图之后当场量一遍：底是不是纯白、尺寸对不对、四张是不是同一规格。

**为什么要机器先看一遍**：四视图这一步的产物要拿去做模型输入，底色不纯（浅灰渐变、地面
投影、网格）会被建模当成几何信息吃进去，而人眼在缩略图上看不出 240 与 255 的差别。让
`vision_reviewer` 去判也不合适——它擅长的是「双尾是不是分离」这类语义问题，「边缘 3% 的像素
是不是 255」是算得出来的事实，算得出来的就不该花一次调用去猜。

判定口径只有阈值，没有方差统计：**背景纯净的定义就是「够白」**。浅灰渐变哪怕过渡得很匀，
落在 240 上就是不合格，方差反而会把它放过去。

透明底单独记一条。它在模型眼里跟白底几乎等价，所以生图端很容易给回来一张 alpha 通道抠好
的图，但素材库的契约是白底 PNG：下游拼图、切图工具遇到透明像素会各自补一个自己的底色，
到时候四张图的底就不是同一个颜色了。

尺寸只跟「要求」比，不做自动缩放。生成端把 2048 做成 1024 是它没按参数走，缩放上去等于把
这个事实抹掉，而清晰度已经丢了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import structlog
from PIL import Image, ImageChops, UnidentifiedImageError

_log = structlog.get_logger(__name__)

WHITE_LEVEL = 245
"""白的下限（单通道）。留 10 级余量是给 PNG/JPEG 压缩的噪点，再松就压不住浅灰底了。"""

EDGE_BAND = 0.05
"""边框带宽度占短边的比例。只看这一圈：主体一般不会顶到画布边，顶到边的那点像素也确实是
背景该负责的地方。"""

MIN_EDGE_WHITE = 0.98
"""边框带里白像素的占比下限。留 2% 是给发丝、飘带这类越界到边上的主体细节。"""

MAX_TRANSPARENT = 0.005
"""透明像素占比上限。超过就是拿了张抠图，不是白底图。"""

MIN_INK = 0.01
"""非白像素占比下限。低于这个数说明主体几乎没画出来——整张白纸也能满分通过背景检查，这条
是专门拦它的。"""

SIZE_TOLERANCE = 0.02
"""尺寸容差。供应商会按自己支持的档位取整，差几十个像素是常态；差一个档位就不是了。"""


class ImageUnreadable(ValueError):
    """字节解不开成图。多半是拿回来的地址给了 HTML 错误页或压缩包。"""


@dataclass(frozen=True, slots=True)
class Report:
    """一张图量出来的事实与问题清单。

    `problems` 里的每一句都是给人看的，直接进 `task_events` 与前端卡片：数字要带上，
    「背景不够白」没法判断是差一点还是差很远。
    """

    width: int
    height: int
    fmt: str
    edge_white: float
    """边框带里白像素的占比。"""

    ink: float
    """全图非白像素的占比，约等于主体占了多大。"""

    transparent: float
    """半透明及全透明像素的占比。"""

    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"


def measure(raw: bytes, *, expect: tuple[int, int] | None = None) -> Report:
    """量一张图。`expect` 给了才校尺寸，不给就只看背景。

    全分辨率上算，不先缩图：缩图是插值，会把边上零星的深色像素平均成浅灰，正好糊掉这里
    要抓的东西。直方图在 Pillow 里是 C 实现的，4K 图也就几十毫秒。
    """
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            fmt = (image.format or "").upper()
            transparent = _transparent_ratio(image)
            rgb = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageUnreadable(f"这堆字节解不开成图：{exc}") from exc

    darkest = _darkest_channel(rgb)
    width, height = rgb.size
    ink = 1.0 - _white_ratio(darkest)
    edge_white = _edge_white_ratio(darkest)

    problems: list[str] = []
    if edge_white < MIN_EDGE_WHITE:
        problems.append(
            f"背景不是纯白：边缘只有 {edge_white:.1%} 的像素够白（要求 {MIN_EDGE_WHITE:.0%}），"
            "常见原因是带了渐变、地面投影或环境"
        )
    if transparent > MAX_TRANSPARENT:
        problems.append(
            f"有 {transparent:.1%} 的像素是透明的，这是抠好的图不是白底图，"
            "下游拼图时会各补一个自己的底色"
        )
    if ink < MIN_INK:
        problems.append(f"整张几乎是白的（非白像素只有 {ink:.2%}），主体没画出来")
    if expect is not None:
        problems.extend(_size_problems(width, height, expect))

    report = Report(
        width=width,
        height=height,
        fmt=fmt,
        edge_white=round(edge_white, 4),
        ink=round(ink, 4),
        transparent=round(transparent, 4),
        problems=tuple(problems),
    )
    if not report.ok:
        _log.info("imaging.rejected", size=report.size, problems=report.problems)
    return report


def measure_file(path: Path, *, expect: tuple[int, int] | None = None) -> Report:
    """量磁盘上的一张图。已经落在 `tmp/` 里的候选走这条路。"""
    return measure(path.read_bytes(), expect=expect)


def same_size(reports: Sequence[Report]) -> str | None:
    """四张是不是同一规格。不一致返回一句人话，一致返回 None。

    四视图要拼到一起看、要一起喂给建模，尺寸不齐会让「侧面比正面矮一截」这种错觉变成真的
    比例问题。单张全过了也得再过这一关。
    """
    sizes = {report.size for report in reports}
    if len(sizes) <= 1:
        return None
    return "四张图的尺寸不一致（" + "、".join(sorted(sizes)) + "），拼视图与建模都要求同一规格"


def _size_problems(width: int, height: int, expect: tuple[int, int]) -> list[str]:
    want_width, want_height = expect
    off = max(
        abs(width - want_width) / max(want_width, 1),
        abs(height - want_height) / max(want_height, 1),
    )
    if off <= SIZE_TOLERANCE:
        return []
    return [f"尺寸是 {width}x{height}，要的是 {want_width}x{want_height}（差了 {off:.0%}）"]


def _darkest_channel(rgb: Image.Image) -> Image.Image:
    """逐像素取 R/G/B 里最小的那一档。

    只有三个通道都够亮才算白，所以最小值就是这个像素的「白度」。取最小而不是取灰度：浅蓝
    底的灰度值也很高，会被当成白底放过去。
    """
    red, green, blue = rgb.split()
    return ImageChops.darker(ImageChops.darker(red, green), blue)


def _white_ratio(darkest: Image.Image) -> float:
    histogram = darkest.histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0
    return sum(histogram[WHITE_LEVEL:]) / total


def _edge_white_ratio(darkest: Image.Image) -> float:
    """只统计外圈那一条带。四块互不重叠地切，免得四个角被算两次。"""
    width, height = darkest.size
    band = max(1, round(min(width, height) * EDGE_BAND))
    if band * 2 >= min(width, height):
        return _white_ratio(darkest)

    boxes = (
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, band, band, height - band),
        (width - band, band, width, height - band),
    )
    white = 0
    total = 0
    for box in boxes:
        histogram = darkest.crop(box).histogram()
        white += sum(histogram[WHITE_LEVEL:])
        total += sum(histogram)
    if total == 0:
        return 0.0
    return white / total


def _transparent_ratio(image: Image.Image) -> float:
    """半透明也算。alpha=254 的一圈边缘在白底上看不见，落到深色底上就是一道毛边。

    调色板图要先展开成 RGBA：它的透明信息挂在 `info["transparency"]` 上，不在通道里，直接
    看 bands 会当它是不透明的。
    """
    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if "A" not in image.getbands():
        return 0.0
    histogram = image.getchannel("A").histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0
    return sum(histogram[:255]) / total
