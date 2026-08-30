"""出图当场的那道机器检查：底够不够白、尺寸对不对、四张齐不齐。

这一层没有网络也没有库，所以用真图钉真数字：合成一张纯白底的、一张浅灰渐变的、一张抠图
的，看它认不认得出来。要钉住的是**判定口径**——240 的浅灰底必须算不合格（人眼看不出，建模
会当成几何吃进去），而主体上的深色像素不能让整张图被判成脏。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from atelier.assets import imaging


def render(image: Image.Image, kind: str = "PNG") -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=kind)
    return buffer.getvalue()


def subject(
    size: tuple[int, int] = (200, 200),
    background: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """一张规整的素材图：正中一个深色主体，四周留白。"""
    image = Image.new("RGB", size, background)
    width, height = size
    box = (width // 4, height // 4, width * 3 // 4, height * 3 // 4)
    image.paste((30, 40, 60), box)
    return image


def test_白底图过关() -> None:
    report = imaging.measure(render(subject()))

    assert report.ok
    assert report.problems == ()
    assert report.size == "200x200"
    assert report.fmt == "PNG"
    assert report.edge_white == 1.0


def test_主体够深也不算脏() -> None:
    """主体本身是深色的，占了全图四分之一。这不该被算成背景问题。"""
    report = imaging.measure(render(subject()))

    assert report.ink == pytest.approx(0.25, abs=0.01)
    assert report.edge_white == 1.0


def test_浅灰底不算白() -> None:
    """人眼在缩略图上看不出 240 与 255，建模会当成几何吃进去，所以这里必须拦下。"""
    report = imaging.measure(render(subject(background=(240, 240, 240))))

    assert not report.ok
    assert report.edge_white == 0.0
    assert "背景不是纯白" in report.problems[0]
    assert "0.0%" in report.problems[0]


def test_浅蓝底也不算白() -> None:
    """三个通道都得够亮。按灰度判的话浅蓝底会被放过去。"""
    report = imaging.measure(render(subject(background=(250, 250, 200))))

    assert not report.ok
    assert "背景不是纯白" in report.problems[0]


def test_地面投影拦得住() -> None:
    """底部一条阴影带。整张图的白像素占比还很高，问题全在边上。"""
    image = subject()
    image.paste((205, 205, 205), (0, 190, 200, 200))

    report = imaging.measure(render(image))

    assert not report.ok
    assert "地面投影" in report.problems[0]


def test_压缩噪点不算脏() -> None:
    """JPEG 存出来的白底不会是整数 255，留的那 10 级余量就是给它的。"""
    report = imaging.measure(render(subject(), kind="JPEG"), expect=(200, 200))

    assert report.ok
    assert report.fmt == "JPEG"


def test_抠图单独报一条() -> None:
    """透明底在模型眼里跟白底差不多，但下游拼图时会各补一个自己的底色。"""
    image = Image.new("RGBA", (200, 200), (255, 255, 255, 0))
    image.paste((30, 40, 60, 255), (50, 50, 150, 150))

    report = imaging.measure(render(image))

    assert report.transparent == pytest.approx(0.75, abs=0.01)
    assert any("透明" in one for one in report.problems)


def test_调色板图的透明也认得() -> None:
    """透明信息挂在 info 里不在通道里，只看 bands 会当它是不透明的。"""
    palette = Image.new("P", (100, 100), 0)
    palette.putpalette((255, 255, 255) * 256)
    palette.info["transparency"] = 0

    report = imaging.measure(render(palette))

    assert any("透明" in one for one in report.problems)


def test_整张白纸不算过关() -> None:
    """背景检查满分，但主体没画出来。生图端偶尔会回一张空图。"""
    report = imaging.measure(render(Image.new("RGB", (200, 200), (255, 255, 255))))

    assert not report.ok
    assert report.ink == 0.0
    assert any("主体没画出来" in one for one in report.problems)


def test_尺寸不对要说清要的是多少() -> None:
    report = imaging.measure(render(subject((512, 512))), expect=(2048, 2048))

    assert not report.ok
    assert "512x512" in report.problems[0]
    assert "2048x2048" in report.problems[0]


def test_档位取整放过去() -> None:
    """供应商按自己支持的档位取整，差几十个像素是常态。"""
    report = imaging.measure(render(subject((2048, 2032))), expect=(2048, 2048))

    assert report.ok


def test_不给期望就不校尺寸() -> None:
    report = imaging.measure(render(subject((333, 777))))

    assert report.ok
    assert report.size == "333x777"


def test_解不开的字节直接报错() -> None:
    with pytest.raises(imaging.ImageUnreadable) as err:
        imaging.measure(b"<html>404 Not Found</html>")

    assert "解不开" in str(err.value)


def test_量磁盘上的候选图(tmp_path: Path) -> None:
    path = tmp_path / "候选.png"
    path.write_bytes(render(subject()))

    assert imaging.measure_file(path, expect=(200, 200)).ok


def test_四张不同尺寸要报出来() -> None:
    """单张各自都合格，凑一起才发现不是同一规格。"""
    reports = [
        imaging.measure(render(subject((512, 512)))),
        imaging.measure(render(subject((512, 512)))),
        imaging.measure(render(subject((512, 512)))),
        imaging.measure(render(subject((256, 512)))),
    ]

    assert all(one.ok for one in reports)
    complaint = imaging.same_size(reports)
    assert complaint is not None
    assert "512x512" in complaint
    assert "256x512" in complaint


def test_四张同尺寸没话说() -> None:
    reports = [imaging.measure(render(subject((512, 512)))) for _ in range(4)]

    assert imaging.same_size(reports) is None
