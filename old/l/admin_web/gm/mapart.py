"""``gm.mapart`` —— 把图片画到 Minecraft 地图上。

主插件面板的「地图画」功能（Web 上传 → 生成地图）在这里封装成
给子插件用的 API。子插件只需给出**图片字节**和目标玩家，
缩放、色彩空间、BMP 编码、发放全部由本模块负责。

    from gm import mapart

    # 最简单：一张图发给一个玩家
    ok, msg = mapart.give("Steve", png_bytes)

    # 按像素点阵直接画（已有 RGB 数据时）
    ok, msg = mapart.give_pixels("Steve", pixels)

    # 只生成地图、不发放（自己决定怎么给）
    view_id = mapart.create(png_bytes)

为什么要有这层封装
------------------
主插件内部要求输入是 **128×128、24 位、无压缩 BMP**，
并且渲染必须在服务器主线程执行。这些约束对子插件来说是纯负担，
而且很容易踩坑（尺寸不对直接抛「图片尺寸必须为 128x128」）。
本模块把它们收在一起，子插件传任意尺寸的 PNG/JPG 即可。

依赖 Pillow（``PIL``）做缩放与解码。若运行环境缺失，``available()``
会返回 False，``diagnose()`` 给出具体原因（缺库 / 库装了但 C 扩展
加载失败），调用会给出明确报错而不是崩溃。

**Pillow 的三种来源**，``available()`` / ``diagnose()`` 会区分对待：

1. 运行环境自带（BDS 的 Python 环境里已装）—— 最省事
2. 插件 libs 目录里的 ``.whl`` —— 启动时由 ``admin_web/libs_loader.py``
   解压后注入，之后 ``import PIL`` 即可用
3. 没有 —— 报可照做的提示

⚠ 第 2 条**必须解压**：``zipimport`` 读不了 wheel 里的 ``.so``，
直接挂 whl 路径会得到"能 import、一调用就炸"的空壳 Pillow。
这是 ``available()`` 特意去取 ``PIL._imaging`` 的原因。

权限：需在 manifest 声明 ``"players"``（涉及玩家与物品发放）。
"""

from __future__ import annotations

import io
import struct
import sys
from typing import Any, Dict, List, Optional, Tuple

from ._context import require_grant, require_plugin
from ._sched import call

__all__ = [
    "available", "diagnose", "SIZE", "create", "give", "give_pixels",
    "to_pixels", "pixels_to_bmp",
]

_SCOPE = "players"

#: 地图画的分辨率（Minecraft 地图固定 128×128）
SIZE = 128


def available() -> bool:
    """图像处理依赖（Pillow）是否**真的能用**。

    ⚠ 不能只判断 ``import PIL`` 能不能过
    ------------------------------------
    Pillow 的核心功能全在 C 扩展 ``PIL._imaging`` 里（``Image.new`` /
    ``Image.open`` / ``resize`` 都要经过它）。而 ``_imaging`` 加载失败的
    情况下，``import PIL`` **仍然会成功** —— 拿到的是纯 Python 的空壳。

    这种"空壳"最典型的来源：把 Pillow 的 ``.whl`` 直接丢进 ``sys.path``。
    ``zipimport`` 能读 wheel 里的 ``.py``，但**读不了 ``.so``**，
    于是导入成功、一调用就炸。若这里只检查 ``import PIL``，就会
    误判为「依赖正常」，然后每张图都静默失败 —— 排查成本极高。

    所以要真正取一次 ``_imaging``，确认 C 扩展加载成功。
    """
    try:
        from PIL import _imaging  # noqa: F401
        return True
    except Exception:
        return False


def diagnose() -> str:
    """返回依赖状态的可读说明，供日志与子插件自检使用。

    分三种情况，因为**处理方式完全不同**，笼统报「缺 Pillow」会误导：

    1. 没有 PIL → 需要安装 Pillow
    2. 有 PIL 但 C 扩展加载失败 → Pillow"装上了但不可用"，
       多半是 whl 与服务器 Python 版本/平台不匹配，或 whl 没被解压
    3. 正常
    """
    try:
        import PIL
    except Exception as e:
        return (
            f"缺少 Pillow（{type(e).__name__}）。"
            "请安装 Pillow，或把与服务器 Python 版本匹配的 .whl 放进插件 libs 目录。"
        )

    ver = getattr(PIL, "__version__", "?")
    try:
        from PIL import _imaging  # noqa: F401
    except Exception as e:
        loc = getattr(PIL, "__file__", "?")
        return (
            f"Pillow {ver} 已存在但 C 扩展不可用（{type(e).__name__}: {e}）。"
            f"当前位置：{loc}。"
            "常见原因：.whl 没有解压就直接用（zipimport 读不了 .so），"
            "或 whl 与本机 Python 版本/架构不匹配。"
            "请把 whl 放进插件 libs 目录（会自动解压），并确认是 "
            f"cp{sys.version_info.major}{sys.version_info.minor} 匹配的版本。"
        )
    return f"Pillow {ver} 可用（C 扩展已加载）"


# ------------------------------------------------------------------ 像素

def to_pixels(data: bytes, size: int = SIZE) -> Optional[List[List[Tuple[int, int, int]]]]:
    """把任意图片字节解码并缩放到 ``size×size``，返回 RGB 点阵。

    返回 ``[[(r, g, b), ...], ...]``（``size`` 行，每行 ``size`` 个像素）；
    失败返回 None。

    缩放用 LANCZOS —— 对缩小来说质量最好，地图画只有 128 像素，
    用 NEAREST 会丢掉大量细节，看起来像马赛克。
    """
    if not data:
        return None
    try:
        from PIL import Image
    except Exception:
        return None

    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return None

    try:
        # 统一转 RGB：PNG 可能带透明通道（RGBA/P），
        # 地图画没有透明度概念，不转会在取像素时炸掉。
        if img.mode != "RGB":
            if img.mode in ("RGBA", "LA", "P"):
                # 透明部分垫成黑底再转 —— 直接 convert 会把透明
                # 当成黑，但半透明像素会得到偏暗的意外颜色，
                # 先合成到黑底更可预期。
                rgba = img.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
                bg.alpha_composite(rgba)
                img = bg.convert("RGB")
            else:
                img = img.convert("RGB")

        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)

        px = img.load()
        return [[tuple(px[x, y])[:3] for x in range(size)] for y in range(size)]
    except Exception:
        return None


def pixels_to_bmp(pixels: List[List[Tuple[int, int, int]]]) -> Optional[bytes]:
    """把 RGB 点阵编码成主插件要求的 24 位无压缩 BMP 字节。

    BMP 是**自下而上**存储的（第一行是图片最底部），每行按 4 字节对齐 ——
    这两个细节写错的话，图会上下颠倒或整体斜掉，所以对齐全交给 Pillow。
    """
    if not pixels:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        h = len(pixels)
        w = len(pixels[0]) if h else 0
        if w != SIZE or h != SIZE:
            return None
        img = Image.new("RGB", (w, h))
        img.putdata([tuple(p[:3]) for row in pixels for p in row])
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        return buf.getvalue()
    except Exception:
        return None


# ------------------------------------------------------------------ 生成

def create(data: bytes, dimension: Any = None) -> Optional[int]:
    """用图片字节创建一张地图画，返回地图 id；失败返回 None。

    生成的是一张**空白地图视图 + 渲染器**，尚未绑定到物品、
    也未发放给任何人。多数场景直接用 ``give()`` 更省事。
    """
    pixels = to_pixels(data)
    if pixels is None:
        return None
    return _create_from_pixels(pixels, dimension)


def _create_from_pixels(pixels: List[List[Tuple[int, int, int]]],
                        dimension: Any = None) -> Optional[int]:
    """在主线程创建地图视图并挂上渲染器，返回地图 id。"""
    require_grant(_SCOPE)
    plugin = require_plugin()

    def _do() -> Optional[int]:
        try:
            from ._mapview import ImageMapRenderer
        except Exception:
            return None

        srv = plugin.server
        if dimension is not None:
            try:
                view = srv.create_map(dimension)
            except TypeError:
                try:
                    view = srv.create_map()
                except Exception:
                    return None
            except Exception:
                return None
        else:
            try:
                view = srv.create_map()
            except Exception:
                return None

        try:
            view.add_renderer(ImageMapRenderer(pixels))
        except Exception:
            return None
        try:
            return int(view.id)
        except Exception:
            return None

    return call(_do, timeout=5.0, default=None)


# ------------------------------------------------------------------ 发放

def give(player_name: str, data: bytes) -> Tuple[bool, str]:
    """把图片作为地图画发给在线玩家。返回 ``(是否成功, 说明文字)``。

    这是最常用的入口 —— 缩放、编码、创建、发放一条龙。

        ok, msg = mapart.give("Steve", png_bytes)
    """
    require_grant(_SCOPE)

    name = str(player_name or "").strip()
    if not name:
        return False, "玩家名不能为空"

    pixels = to_pixels(data)
    if pixels is None:
        return False, "图片解码失败（格式不支持，或运行环境缺少 Pillow）"

    return give_pixels(name, pixels)


def give_pixels(player_name: str,
                pixels: List[List[Tuple[int, int, int]]]) -> Tuple[bool, str]:
    """直接按 RGB 点阵发放地图画（已有像素数据时用这个，省一次编解码）。

    点阵必须是 ``128×128``。尺寸不符返回失败，不会静默裁切 ——
    静默裁切会让用户拿到一张缺角的图还找不到原因。
    """
    require_grant(_SCOPE)
    plugin = require_plugin()

    name = str(player_name or "").strip()
    if not name:
        return False, "玩家名不能为空"

    try:
        h = len(pixels)
        w = len(pixels[0]) if h else 0
    except Exception:
        return False, "点阵格式不正确"
    if h != SIZE or w != SIZE:
        return False, f"点阵尺寸必须是 {SIZE}×{SIZE}，当前 {w}×{h}"

    bmp = pixels_to_bmp(pixels)
    if not bmp:
        return False, "BMP 编码失败（运行环境缺少 Pillow）"

    def _do() -> Tuple[bool, str]:
        # 主插件的 _execute_map 收 BMP 字节，内部完成地图创建 + 发放，
        # 并会提醒玩家「渲染可能不稳定」。直接复用它，避免两套逻辑分叉。
        try:
            ok, msg = plugin._execute_map(name, bmp)
            return bool(ok), str(msg)
        except Exception as e:
            return False, f"生成地图失败: {e}"

    got = call(_do, timeout=10.0, default=None)
    if got is None:
        return False, "服务器繁忙或已关闭（未能在主线程完成）"
    try:
        return bool(got[0]), str(got[1])
    except Exception:
        return False, "生成地图失败"
