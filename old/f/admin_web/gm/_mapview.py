"""``gm._mapview`` —— 地图渲染器的内部实现。

主插件的 ``ImageMapRenderer`` 定义在 ``admin_web.web`` 里，而 ``web``
会反向导入 ``core``、``qqbot`` 等一堆模块。子插件（py 型）在运行时
导入 ``gm.mapart`` 时，如果直接 ``from admin_web.web import ImageMapRenderer``，
容易在「插件尚未完全加载」的时序下踩循环导入。

因此这里放一份**等价的本地实现**，只依赖 endstone 的 map 库，
不牵扯面板的其它子系统。两者渲染结果完全一致（都是把 RGB 逐像素
写进 canvas），主插件那份保持不动以免影响既有行为。
"""

from __future__ import annotations

from typing import Any, List, Tuple

__all__ = ["ImageMapRenderer", "attach_mapart_renderer", "has_mapart_renderer"]

try:
    from endstone.map import MapRenderer as _MapRenderer
except Exception:  # pragma: no cover - 仅在没有 endstone 的裸环境触发
    class _MapRenderer(object):  # type: ignore
        def __init__(self, *a, **k):
            pass


class ImageMapRenderer(_MapRenderer):
    """把一张 ``128×128`` 的 RGB 点阵渲染到地图上。

    点阵结构：``pixels[y][x] = (r, g, b)``，取值 0-255。

    ★★★ 白图 bug 的根因（反汇编 endstone 0.11.10 确认，别删）
    ----------------------------------------------------------
    ``EndstoneMapView`` 的**构造函数**会自己挂一个内置
    ``EndstoneMapRenderer``（``1d5bc0: call addRenderer``），它画的是
    ``MapItemSavedData::getPixels()`` 的**存档像素**。而地图画**从来没
    进过存档**（endstone 没有任何 API 能写进去），所以它画的恒为全 0。

    更关键的是：``EndstoneMapView::render(player)`` **顺序遍历**渲染器
    列表，每个渲染器都往**同一张 canvas** 上画，并各自先用存档像素
    ``setBase`` 打底。⇒ **后画的覆盖先画的。**

        内置(先) → 我们(后)  ⇒ 我们赢   ✅
        我们(先) → 内置(后)  ⇒ 内置把画面洗成全 0  ❌ 白图

    ⇒ 两个推论，缺一不可：
       1. **不能**用 ``len(view.renderers) >= 1`` 判断"我们的渲染器在不在"
          —— 内置那个恒存在，这个判断永远为真（用户日志里
          「现有渲染器 1 个」就是这么被误读的）；
       2. 必须保证我们的渲染器**排在列表末尾**。

    见 ``attach_mapart_renderer`` / ``has_mapart_renderer``。

    ★ 像素去向（同一份反汇编）
    --------------------------
    ``render()`` 的结果进 ``EndstoneMapView::RenderData``（**内存**），
    再由发包链路的 hook 填进地图包：

        Player::sendMap → 地图包（128×128 全 0 占位）
                        → sendPacket → BatchedNetworkPeer 的地图 hook
                          → view->render(player) → 覆盖 packet.pixels

    ``render()`` 在整个运行时**只有这一个调用点**。

    与 ``admin_web.web.ImageMapRenderer`` 保持完全一致（测试有断言锁定）。
    """

    #: 缓存键选择，不是持久化开关：True = 每位玩家一份 RenderData，
    #: False = 全体共用一个槽。保持一致用 True。
    #: 单独提出来是为了让测试能直接断言。
    CONTEXTUAL = True

    def __init__(self, pixels: List[List[Tuple[int, int, int]]]):
        try:
            super().__init__(is_contextual=self.CONTEXTUAL)
        except TypeError:
            # 老版本 endstone 的 MapRenderer 不接受关键字参数
            super().__init__()
        self.pixels = pixels
        #: 首次调用打点用的标志（见 render 的注释）。
        #: 与 admin_web.web.ImageMapRenderer 保持一致 ——
        #: 两份实现的渲染语义逐句等价（测试有断言锁定）。
        self._logged_first_call = False

    def initialize(self, view: Any) -> None:
        """渲染器挂到地图上时的初始化钩子 —— **故意留空**。

        与 ``admin_web.web.ImageMapRenderer.initialize`` 保持一致，
        两份实现的渲染语义必须逐句等价（测试有断言锁定）。

        为什么不在里面画像素：这个钩子拿不到 ``MapCanvas``，签名只有
        ``(view)``；而且它跑在 tick 里，在这里做重活一抛异常就是崩 tick。
        地图画的可见性靠 ``render()`` 自己全量铺像素 + 恢复流程补挂，
        不是这个钩子。
        """
        return None

    def render(self, view: Any, canvas: Any, player: Any) -> None:
        # ★ 诊断埋点：只在首次调用时打一条日志。
        # 这个方法由 C++ 在发包时调用，插件侧看不到它有没有被执行；
        # 而"白图"的两种成因（没被调用 / 调用了但没效果）从日志上
        # 完全一样。打这一条就能把两种情况区分开。
        # 与 admin_web.web.ImageMapRenderer.render 保持一致 ——
        # 两份实现的渲染语义逐句等价（测试有断言锁定）。
        if not self._logged_first_call:
            self._logged_first_call = True
            try:
                import logging as _logging
                _logging.getLogger("GreenMoonPanel").info(
                    "[地图画] 渲染器已被调用（render 首次执行）—— "
                    "说明渲染器挂载成功、原版正在使用它")
            except Exception:
                pass

        # 固定渲染 128×128。点阵尺寸在 mapart 层已校验过，
        # 但渲染发生在主线程的 tick 里，这里再兜一层：
        # 越界会直接抛异常打断整个 tick，比画错更严重。
        for y in range(128):
            try:
                row = self.pixels[y]
            except IndexError:
                break
            for x in range(128):
                try:
                    r, g, b = row[x]
                    canvas.set_pixel_color(x, y, (r, g, b))
                except Exception:
                    continue


def attach_mapart_renderer(view: Any, renderer: Any) -> bool:
    """把 ``renderer`` 挂到 ``view`` 上，并保证它**排在渲染器列表末尾**。

    与 ``admin_web.web.attach_mapart_renderer`` 逐句等价
    （测试有断言锁定）。详细理由见 ``ImageMapRenderer`` 的类 docstring。

    ★ 为什么必须排在末尾
    --------------------
    ``EndstoneMapView::render(player)`` 顺序遍历渲染器，每个都往同一张
    canvas 上画、各自先用存档像素 ``setBase`` 打底 ⇒ **后画的覆盖先画的**。
    而 ``EndstoneMapView`` 构造时会**先**挂一个内置渲染器（画存档像素，
    对地图画恒为全 0）。所以：

        内置(先) → 我们(后)  ⇒ 我们赢   ✅
        我们(先) → 内置(后)  ⇒ 被洗成全 0  ❌ 白图

    ``add_renderer`` 是追加，单次挂载天然正确；本函数额外做**幂等**与
    **位置复核**，防止顺序被后续操作弄反。

    返回 True 表示"挂载/排序后本渲染器确实在末尾"。
    **任何情况下都不抛异常** —— 它在 tick 里被调用。
    """
    try:
        target = id(renderer)

        def _ids():
            try:
                return [id(r) for r in (view.renderers or [])]
            except Exception:
                return []

        def _others():
            """取出"除我们以外的"渲染器。

            ⚠ 不能只靠 id(r) != target 过滤：``view.renderers`` 返回的是
            vector 的**拷贝**，元素是 C++ 侧重新包装过的对象，``id()``
            并不稳定 —— 只按 id 过滤会把自己也算成"别人"，重排时再追加
            一遍，产生**副本**（实测 ``['builtin', 'builtin', 'ours']``）。
            所以加一层**按类型认人**：同类型的都算我们的，全部剔除。
            """
            out = []
            try:
                for r in (view.renderers or []):
                    if id(r) == target:
                        continue
                    if isinstance(r, type(renderer)):
                        continue
                    out.append(r)
            except Exception:
                return None
            return out

        def _ours_all():
            """取出列表里**所有**我们的渲染器（含挂在错位的副本）。"""
            out = []
            try:
                for r in (view.renderers or []):
                    if id(r) == target or isinstance(r, type(renderer)):
                        out.append(r)
            except Exception:
                return None
            return out

        seen = _ids()
        if target in seen:
            if seen and seen[-1] == target and seen.count(target) == 1:
                return True
        else:
            try:
                view.add_renderer(renderer)
            except Exception:
                return False
            seen = _ids()
            if seen and seen[-1] == target:
                return True

        others = _others()
        if others is None:
            return False

        # ★ 先把**所有**我们的实例摘干净（不只是 target），否则挂在错位的
        # 副本会留下来 —— 实测得到 ['ours', 'builtin', 'ours']。
        for r in (_ours_all() or []):
            try:
                view.remove_renderer(r)
            except Exception:
                continue
        for r in others:
            try:
                view.remove_renderer(r)
            except Exception:
                continue
        for r in others:
            try:
                view.add_renderer(r)
            except Exception:
                continue
        try:
            view.add_renderer(renderer)
        except Exception:
            return False

        final = _ids()
        return bool(final) and final[-1] == target
    except Exception:
        return False


def has_mapart_renderer(view: Any, renderer_cls: Any = None) -> bool:
    """``view`` 上有没有**我们自己的**地图画渲染器。

    与 ``admin_web.web.has_mapart_renderer`` 逐句等价
    （测试有断言锁定）。

    ★ 为什么不能只看数量：``EndstoneMapView`` 构造时自动挂一个内置
    ``EndstoneMapRenderer``，所以 ``len(view.renderers) >= 1`` **恒成立**。
    用它判断会让恢复逻辑误以为"渲染器还在"而什么都不做 —— 白图就是这么
    来的。必须**按类型认人**。

    任何异常一律返回 False（宁可多补挂一次，也不要漏掉）。
    """
    cls = renderer_cls if renderer_cls is not None else ImageMapRenderer
    try:
        for r in (view.renderers or []):
            if isinstance(r, cls):
                return True
    except Exception:
        pass
    return False
