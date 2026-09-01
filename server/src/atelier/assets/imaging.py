"""出图后检查目标纯色背景、透明像素、主体占比与尺寸。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import structlog
from PIL import Image, ImageChops, UnidentifiedImageError

_log = structlog.get_logger(__name__)

COLOR_TOLERANCE = 10
"""目标色各通道允许的压缩误差。"""

EDGE_BAND = 0.05
"""边框带宽度占短边的比例。"""

MIN_EDGE_MATCH = 0.98
"""边框带里匹配目标纯色的像素占比下限。"""

MAX_TRANSPARENT = 0.005
"""透明像素占比上限。"""

MIN_SUBJECT = 0.01
"""非背景像素占比下限，防止纯色空图通过。"""

SIZE_TOLERANCE = 0.02
"""尺寸容差。供应商按支持档位取整时允许少量偏差。"""


class ImageUnreadable(ValueError):
    """字节解不开成图。"""


@dataclass(frozen=True, slots=True)
class Report:
    """一张图量出来的事实与问题清单。"""

    width: int
    height: int
    fmt: str
    target_color: str
    edge_match: float
    """边框带里与目标背景色匹配的像素占比。"""

    subject: float
    """全图非目标背景色的像素占比，约等于主体占比。"""

    transparent: float
    """半透明及全透明像素的占比。"""

    problems: tuple[str, ...]

    @property
    def edge_white(self) -> float:
        """兼容旧调用；现在表示边缘目标色匹配率。"""
        return self.edge_match

    @property
    def ink(self) -> float:
        """兼容旧调用；现在表示主体占比。"""
        return self.subject

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"


def measure(
    raw: bytes,
    *,
    expect: tuple[int, int] | None = None,
    background_color: str = "#FFFFFF",
) -> Report:
    """量一张图；默认白色仅用于兼容，四视图会传入卡片指定的目标纯色。"""
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            fmt = (image.format or "").upper()
            transparent = _transparent_ratio(image)
            rgb = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageUnreadable(f"这堆字节解不开成图：{exc}") from exc

    target = _parse_color(background_color)
    distance = _color_distance(rgb, target)
    width, height = rgb.size
    subject = 1.0 - _match_ratio(distance)
    edge_match = _edge_match_ratio(distance)
    normalized = "#" + "".join(f"{channel:02X}" for channel in target)

    problems: list[str] = []
    if edge_match < MIN_EDGE_MATCH:
        problems.append(
            f"背景不符合目标纯色 {normalized}：边缘只有 {edge_match:.1%} 的像素匹配"
            f"（要求 {MIN_EDGE_MATCH:.0%}），常见原因是颜色错误、渐变、地面投影或环境"
        )
    if transparent > MAX_TRANSPARENT:
        problems.append(
            f"有 {transparent:.1%} 的像素是透明的；四视图要求不透明纯色 {normalized}，"
            "不能使用透明底、alpha channel 或棋盘格"
        )
    if subject < MIN_SUBJECT:
        problems.append(
            f"整张几乎都是背景色 {normalized}（主体像素只有 {subject:.2%}），主体没画出来"
        )
    if expect is not None:
        problems.extend(_size_problems(width, height, expect))

    report = Report(
        width=width,
        height=height,
        fmt=fmt,
        target_color=normalized,
        edge_match=round(edge_match, 4),
        subject=round(subject, 4),
        transparent=round(transparent, 4),
        problems=tuple(problems),
    )
    if not report.ok:
        _log.info("imaging.rejected", size=report.size, problems=report.problems)
    return report


def measure_file(
    path: Path,
    *,
    expect: tuple[int, int] | None = None,
    background_color: str = "#FFFFFF",
) -> Report:
    """量磁盘上的一张候选图。"""
    return measure(path.read_bytes(), expect=expect, background_color=background_color)


def same_size(reports: Sequence[Report]) -> str | None:
    """四张是不是同一规格。不一致返回一句人话，一致返回 None。"""
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


def _parse_color(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"背景色必须是 #RRGGBB，收到 {value!r}")
    try:
        channels = tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError as exc:
        raise ValueError(f"背景色必须是 #RRGGBB，收到 {value!r}") from exc
    return channels  # type: ignore[return-value]


def _color_distance(rgb: Image.Image, target: tuple[int, int, int]) -> Image.Image:
    reference = Image.new("RGB", rgb.size, target)
    red, green, blue = ImageChops.difference(rgb, reference).split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def _match_ratio(distance: Image.Image) -> float:
    histogram = distance.histogram()
    total = sum(histogram)
    return sum(histogram[: COLOR_TOLERANCE + 1]) / total if total else 0.0


def _edge_match_ratio(distance: Image.Image) -> float:
    """只统计外圈带；四块互不重叠，避免四个角重复计数。"""
    width, height = distance.size
    band = max(1, round(min(width, height) * EDGE_BAND))
    if band * 2 >= min(width, height):
        return _match_ratio(distance)

    boxes = (
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, band, band, height - band),
        (width - band, band, width, height - band),
    )
    matched = 0
    total = 0
    for box in boxes:
        histogram = distance.crop(box).histogram()
        matched += sum(histogram[: COLOR_TOLERANCE + 1])
        total += sum(histogram)
    return matched / total if total else 0.0


def _transparent_ratio(image: Image.Image) -> float:
    """半透明也算；调色板图先展开 RGBA。"""
    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if "A" not in image.getbands():
        return 0.0
    histogram = image.getchannel("A").histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0
    return sum(histogram[:255]) / total
