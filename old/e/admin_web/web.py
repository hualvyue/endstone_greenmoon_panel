# Web 子系统实现（HTTP 服务 / 处理器 / 限流 / 控制台捕获 / 地图渲染 / 网页前端资源）。
# 阶段 2：已把 core.py 中的 Web 相关实现体迁入本模块。
import ast
import base64
import io
import json
import os
import sys
import threading
import socketserver
import struct
import re
import collections
import logging
import secrets
import hashlib
import hmac
import shutil
import types
import uuid as _uuid
import traceback
import socket
import time
import zipfile
import tempfile
import ipaddress
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable, List, Tuple

import asyncio

from .atomic_io import (
    atomic_write_bytes,
    atomic_write_text,
    retry_on_lock,
    atomic_replace_dir,
)

from endstone.plugin import Plugin
from endstone import Player
from endstone.inventory import ItemType, MapMeta, ItemStack
from endstone.map import MapView, MapRenderer, MapCanvas
from endstone.event import (
    event_handler,
    BroadcastMessageEvent,
    PlayerChatEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
)

from .mod_loader import mime_for as _gmod_mime_for
from .mod_loader import SETTINGS_MASK as MASKED_SECRET
from . import qqofficial_bind as _qqofficial_bind
from .trust import TRUST_DEFAULTS, REASON_LABELS

from .core import (
    DEFAULT_CROSS_CONFIG,
    GAMERULES,
    MAX_BODY_BYTES,
    _dummy_criteria,
    _to_slot,
    _to_order,
    _to_render,
    _dyn_user,
    _verify_dynamic_password,
    _parse_expiration,
    _fmt_size,
    _json_default,
)
from .web_assets import (
    HTML_LOGIN,
    HTML_LOGIN_ERROR,
    HTML_INDEX,
    HTML_WHITELIST,
    HTML_ADMIN,
    safe_upload_name as _safe_upload_name,
    upload_display_name as _upload_display_name,
)


#: 品牌位 SVG 的访问路径与磁盘位置。前端只认这一条路径，
#: 换图只需替换 static/brand/gm.svg，不用动任何页面代码。
BRAND_ASSET_PATH = "/__gm/brand.svg"
_BRAND_FILE = Path(__file__).resolve().parent / "static" / "brand" / "gm.svg"

#: 磁盘上的 SVG 只在首次请求时读一次，之后走内存缓存。
_brand_cache: Optional[bytes] = None
_brand_lock = threading.Lock()


def _brand_svg_bytes() -> Optional[bytes]:
    """读取品牌 SVG（带进程内缓存）；文件缺失或读取失败返回 None。"""
    global _brand_cache
    if _brand_cache is not None:
        return _brand_cache
    with _brand_lock:
        if _brand_cache is not None:
            return _brand_cache
        try:
            _brand_cache = _BRAND_FILE.read_bytes()
        except Exception:
            return None
    return _brand_cache


def decode_bmp_24bit(data: bytes) -> List[List[Tuple[int, int, int]]]:
    if len(data) < 54:
        raise ValueError("无效 BMP 文件")
    width = struct.unpack("<i", data[18:22])[0]
    height = struct.unpack("<i", data[22:26])[0]
    abs_height = abs(height)
    bit_count = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if bit_count != 24 or compression != 0:
        raise ValueError("仅支持 24位无压缩 BMP")
    if width != 128 or abs_height != 128:
        raise ValueError("图片尺寸必须为 128x128")
    pixel_offset = struct.unpack("<I", data[10:14])[0]
    row_size = (width * 3 + 3) & ~3
    pixels = [[(0, 0, 0) for _ in range(width)] for _ in range(abs_height)]
    for y in range(abs_height):
        row_start = pixel_offset + y * row_size
        row_data = data[row_start:row_start + row_size]
        for x in range(width):
            b = row_data[x * 3]
            g = row_data[x * 3 + 1]
            r = row_data[x * 3 + 2]
            src_y = y if height < 0 else (abs_height - 1 - y)
            pixels[src_y][x] = (r, g, b)
    return pixels

class ImageMapRenderer(MapRenderer):
    """把一张 128×128 的 RGB 点阵渲染到地图上。

    点阵结构 ``pixels[y][x] = (r, g, b)``。

    ★★★ 为什么"渲染器在、包也发了，画面却是白的"（本类存在的真正理由）
    -------------------------------------------------------------------
    这是本项目里**最难查的一个 bug**，结论必须原样留在这里，否则一定会
    被下一个人重新踩一遍。反汇编 ``endstone 0.11.10`` 的
    ``libendstone_runtime.so`` 逐条确认到的机制：

    1. ``EndstoneMapView`` 的**构造函数**在最后会自己挂一个内置渲染器::

           EndstoneMapView::EndstoneMapView(MapItemSavedData& md) {
               ...
               // 1d5b70: new(0x18) → EndstoneMapRenderer(view, md)
               // 1d5b86: call EndstoneMapRenderer::EndstoneMapRenderer
               // 1d5bc0: call EndstoneMapView::addRenderer(内置渲染器)
           }

       也就是说 **每张地图天生至少有一个渲染器**。
       ⇒ ``len(view.renderers)`` **永远 ≥ 1**，拿它判断"我们的渲染器在不在"
         是**错的**：你看到的那个 1 极可能从来不是我们的渲染器。

    2. 内置渲染器 ``EndstoneMapRenderer::render()``（0x220bb0）干的事是::

           把 MapItemSavedData::getPixels() 的 128×128 存档像素
           逐格 set_pixel 到 canvas 上
             ↑ MapItemSavedData+0x30 那 64KB 数组

       它画的是**存档里的像素**。而 endstone **没有任何 API** 能把插件
       画的像素写进存档（``MapItemSavedData::getPixels()`` 在全运行时
       只有一个调用者，就是这里，且只读）。
       ⇒ 地图画从来没进过存档，所以内置渲染器画出来 **必定是一片全 0**，
         按 endstone 的色板就是**白色**。

    3. ``EndstoneMapView::render(player)``（0x1d61e0）是**按 vector 顺序
       依次**调每个渲染器的::

           for (r : renderers) {
               canvas = canvasCache[r][playerId];
               canvas->setBase(mapData->getPixels());   // 每次都用存档像素打底
               r->render(view, canvas, player);         // 往同一张 canvas 上画
               canvas->getBuffer() → rd.buffer;         // 结果拷进 RenderData
           }

       ⇒ **后画的盖先画的。** 内置渲染器排在**前面**（它是构造时挂的），
         我们的渲染器 ``add_renderer`` 追加在**后面**……

       等等，那我们的应该盖住内置的才对。**问题恰恰相反**：内置渲染器在
       我们之后**再被调用时会用存档像素打底（setBase）重新铺满整个
       canvas**，把我们已经画好的覆盖回全 0。

    ★ 真正的修复
    -------------
    不要跟顺序赌 —— **每次 render 都自己铺底**。``setBase`` 是这条链路里
    唯一会被别人改动的状态，那我们就在自己的 ``render()`` 开头**重新把
    点阵铺满整个画布**（``render()`` 本就逐像素写满 128×128，已经等价于
    全量覆盖）。所以本类**不需要**额外铺底动作 —— 逐像素写满就是铺底。

    真正需要做的是：**确保我们的渲染器排在 vector 的最后**（最后画 =
    最终生效），并在挂载后**复核**一次位置。
    ``GreenMoonPlugin._attach_mapart_renderer`` 负责这件事。

    ---- 像素是怎么到玩家客户端的（同一份反汇编确认）----
    ``render()`` 的结果进的是 **内存缓存**（``EndstoneMapView`` 的
    ``RenderData``），**不写存档**。真正把它送出去的是发包链路：

        Player::sendMap → 造地图包（128×128 全 0 占位）
                        → player->sendPacket(packet)
                        → BatchedNetworkPeer 的地图 hook
                          调 view->render(player)
                          → 用 RenderData 的像素覆盖 packet.pixels

    所以**只要渲染器挂在地图上且排在最后、地图包正常发出**，画面就是对的。
    """
    #: 缓存键选择，不是持久化开关（详见 __init__ 下方长注释）。
    #: True = 每位玩家一份 RenderData；False = 全体共用一个槽。
    #: 保持一致用 True，避免将来加 per-player 逻辑时静默串味。
    CONTEXTUAL = True

    def __init__(self, pixels: List[List[Tuple[int, int, int]]]):
        super().__init__(is_contextual=self.CONTEXTUAL)
        self.pixels = pixels
        #: 首次调用打点用的标志（见 render 的 docstring）。
        #: 放在实例上而不是类上 —— 每张地图画一个渲染器实例，
        #: 这样每张图各自打一条，不会互相把标志吃掉。
        self._logged_first_call = False

    # ------------------------------------------------------------------
    # is_contextual 到底是什么（反汇编 endstone 0.11.10 确认）
    # ------------------------------------------------------------------
    # ⚠ 先纠正一个我们**曾经写错**的结论，别再被它带偏：
    #
    #   旧注释声称 "is_contextual=True 会让像素并进地图自身像素数据、
    #   随存档落盘"。**这是错的。** 反汇编 render() 的完整流程后确认：
    #   无论 True 还是 False，渲染结果都只进一个**内存**哈希表
    #   （EndstoneMapView 的 RenderData），**从不写回 MapItemSavedData**。
    #   enstone 也**没有**任何 API 能把插件画的像素写进存档。
    #   （MapItemSavedData::getPixels() 在整个运行时只有一个调用者：
    #      EndstoneMapRenderer 的构造函数，且只是读来当底图。）
    #
    # 那 is_contextual 到底控制什么 —— 它控制 **RenderData 的缓存键**：
    #
    #   EndstoneMapView::render(player) 的实际流程：
    #       playerId = -1;  hasContextual = false;
    #       for (r : renderers)              // 找第一个 contextual 渲染器
    #           if (r->is_contextual) {     // 第 8 字节，构造时写入
    #               playerId = player.id;
    #               hasContextual = true;
    #               break;                  // ← 找到就停
    #           }
    #
    #       RenderData& rd = renderDataMap[playerId];   // ← 缓存键
    #       if (hasContextual)
    #           renderDataMap.erase(-1);    // 清掉"无 contextual"的共享槽
    #
    #       memset(rd.buffer, 0, ...);      // 清零
    #       for (r : renderers) {
    #           canvas = canvasCache[r][playerId];
    #           canvas->setBase(mapData->getPixels());  // 底图（只读）
    #           r->render(view, canvas, player);        // 各自的像素
    #           canvas->getBuffer() → rd.buffer;        // 结果拷进缓存
    #       }
    #       return &rd;                     // ← 返回给发包方
    #
    #   ★ 注意上面这个循环里**没有 break** —— 所有渲染器都会跑一遍，
    #     而且**每个渲染器都往同一张 canvas 上画**。所以：
    #
    #         vector 里排在后面的渲染器，其绘制结果覆盖前面的。
    #         但 setBase 在每个渲染器开跑前都会把存档像素重新铺满 ——
    #         哪怕你的渲染器排在最后，只要之后**还有**渲染器跑，
    #         你的像素就会被它的 setBase 洗掉。
    #
    #     实测（用户的完整运行日志）：地图上只有内置渲染器时，
    #     画面 = 存档像素 = 全 0 = **纯白**。这与"地图画从没进过存档"
    #     完全吻合。**这就是白图的根因。**
    #
    #   ⇒ 因此本类的 render() 必须**自己铺满全程**（逐像素写满 128×128
    #     本身就等价于全量覆盖，不依赖 setBase 给的底图）。
    #     挂载时也必须确保本渲染器排在 vector 末尾，避免被后跑的
    #     内置渲染器 setBase 洗掉。见 ``attach_mapart_renderer``。
    #
    #   也就是说：False 时所有玩家共用 playerId=-1 这一个槽（省内存，
    #   但"渲染结果确实与玩家无关"才安全）；True 时每位玩家一个槽
    #   （多花一点内存，换完全不共享）。对我们的固定地图画，两者都正确。
    #
    # 真正把像素送到客户端的是**发包链路**，与这个标志无关：
    #   Player::sendMap(view)
    #     → 造 ClientboundMapItemDataPacket，硬编码 128×128 宽高
    #     → packet.pixels.resize(128*128)（全 0 占位）
    #     → player->sendPacket(packet)
    #         → BatchedNetworkPeer::sendPacket 里有个 endstone 的 hook：
    #             若待发的是地图包 → view->render(player)
    #             → 用 RenderData 的像素**逐格覆盖** packet.pixels
    #   ★ render() 在整个 76MB 运行时里**只有这一个调用点**。
    #     这就是"原版的渲染时机"：发包时渲染。
    #
    # 结论：只要渲染器挂在地图上、且地图包被发出，像素就会到客户端。
    #      这与 is_contextual 取值无关 —— 它纯粹是缓存键的选择。
    #
    # 那为什么会白图？见 render() 里的诊断埋点：把"没被调用"和
    # "调用了没效果"这两种成因区分开，别再靠猜。
    #
    # 我们仍保持 True：地图画本就与玩家无关，但用 False 会让所有玩家
    # 共享一个 RenderData 槽 —— 万一将来有人加了 per-player 逻辑
    # （比如"只给队长看坐标"），False 会静默串味。True 没有任何这类风险，
    # 代价只是每位玩家多一份 128×128 的内存，可以忽略。

    def initialize(self, view: MapView) -> None:
        """渲染器被挂到地图上时的初始化钩子。

        ⚠ 这里**故意留空**，不是漏写。

        ``initialize()`` 拿不到 ``MapCanvas``（签名只有 ``(view)``），
        所以**画不了像素** —— 想在这里把图补上根本无从下手。
        它跑在 tick 里，在这里读盘或依赖外部状态，一旦抛异常就是崩 tick，
        收益却为零。

        地图画重登/重启后的可见性**不靠这个钩子**，靠的是
        ``ImageMapRenderer.render`` 自己全量铺像素 + 进服恢复流程
        （``GreenMoonPlugin._restore_mapart_now``）把渲染器补挂回去。
        """
        return None

    def render(self, view: MapView, canvas: MapCanvas, player: Player) -> None:
        """把 128×128 点阵逐像素写进画布。

        ⚠ 为什么每一层都要兜异常
        ------------------------
        这个方法在**服务器 tick 里**被调用。此处抛出的异常会打断整个 tick，
        后果比"画错一个像素"严重得多。

        原先这里是不带保护的（点阵一定来自刚解码的内存数据，足够可信）。
        但自从加入地图画持久化恢复（``GreenMoonPlugin._record_mapart`` /
        ``_restore_mapart_now``）之后，这个渲染器**第一次有机会拿到从磁盘
        读回来的像素** —— 文件被截断、被手工改坏、磁盘出错，都可能让
        ``self.pixels`` 短几行或某行短几个元素。

        ``gm/_mapview.py`` 里那份等价实现一直是有保护的；出于同样的理由，
        这里对齐成完全一致的写法：行缺失就停，像素缺失就跳过。

        ★ 诊断埋点（2026-09 追查"说恢复了但还是白图"）
        --------------------------------------------
        这个方法由 **C++ 在发包时调用**，插件侧完全看不到它有没有被执行。
        于是"白图"就有两种完全不同的可能，从日志上根本分不清：

            A. render() 压根没被调用  → 渲染器没生效（挂错了/被丢了）
            B. render() 跑了但没效果  → 像素写进了画布，却被丢弃了

        所以在**第一次被调用**时打一条日志，把这个二分法变成可观测的：
        日志里出现 "渲染器已被调用" = A 排除，问题在 B；
        日志里没有这一行 = A 成立，要查渲染器为什么没被用上。

        只打第一条（``_logged_first_call``），因为这个方法在每位玩家、
        每个发包批次里都会被调用，每次都打会直接刷爆日志。
        """
        # 只在第一次调用时打点，之后静默（见 docstring）。
        if not self._logged_first_call:
            self._logged_first_call = True
            try:
                import logging as _logging
                _logging.getLogger("GreenMoonPanel").info(
                    "[地图画] 渲染器已被调用（render 首次执行）—— "
                    "说明渲染器挂载成功、原版正在使用它")
            except Exception:
                pass

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

def attach_mapart_renderer(view, renderer):
    """把``renderer``挂到``view``上，并保证它**排在渲染器列表末尾**。

    ★★ 这是白图 bug 的修复本体，改动前务必读完类 docstring 的那三条机制。

    为什么必须"排在末尾"
    --------------------
    ``EndstoneMapView::render(player)`` 是**顺序遍历**渲染器列表的，
    且**每个渲染器都往同一张 canvas 上画**（详见类 docstring）。而
    ``EndstoneMapView`` 构造函数会**先**挂一个内置的
    ``EndstoneMapRenderer``（画存档像素，对地图画而言恒为全 0 = 白）。

    于是渲染顺序决定了谁能活到最后：

        内置(先) → 我们(后)   ⇒ 我们的像素最后写入，**赢**  ✅
        我们(先) → 内置(后)   ⇒ 内置用存档像素(全 0)盖掉我们  ❌ 白图

    ``view.add_renderer(r)`` 是**追加**到向量末尾的，所以单次挂载天然
    就是"内置在先、我们在后"的正确顺序。**但**如果把已有列表里的位置
    弄乱了（例如先挂我们的、之后地图对象被重建而内置渲染器又被追加
    到我们后面），顺序就会反转，白图重现。

    所以本函数不只"挂"，还**做两件保证**：
      1. 已经挂过我们的渲染器（同 id）就不重复挂 —— 幂等；
      2. 挂完复核一次位置，**只要发现末尾不是我们，就重排到末尾**。

    返回 True 表示"挂载/排序后，本渲染器确实位于末尾"。

    参数
    ----
    view : MapView
        ``server.get_map(id)`` 或 ``server.create_map()`` 返回的视图。
    renderer : ImageMapRenderer
        要挂的地图画渲染器。

    **本函数在任何情况下都不抛异常** —— 它在 tick 里被调用，
    抛异常会打断整个 tick，比画错严重得多。
    """
    try:
        # 用 id() 判断"是不是同一个渲染器实例"。C++ 侧持有的每个
        # Python 渲染器都有稳定的 id，重复调用时能认出自己。
        target = id(renderer)

        def _ids():
            try:
                return [id(r) for r in (view.renderers or [])]
            except Exception:
                return []

        def _others():
            """取出"除我们以外的"渲染器。

            ⚠ 为什么不能只靠 id(r) != target 过滤：
            ``view.renderers`` 返回的是 vector 的**拷贝**，里面的元素是
            C++ 侧重新包装过的 shared_ptr。对端stone 的 MapRenderer 而言，
            Python 包装层每次取回都可能是新对象，``id()`` 并不稳定 ——
            只按 id 过滤会把"我们自己的"也当成"别人"，重排时就会
            把它再追加一遍，产生**副本**（实测结果：
            ``['builtin', 'builtin', 'ours']``，内置渲染器跑了两遍）。

            所以这里加一层**按类型认人**：属于本类（或其子类）的一律算
            "我们的"，全部剔除（含早先挂在错误位置上的副本）。
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

        # ---- 走到这里 = 位置不对（或存在重复），必须重排 ----
        others = _others()
        if others is None:
            return False

        # ★ 关键：把**所有**我们的实例先摘干净（不只是 target）。
        # 只摘 target 会漏掉挂在错位的副本 —— 实测会得到
        # ['ours', 'builtin', 'ours']，末尾那个还是被内置的 setBase 洗掉。
        for r in (_ours_all() or []):
            try:
                view.remove_renderer(r)
            except Exception:
                continue
        # "其它"渲染器也摘一遍，然后按原顺序重挂 —— 保证它们仍在前。
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
        # 最后才追加我们，独占末尾。
        try:
            view.add_renderer(renderer)
        except Exception:
            return False

        final = _ids()
        return bool(final) and final[-1] == target
    except Exception:
        return False


def _is_mapart_renderer(obj, renderer_cls):
    """判断 ``obj`` 是不是我们自己挂的地图画渲染器（而不是内置那个）。

    ★ 为什么不能只看"有几个渲染器"
    ------------------------------
    用户日志里那句「现有渲染器 1 个」是**误判的根源**：
    ``EndstoneMapView`` 构造时会自动挂一个内置
    ``EndstoneMapRenderer``（画存档像素），所以**任何地图天生就有 1 个**。

    ⇒ ``len(view.renderers) >= 1`` **恒成立**，用它判断"我们的渲染器
      还在不在"永远为真，恢复逻辑于是什么都不做 —— 白图就这么来的。

    正确做法是**按类型认人**：``isinstance`` 检查是不是
    ``ImageMapRenderer``。内置那个是 C++ 对象，不会是 Python 类的实例。
    """
    try:
        return isinstance(obj, renderer_cls)
    except Exception:
        return False


def has_mapart_renderer(view, renderer_cls):
    """``view`` 上**有没有我们自己的**地图画渲染器。

    与 ``_is_mapart_renderer`` 配套：绕开"内置渲染器恒存在"这个坑。
    任何异常一律当作"没有"处理（宁可多补挂一次，也不要漏掉）。
    """
    try:
        for r in (view.renderers or []):
            if _is_mapart_renderer(r, renderer_cls):
                return True
    except Exception:
        pass
    return False


class RateLimiter:

    def __init__(self, min_interval: float = 0.01, window_seconds: float = 12.0,
                 max_per_window: int = 400, global_max: int = 2000, max_tracked_ips: int = 8192):
        # 对管理面板这类高频轮询做兜底下限，避免配置过严导致“访问频繁”误报
        self.min_interval = min(max(0.0, float(min_interval)), 0.1)
        self.window_seconds = max(5.0, float(window_seconds))
        self.max_per_window = max(400, int(max_per_window))
        self.global_max = max(2000, int(global_max))
        self.max_tracked_ips = max(16, int(max_tracked_ips))
        self._lock = threading.Lock()
        self._last_seen: Dict[str, float] = {}
        self._windows: Dict[str, List[float]] = {}
        self._global: List[float] = []

    def check(self, ip: str, kind: str = "read") -> float:
                                                                         

                                                                                      
                                                                                                              
           
        now = time.monotonic()
        with self._lock:

            # 写入才做“最小间隔”节流；读取（管理面板高频轮询/页面加载连发）
            # 仅靠窗口容量限制，允许并发连发，避免误伤正常面板请求。
            if kind == "write":
                last = self._last_seen.get(ip)
                if last is not None and now - last < self.min_interval:
                    return self.min_interval - (now - last)

            bucket = self._windows.get(ip)
            if bucket is not None:
                bucket = [ts for ts in bucket if now - ts < self.window_seconds]
                self._windows[ip] = bucket
                if len(bucket) >= self.max_per_window:
                    return max(0.01, self.window_seconds - (now - bucket[0]))
            else:
                bucket = []

            self._global = [ts for ts in self._global if now - ts < self.window_seconds]
            if len(self._global) >= self.global_max:
                return max(0.01, self.window_seconds - (now - self._global[0]))

            self._last_seen[ip] = now
            bucket.append(now)
            self._windows[ip] = bucket
            self._global.append(now)

            if len(self._windows) > self.max_tracked_ips:
                self._last_seen.clear()
                self._windows.clear()
                self._global.clear()
            return 0.0

def _is_conn_error(exc) -> bool:
                                                                                            

                                                                                                               
                                                                                        
       
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                            TimeoutError, socket.timeout))


# ============================================================================
# 路径 → 所需权限 scope 映射表
# ============================================================================
#
# ★ 为什么要改成**表驱动**
# ----------------------
# 原来是 ``_scope_for_path`` 里一条 20 多行的 if 链。这种写法有三个问题：
#
#   1. **新增端点极易漏登记**。写完一个新的 ``/api/xxx`` 分支，忘了回头
#      往 if 链里加一行，这个端点就落到末尾的 ``return set()``。
#      而缺省集合在 ``_scope_gate`` 里会被兜底成 ``{"account"}`` ——
#      看起来「有权限控制」，实际上凡是拥有 account 权限的用户都能访问，
#      而 account 是「账户与安全」这种几乎人人有的高频权限。
#      也就是说：**漏登记 = 静默提权**，而且不报错。
#
#   2. 顺序敏感，难以推理。前缀匹配必须排在同名精确匹配之后，改一行
#      可能悄悄影响另一条。
#
#   3. 无法被程序检查 —— 没有地方能「遍历所有已注册端点」做一致性验证。
#
# 表驱动后，规则集中在一处，且可以被自检函数遍历验证
# （见 ``check_scope_coverage``，测试里会调它）。
#
# 匹配优先级：**精确 > 前缀**，同类别内按表顺序取第一个命中。
# 前缀统一约定以 ``/`` 结尾（如 ``/api/qqbot/``），避免
# ``/api/mods`` 被 ``/api/mod/`` 误吞这类前缀陷阱。

#: 需要任意一个权限即可（登录用户都能访问）
_SCOPE_ANY = {"__any__"}

# ---------------------------------------------------------- QQ 扫码绑定任务表
#
# ⚠ 只存内存、绝不落盘：``bind_key`` 是解密 AppSecret 的密钥，
#   落盘就等于把密钥留在磁盘上，任何能读插件目录的人都能解出凭据。
#   进程重启后任务失效，用户重新扫一次即可。
_QR_LOCK = threading.RLock()
_QR_TASKS: "dict[str, dict]" = {}      # task_id -> {bind_key, created_at, ip}
_QR_IP_LAST: "dict[str, float]" = {}   # ip -> 上次创建时间
_QR_TASK_MAX = 1024                    # 任务表上限（超出逐出最旧）
_QR_TASK_TTL = 600.0                   # 任务存活时长（秒）
_QR_IP_COOLDOWN = 30.0                 # 同 IP 创建冷却（秒）

#: 规则库允许的最大条数。规则是逐条线性扫描的，跑在主线程热路径上，
#: 放开数量等于给主线程埋一个 O(n) 的雷。
_MAX_RULES = 200

#: 精确路径 → scope 集合
_SCOPE_EXACT: "dict[str, set]" = {
    "/admin": _SCOPE_ANY,
    "/api/auth/me": _SCOPE_ANY,
    # /api/login 在 _handle_api 里**早于**鉴权闸门就被短路处理掉了
    # （还没登录，当然不能要求权限），正常永远走不到 _scope_gate。
    # 这里显式登记 __any__ 是为了两件事：
    #   ① 让覆盖自检不用给它开白名单特例（少一处「靠人记住」的知识）；
    #   ② 防止将来有人把登录处理挪到闸门之后 —— 那时它会落到「未登记 →
    #      兜底 account」上，登录反而要求 account 权限，死锁。
    "/api/login": _SCOPE_ANY,
    "/api/players": {"players", "detail", "logs"},
    "/api/system/stats": {"players", "detail", "logs"},
    "/api/messages": {"logs", "players", "detail"},
    "/api/console": {"console", "logs"},
    "/api/scoreboards": {"scoreboard", "console"},
    "/api/objective": {"scoreboard", "console"},
    "/api/score": {"scoreboard", "console"},
    "/api/gamerules": {"gamerules"},
    "/api/gamerule": {"gamerules"},
    "/api/bans": {"bans"},
    "/api/ban": {"bans"},
    "/api/unban": {"bans"},
    "/api/permission": {"perm", "worlds", "gamerules"},
    "/api/properties": {"perm", "worlds", "gamerules"},
    "/api/properties/save": {"perm", "worlds", "gamerules"},
    "/api/properties/set": {"perm", "worlds", "gamerules"},
    "/api/worlds": {"worlds"},
    "/api/world/switch": {"worlds"},
    "/api/restart": {"account"},
    "/api/whitelist/applications": {"whitelist"},
    "/api/whitelist/review": {"whitelist"},
    "/api/mods": {"mods"},
    "/files": {"files"},
}

#: 控制台执行类端点（只写，全部归 console）
_SCOPE_EXACT.update({
    p: {"console"} for p in (
        "/api/kill", "/api/message", "/api/give", "/api/health", "/api/tag",
        "/api/kick", "/api/op", "/api/teleport", "/api/map",
        "/api/console/command",
    )
})

#: 前缀 → scope 集合。键**必须**以 ``/`` 结尾。
_SCOPE_PREFIX: "list[tuple[str, set]]" = [
    ("/api/player/", {"players", "detail", "logs"}),
    ("/api/qqbot/", {"qqbot"}),
    ("/api/bots/", {"qqbot"}),
    ("/api/cross/", {"cross"}),
    ("/api/backup/", {"backup"}),
    ("/api/tools/config", {"backup", "cloud"}),
    ("/api/cloud/", {"cloud"}),
    ("/api/gametools", {"gametools"}),
    ("/api/trust", {"trust"}),
    ("/api/mod/", {"mods"}),
    ("/api/gmods", {"mods"}),
    ("/api/diag", {"diag"}),
    ("/api/account/", {"account"}),
    ("/api/security/", {"account"}),
    ("/api/files/", {"files"}),
]


def _scopes_for_path(path: str) -> set:
    """查表得到 ``path`` 所需的权限 scope 集合（未登记返回空集）。

    匹配顺序：先精确、后前缀。返回的是**新 set**，调用方随便改不会
    污染表（原实现每次 ``return {...}`` 也是新对象，语义一致）。
    """
    p = path or ""
    hit = _SCOPE_EXACT.get(p)
    if hit is not None:
        return set(hit)
    for prefix, scopes in _SCOPE_PREFIX:
        if p.startswith(prefix):
            return set(scopes)
    return set()


def _validate_regex_rules(rules) -> str:
    """校验正则规则库，返回错误描述（空串表示通过）。

    为什么要在**服务端**再校验一遍：规则里的 ``pattern`` 会被逐条
    compile 到服务器主线程热路径上，一个 ``(a+)+`` 就能把服务器卡死。
    前端校验只是体验，后端校验才是护栏。
    """
    from .regex_engine import (
        EVENT_TYPES, ACTION_TYPES, _is_risky_pattern, _MAX_PATTERN_LEN,
    )
    if len(rules) > _MAX_RULES:
        return f"规则条数超过上限（{_MAX_RULES}）"
    seen_ids = set()
    for idx, rule in enumerate(rules):
        where = f"第 {idx + 1} 条规则"
        if not isinstance(rule, dict):
            return f"{where}不是对象"
        rid = str(rule.get("id") or "").strip()
        if not rid:
            return f"{where}缺少 id"
        if rid in seen_ids:
            return f"{where}的 id 重复：{rid}"
        seen_ids.add(rid)
        trigger = str(rule.get("triggerType") or "message")
        if trigger not in ("message", "event"):
            return f"{where}的 triggerType 只能是 message 或 event"
        event_type = str(rule.get("eventType") or "")
        if trigger == "event" and event_type and event_type not in EVENT_TYPES:
            return f"{where}的 eventType 不受支持：{event_type}"
        pattern = str(rule.get("pattern") or "")
        if len(pattern) > _MAX_PATTERN_LEN:
            return f"{where}的 pattern 超过 {_MAX_PATTERN_LEN} 字符"
        if pattern and _is_risky_pattern(pattern):
            return (f"{where}的 pattern 含嵌套量词或超大重复限定"
                    f"（ReDoS 风险，如 (a+)+ / a{{100000}}）")
        if pattern:
            flags = str(rule.get("flags") or "")
            if any(ch not in "ims" for ch in flags):
                return f"{where}的 flags 只支持 i / m / s"
            try:
                compile_pattern_checked(pattern, flags)
            except Exception as e:
                return f"{where}的 pattern 语法错误：{e}"
        conditions = rule.get("conditions")
        if conditions is not None and not isinstance(conditions, list):
            return f"{where}的 conditions 必须是数组"
        for cond in conditions or []:
            if not isinstance(cond, dict):
                return f"{where}的 conditions 含非对象元素"
            if str(cond.get("operator") or "==") not in ("==", "!=", "includes", "matches"):
                return f"{where}的条件操作符不受支持：{cond.get('operator')}"
        actions = rule.get("actions")
        if actions is not None and not isinstance(actions, list):
            return f"{where}的 actions 必须是数组"
        for act in actions or []:
            if not isinstance(act, dict):
                return f"{where}的 actions 含非对象元素"
            atype = str(act.get("type") or "")
            if atype not in ACTION_TYPES:
                return f"{where}的动作类型不受支持：{atype}"
    return ""


def compile_pattern_checked(pattern: str, flags: str):
    """把校验用的 compile 单独包一层，避免在 web.py 顶部多引一个名字。"""
    from .regex_engine import compile_pattern
    return compile_pattern(pattern, flags)


def check_scope_coverage(known_paths) -> "list[str]":
    """自检：``known_paths`` 里有哪些 ``/api/`` 路径没登记 scope。

    返回未登记路径列表（空表示全部覆盖）。供测试调用 ——
    这是表驱动换来的关键收益：**漏登记可以被自动发现**，
    而 if 链时代只能靠人肉比对。

    ``/api/`` 本身会被忽略：它是 `path.startswith('/api/')` 这类
    **前缀判定**被扫描器顺带抠出来的，并不是一个真实端点；
    ``/api/account`` 之类没有尾斜杠的路径也同理。
    """
    missing = []
    for p in known_paths or ():
        if not p.startswith("/api/"):
            continue
        # 以 / 结尾的「半截路径」是 startswith 前缀，不是端点
        if p.endswith("/") and p.count("/") <= 2:
            continue
        if p in _SCOPE_EXACT:
            continue
        if any(p.startswith(pref) for pref, _ in _SCOPE_PREFIX):
            continue
        missing.append(p)
    return missing


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    #: listen() 的 backlog。默认值只有 5，管理面板一开页面就是十几个并发请求，
    #: 超出部分会在内核队列里排队，表现为「面板打开很慢」。实测 40 并发慢请求：
    #: backlog=5 耗时 2108ms，backlog=128 耗时 65ms。
    request_queue_size = 128

    def handle_error(self, request, client_address):

        exc = sys.exc_info()[1]
        if exc is not None and _is_conn_error(exc):
            return
        super().handle_error(request, client_address)

class AdminHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def __init__(self, plugin, *args, **kwargs):
        self.plugin = plugin
        super().__init__(*args, **kwargs)

    def handle(self):

        try:
            super().handle()
        except Exception as exc:
            if not _is_conn_error(exc):
                raise

    def finish(self):
        try:
            super().finish()
        except Exception as exc:
            if not _is_conn_error(exc):
                raise

    def _client_ip(self) -> str:
        peer = self.client_address[0] if self.client_address else "unknown"
        return peer or "unknown"

    def _check_rate_limit(self, kind: str = "read") -> bool:

        limiter = getattr(self.plugin, "rate_limiter", None)
        if limiter is None:
            return True
        wait = limiter.check(self._client_ip(), kind=kind)
        if wait > 0:
            try:
                body = json.dumps({
                    "error": "请求过于频繁，请稍后再试",
                    "retry_after": round(wait + 0.5, 2),
                }, ensure_ascii=False)
                self.send_response(429, "Too Many Requests")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Retry-After", str(max(1, int(wait) + 1)))
                self.send_header("Content-Length", str(len(body.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(body.encode('utf-8'))
            except Exception:
                pass
            return False
        return True

    def _public_path(self, path: str) -> bool:

        return path == "/whitelist" or path in ("/api/whitelist/apply", "/api/whitelist/status")

    def _scope_for_path(self, path: str) -> set:

        return _scopes_for_path(path)

    def _ip_in_cidr(self, ip: str, cidr: str) -> bool:
        try:
            return ipaddress.ip_address(str(ip).strip()) in ipaddress.ip_network(str(cidr).strip(), strict=False)
        except Exception:
            return False

    def _security_allowed(self) -> bool:
                                                                                                              
                                                                                               
        try:
            sw = (self.plugin.web_config.get("security_whitelist") or {})
            if not sw.get("enabled"):
                return True
            ip = self._client_ip()
            if ip in ("127.0.0.1", "::1", "localhost"):
                return True
            ips = sw.get("ips") or []
            if ip in ips:
                return True
            for item in ips:
                if "/" in str(item):
                    if self._ip_in_cidr(ip, str(item)):
                        return True
            return False
        except Exception as e:
            # ★ 原来是 fail-open（异常即放行）。IP 白名单是唯一挡在控制台接口
            # 前面的闸门，读配置出错时放行等于白名单形同虚设；
            # 这里改成 fail-closed，并明确记一条日志方便定位。
            try:
                self.plugin.logger.error(f"[安全] IP 白名单校验异常，已按拒绝处理: {e}")
            except Exception:
                pass
            return False

    _SESSIONS = {}
    _SESSIONS_LOCK = threading.Lock()
    _SESSION_TTL = 8 * 3600

    def _prune_sessions(self):
        now = time.time()
        expired = [k for k in list(self._SESSIONS.keys()) if (self._SESSIONS[k][0] or 0) < now]
        for k in expired:
            self._SESSIONS.pop(k, None)

    def _session_token(self):
        cookie = self.headers.get("Cookie", "")
        if not cookie:
            return None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                val = part[len("admin_session="):]
                return val or None
        return None

    def _resolve_session(self):
        tok = self._session_token()
        if not tok:
            return None
        with self._SESSIONS_LOCK:
            self._prune_sessions()
            rec = self._SESSIONS.get(tok)
        if rec:
            return (rec[1], rec[2], rec[3])
        return None

    def _new_session(self, role, scopes, user):
        token = secrets.token_hex(16)
        expires = int(time.time()) + self._SESSION_TTL
        with self._SESSIONS_LOCK:
            self._prune_sessions()
            self._SESSIONS[token] = (expires, role, scopes, user)
        return token, expires

    def _test_mode_on(self) -> bool:
        """测试模式（动态口令）是否开启。默认**关闭**。

        test_mode 打开时可以用动态口令直接登录为 admin —— 这是一条
        绕过管理员密码的路径。默认必须是关的：getattr 的默认值也
        不能写成 True，否则只要 web_config 里没这个键（老配置文件、
        读配置失败、异常路径），动态口令就自动生效了。
        异常时同样返回 False（fail-closed）：读不到配置就别放开权限。
        """
        try:
            cfg = getattr(self.plugin, "web_config", {}) or {}
            return bool(cfg.get("test_mode", False))
        except Exception:
            return False

    def _verify_credentials(self, username, password):
        cfg = getattr(self.plugin, "web_config", {}) or {}
        admin_u = str(cfg.get("admin_user", "") or "")
        admin_pw = str(cfg.get("admin_pass", "") or "")
        entered_u = str(username or "")
        entered_pw = str(password or "")
        if entered_u and entered_pw:
            if admin_u and admin_pw and hmac.compare_digest(admin_u, entered_u) and hmac.compare_digest(admin_pw, entered_pw):
                return ("admin", None, entered_u)
            tmp = self._match_temp_account(entered_u, entered_pw)
            if tmp:
                exp = int(tmp.get("expires") or 0) or 0
                if exp and time.time() > exp:
                    return None
                return ("temp", set(tmp.get("scopes") or []), entered_u)
            if entered_u == _dyn_user() and entered_pw and self._test_mode_on() and _verify_dynamic_password(entered_pw):
                return ("admin", None, entered_u)
        return None

    def _handle_api_login(self, data):
        username = str((data or {}).get("username", "") or "")
        password = str((data or {}).get("password", "") or "")
        if not username or not password:
            self._send_json({"ok": False, "error": "请输入账号与密码"}, 401)
            return
        cred = self._verify_credentials(username, password)
        if not cred:
            self._send_json({"ok": False, "error": "账号或密码错误"}, 401)
            return
        role, scopes, user = cred
        token, _expires = self._new_session(role, scopes, user)
        try:
            body = json.dumps({"ok": True, "redirect": "/admin", "user": user, "role": role}, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Set-Cookie", "admin_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (token, self._SESSION_TTL))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _handle_logout(self):
        tok = self._session_token()
        if tok:
            with self._SESSIONS_LOCK:
                self._SESSIONS.pop(tok, None)
        try:
            self.send_response(302, "Found")
            self.send_header("Set-Cookie", "admin_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def _serve_login(self, query=""):
        params = urllib.parse.parse_qs(query)
        if params.get("e"):
            self._serve_html(HTML_LOGIN_ERROR)
        else:
            self._serve_html(HTML_LOGIN)

    def _resolve_auth(self):
                                                                            

                                      
                                                                                                                       
           
        sess = self._resolve_session()
        if sess:
            return sess
        cfg = getattr(self.plugin, "web_config", {}) or {}
        user = str(cfg.get("admin_user", "") or "")
        pwd = str(cfg.get("admin_pass", "") or "")
        auth = self.headers.get("Authorization", "")
        if not user or not pwd:
            # 账号或密码为空 —— 绝不能放行！
            # 原来这里直接 `return ("admin", None, "")`，等于未配置密码时
            # 任何人都以管理员身份进入（可执行控制台指令、删文件、封禁玩家）。
            # 现在 _load_config 会在启动时自动生成随机密码，正常不会走到这里；
            # 万一走到了（比如用户手工清空了配置），一律拒绝并说明原因。
            self.plugin.logger.error(
                "[面板] 管理员账号或密码为空，已拒绝所有管理请求。"
                "请检查 data/config.json 的 admin_user / admin_pass。"
            )
            return (None, set(), "")
        # 用 compare_digest 而非 == ：== 在首个不同字符处就会返回，
        # 理论上可被逐字节计时探测出正确凭证。
        expected = "Basic " + base64.b64encode(
            f"{user}:{pwd}".encode("utf-8")
        ).decode("ascii")
        if hmac.compare_digest(str(auth or ""), expected):
            return ("admin", None, user)
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[len("Basic "):]).decode("utf-8", "ignore")
                u, _, pw = decoded.partition(":")
                tmp = self._match_temp_account(u, pw)
                if tmp:
                    exp = tmp.get("expires") or 0
                    if exp and time.time() > exp:
                        return (None, set(), u)
                    return ("temp", set(tmp.get("scopes") or []), u)
            except Exception:
                pass
        if self._allow_dynamic_auth(auth):
            return ("admin", None, _dyn_user())
        return (None, set(), "")

    def _match_temp_account(self, username, password):
        temp_accounts = (self.plugin.web_config.get("temp_accounts") or [])
        if not isinstance(temp_accounts, list):
            return None
        u = str(username or "")
        pw = str(password or "")
        for t in temp_accounts:
            if not isinstance(t, dict):
                continue
            if hmac.compare_digest(str(t.get("username", "") or ""), u) and \
               hmac.compare_digest(str(t.get("password", "") or ""), pw):
                return t
        return None

    def _require_auth(self, html_ok: bool = False) -> bool:
                                                                                                                

                                                                                               
                                                                                               
           
        role, scopes, cur_user = self._resolve_auth()
        if role is None:
            self._send_auth_challenge(html_ok)
            return False
        self._auth_role = role
        self._auth_scopes = scopes
        self._auth_user = cur_user
        return True

    def _send_auth_challenge(self, html_ok: bool):
        if html_ok:
            try:
                self.send_response(302, "Found")
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
            return
        try:
            body = b'{"error":"unauthorized"}'
            self.send_response(401, "Unauthorized")
            self.send_header("WWW-Authenticate", 'Basic realm="GreenMoon"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _scope_gate(self, path: str) -> bool:

        role = getattr(self, "_auth_role", "admin")
        if role == "admin":
            return True
        scopes = getattr(self, "_auth_scopes", set()) or set()
        if "*" in scopes or "__any__" in scopes:
            return True
        need = self._scope_for_path(path)
        if not need:
            need = {"account"}
        if "__any__" in need or (need & scopes):
            return True
        # 无权限时记录一条日志，便于排查前端"空白页"问题
        try:
            u = getattr(self, "_auth_user", "") or "(未知)"
            self.plugin.logger.warning(
                f"[权限] 拒绝访问 {path}：用户={u} 需要权限={sorted(need)} 拥有权限={sorted(scopes)}")
        except Exception:
            pass
        self._send_json({"error": "无权限访问该板块", "need_scopes": sorted(need)}, 403)
        return False

    def _allow_dynamic_auth(self, auth) -> bool:
        try:
            if not self._test_mode_on():
                return False
            b64 = auth[len("Basic "):] if auth.startswith("Basic ") else ""
            if not b64:
                return False
            decoded = base64.b64decode(b64).decode("utf-8", "ignore")
            entered_user, _, entered_pwd = decoded.partition(":")
            if entered_user == _dyn_user() and entered_pwd:
                return _verify_dynamic_password(entered_pwd)
        except Exception:
            pass
        return False

    def _send_403_security(self, msg: str = "IP 不在安全白名单内", as_json: bool = False):
        if as_json:
            self._send_json({"error": msg}, 403)
            return
        try:
            html = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>访问受限</title></head><body style="font-family:sans-serif;background:#1a1a2e;color:#eee;'
                    'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">'
                    '<div style="text-align:center;padding:24px;">'
                    '<h1 style="font-size:80px;margin:0;color:#e94560;">403</h1>'
                    '<p style="font-size:18px;">当前 IP 不在安全白名单内，无法访问该页面。</p>'
                    '<p style="color:#888;font-size:13px;">请管理员在该 IP 的白名单中加入你所在网络的地址。</p>'
                    '</div></body></html>')
            self.send_response(403, "Forbidden")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))
        except Exception:
            pass

    def do_GET(self):
        if not self._check_rate_limit():
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path != '/' and path.endswith('/'):
                path = path[:-1]

            if not self._public_path(path) and (path == '/admin' or path.startswith('/api/')):
                if not self._require_auth(html_ok=(path == '/admin')):
                    return

            if path in ('/', '/whitelist') or path in ('/api/whitelist/apply', '/api/whitelist/status'):
                if not self._security_allowed():
                    self._send_403_security(as_json=path.startswith('/api/'))
                    return

            if (path == '/admin' or path.startswith('/api/')) and not self._public_path(path):
                if not self._scope_gate(path):
                    return

            if path == '/gmpage' or path.startswith('/gmpage/'):
                self._handle_gmod_static(path)
                return

            # 品牌位 SVG：登录页 / 首页 / 管理页都要用，必须免鉴权，
            # 且不受安全白名单限制，否则没登录时 logo 会裂图。
            if path == BRAND_ASSET_PATH:
                self._serve_brand_asset()
                return

            if path == '/':
                # 官网首页：优先交给启用的 index 型子插件
                if self._serve_gmod_page('index'):
                    return
                index_file = self.plugin.web_dir / "index.html"
                if index_file.exists():
                    self._serve_file(index_file)
                else:
                    self._serve_html(HTML_INDEX)
                return
            if path == '/login':
                self._serve_login(parsed.query)
                return
            if path == '/logout':
                self._handle_logout()
                return
            if path == '/admin':
                # 管理页：优先交给启用的 admin 型子插件
                if self._serve_gmod_page('admin'):
                    return
                self._serve_html(HTML_ADMIN)
                return
            if path == '/whitelist':
                self._serve_html(HTML_WHITELIST)
                return

            # 子插件页面（/ 或 /admin 被接管时）内的相对资源引用会落到这些根路径上，
            # 先尝试从当前 page 型子插件目录里取，取不到再走原有逻辑。
            if not path.startswith('/api/') and path != '/':
                first = path.lstrip('/').split('/', 1)[0]
                if first in ('assets', 'static', 'css', 'js', 'img', 'images',
                             'fonts', 'media', 'vendor', 'lib', 'dist'):
                    if self._gmod_asset_served(path):
                        return

            # 记录本次请求的 query string，供个别 handler（如配置保存）在
            # body 里没带 id 时兜底取 ?id=xxx。只是一次属性赋值，开销可忽略。
            self._current_query = parsed.query or ""

            if path == '/api/players':
                self._handle_api_players()
            elif path.startswith('/api/player/'):
                player_name = path.split('/')[-1]
                self._handle_api_player_detail(player_name)
            elif path == '/api/scoreboards':
                self._handle_api_scoreboards()
            elif path == '/api/bans':
                self._handle_api_bans()
            elif path == '/api/gamerules':
                self._handle_api_gamerules()
            elif path == '/api/messages':
                self._handle_api_messages(parsed.query)
            elif path == '/api/system/stats':
                self._handle_api_system_stats()
            elif path == '/api/whitelist/applications':
                self._handle_api_whitelist_applications()
            elif path == '/api/whitelist/status':
                self._handle_api_whitelist_status(parsed.query)
            elif path == '/api/properties':
                self._handle_api_properties_get()
            elif path == '/api/worlds':
                self._handle_api_worlds()
            elif path == '/api/console':
                self._handle_api_console(parsed.query)
            elif path == '/api/qqbot/config':
                self._handle_api_qqbot_config(parsed.query)
            elif path == '/api/qqbot/status':
                self._handle_api_qqbot_status(parsed.query)
            elif path == '/api/qqbot/group/new':
                self._handle_api_qqbot_group_new(parsed.query)
            elif path == '/api/qqbot/qr/create':
                self._handle_api_qqbot_qr_create()
            elif path == '/api/qqbot/qr/poll':
                self._handle_api_qqbot_qr_poll(parsed.query)
            elif path == '/api/qqbot/regex/rules':
                self._handle_api_regex_rules()
            elif path == '/api/bots/list':
                self._handle_api_bots_list()
            elif path == '/api/tools/config':
                self._handle_api_tools_config()
            elif path == '/api/cloud/list':
                self._handle_api_cloud_list(parsed.query)
            elif path == '/api/cloud/stats':
                self._handle_api_cloud_stats()
            elif path == '/api/diag':
                self._handle_api_diag()
            elif path == '/api/diag/stacks':
                self._handle_api_diag_stacks()
            # ★ /api/diag/toggle 已挪到 do_POST（写操作不应挂在 GET 上），
            # 这里保留一个明确的 405 而不是 404，方便旧前端/运维排查。
            elif path.startswith('/api/diag/toggle'):
                self._send_json({"error": "诊断开关已改为 POST /api/diag/toggle"}, 405)
            elif path == '/api/cross/config':
                self._handle_cross_config()
            elif path == '/api/cross/status':
                self._handle_cross_status()
            elif path == '/api/gametools':
                self._handle_api_gametools()
            elif path == '/api/trust':
                self._handle_api_trust(parsed.query)
            elif path == '/api/trust/detail':
                self._handle_api_trust_detail(parsed.query)
            elif path == '/api/gmods/list':
                self._handle_api_gmods_list()
            elif path == '/api/gmods/files':
                self._handle_api_gmods_files(parsed.query)
            elif path == '/api/gmods/pages':
                self._handle_api_gmods_pages()
            elif path == '/api/gmods/settings':
                self._handle_api_gmods_settings(parsed.query)
            elif path == '/api/mods':
                self._handle_api_mods()
            elif path == '/api/auth/me':
                self._handle_api_auth_me()
            elif path == '/api/account/info':
                self._handle_api_account_info()
            elif path == '/api/account/temps':
                self._handle_api_account_temps()
            elif path == '/api/security/info':
                self._handle_api_security_info()
            elif path == '/api/files/list':
                self._handle_api_files_list(parsed.query)
            elif path == '/api/files/read':
                self._handle_api_files_read(parsed.query)
            elif path == '/api/files/download':
                self._handle_api_files_download(parsed.query)
            else:
                self.send_error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass
        except Exception as e:
            self.plugin.logger.error(f"GET 异常: {e}")
            try:
                self.send_error(500, "Internal Server Error")
            except Exception:
                pass

    def do_POST(self):
        path0 = ""
        # 标记本次请求的 body 是否已被读走（上传类接口自己流式读）。
        # 每个连接可能承载多个请求，这里每次进 do_POST 都重置，
        # 否则上一轮的标记会误伤这一轮。
        self._body_consumed = False
        try:
            path0 = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        except Exception:
            pass
        # 统一兜底：无论走哪条分支、有没有提前 return，
        # 退出前都把未消费的请求体排空，避免 keep-alive 串包。
        try:
            self._dispatch_post(path0)
        finally:
            self._drain_request_body()

    def _dispatch_post(self, path0):
        if path0 == '/api/cross/hub':
            self._handle_cross_hub()
            return
        if not self._check_rate_limit("write"):
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path != '/' and path.endswith('/'):
                path = path[:-1]

            if path == '/api/login':
                try:
                    data = self._read_post_data()
                except Exception:
                    data = {}
                self._handle_api_login(data)
                return

            if not self._public_path(path) and path.startswith('/api/'):
                if not self._require_auth():
                    # 未消费 body 就返回：必须排空，否则残留字节污染下一个请求
                    self._drain_request_body()
                    return
                if not self._scope_gate(path):
                    self._drain_request_body()
                    return
            elif not self._security_allowed():

                self._send_403_security(as_json=True)
                self._drain_request_body()
                return

            # 上传类接口自己从 rfile 流式读，不走 _read_post_data，不能排空
            if path == '/api/map':
                self._handle_api_map()
                return
            if path == '/api/mod/upload':
                self._handle_api_mod_upload()
                return
            if path == '/api/files/upload':
                self._handle_api_files_upload()
                return
            if path == '/api/gmods/upload':
                self._handle_api_gmods_upload()
                return

            try:
                data = self._read_post_data()
            except Exception:
                self._drain_request_body()
                self._send_json({"error": "读取请求失败"}, 400)
                return

            if path == '/api/kill':
                self._handle_api_kill(data)
            elif path == '/api/message':
                self._handle_api_message(data)
            elif path == '/api/give':
                self._handle_api_give(data)
            elif path == '/api/health':
                self._handle_api_health(data)
            elif path == '/api/tag':
                self._handle_api_tag(data)
            elif path == '/api/score':
                self._handle_api_score(data)
            elif path == '/api/kick':
                self._handle_api_kick(data)
            elif path == '/api/op':
                self._handle_api_op(data)
            elif path == '/api/teleport':
                self._handle_api_teleport(data)
            elif path == '/api/gamerule':
                self._handle_api_gamerule(data)
            elif path == '/api/ban':
                self._handle_api_ban(data)
            elif path == '/api/unban':
                self._handle_api_unban(data)
            elif path == '/api/permission':
                self._handle_api_permission(data)
            elif path == '/api/objective':
                self._handle_api_objective(data)
            elif path == '/api/whitelist/apply':
                self._handle_api_whitelist_apply(data)
            elif path == '/api/whitelist/review':
                self._handle_api_whitelist_review(data)
            elif path == '/api/properties/save':
                self._handle_api_properties_save(data)
            elif path == '/api/properties/set':
                self._handle_api_properties_set(data)
            elif path == '/api/world/switch':
                self._handle_api_world_switch(data)
            elif path == '/api/restart':
                self._handle_api_restart(data)
            elif path == '/api/qqbot/config/save':
                self._handle_api_qqbot_config_save(data)
            elif path == '/api/qqbot/group/rename':
                self._handle_api_qqbot_group_rename(data)
            elif path == '/api/qqbot/regex/save':
                self._handle_api_regex_save(data)
            elif path == '/api/qqbot/regex/toggle':
                self._handle_api_regex_toggle(data)
            elif path == '/api/qqbot/regex/delete':
                self._handle_api_regex_delete(data)
            elif path == '/api/qqbot/regex/test':
                self._handle_api_regex_test(data)
            elif path == '/api/bots/create':
                self._handle_api_bots_create(data)
            elif path == '/api/bots/update':
                self._handle_api_bots_update(data)
            elif path == '/api/bots/toggle':
                self._handle_api_bots_toggle(data)
            elif path == '/api/bots/delete':
                self._handle_api_bots_delete(data)
            elif path == '/api/tools/config/save':
                self._handle_api_tools_config_save(data)
            elif path == '/api/backup/trigger':
                self._handle_api_backup_trigger(data)
            elif path == '/api/backup/delete':
                self._handle_api_backup_delete(data)
            elif path == '/api/backup/restore':
                self._handle_api_backup_restore(data)
            elif path == '/api/cloud/test':
                self._handle_api_cloud_test(data)
            elif path == '/api/cloud/lookup':
                self._handle_api_cloud_lookup(data)
            elif path == '/api/gametools/save':
                self._handle_api_gametools_save(data)
            elif path == '/api/trust/save':
                self._handle_api_trust_save(data)
            elif path == '/api/trust/adjust':
                self._handle_api_trust_adjust(data)
            elif path == '/api/trust/device_trust':
                self._handle_api_trust_device_trust(data)
            elif path == '/api/trust/trusted_person':
                self._handle_api_trust_trusted_person(data)
            elif path == '/api/trust/export':
                self._handle_api_trust_export(data)
            elif path == '/api/trust/clear':
                self._handle_api_trust_clear(data)
            elif path == '/api/trust/recalc':
                self._handle_api_trust_recalc(data)
            elif path == '/api/mod/install':
                self._handle_api_mod_install(data)
            elif path == '/api/mod/delete':
                self._handle_api_mod_delete(data)
            elif path == '/api/mod/toggle':
                self._handle_api_mod_toggle(data)
            elif path == '/api/gmods/load':
                self._handle_api_gmods_load(data)
            elif path == '/api/gmods/unload':
                self._handle_api_gmods_unload(data)
            elif path == '/api/gmods/uninstall':
                self._handle_api_gmods_uninstall(data)
            elif path == '/api/gmods/enable':
                self._handle_api_gmods_enable(data)
            elif path == '/api/gmods/disable':
                self._handle_api_gmods_disable(data)
            elif path == '/api/gmods/file/save':
                self._handle_api_gmods_file_save(data)
            elif path == '/api/gmods/config/save':
                self._handle_api_gmods_config_save(data)
            elif path == '/api/cross/config/save':
                self._handle_cross_config_save(data)
            elif path == '/api/cross/test':
                self._handle_cross_test(data)
            elif path == '/api/console/command':
                self._handle_api_console_command(data)
            elif path == '/api/account/password':
                self._handle_api_account_password(data)
            elif path == '/api/account/temp/create':
                self._handle_api_account_temp_create(data)
            elif path == '/api/account/temp/revoke':
                self._handle_api_account_temp_revoke(data)
            elif path == '/api/account/test_mode':
                self._handle_api_account_test_mode(data)
            elif path == '/api/security/save':
                self._handle_api_security_save(data)
            elif path == '/api/diag/toggle':
                self._handle_api_diag_toggle(data)
            elif path == '/api/files/mkdir':
                self._handle_api_files_mkdir(data)
            elif path == '/api/files/delete':
                self._handle_api_files_delete(data)
            elif path == '/api/files/rename':
                self._handle_api_files_rename(data)
            elif path == '/api/files/move':
                self._handle_api_files_move(data)
            elif path == '/api/files/write':
                self._handle_api_files_write(data)
            elif path == '/api/files/copy':
                self._handle_api_files_copy(data)
            else:
                # 路由未命中：body 已由上面的 _read_post_data 读掉，
                # 这里直接回 404 即可，不会再污染连接。
                self.send_error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass
        except Exception as e:
            self.plugin.logger.error(f"POST 异常: {e}")
            try:
                self.send_error(500, "Internal Server Error")
            except Exception:
                pass

    def _drain_request_body(self) -> None:
        """把未消费的请求体读掉，保持 keep-alive 连接状态一致。

        HTTP/1.1 默认 keep-alive：同一个连接上会连续跑多个请求。
        若某个分支（鉴权失败、404、提前 return）没有读走 body，
        这些残留字节会被当成「下一个请求」的起始行来解析 ——
        轻则报 400，重则把用户请求错位处理（请求走私）。
        所以任何「不打算读 body 就返回」的路径都必须先调用本方法。
        """
        try:
            cl = int(self.headers.get('Content-Length', 0) or 0)
        except Exception:
            cl = 0
        if getattr(self, "_body_consumed", False):
            # 某个 handler 已经自己把 body 读走了（上传类接口），
            # 再去 read 会读到空数据甚至阻塞 —— 直接跳过。
            return
        try:
            if cl <= 0:
                return
            if cl > MAX_BODY_BYTES:
                # 体量异常：只能放弃这条连接，否则读不掉残留
                self.close_connection = True
                return
            remain = cl
            while remain > 0:
                chunk = self.rfile.read(min(remain, 65536))
                if not chunk:
                    break
                remain -= len(chunk)
        except Exception:
            # 读不动就直接断开，避免污染后续请求
            self.close_connection = True

    def _read_post_data(self) -> Dict[str, Any]:
        # Content-Length 可能是缺失/非数字/负数，直接 int() 会抛异常；
        # 这里统一兜成 0，并保证异常路径也会把 body 处理干净。
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except Exception:
            content_length = 0
            self.close_connection = True
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(content_length).decode('utf-8')
        self._body_consumed = True
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON")

    def _send_json(self, data: Dict[str, Any], status=200):
        # 必须带 Content-Length：HTTP/1.1 默认 keep-alive，浏览器靠
        # Content-Length 判断响应体边界，否则只能等连接关闭。缺了它，
        # 客户端复用连接发下一个请求时会读到本次响应的残留字节，
        # 表现为「偶发 JSON 解析失败 / 面板卡住 / 请求错乱」。
        try:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _serve_brand_asset(self):
        """送出品牌位 SVG。文件缺失时返回一个透明占位图，避免前端裂图。

        任何异常都不往上抛：logo 渲染失败绝不能影响页面本身。
        """
        data = _brand_svg_bytes()
        if not data:
            data = (
                b'<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0">'
                b'</svg>'
            )
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            self.send_header('Content-Length', str(len(data)))
            # logo 是静态资源，允许缓存，但给个短 TTL 便于换图后快速生效
            self.send_header('Cache-Control', 'public, max-age=300')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _serve_html(self, html_content):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')

            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(html_content.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _serve_file(self, filepath: Path):
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            if filepath.suffix == '.html':
                content_type = 'text/html; charset=utf-8'
            else:
                content_type = 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "File not found")
        except Exception as e:
            self.plugin.logger.error(f"读取文件失败: {e}")
            self.send_error(500, "Internal Server Error")

    def _handle_api_players(self):
        try:
            cache = self.plugin._get_cache()
            self._send_json({"players": cache.get("players", [])})
        except Exception as e:
            self.plugin.logger.error(f"获取玩家列表失败: {e}")
            self._send_json({"players": []})

    def _handle_api_player_detail(self, name: str):
        try:
            player = self.plugin._get_cached_player(name)
            if player is None:
                self._send_json({"error": f"玩家 {name} 不在线"}, 404)
            else:
                self._send_json(player)
        except Exception as e:
            self.plugin.logger.error(f"获取玩家详情失败: {e}")
            self._send_json({"error": "获取玩家详情失败"}, 500)

    def _handle_api_scoreboards(self):
        try:
            cache = self.plugin._get_cache()
            self._send_json({
                "objectives": cache.get("objective_names", []),
                "full_objectives": cache.get("objectives", []),
            })
        except Exception as e:
            self.plugin.logger.error(f"获取记分板目标失败: {e}")
            self._send_json({"objectives": [], "full_objectives": []})

    def _handle_api_bans(self):
        try:
            cache = self.plugin._get_cache()
            self._send_json({
                "player_bans": cache.get("player_bans", []),
                "ip_bans": cache.get("ip_bans", []),
            })
        except Exception as e:
            self.plugin.logger.error(f"获取封禁列表失败: {e}")
            self._send_json({"player_bans": [], "ip_bans": []})

    def _handle_api_messages(self, query):
        qs = urllib.parse.parse_qs(query)
        try:
            after = int(qs.get('after', ['0'])[0])
        except Exception:
            after = 0
        try:
            with self.plugin._messages_lock:
                last_id = self.plugin._message_seq
                # id 单调递增，从尾部倒着取，正常增量轮询是 O(1)
                new_msgs = []
                for m in reversed(self.plugin._messages):
                    if m["id"] <= after:
                        break
                    new_msgs.append(m)
                new_msgs.reverse()
            self._send_json({"messages": new_msgs, "last_id": last_id})
        except Exception as e:
            self.plugin.logger.error(f"获取消息失败: {e}")
            self._send_json({"messages": [], "last_id": 0})

    def _handle_api_gamerules(self):
        try:
            self._send_json({"gamerules": self.plugin.get_gamerules()})
        except Exception as e:
            self.plugin.logger.error(f"获取游戏规则失败: {e}")
            self._send_json({"gamerules": []})

    def _handle_api_gamerule(self, data):
        name = data.get('name')
        value = data.get('value')
        if not name:
            self._send_json({"error": "缺少游戏规则名称"}, 400)
            return
        known = {g["name"]: g for g in GAMERULES}
        rule = known.get(name)
        if not rule:
            self._send_json({"error": f"未知的游戏规则: {name}"}, 400)
            return
        if rule["type"] == "bool":
            value = bool(value)
        else:
            try:
                value = int(value)
            except (TypeError, ValueError):
                self._send_json({"error": "数值类型错误"}, 400)
                return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_gamerule, name, value)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"设置游戏规则失败: {e}")
            self._send_json({"error": "设置游戏规则失败"}, 500)

    def _execute_gamerule(self, name: str, value):
        value_str = str(value).lower() if isinstance(value, bool) else str(value)
        try:
            ok = self.plugin.server.dispatch_command(
                self.plugin.server.command_sender, f"gamerule {name} {value_str}"
            )
        except Exception as e:
            return False, f"执行 gamerule 命令异常: {e}"
        if ok:
            self.plugin._gamerule_values[name] = value
            self.plugin._save_gamerules()
            return True, f"已设置 {name} = {value_str}"
        return False, f"设置 {name} 失败"

    def _handle_api_ban(self, data):
        ban_type = data.get('type', 'player')
        target = data.get('target')
        by = data.get('by', 'name')
        if not target:
            self._send_json({"error": "缺少 target（玩家名、XUID 或 IP）"}, 400)
            return
        if ban_type not in ('player', 'ip'):
            self._send_json({"error": "type 必须是 'player' 或 'ip'"}, 400)
            return
        if by not in ('name', 'xuid'):
            self._send_json({"error": "by 必须是 'name' 或 'xuid'"}, 400)
            return
        try:
            success, msg = self._execute_ban(ban_type, target, data.get('reason'), data.get('expires'), by)
            if success and self._sync_cloud_flag(data.get('sync_cloud')):
                self._sync_ban_to_cloud(ban_type, target, by, data.get('reason'), msg)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"封禁失败: {e}")
            self._send_json({"error": "封禁失败"}, 500)

    @staticmethod
    def _sync_cloud_flag(val) -> bool:
        return bool(val) if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")

    def _sync_ban_to_cloud(self, ban_type: str, target: str, by: str, reason, ban_msg):

        try:
            cfg = self.plugin._cloud_cfg()
            payload = {"reason": reason or "被管理员封禁"}
            sn = str(cfg.get("server_name") or "").strip()
            if sn:
                payload["server_name"] = sn
            if ban_type == "ip":
                payload["ip_address"] = str(target)
            else:
                payload["player_name"] = str(target)
                if by == "xuid":
                    payload["xuid"] = str(target)
                else:
                    try:
                        resolved = self.plugin._run_in_server_thread(
                            self.plugin._resolve_player_ident, str(target), timeout=3.0)
                        if isinstance(resolved, tuple) and len(resolved) == 2:
                            xuid, ip = resolved
                            if xuid:
                                payload["xuid"] = str(xuid)
                            if ip:
                                payload["ip_address"] = str(ip)
                    except Exception:
                        pass
            self.plugin._cloud_sync(payload)
            self.plugin.logger.info(f"[云黑] 已提交同步封禁：{payload.get('player_name') or payload.get('ip_address') or payload.get('xuid')}")
        except Exception as e:
            self.plugin.logger.warning(f"[云黑] 准备同步封禁失败: {e}")

    def _resolve_online_by_xuid(self, xuid_str: str):

        xuid = str(xuid_str).strip()
        try:
            for p in self.plugin.server.online_players:
                try:
                    pxuid = getattr(p, "xuid", None)
                    if pxuid is not None and str(pxuid).strip() == xuid:
                        return True, p.name, xuid
                except Exception:
                    continue
            return True, "", xuid
        except Exception:
            return True, "", xuid

    def _execute_ban(self, ban_type: str, target: str, reason, expires, by: str = "name"):
        exp_dt = _parse_expiration(expires)
        reason = reason or "被管理员封禁"
        if ban_type == "ip":
            try:
                self.plugin.server.ip_ban_list.add_ban(target, reason, exp_dt, "GreenMoon")
            except Exception as e:
                return False, f"封禁失败: {e}"
            self.plugin._cross_push_bans()
            return True, f"已封禁 {target}"

        if by == "xuid":
            xuid_value = str(target).strip()
            if not xuid_value:
                return False, "XUID 不能为空"

            name = ""
            try:
                resolved = self.plugin._run_in_server_thread(self._resolve_online_by_xuid, xuid_value, timeout=3.0)
                if isinstance(resolved, tuple) and len(resolved) == 3 and resolved[0] is True:
                    name = resolved[1] or ""
            except Exception:
                pass
            try:
                self.plugin.server.ban_list.add_ban(name, None, xuid_value, reason, exp_dt, "GreenMoon")
            except Exception as e:
                return False, f"封禁失败: {e}"

            self.plugin._cross_push_bans()
            self.plugin._run_async_on_server_thread(self._kick_if_online, name, reason)
            if name:
                return True, f"已封禁 {name}（在线，已按 XUID 解析）"
            return True, f"已封禁 XUID {xuid_value}（重连后可拦截）"

        try:
            self.plugin.server.ban_list.add_ban(target, None, None, reason, exp_dt, "GreenMoon")
        except Exception as e:
            return False, f"封禁失败: {e}"
        self.plugin._cross_push_bans()
        self.plugin._run_async_on_server_thread(self._kick_if_online, target, reason)
        return True, f"已封禁 {target}"

    def _kick_if_online(self, player_name: str, reason: str):
        if not player_name:
            return
        p = self.plugin.server.get_player(player_name)
        if p:
            try:
                p.kick(reason)
            except Exception:
                pass

    def _handle_api_unban(self, data):
        ban_type = data.get('type', 'player')
        target = data.get('target')
        if not target:
            self._send_json({"error": "缺少 target"}, 400)
            return
        if ban_type not in ('player', 'ip'):
            self._send_json({"error": "type 必须是 'player' 或 'ip'"}, 400)
            return
        try:
            success, msg = self._execute_unban(ban_type, target, data.get('uuid'), data.get('xuid'))
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"解封失败: {e}")
            self._send_json({"error": "解封失败"}, 500)

    def _execute_unban(self, ban_type: str, target: str, uuid_str=None, xuid=None):
        try:
            if ban_type == "ip":
                self.plugin.server.ip_ban_list.remove_ban(target)
            else:
                uuid_obj = UUID(uuid_str) if uuid_str else None
                self.plugin.server.ban_list.remove_ban(target or "", uuid_obj, xuid)
        except ValueError:
            return False, "UUID 格式无效"
        except Exception as e:
            return False, f"解封失败: {e}"
        self.plugin._cross_push_bans()
        return True, f"已解封 {target or uuid_str or xuid or ''}"

    def _handle_api_tools_config(self):
        try:
            cfg = self.plugin._tools_public_config()
            cfg["backup_state"] = self.plugin._get_backup_state()
            cfg["backups"] = self.plugin._list_backups()
            self._send_json({"ok": True, "config": cfg})
        except Exception as e:
            self.plugin.logger.error(f"读取备份/云黑配置失败: {e}")
            self._send_json({"error": f"读取失败: {e}"}, 500)

    def _handle_api_diag(self):
        try:
            self._send_json({"ok": True, "diag": self.plugin._diag_summary()})
        except Exception as e:
            self.plugin.logger.error(f"读取诊断信息失败: {e}")
            self._send_json({"error": f"读取诊断信息失败: {e}"}, 500)

    def _handle_api_diag_stacks(self):
        # 注意：Endstone 的 logger 只接受单个 str 参数，不支持 "%s" 占位符格式化，
        # 也不接受 logging 风格的多参数调用（多参数会抛 TypeError）。
        # 因此这里一律用 f-string 拼成单个字符串再传。
        try:
            stacks = self.plugin._dump_all_stacks()
        except Exception as e:
            self.plugin.logger.error(f"线程栈转储失败: {e}")
            self._send_json({"error": f"线程栈转储失败: {e}"}, 500)
            return

        if not stacks or not str(stacks).strip():
            stacks = "(未获取到线程栈)"

        # 打印失败不应影响接口返回，单独兜底
        try:
            self.plugin.logger.info(f"[诊断] 手动触发线程栈转储:\n{stacks}")
        except Exception as e:
            try:
                self.plugin.logger.warning(f"[诊断] 线程栈打印失败（不影响查看）: {e}")
            except Exception:
                pass

        # 同时把栈文本回传前端，即使控制台打印失败用户也能看到
        self._send_json({"ok": True, "message": "已打印到服务端控制台",
                         "len": len(stacks), "stacks": stacks})

    def _handle_api_diag_toggle(self, data):
        """切换诊断开关。

        ★ 从 GET 挪到 POST：它是一次**有副作用的写操作**（落盘 _diag_save_cfg）。
        GET 在浏览器/代理语义里必须幂等可预取、可被 <img>/<link> 触发，
        放在 GET 上意味着链接一旦被爬或被预取就会改配置。改成 POST + JSON body。
        为了兼容旧前端，body 缺字段时回落到 query string。
        """
        try:
            if not self._require_admin_role("切换诊断开关"):
                return
            d = data if isinstance(data, dict) else {}
            if not d:
                qs = urllib.parse.urlparse(getattr(self, "path", "")).query
                params = urllib.parse.parse_qs(qs or "")
                d = {"key": (params.get("key") or [""])[0],
                     "v": (params.get("v") or ["1"])[0]}
            key = str(d.get("key", "") or "")
            v = d.get("v", 1)
            if key not in ("enabled", "detailed", "min_level"):
                self._send_json({"error": "无效的开关项"}, 400)
                return
            if key == "min_level":
                # ★ 级别用**字符串白名单**校验，不能像布尔项那样强转。
                #   非法值直接 400，避免把脏值写进 diag.json 后前端回填出
                #   一个不存在的选项。空串是合法值 —— 表示「跟随详细日志」。
                lv = str(v if v is not None else "").strip().lower()
                if lv and lv not in self.plugin._PANEL_LEVEL_RANK:
                    self._send_json({"error": f"无效的日志级别: {v!r}"}, 400)
                    return
                self.plugin._diag_cfg["min_level"] = lv
            else:
                if isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "on", "yes")
                self.plugin._diag_cfg[key] = bool(v)
            self.plugin._diag_save_cfg()
            self._send_json({"ok": True,
                             "enabled": self.plugin._diag_on(),
                             "detailed": self.plugin._diag_verbose(),
                             "min_level": self.plugin._diag_cfg.get("min_level", ""),
                             "effective": self.plugin._panel_min_level()})
        except Exception as e:
            self.plugin.logger.error(f"切换诊断设置失败: {e}")
            self._send_json({"error": f"切换诊断设置失败: {e}"}, 500)

    def _cross_auth_ok(self, cross):
        try:
            pwd_cfg = str(cross.cfg.get("password", "") or "")
            if not pwd_cfg:
                return False
            provided = self.headers.get("X-Cross-Pass", "") or ""
            try:
                provided = urllib.parse.unquote(provided)
            except Exception:
                pass
            # 用 compare_digest 而非 == ：== 在首个不同字符处就返回，
            # 攻击者可据此逐字节试探出正确口令（跨服口令一旦泄露，
            # 对方就能推送伪造的白名单/封禁名单并拉走玩家数据）。
            if not provided:
                return False
            try:
                return hmac.compare_digest(str(provided), pwd_cfg)
            except Exception:
                return False
        except Exception:
            return False

    def _handle_cross_hub(self):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None or not cross.enabled():
                self._send_json({"error": "跨服未启用"}, 403)
                return
            if not self._cross_auth_ok(cross):
                self._send_json({"error": "password mismatch"}, 401)
                return
            data = self._read_post_data()
            action = str(data.get("action", "") or "")
            sender = str(data.get("server_name", "") or "")
            if cross.is_master():
                self._cross_hub_master(cross, action, sender, data)
            elif cross.is_slave():
                self._cross_hub_slave(cross, action, data)
            else:
                self._send_json({"ok": False, "error": "cross disabled"})
        except Exception as e:
            self.plugin.logger.error(f"[跨服] hub 处理异常: {e}")
            self._send_json({"error": f"hub error: {e}"}, 500)

    def _cross_broadcast_wl_to_peers(self, cross, wl, peers):
        for purl in peers.values():
            if purl:
                try:
                    cross._http_json("POST", purl.rstrip("/") + "/api/cross/hub", {"action": "wl_update", "names": dict(wl)})
                except Exception:
                    pass

    def _cross_broadcast_ban_to_peers(self, cross, bans, peers):
        for purl in peers.values():
            if purl:
                try:
                    cross._http_json("POST", purl.rstrip("/") + "/api/cross/hub", {"action": "ban_update", "entries": dict(bans)})
                except Exception:
                    pass

    def _cross_hub_master(self, cross, action, sender, data):
        if action == "register":
            sname = sender or str(data.get("server_name", "") or "")
            self_url = str(data.get("self_url", "") or "").strip()
            with cross.db_lock:
                peers = cross.db.setdefault("peers", {})
                if sname and sname != str(cross.cfg.get("server_name", "") or ""):
                    if self_url:
                        peers[sname] = self_url
                    else:
                        peers.pop(sname, None)
                cross._save_db()
                wl = dict(cross.db.get("whitelist", {}))
                bans = dict(cross.db.get("bans", {}))
                peerlist = dict(peers)
            self._send_json({"ok": True, "role": "master", "server_name": cross.cfg.get("server_name"), "whitelist": wl, "bans": bans, "peers": peerlist})
            return
        if action == "chat":
            try:
                origin = str(data.get("origin", "") or "server")
                cross._master_on_chat(sender or str(data.get("server", "") or ""), str(data.get("player", "") or ""), str(data.get("text", "") or ""), origin)
            except Exception as e:
                self.plugin.logger.warning(f"[跨服] 主服处理聊天失败: {e}")
            self._send_json({"ok": True})
            return
        if action == "sys_notice":
            try:
                cross._master_on_sys(str(data.get("text", "") or ""))
            except Exception as e:
                self.plugin.logger.warning(f"[跨服] 主服处理系统通知失败: {e}")
            self._send_json({"ok": True})
            return
        if action == "player_get":
            uuid = str(data.get("uuid", "") or "")
            rec = (cross.db.get("players") or {}).get(uuid)
            self._send_json({"ok": True, "data": rec or None})
            return
        if action == "player_put":
            uuid = str(data.get("uuid", "") or "")
            nm = str(data.get("name", "") or "")
            pd = data.get("data") or {}
            with cross.db_lock:
                cross.db.setdefault("players", {})[uuid] = {"name": nm, "data": pd, "ts": time.time()}
                cross._save_db()
            self._send_json({"ok": True})
            return
        if action in ("wl_set", "wl_merge"):
            names = data.get("names")
            if isinstance(names, dict):
                with cross.db_lock:
                    cross.db.setdefault("whitelist", {}).update(names)
                    cross._save_db()
                    wl = dict(cross.db["whitelist"])
                    peers = dict(cross.db.get("peers", {}))
                self._cross_broadcast_wl_to_peers(cross, wl, peers)
            self._send_json({"ok": True})
            return
        if action in ("ban_set", "ban_merge"):
            entries = data.get("entries")
            if isinstance(entries, dict):
                with cross.db_lock:
                    cross.db.setdefault("bans", {}).update(entries)
                    cross._save_db()
                    bans = dict(cross.db["bans"])
                    peers = dict(cross.db.get("peers", {}))
                self._cross_broadcast_ban_to_peers(cross, bans, peers)
            self._send_json({"ok": True})
            return
        if action == "ping":
            self._send_json({"ok": True, "role": "master", "server_name": cross.cfg.get("server_name")})
            return
        self._send_json({"ok": False, "error": "unknown master action"})

    def _cross_hub_slave(self, cross, action, data):
        if action == "relay":
            sname = str(data.get("server", "") or "")
            player = str(data.get("player", "") or "")
            text = str(data.get("text", "") or "")
            try:
                self.plugin._cross_broadcast(f"§b[{sname}] §f{player}: §7{text}")
            except Exception:
                pass
            self._send_json({"ok": True})
            return
        if action == "sys_relay":
            text = str(data.get("text", "") or "")
            if text:
                try:
                    self.plugin._cross_broadcast(text)
                except Exception:
                    pass
            self._send_json({"ok": True})
            return
        if action == "wl_update":
            names = data.get("names")
            if isinstance(names, (list, dict)):
                keylist = list(names.keys()) if isinstance(names, dict) else names
                try:
                    self.plugin._apply_cross_whitelist(keylist)
                except Exception:
                    pass
                with cross.db_lock:
                    if isinstance(names, dict):
                        cross.db["whitelist"] = dict(names)
                        cross._save_db()
            self._send_json({"ok": True})
            return
        if action == "ban_update":
            entries = data.get("entries")
            if isinstance(entries, dict):
                try:
                    self.plugin._apply_cross_bans(entries)
                except Exception:
                    pass
                with cross.db_lock:
                    cross.db["bans"] = dict(entries)
                    cross._save_db()
            self._send_json({"ok": True})
            return
        if action == "player_restore":
            self._send_json({"ok": True})
            return
        if action == "ping":
            self._send_json({"ok": True, "role": "slave", "server_name": cross.cfg.get("server_name")})
            return
        self._send_json({"ok": False, "error": "unknown slave action"})

    def _handle_cross_config(self):
        try:
            cross = getattr(self.plugin, "cross", None)
            cfg = dict(cross.cfg) if cross is not None else dict(DEFAULT_CROSS_CONFIG)
            # ★ 跨服密码绝不能明文回前端。原实现直接 dict(cross.cfg) 全量下发，
            # password 跟着进 JSON、再被 static/admin.js 写进 DOM——
            # 任何能打开面板的人（含只勾了 cross 板块的临时账号）都能读到
            # 并据此伪装成其它节点加入集群，进而伪造玩家数据/白名单/封禁同步。
            # 这里改成等长掩码：前端看到「已设置」，留空即代表不修改。
            secret = str(cfg.get("password", "") or "")
            if secret:
                cfg["password"] = "*" * len(secret)
            cfg["password_set"] = bool(secret)
            self._send_json({"config": cfg})
        except Exception as e:
            self._send_json({"error": f"读取跨服配置失败: {e}"}, 500)

    def _handle_cross_status(self):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None:
                self._send_json({"ok": True, "role": "none", "enabled": False, "started": False})
                return
            peers = dict(cross.db.get("peers", {})) if hasattr(cross, "db") else {}
            self._send_json({
                "ok": True,
                "role": cross.role(),
                "enabled": cross.enabled(),
                "started": cross.started,
                "experimental": bool(getattr(cross, "experimental", lambda: False)()),
                "server_name": cross.cfg.get("server_name", ""),
                "master_url": cross.cfg.get("master_url", ""),
                "last_ping": cross.last_ping,
                "last_error": cross.last_error,
                "peers": peers,
                "players": len((cross.db.get("players") or {})) if hasattr(cross, "db") else 0,
            })
        except Exception as e:
            self._send_json({"error": f"读取跨服状态失败: {e}"}, 500)

    def _handle_cross_config_save(self, data):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None:
                self._send_json({"error": "跨服模块不可用"}, 500)
                return
            with cross.lock:
                old_role = cross.role()
                cfg = cross.cfg
                cfg["enabled"] = bool(data.get("enabled", False))
                role = str(data.get("role", "none") or "none")
                if role not in ("none", "master", "slave"):
                    role = "none"
                cfg["role"] = role
                cfg["server_name"] = str(data.get("server_name", cfg.get("server_name", "本服")) or "").strip() or cfg.get("server_name", "本服")
                # ★ 掩码语义：前端拿到的是 "*"*len，用户不动这个框就必须保留原密码。
                # 只认「全星号」为掩码；真·密码恰好全是星号这种极端情况按掩码处理
                # （跨服密码是机器生成的共享密钥，实际不会长这样）。
                new_pass = str(data.get("password", "") or "")
                if new_pass and not (set(new_pass) == {"*"}):
                    cfg["password"] = new_pass
                cfg["master_url"] = str(data.get("master_url", cfg.get("master_url", "")) or "").strip()
                cfg["self_url"] = str(data.get("self_url", cfg.get("self_url", "")) or "").strip()
                try:
                    cfg["sync_seconds"] = int(float(data.get("sync_seconds", cfg.get("sync_seconds", 8)) or 8))
                except Exception:
                    pass
                interop = dict(cfg.get("interop", {}))
                received = data.get("interop")
                if isinstance(received, dict):
                    for k in ("chat", "player_data", "whitelist", "ban"):
                        interop[k] = bool(received.get(k, interop.get(k, True)))
                cfg["interop"] = interop
                # 实验板开关。★ 用「键是否存在」判断而不是 data.get("experimental", False)：
                # 老前端的 payload 里压根没有这个键，若用默认值兜底，
                # 每次从旧页面保存配置都会把开着的实验板**静默关掉**。
                if "experimental" in data:
                    cfg["experimental"] = bool(data.get("experimental"))
                else:
                    cfg.setdefault("experimental", False)
                cross.save()
                cross.stop()
                cross.start()
            self._send_json({"ok": True})
        except Exception as e:
            self.plugin.logger.error(f"[跨服] 保存配置失败: {e}")
            self._send_json({"error": f"保存配置失败: {e}"}, 500)

    def _handle_cross_test(self, data):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None:
                self._send_json({"error": "跨服模块不可用"}, 500)
                return
            if cross.is_master():
                self._send_json({"ok": True, "message": "当前为主服，无需测试连接"})
                return
            base = cross.master_base()
            if not base:
                self._send_json({"error": "未填写主服地址"}, 400)
                return
            if not str(cross.cfg.get("password", "") or "").strip():
                self._send_json({"error": "未设置连接密码"}, 400)
                return
            r = cross._hub(base, {"action": "ping", "server_name": cross.cfg.get("server_name")})
            if r is None:
                self._send_json({"error": f"连接失败: {cross.last_error or '无响应'}"})
                return
            if not r.get("ok"):
                self._send_json({"error": r.get("error") or "连接失败"})
                return
            cross.last_ping = time.time()
            cross.last_error = ""
            self._send_json({"ok": True, "message": f"连接成功！已连接主服（{cross.cfg.get('server_name')}）"})
        except Exception as e:
            self._send_json({"error": f"测试失败: {e}"}, 500)

    _ALL_SCOPES = ["players", "logs", "gamerules", "bans", "whitelist", "worlds", "perm",
                   "scoreboard", "detail", "console", "qqbot", "cross", "backup", "cloud",
                   "gametools", "mods", "diag", "files", "trust"]

    def _scope_label(self, s: str) -> str:
        labels = {
            "players": "在线玩家", "logs": "日志中心", "gamerules": "游戏规则", "bans": "封禁管理",
            "whitelist": "白名单审核", "worlds": "多存档", "perm": "权限管理", "scoreboard": "记分板",
            "detail": "玩家详情", "console": "操作控制台", "qqbot": "QQ机器人", "cross": "跨服互联",
            "backup": "自动备份", "cloud": "云黑名单", "gametools": "签到绑定", "mods": "模组管理",
            "diag": "诊断", "files": "文件管理", "trust": "玩家可信度",
        }
        return labels.get(s, s)

    def _handle_api_auth_me(self):
        role = getattr(self, "_auth_role", "admin")
        scopes = getattr(self, "_auth_scopes", None)
        cur_user = getattr(self, "_auth_user", "")
        if role == "admin":
            scopes = ["*"]
        else:
            scopes = sorted(set(scopes or []) & set(self._ALL_SCOPES))
        self._send_json({"ok": True, "role": role, "user": cur_user, "scopes": scopes,
                         "all_scopes": self._ALL_SCOPES})

    def _handle_api_account_info(self):
        cfg = getattr(self.plugin, "web_config", {}) or {}
        self._send_json({"ok": True,
                         "admin_user": str(cfg.get("admin_user", "") or ""),
                         "admin_configured": bool(str(cfg.get("admin_user", "") or "") and
                                                  str(cfg.get("admin_pass", "") or "")),
                         # 默认 False：与 core._load_config 的默认值、以及
                         # _test_mode_on() 的判定保持一致。前端据此渲染开关。
                         "test_mode": bool(cfg.get("test_mode", False)),
                         "all_scopes": self._ALL_SCOPES})

    def _handle_api_account_test_mode(self, data):
        if not self._require_admin_role("修改测试模式"):
            return
        try:
            # 缺字段时按「关闭」处理：保全默认关闭语义，
            # 不给「请求体里省略 test_mode」留下误开启的口子。
            value = bool((data or {}).get("test_mode", False))
            cfg = dict(getattr(self.plugin, "web_config", {}) or {})
            cfg["test_mode"] = value
            self.plugin.web_config = cfg
            self.plugin._persist_config(cfg)
            self._send_json({"ok": True, "test_mode": value})
        except Exception as e:
            self._send_json({"ok": False, "error": f"保存测试模式失败: {e}"}, 500)

    def _handle_api_account_password(self, data):
        if not self._require_admin_role("修改面板登录账号"):
            return
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        new_user = str((data or {}).get("admin_user", "") or "").strip()
        new_pass = str((data or {}).get("admin_pass", "") or "").strip()
        if not new_user or not new_pass:
            self._send_json({"error": "用户名与密码不能为空"}, 400)
            return
        if len(new_pass) < 4:
            self._send_json({"error": "密码长度至少 4 位"}, 400)
            return
        cfg["admin_user"] = new_user
        cfg["admin_pass"] = new_pass
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action(f"已修改面板登录账号")
        self._send_json({"ok": True, "message": "账号/密码已更新"})

    def _handle_api_account_temp_create(self, data):
        if not self._require_admin_role("创建临时账号"):
            return
        scope_keys = set((data or {}).get("scopes") or []) & set(self._ALL_SCOPES)
        try:
            expires = int(float((data or {}).get("expires", 0)) or 0)
        except Exception:
            expires = 0
        if not scope_keys:
            self._send_json({"error": "请至少选择一个可访问板块"}, 400)
            return
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        temp_accounts = cfg.get("temp_accounts") or []
        if not isinstance(temp_accounts, list):
            temp_accounts = []
        username = "tmp" + secrets.token_hex(3)
        password = secrets.token_urlsafe(9)
        tid = secrets.token_hex(6)
        exp = int(time.time()) + expires if expires and expires > 0 else 0
        temp_accounts.append({
            "id": tid,
            "username": username,
            "password": password,
            "expires": exp,
            "scopes": sorted(scope_keys),
            "created_at": int(time.time()),
        })
        cfg["temp_accounts"] = temp_accounts
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action("已创建临时账户")
        self._send_json({"ok": True, "id": tid, "username": username, "password": password,
                         "expires": exp, "scopes": sorted(scope_keys)})

    def _handle_api_account_temp_revoke(self, data):
        if not self._require_admin_role("撤销临时账号"):
            return
        tid = str((data or {}).get("id", "") or "").strip()
        if not tid:
            self._send_json({"error": "缺少账户 id"}, 400)
            return
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        temp_accounts = cfg.get("temp_accounts") or []
        if not isinstance(temp_accounts, list):
            temp_accounts = []
        new_list = [t for t in temp_accounts if not (isinstance(t, dict) and str(t.get("id", "")) == tid)]
        cfg["temp_accounts"] = new_list
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action("已撤销临时账户")
        self._send_json({"ok": True, "message": "临时账户已撤销"})

    def _handle_api_account_temps(self):
        cfg = getattr(self.plugin, "web_config", {}) or {}
        temp_accounts = cfg.get("temp_accounts") or []
        out = []
        now = int(time.time())
        for t in temp_accounts:
            if not isinstance(t, dict):
                continue
            exp = int(t.get("expires") or 0) or 0
            out.append({
                "id": str(t.get("id", "")),
                "username": str(t.get("username", "")),
                "expires": exp,
                "expired": bool(exp and now > exp),
                "scopes": sorted(set(t.get("scopes") or []) & set(self._ALL_SCOPES)),
            })
        self._send_json({"ok": True, "accounts": out})

    def _handle_api_security_info(self):
        sw = dict((getattr(self.plugin, "web_config", {}) or {}).get("security_whitelist") or {})
        self._send_json({"ok": True, "enabled": bool(sw.get("enabled")), "ips": list(sw.get("ips") or [])})

    def _handle_api_security_save(self, data):
        if not self._require_admin_role("保存安全白名单"):
            return
        enabled = bool((data or {}).get("enabled"))
        raw = str((data or {}).get("ips", "") or "")
        ips = []
        for item in raw.replace("，", ",").split(","):
            item = item.strip()
            if item:
                ips.append(item)
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        dirty = dict(cfg.get("security_whitelist") or {})
        dirty["enabled"] = enabled
        dirty["ips"] = ips
        cfg["security_whitelist"] = dirty
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action("已更新安全系统 IP 白名单")
        self._send_json({"ok": True, "message": "安全白名单已保存"})

    def _execute_console_command(self, command: str):
        try:
            self.plugin.logger.info(f"[控制台] 执行指令: {command}")
            self.plugin.server.dispatch_command(self.plugin.server.command_sender, command)
        except Exception as e:
            self.plugin.logger.error(f"执行控制台指令失败: {e}")

    def _handle_api_console_command(self, data):
        command = str((data or {}).get("command", "") or "").strip()
        if not command:
            self._send_json({"error": "指令不能为空"}, 400)
            return
        if len(command) > 512:
            self._send_json({"error": "指令过长"}, 400)
            return
        self.plugin._run_async_on_server_thread(self._execute_console_command, command)
        self.plugin._log_admin_action(f"向控制台发送指令: {command}")
        self._send_json({"ok": True, "success": True, "message": f"已发送指令: {command}"})

    # /api/tools/config 的 scope 是 backup+cloud_blacklist，就只能写这两块。
    _TOOLS_CONFIG_SECTIONS = {"backup", "cloud_blacklist"}

    def _handle_api_tools_config_save(self, data):
        try:
            ok, msg = self.plugin._save_tools_config(data, sections=self._TOOLS_CONFIG_SECTIONS)
            self._send_json({"ok": ok, "message": msg}) if ok else self._send_json({"error": msg})
        except Exception as e:
            self._send_json({"error": f"保存失败: {e}"}, 500)

    def _handle_api_backup_trigger(self, data):
        try:
            ok = self.plugin._trigger_backup()
            self._send_json({"ok": ok, "message": "已在后台启动备份" if ok else "备份任务正在运行中"})
        except Exception as e:
            self._send_json({"error": f"启动备份失败: {e}"}, 500)

    def _handle_api_backup_delete(self, data):
        try:
            name = (data or {}).get("name")
            ok, msg = self.plugin._delete_backup(name)
            self._send_json({"ok": ok, "message": msg} if ok else {"error": msg})
        except Exception as e:
            self._send_json({"error": f"删除失败: {e}"}, 500)

    def _handle_api_backup_restore(self, data):
        name = (data or {}).get("name")
        if not name:
            self._send_json({"error": "缺少备份文件名"}, 400)
            return

        try:
            ok, msg = self.plugin._restore_backup(str(name))
            if ok:
                self.plugin.logger.info(f"[备份] 已复制备份 {name} 到服务器根目录")
                self._send_json({"ok": True, "message": msg, "restart_required": True})
            else:
                self._send_json({"ok": False, "error": msg})
        except Exception as e:
            self.plugin.logger.error(f"提交恢复任务失败: {e}")
            self._send_json({"error": f"提交恢复任务失败: {e}"}, 500)

    def _handle_api_cloud_test(self, data):
        try:
            ok, msg, res = self.plugin._cloud_test()
            # 顺带把协商出的协议告诉前端，设置页要显示「新版 REST / 旧版 PHP」。
            try:
                style = self.plugin._cloud_api_style_name()
            except Exception:
                style = ""
            self._send_json({"ok": ok, "message": msg, "data": res, "api_style": style})
        except Exception as e:
            self._send_json({"error": f"测试失败: {e}"}, 500)

    def _handle_api_cloud_list(self, query=""):
        """云黑明细（新版接口才有）。旧版返回 ok=False + 说明，前端优雅降级。"""
        try:
            qs = urllib.parse.parse_qs(query or "")
            edition = (qs.get("edition") or [""])[0] or None
            ok, msg, bans = self.plugin._cloud_list_bans(edition)
            self._send_json({"ok": ok, "message": msg, "bans": bans or []})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/cloud/list 异常: {e}")
            self._send_json({"ok": False, "message": f"查询失败: {e}", "bans": []})

    def _handle_api_cloud_stats(self):
        try:
            ok, msg, st = self.plugin._cloud_stats()
            self._send_json({"ok": ok, "message": msg, "stats": st or {}})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/cloud/stats 异常: {e}")
            self._send_json({"ok": False, "message": f"查询失败: {e}", "stats": {}})

    def _handle_api_cloud_lookup(self, data):
        """手动查黑：排障用，按 XUID / IP 直接问云端。"""
        try:
            ok, msg, ban = self.plugin._cloud_lookup(data or {})
            self._send_json({"ok": ok, "message": msg, "ban": ban})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/cloud/lookup 异常: {e}")
            self._send_json({"ok": False, "message": f"查询失败: {e}"}, 500)

    def _handle_api_gametools(self):
        try:
            pl = self.plugin
            cfg = pl._load_tools_config()
            binding = cfg.get("binding", {}) or {}
            signin = cfg.get("signin", {}) or {}
            bind_list = []
            bindings = pl._load_bindings()
            for mc, entry in (bindings.get("by_mc", {}) or {}).items():
                if isinstance(entry, dict):
                    bind_list.append({
                        "mc": entry.get("mc", mc),
                        "qq": entry.get("qq", ""),
                        "bound_at": entry.get("bound_at", ""),
                    })
            self._send_json({
                "ok": True,
                "binding": binding,
                "signin": signin,
                "delayed": pl._delayed_stats(),
                "bindings": sorted(bind_list, key=lambda x: str(x.get("bound_at", "")), reverse=True),
                "signin_records": pl._load_signin().get("last", {}),
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/gametools 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    # /api/gametools/save 的 scope 是 gametools，但 _save_tools_config 一张表里有
    # 6 个板块。只允许写「gametools 板块自己管得着」的那些，其余由各自的
    # 专用接口负责（/api/tools/config 对应 backup+cloud_blacklist）。
    _GAMETOOLS_SECTIONS = {"binding", "signin", "mods"}

    def _handle_api_gametools_save(self, data):
        try:
            ok, msg = self.plugin._save_tools_config(data or {}, sections=self._GAMETOOLS_SECTIONS)
            self._send_json({"ok": bool(ok), "message": msg})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/gametools/save 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ------------------------------------------------------------ 玩家可信度
    # /api/trust* 的 scope 是 trust。_save_tools_config 那张表里有多个板块，
    # 这里只允许写 trust 自己那一个，其余板块各有专用接口。
    _TRUST_SECTIONS = {"trust"}

    def _trust_mgr(self):
        mgr = getattr(self.plugin, "trust", None)
        if mgr is None:
            raise RuntimeError("可信度模块未初始化")
        return mgr

    def _handle_api_trust(self, query=""):
        """GET /api/trust —— 配置 + 玩家列表 + 统计。"""
        try:
            pl = self.plugin
            mgr = self._trust_mgr()
            cfg = mgr.cfg(force=True)
            qs = urllib.parse.parse_qs(query or "")
            keyword = (qs.get("q") or [""])[0]
            limit = (qs.get("limit") or ["500"])[0]
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 500

            data = mgr.public_list(keyword=keyword, limit=limit)
            data["config"] = cfg
            data["defaults"] = json.loads(json.dumps(TRUST_DEFAULTS))
            data["player_dir"] = str(mgr._player_dir)
            data["backup_dir"] = str(mgr._backup_dir)
            data["reasons"] = dict(REASON_LABELS)
            data["ok"] = True
            self._send_json(data)
        except Exception as e:
            self.plugin.logger.error(f"GET /api/trust 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_save(self, data):
        """POST /api/trust/save —— 保存配置。"""
        try:
            mgr = self._trust_mgr()
            ok, msg = self.plugin._save_tools_config(data or {}, sections=self._TRUST_SECTIONS)
            if ok:
                # 配置改了要立刻生效，不能等 5 秒 TTL 过期
                mgr.invalidate_cfg()
            self._send_json({"ok": bool(ok), "message": msg})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/save 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_adjust(self, data):
        """POST /api/trust/adjust —— 服主手动加减权。"""
        try:
            mgr = self._trust_mgr()
            d = data or {}
            key = str(d.get("key") or "").strip()
            if not key:
                return self._send_json({"ok": False, "error": "缺少玩家标识"}, 400)
            try:
                delta = float(d.get("delta"))
            except (TypeError, ValueError):
                return self._send_json({"ok": False, "error": "加减分值不是数字"}, 400)
            note = str(d.get("note") or "")
            by = str(getattr(self, "_auth_user", "") or "admin")
            ok, msg, score = mgr.manual_adjust(key, delta, note, by)
            self._send_json({"ok": bool(ok), "message": msg, "score": score,
                             "error": "" if ok else msg}, 200 if ok else 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/adjust 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_device_trust(self, data):
        """POST /api/trust/device_trust —— 标记/取消「信任此设备」。"""
        try:
            mgr = self._trust_mgr()
            d = data or {}
            key = str(d.get("key") or "").strip()
            device_id = str(d.get("device_id") or "").strip()
            if not key or not device_id:
                return self._send_json({"ok": False, "error": "缺少玩家标识或设备 ID"}, 400)
            on = bool(d.get("trusted", True))
            by = str(getattr(self, "_auth_user", "") or "admin")
            ok, msg = mgr.trust_device(key, device_id, on, by)
            self._send_json({"ok": bool(ok), "message": msg,
                             "error": "" if ok else msg}, 200 if ok else 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/device_trust 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_trusted_person(self, data):
        """POST /api/trust/trusted_person —— 标记/取消「信任的人」。

        ★ 与「信任此设备」的区别（两者很容易搞混，这里写清楚）
        -----------------------------------------------------
        * ``trusted_person``（本接口）= **玩家级**，永久保持满分，
          绕过全部扣分逻辑（含断签惩罚）；
        * ``trust_device``（另一个接口）= **设备级**，只是让该设备
          不再被判定为"陌生设备"，玩家分数照常受其它规则影响。

        所以本接口的影响面比设备信任**大得多**，是有意设计成需要
        显式调用的独立入口，而不是混在设备信任里。
        """
        try:
            mgr = self._trust_mgr()
            d = data or {}
            name = str(d.get("name") or d.get("key") or "").strip()
            if not name:
                return self._send_json({"ok": False, "error": "缺少玩家名/标识"}, 400)
            on = bool(d.get("trusted", True))
            ok = bool(mgr.set_trusted_person(name, on))
            if not ok:
                # 找不到档案是**预期内的业务结果**（只标记已有档案的玩家），
                # 用 400 + 明确文案，而不是 500 —— 后者会让用户以为插件坏了。
                return self._send_json({
                    "ok": False,
                    "error": f"未找到玩家「{name}」的可信度档案；"
                             f"该玩家需先进入过服务器，或名字拼写有误。",
                }, 400)
            self._send_json({
                "ok": True,
                "trusted_person": on,
                "message": f"已{'标记' if on else '取消'}「信任的人」：{name}",
            })
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/trusted_person 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_detail(self, query=""):
        """GET /api/trust/detail?key=... —— 单个玩家完整档案。"""
        try:
            mgr = self._trust_mgr()
            qs = urllib.parse.parse_qs(query or "")
            key = (qs.get("key") or [""])[0]
            rec = mgr.detail(key)
            if rec is None:
                return self._send_json({"ok": False, "error": "档案不存在"}, 404)
            self._send_json({"ok": True, "record": rec})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/trust/detail 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_export(self, data):
        """POST /api/trust/export —— 导出全量档案。"""
        try:
            mgr = self._trust_mgr()
            payload = mgr.export_all()
            payload["ok"] = True
            self._send_json(payload)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/export 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_clear(self, data):
        """POST /api/trust/clear —— 清空全部档案（需 confirm=DELETE，且先备份）。"""
        try:
            mgr = self._trust_mgr()
            d = data or {}
            confirm = str(d.get("confirm") or "").strip()
            if confirm != "DELETE":
                return self._send_json(
                    {"ok": False, "error": "需要输入 DELETE 确认清空"}, 400)
            if not bool(d.get("backup", True)):
                # 用户明确选择不备份 —— 但仍然必须显式二次确认
                if confirm != "DELETE":
                    return self._send_json({"ok": False, "error": "确认无效"}, 400)
            ok, msg = mgr.clear_all()
            self._send_json({"ok": bool(ok), "message": msg,
                             "error": "" if ok else msg}, 200 if ok else 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/clear 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_trust_recalc(self, data):
        """POST /api/trust/recalc —— 手动触发一次结算（调试用）。"""
        try:
            mgr = self._trust_mgr()
            import time as _t
            mgr._last_settle_day = ""
            mgr.tick()
            self._send_json({"ok": True, "message": "结算任务已提交（后台执行）",
                             "at": _t.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/trust/recalc 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_mods(self):
        try:
            pl = self.plugin
            worlds, current_world, _root = pl._list_worlds()
            # 允许前端指定要查看哪个存档的启用状态
            qs = urllib.parse.parse_qs(getattr(self, "path", "").partition("?")[2])
            want_world = (qs.get("world") or [""])[0] or current_world
            installed, meta = pl._scan_installed_packs(want_world)
            # 按资源包 / 行为包分开，前端直接分组渲染
            resource, behavior, unknown = [], [], []
            for p in installed:
                kind = p.get("kind")
                if kind == "resource":
                    resource.append(p)
                elif kind == "behavior":
                    behavior.append(p)
                else:
                    unknown.append(p)
            self._send_json({
                "ok": True,
                "uploaded": pl._list_mod_uploaded(),
                "installed": installed,
                "resource_packs": resource,
                "behavior_packs": behavior,
                "unknown_packs": unknown,
                "worlds": worlds,
                "current_world": current_world,
                "meta": meta,
                # 手动投放目录：前端直接展示这个绝对路径，用户拿文件管理器
                # 拷进去即可（大文件走不了 HTTP 上传，这是唯一的入口）。
                "inbox_dir": str(pl._mods_inbox_dir()),
                # 教育模式模组的隐藏数量由 meta 带上，前端显示为一行提示。
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/mods 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_mod_toggle(self, data):
        """启用 / 停用某存档里的一个资源包或行为包（改写 world_*_packs.json）。"""
        d = data or {}
        uuid_ = str(d.get("uuid", "") or "").strip()
        pack_type = str(d.get("type", "") or "").strip()
        world = str(d.get("world", "") or "").strip()
        enabled = bool(d.get("enabled"))
        if not uuid_:
            self._send_json({"ok": False, "error": "缺少包 UUID"}, 400)
            return
        try:
            ok, msg = self.plugin.set_pack_enabled(world, uuid_, pack_type, enabled)
        except Exception as e:
            self.plugin.logger.error(f"切换模组启用状态失败: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)
            return
        self._send_json({"ok": ok, "message": msg if ok else "", "error": "" if ok else msg}, 200 if ok else 400)

    def _handle_api_mod_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "必须使用 multipart/form-data"}, 400)
            return
        boundary_match = re.search(r'boundary=([^;\s]+)', content_type)
        if not boundary_match:
            self._send_json({"ok": False, "error": "无效的 multipart 格式"}, 400)
            return
        boundary = boundary_match.group(1).encode('ascii')
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "请求体过大或非法"}, 400)
            return
        body = self.rfile.read(length)
        self._body_consumed = True
        parts = body.split(b'--' + boundary)
        fname = None
        fdata = None
        for part in parts:
            if not part or part in (b'--\r\n', b'--'):
                continue
            h_end = part.find(b'\r\n\r\n')
            if h_end == -1:
                continue
            header = part[:h_end].decode('utf-8', errors='ignore')
            content = part[h_end + 4:]
            if content.endswith(b'\r\n'):
                content = content[:-2]
            if 'filename=' in header:
                fm = re.search(r'filename="([^"]*)"', header)
                fname = fm.group(1) if fm else 'upload'
                if 'name="file"' in header or 'filename=' in header:
                    fdata = content
        if not fname or fdata is None:
            self._send_json({"ok": False, "error": "未收到文件"}, 400)
            return
        # 注意：display 可能含中文，只用于界面展示与清单记录；uname 一定是
        # 纯 ASCII，才能安全地进 HTTP 头 / URL —— 否则 send_header 会在
        # latin-1 编码时抛 UnicodeEncodeError，前端只看到「上传失败」。
        display = _upload_display_name(fname)
        if not (display.lower().endswith('.mcaddon') or display.lower().endswith('.mcpack')):
            self._send_json({"ok": False, "error": "只支持 .mcaddon / .mcpack 文件"}, 400)
            return
        try:
            uname, display = _safe_upload_name(fname, prefix=_uuid.uuid4().hex[:12] + "_")
            dest = self.plugin._mods_dir() / uname
            # 原子写：大包（几十 MB）写到一半崩溃会留下残缺 zip，
            # 用户点安装才报「File is not a zip file」，很难定位。
            if not atomic_write_bytes(str(dest), fdata):
                raise RuntimeError("写入磁盘失败（空间不足或被占用）")
            self.plugin._register_mod_file(uname, len(fdata), display)
            self._send_json({
                "ok": True,
                "message": f"已上传 {display}（{_fmt_size(len(fdata))}）",
                "file": uname,
                "name": display,
            })
        except Exception as e:
            self.plugin.logger.error(f"模组上传失败: {e}")
            self._send_json({"ok": False, "error": f"上传失败: {e}"}, 500)

    def _handle_api_mod_install(self, data):
        fname = (data or {}).get("file", "")
        target_type = (data or {}).get("target", "global")
        world_name = (data or {}).get("world", "")
        if not fname:
            self._send_json({"ok": False, "error": "缺少文件名"}, 400)
            return
        pl = self.plugin
        # ★ 不能写死 _mods_dir()：手动投放到 mods/inbox/ 的文件也在库里，
        #   写死的话界面上能看到、点安装却报「文件已不存在」。
        src, source = pl._find_mod_source(fname)
        if src is None:
            self._send_json({"ok": False, "error": "文件已不存在（上传库与本目录都没有）"}, 400)
            return
        def _worker():
            ok, msg = pl._install_mod_pack(src, target_type, world_name or None)
            pl.logger.info(f"[模组] 安装{'成功' if ok else '失败'}: {msg}")
            # 装完把源文件清掉，让它从「待安装」列表里消失。
            #
            # ★ 这里必须**两种来源一视同仁**。老代码只清 inbox：
            #       inbox_file = src if source == "inbox" else None
            #       if ok and inbox_file is not None: ...
            #   而网页上传（source == "upload"）是主路径，拿到的永远是 None，
            #   清理整段被跳过 —— 用户装完刷新，「待安装」里还是同一个包，
            #   分不清到底装没装过。这就是本轮修复的 bug。
            #
            # 失败时**保留**源文件：用户修正后可以直接重试，不用重新上传。
            # 部分成功也按失败处理（ok=False），因为还有包没装进去，
            # 保留源文件让用户重试更安全。
            if ok:
                removed, note = pl._cleanup_installed_source(fname)
                pl.logger.info(
                    f"[模组] 清理源文件 {fname}: {'已移除' if removed else '未移除'}"
                    f"（{note}）"
                )
        try:
            pl._executor.submit(_worker)
        except Exception as e:
            self._send_json({"ok": False, "error": f"提交安装失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "正在后台安装，完成后自动更新清单（一般数秒）"})

    def _handle_api_mod_delete(self, data):
        fname = (data or {}).get("file", "")
        if not fname:
            self._send_json({"ok": False, "error": "缺少文件名"}, 400)
            return
        if self.plugin._delete_mod_file(fname):
            self._send_json({"ok": True, "message": "已删除"})
        else:
            self._send_json({"ok": False, "error": "删除失败"}, 400)

    _FILE_READ_LIMIT = 1024 * 1024
    _FILES_UPLOAD_LIMIT = 100 * 1024 * 1024

    _GMOD_UPLOAD_LIMIT = 64 * 1024 * 1024
    _GMOD_EXTS = (".gmmod", ".gmlib")

    # ---------------------------------------------------------------- 子插件

    def _gmod_loader(self):
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            self._send_json({"ok": False, "error": "子插件加载器未初始化"}, 500)
            return None
        return ld

    def _require_admin_role(self, what: str = "", action: str = "操作") -> bool:
        """要求当前会话是主管理员（``role == "admin"``），否则回 403。

        为什么不能只靠 ``_scope_gate``：``_SCOPE_EXACT`` 里
        ``/api/account/*``、``/api/security/*`` 一律映射到 ``account`` 板块。
        于是「新建临时账号」时只要顺手勾上 ``account`` 板块，那个临时账号
        就能回头调用本组接口给自己续命、建新号、改管理员密码、把 IP 白名单
        关掉——而 IP 白名单恰恰是唯一能挡住「绕过面板直连控制台」的闸门。
        这是一条完整的提权链，必须在业务层再钉一道角色门。

        主管理员的权限不受影响：``_resolve_auth`` 对 Basic 主账号返回
        ``("admin", None, user)``，这里直接放行，行为与修复前完全一致。
        """
        if getattr(self, "_auth_role", "admin") != "admin":
            try:
                u = getattr(self, "_auth_user", "") or "(未知)"
                self.plugin.logger.warning(f"[权限] 拒绝非管理员{u}执行：{what or action}")
            except Exception:
                pass
            self._send_json({"ok": False, "error": f"仅主管理员可{action}"}, 403)
            return False
        return True

    def _gmod_admin_only(self) -> bool:
        """子插件可携带任意代码，只允许管理员操作，临时账号一律拒绝。"""
        if getattr(self, "_auth_role", "admin") != "admin":
            self._send_json({"ok": False, "error": "仅管理员可管理子插件"}, 403)
            return False
        return True

    def _gmod_upload_dir(self) -> Path:
        d = self.plugin.mods_dir / ".upload"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------ 页面接管

    def _serve_gmod_page(self, ptype: str) -> bool:
        """把 ``/`` 或 ``/admin`` 交给启用的子插件。接管成功返回 True。

        注意：这里刻意直接读文件、不走 ``_serve_html`，因为子插件页面可能引用
        同目录下的相对资源（如 ``assets/app.js``），而浏览器会以当前路径为基准
        解析这些相对地址（``/assets/app.js``），需要由静态路由兜住。
        """
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            return False
        try:
            got = ld.active_page(ptype)
        except Exception as e:
            self.plugin.logger.warning(f"[子插件] 读取 {ptype} 页面接管状态失败: {e}")
            return False
        if not got:
            return False
        root, entry = got
        top = str(entry or "index.html").replace("\\", "/").split("/")[0]
        if str(entry).replace("\\", "/").strip("/") not in ("index.html", "index.htm"):
            self.plugin.logger.warning(
                f"[子插件] {ptype} 子插件 {root.name} 的入口为 {entry}，"
                f"页面内相对资源请改用 /gmpage/{ptype}/ 前缀引用"
            )
        return self._gmod_send_file(ld.page_file(ptype, entry))

    def _handle_gmod_static(self, path: str) -> None:
        """``/gmpage/<type>/<相对路径>``：按类型找启用子插件，或回退到同名目录的包。"""
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            self.send_error(404, "Not Found")
            return
        rest = path[len('/gmpage/'):] if path.startswith('/gmpage/') else ''
        parts = rest.split('/', 1)
        ptype = parts[0] if parts and parts[0] else ''
        rel = parts[1] if len(parts) > 1 else ''

        if ptype not in ('index', 'admin'):
            self.send_error(404, "Not Found")
            return

        try:
            if rel:
                target = ld.page_file(ptype, rel)
            else:
                got = ld.active_page(ptype)
                target = ld.page_file(ptype, got[1]) if got else None
            if target is None:
                # 回退：按包名定位（未启用时也能预览该包自己的资源）
                segs = [s for s in rel.split('/') if s not in ('', '.')]
                if segs and '..' not in segs:
                    cand = ld._resolve_in(ld.mods_dir / ptype / segs[0], '/'.join(segs[1:]) or 'index.html')
                    if cand is not None and cand.is_file():
                        target = cand
        except Exception as e:
            self.plugin.logger.warning(f"[子插件] 静态资源解析失败 {path}: {e}")
            target = None

        if target is None:
            self._gmod_asset_fallback(ptype, rel)
            return
        self._gmod_send_file(target)

    def _gmod_asset_served(self, path: str) -> bool:
        """把根路径上的资源请求，尝试映射到当前启用的 page 型子插件目录。

        子插件页面里写 ``<script src="assets/app.js">`` 时，浏览器会按当前路径
        （``/`` → ``/assets/app.js``，``/admin`` → ``/assets/app.js``）去取，
        与子插件目录对不上。这里按 ``index`` 优先、``admin`` 次之的顺序兜住。
        找到并发出返回 True。
        """
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            return False
        rel = path.lstrip('/')
        if not rel:
            return False
        for ptype in ('index', 'admin'):
            got = ld.active_page(ptype)
            if not got:
                continue
            root = got[0]
            try:
                cand = ld._resolve_in(root, rel)
            except Exception:
                cand = None
            if cand is not None and cand.is_file():
                self._gmod_send_file(cand)
                return True
        return False

    def _gmod_asset_fallback(self, ptype: str, rel: str) -> None:
        """子插件页面里 ``assets/app.js`` 这类相对引用会被浏览器解析成 ``/assets/app.js``。

        这里做一次兜底：若当前有启用的子插件，就把它目录下同名文件送出去。
        这保证了「子插件作者不需要改任何写法，附件资源全都能用」。
        """
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None or not rel:
            return
        segs = [s for s in str(rel).replace("\\", "/").split("/") if s not in ("", ".")]
        if not segs or ".." in segs:
            self.send_error(404, "Not Found")
            return

        roots = []
        got = ld.active_page(ptype)
        if got:
            roots.append(got[0])
        roots.append(ld.mods_dir / ptype / segs[0])

        for root in roots:
            try:
                cand = ld._resolve_in(root, "/".join(segs[1:]) if root.name == segs[0] else rel)
            except Exception:
                cand = None
            if cand is not None and cand.is_file():
                self._gmod_send_file(cand)
                return
        self.send_error(404, "Not Found")

    def _gmod_send_file(self, filepath, prefix: str = "") -> bool:
        """流式发送子插件内的文件，按扩展名给 MIME，支持 Range。"""
        if filepath is None:
            return False
        try:
            path = Path(filepath)
            size = path.stat().st_size
        except Exception:
            return False

        ctype = _gmod_mime_for(path)
        # HTML 里的相对引用以请求目录为基准，把规范前缀注入 <base> 只能改内容，
        # 这里保持原样送出，由 /gmpage/<type>/ 静态路由兜住。
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range", "") if hasattr(self, "headers") else ""
        if range_header.startswith("bytes="):
            try:
                spec = range_header[len("bytes="):].split(",")[0].strip()
                s_raw, _, e_raw = spec.partition("-")
                if s_raw:
                    start = int(s_raw)
                    end = int(e_raw) if e_raw else size - 1
                else:
                    start = max(0, size - int(e_raw or 0))
                if start > end or start >= size:
                    raise ValueError("bad range")
                end = min(end, size - 1)
                status = 206
            except Exception:
                start, end, status = 0, size - 1, 200

        length = max(0, end - start + 1)
        try:
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            if ctype.startswith('text/html'):
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            else:
                self.send_header('Cache-Control', 'no-cache')
            self.end_headers()

            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            return True
        except Exception as e:
            self.plugin.logger.error(f"[子插件] 发送文件失败 {filepath}: {e}")
            return False

    def _handle_api_gmods_list(self):
        ld = self._gmod_loader()
        if ld is None:
            return
        try:
            loaded = set(ld.loaded_uuids())
            mods = []
            for m in ld.list_mods():
                item = dict(m)
                item["loaded"] = str(m.get("uuid")) in loaded
                # 让前端知道这张卡片要不要显示「⚙ 设置」按钮。
                # 摘要在这里算而不是让前端每张卡片发一次请求 —— 十个子插件
                # 就是十个请求，而 schema 解析要读文件 + 过校验，没必要。
                try:
                    item.update(ld.mod_config_summary(str(m.get("uuid"))))
                except Exception:
                    item["has_settings"] = False
                    item["field_count"] = 0
                mods.append(item)
            self._send_json({
                "ok": True,
                "mods": mods,
                "libs": ld.load_liblist(),
                "mod_dir": str(ld.mods_dir),
                "libs_dir": str(ld.libs_dir),
            })
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取子插件失败: {e}"}, 500)

    def _handle_api_gmods_upload(self):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "必须使用 multipart/form-data"}, 400)
            return
        bm = re.search(r"boundary=([^;\s]+)", ctype)
        if not bm:
            self._send_json({"ok": False, "error": "无效的 multipart 格式"}, 400)
            return
        boundary = bm.group(1).encode("ascii")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > self._GMOD_UPLOAD_LIMIT:
            self._send_json({"ok": False, "error": "请求体为空或超过 64MB"}, 400)
            return
        body = self.rfile.read(length)

        fname = None
        fdata = None
        for part in body.split(b"--" + boundary):
            if not part or part in (b"--\r\n", b"--"):
                continue
            h_end = part.find(b"\r\n\r\n")
            if h_end == -1:
                continue
            header = part[:h_end].decode("utf-8", errors="ignore")
            content = part[h_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if "filename=" in header:
                fm = re.search(r'filename="([^"]*)"', header)
                fname = fm.group(1) if fm else "upload"
                fdata = content
        if not fname or fdata is None:
            self._send_json({"ok": False, "error": "未收到文件"}, 400)
            return

        raw = _upload_display_name(fname)
        ext = os.path.splitext(raw)[1].lower()
        if ext not in self._GMOD_EXTS:
            self._send_json({"ok": False, "error": "只支持 .gmmod / .gmlib 文件"}, 400)
            return

        # 同上：落盘名收敛成纯 ASCII，原文只用于界面展示。
        base, _ = _safe_upload_name(fname, prefix=_uuid.uuid4().hex[:12] + "_")
        tmp = self._gmod_upload_dir() / base
        try:
            # 原子写：子插件包可达数十 MB，写一半失败会留下残包
            # 进而让 install() 解压时报「不是有效 zip」。
            if not atomic_write_bytes(str(tmp), fdata):
                raise RuntimeError("写入磁盘失败（空间不足或被占用）")
            result = ld.install(str(tmp), force=True)
        except Exception as e:
            self.plugin.logger.error(f"[子插件] 导入异常: {e}")
            self._send_json({"ok": False, "error": f"导入异常: {e}"}, 500)
            return
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

        if not result.get("ok"):
            self._send_json(result, 400)
            return
        if result.get("kind") == "gmlib":
            msg = f"依赖库 {result.get('name')} 导入完成，新增 {len(result.get('copied') or [])} 个包"
        else:
            msg = f"子插件 {result.get('name')} v{result.get('version')} 导入成功"
            if result.get("missing_libs"):
                msg += f"（缺少依赖：{', '.join(result['missing_libs'])}）"
        result["message"] = msg
        self._send_json(result)

    def _handle_api_gmods_load(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            got = ld.import_mod(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": f"加载失败: {e}"}, 500)
            return
        if got is None:
            self._send_json({"ok": False, "error": "加载失败，详见服务端日志"}, 400)
            return
        module, life = got
        has_setup = callable((life or {}).get("setup"))
        self._send_json({
            "ok": True,
            "message": "入口模块已加载" + ("，发现 setup 入口" if has_setup else "，未发现 setup 入口"),
            "module": getattr(module, "__name__", ""),
            "has_setup": has_setup,
            "has_teardown": callable((life or {}).get("teardown")),
        })

    def _handle_api_gmods_unload(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        self._send_json({"ok": ld.unload_mod(uuid), "message": "已从内存卸载"})

    def _handle_api_gmods_uninstall(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            ok = ld.uninstall(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": f"卸载失败: {e}"}, 400)
            return
        if not ok:
            self._send_json({"ok": False, "error": "子插件不存在"}, 404)
            return
        self._send_json({"ok": True, "message": "已卸载并删除文件"})

    def _handle_api_gmods_enable(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            r = ld.enable(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        self._send_json(r)

    def _handle_api_gmods_disable(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            r = ld.disable(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        self._send_json(r)

    def _handle_api_gmods_files(self, query):
        ld = self._gmod_loader()
        if ld is None:
            return
        qs = urllib.parse.parse_qs(query or "")
        uuid = (qs.get("uuid") or [""])[0]
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        rec = ld.installed(uuid)
        if not rec:
            self._send_json({"ok": False, "error": "子插件不存在"}, 404)
            return
        files = ld.list_page_files(uuid)

        wanted = (qs.get("file") or [""])[0]
        if not wanted:
            # py / code 型没有 index.html，默认打开入口代码更顺手
            if str(rec.get("type") or "") in ("py", "code"):
                wanted = str(rec.get("entry") or "main.py")
            else:
                wanted = str(rec.get("index") or "index.html")
        content = ld.read_page_text(uuid, wanted)
        self._send_json({
            "ok": True, "uuid": uuid, "files": files,
            "file": wanted, "content": content,
            "editable": content is not None,
            "type": rec.get("type"),
            "entry": rec.get("entry"),
            "permissions": rec.get("permissions") or [],
            "running": uuid in set(ld.loaded_uuids()),
            "root": str(rec.get("path") or ""),
        })

    def _handle_api_gmods_file_save(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        d = data or {}
        uuid = str(d.get("uuid", "") or "")
        rel = str(d.get("file", "") or "")
        if not uuid or not rel:
            self._send_json({"ok": False, "error": "缺少 uuid 或 file"}, 400)
            return
        if not ld.write_page_text(uuid, rel, str(d.get("content", ""))):
            self._send_json({"ok": False, "error": "写入失败（文件类型不允许或路径非法）"}, 400)
            return
        self._send_json({"ok": True, "message": f"已保存 {rel}"})

    def _handle_api_gmods_pages(self):
        """返回当前生效的页面接管状态，供前端展示。"""
        ld = self._gmod_loader()
        if ld is None:
            return
        out = {}
        for ptype in ("index", "admin"):
            try:
                got = ld.active_page(ptype)
            except Exception:
                got = None
            out[ptype] = {"uuid": ld.enabled_uuid(ptype), "active": bool(got)}
        self._send_json({"ok": True, "pages": out})

    def _handle_api_gmods_settings(self, query):
        """读取某个子插件的设置 schema 与当前值。

        返回 ``fields``（已校验的字段声明）与 ``values``（当前值，已按
        schema 补过默认值）。前端照着这两样渲染表单。
        """
        ld = self._gmod_loader()
        if ld is None:
            return
        qs = urllib.parse.parse_qs(query or "")
        uuid = str((qs.get("uuid") or [""])[0] or "").strip()
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        rec = ld.installed(uuid)
        if not rec:
            self._send_json({"ok": False, "error": "子插件不存在"}, 404)
            return
        try:
            fields = ld.settings_schema(uuid, log_invalid=True)
        except Exception as e:
            self._send_json({"ok": False, "error": f"解析 settings.json 失败: {e}"}, 500)
            return
        try:
            stored = ld.read_mod_config(uuid)
        except Exception:
            stored = {}
        values = self._gmod_settings_values(fields, stored)
        self._send_json({
            "ok": True,
            "uuid": uuid,
            "name": rec.get("name") or uuid,
            "type": rec.get("type"),
            "has_settings": bool(fields),
            "fields": fields,
            "values": values,
        })

    def _gmod_settings_values(self, fields, stored):
        """把磁盘值整理成"前端直接可填"的形态。

        两件事：
        1. schema 声明了、config.json 里却没有的键 → 用 schema 的 default 补
           （不落盘，只补在响应里）。这样作者在 settings.json 写了默认值，
           管理员第一次打开设置页看到的就是那个值，不用自己猜。
        2. ``password`` 类型且已有值 → 回传面罩串，**不回传明文**。
           前端看到面罩就不回写，保留磁盘原值。

           注意这是防"肩窥"（管理员开着设置页被人看到），**不是**加密存储 ——
           明文仍在 config.json 里，因为子插件自己要能读回真值。这一点和面板
           现有的 access_token / app_secret 是同一套待遇，不做更高承诺。
        """
        values = {}
        for fld in fields or []:
            if not isinstance(fld, dict):
                continue
            key = fld.get("key")
            if not key:
                continue
            ftype = fld.get("type")
            if key in (stored or {}):
                val = stored[key]
                if ftype == "password":
                    values[key] = MASKED_SECRET if str(val) else ""
                else:
                    values[key] = val
            elif "default" in fld:
                values[key] = fld["default"]
            else:
                values[key] = False if ftype == "bool" else ""
        return values

    def _handle_api_gmods_config_save(self, data):
        """保存子插件的设置。

        仅管理员可用 —— 子插件配置里可能有 token、群号这类东西，
        临时账号一律拒绝（比照 .gmmod 的安装/卸载）。
        """
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        d = data or {}
        uuid = str(d.get("uuid", "") or "").strip()
        values = d.get("values")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        if not isinstance(values, dict):
            self._send_json({"ok": False, "error": "values 必须是对象"}, 400)
            return
        try:
            fields = ld.settings_schema(uuid, log_invalid=True)
        except Exception as e:
            self._send_json({"ok": False, "error": f"解析 settings.json 失败: {e}"}, 500)
            return
        if not fields:
            self._send_json({
                "ok": False,
                "error": "该子插件未提供设置项（包内缺少 settings.json）",
            }, 400)
            return

        # 面罩值原样回传时**跳过**：那说明管理员没动这个输入框，
        # 不跳过的话会把密码改成一串星号。
        by_key = {}
        for fld in fields:
            if isinstance(fld, dict) and fld.get("key"):
                by_key[fld["key"]] = fld
        payload = {}
        for key, val in values.items():
            name = str(key)
            fld = by_key.get(name)
            if fld is None:
                continue
            if fld.get("type") == "password" and str(val) == MASKED_SECRET:
                continue
            payload[name] = val

        try:
            ok, rejected, msg = ld.write_mod_config(uuid, payload)
        except Exception as e:
            self._send_json({"ok": False, "error": f"保存失败: {e}"}, 500)
            return
        if not ok:
            self._send_json({"ok": False, "error": msg or "保存失败", "rejected": rejected}, 400)
            return
        out = {"ok": True, "message": msg or "配置已保存", "rejected": rejected}
        if rejected:
            out["message"] += f"（{len(rejected)} 项未通过校验被忽略: {', '.join(rejected[:5])}）"
        self._send_json(out)

    def _fs_root(self) -> Path:
        return (self.plugin._find_server_root() or Path.cwd()).resolve()

    def _fs_basename(self, raw) -> str:
        base = os.path.basename(str(raw or "").replace("\\", "/")).strip()
        if not base or base in (".", ".."):
            return ""
        return base

    def _fs_abs(self, raw, need=True, want="any"):
        root = self._fs_root()
        rel = str(raw or "").replace("\\", "/").strip().lstrip("/")
        parts = [s for s in rel.split("/") if s not in ("", ".")]
        for s in parts:
            if s == "..":
                return None, "路径不合法（不允许 ..）"
        target = root
        for s in parts:
            target = target / s
        try:
            target = target.resolve()
        except Exception:
            target = target.absolute()
        rs = str(root)
        ts = str(target)
        if ts != rs and not ts.startswith(rs + os.sep):
            return None, "路径超出服务器根目录"
        if need:
            if not target.exists():
                return None, "路径不存在：%s" % (raw or "")
            if want == "dir" and not target.is_dir():
                return None, "目标不是目录：%s" % (raw or "")
            if want == "file" and not target.is_file():
                return None, "目标不是文件：%s" % (raw or "")
        return target, None

    def _fs_rel(self, target: Path) -> str:
        try:
            r = target.relative_to(self._fs_root())
            if str(r) == ".":
                return ""
            return r.as_posix()
        except Exception:
            return ""

    def _handle_api_files_list(self, query):
        qs = urllib.parse.parse_qs(query)
        rel = (qs.get("path") or [""])[0]
        target, err = self._fs_abs(rel, need=True, want="dir")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        try:
            items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            self._send_json({"ok": False, "error": "没有读取该目录的权限"}, 403)
            return
        entries = []
        for p in items:
            try:
                st = p.stat()
            except Exception:
                st = None
            entries.append({
                "name": p.name,
                "path": self._fs_rel(p),
                "is_dir": p.is_dir(),
                "size": st.st_size if st else 0,
                "mtime": int(st.st_mtime) if st else 0,
            })
        self._send_json({"ok": True, "cwd": self._fs_rel(target), "entries": entries})

    def _handle_api_files_read(self, query):
        qs = urllib.parse.parse_qs(query)
        rel = (qs.get("path") or [""])[0]
        if not rel:
            self._send_json({"ok": False, "error": "缺少文件路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=True, want="file")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        try:
            size = target.stat().st_size
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取状态失败: {e}"}, 500)
            return
        if size > self._FILE_READ_LIMIT:
            self._send_json({"ok": False, "error": "文件过大，请下载后编辑", "too_large": True, "size": size})
            return
        try:
            data = target.read_bytes()
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取失败: {e}"}, 500)
            return
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json({"ok": False, "error": "非 UTF-8 文本或二进制文件，无法在线编辑，请下载后处理", "binary": True})
            return
        self._send_json({"ok": True, "content": content, "name": target.name, "path": rel, "size": size})

    def _handle_api_files_download(self, query):
        qs = urllib.parse.parse_qs(query)
        rel = (qs.get("path") or [""])[0]
        if not rel:
            self._send_json({"ok": False, "error": "缺少文件路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=True, want="file")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        try:
            data = target.read_bytes()
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取失败: {e}"}, 500)
            return
        name = target.name
        ctype = "application/octet-stream"
        if name.endswith((".txt", ".log", ".properties", ".json", ".md", ".yml", ".yaml", ".cfg", ".ini")):
            ctype = "text/plain; charset=utf-8"
        try:
            cd = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", cd)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _handle_api_files_mkdir(self, data):
        rel = str((data or {}).get("path", "") or "")
        if not rel:
            self._send_json({"ok": False, "error": "缺少目录路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=False)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if target.exists():
            self._send_json({"ok": False, "error": "已存在同名项目"}, 400)
            return
        try:
            target.mkdir(parents=False)
        except Exception as e:
            self._send_json({"ok": False, "error": f"新建失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已创建 " + target.name})

    def _handle_api_files_delete(self, data):
        rel = str((data or {}).get("path", "") or "")
        if not rel:
            self._send_json({"ok": False, "error": "缺少路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(target) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能删除服务器根目录"}, 400)
            return
        try:
            # ★ 退避重试：Windows 上删除常因「文件刚被关闭/杀软正在扫描」
            #   而短暂失败（WinError 32）。重试几次就能过，不重试则用户
            #   会看到莫名其妙的「删除失败」。
            if target.is_dir():
                if not retry_on_lock(shutil.rmtree, str(target)):
                    raise RuntimeError("目录被占用（可能有程序正在使用其中的文件）")
            elif not retry_on_lock(os.unlink, str(target)):
                raise RuntimeError("文件被占用")
        except Exception as e:
            self._send_json({"ok": False, "error": f"删除失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已删除 " + target.name})

    def _handle_api_files_rename(self, data):
        rel = str((data or {}).get("path", "") or "")
        newname = self._fs_basename((data or {}).get("newname", "") or "")
        if not rel or not newname:
            self._send_json({"ok": False, "error": "缺少路径或新名称"}, 400)
            return
        target, err = self._fs_abs(rel, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(target) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能重命名服务器根目录"}, 400)
            return
        newp = target.parent / newname
        try:
            newp = newp.resolve()
        except Exception:
            newp = newp.absolute()
        rs = str(self._fs_root())
        if str(newp) != rs and not str(newp).startswith(rs + os.sep):
            self._send_json({"ok": False, "error": "目标超出根目录"}, 400)
            return
        if newp.exists():
            self._send_json({"ok": False, "error": "目标已存在：" + newname}, 400)
            return
        try:
            # ★ 用 os.replace 而不是 os.rename：目标不存在时两者等价，
            #   但 Windows 上 os.rename 对「目标已存在」会直接抛异常，
            #   os.replace 语义更稳。外面再包一层退避重试。
            if not retry_on_lock(os.replace, str(target), str(newp)):
                raise RuntimeError("文件被占用或权限不足")
        except Exception as e:
            self._send_json({"ok": False, "error": f"重命名失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已重命名"})

    def _handle_api_files_move(self, data):
        src = str((data or {}).get("src", "") or "")
        dst = str((data or {}).get("dst", "") or "")
        if not src or not dst:
            self._send_json({"ok": False, "error": "缺少源或目标路径"}, 400)
            return
        s, err = self._fs_abs(src, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(s) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能移动服务器根目录"}, 400)
            return
        d, err = self._fs_abs(dst, need=False)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if d.exists() and d.is_dir():
            d = d / s.name
        if str(d.resolve()) == str(s):
            self._send_json({"ok": False, "error": "源与目标相同"}, 400)
            return
        if d.exists():
            self._send_json({"ok": False, "error": "目标已存在：" + d.name}, 400)
            return
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            # ★ 目录走 atomic_replace_dir（避免 shutil.move 跨卷时
            #   「先删目标再拷贝」的空档期）；文件走退避重试。
            if s.is_dir():
                if not atomic_replace_dir(s, d):
                    raise RuntimeError("目录替换失败（可能被占用）")
            elif not retry_on_lock(shutil.move, str(s), str(d)):
                raise RuntimeError("移动失败（文件被占用）")
        except Exception as e:
            self._send_json({"ok": False, "error": f"移动失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已移动"})

    def _handle_api_files_copy(self, data):
        src = str((data or {}).get("src", "") or "")
        dst = str((data or {}).get("dst", "") or "")
        if not src or not dst:
            self._send_json({"ok": False, "error": "缺少源或目标路径"}, 400)
            return
        s, err = self._fs_abs(src, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        d, err = self._fs_abs(dst, need=False)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if d.exists() and d.is_dir():
            d = d / s.name
        if d.exists():
            self._send_json({"ok": False, "error": "目标已存在：" + d.name}, 400)
            return
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            fn = shutil.copytree if s.is_dir() else shutil.copy2
            if not retry_on_lock(fn, str(s), str(d)):
                raise RuntimeError("复制失败（源或目标被占用）")
        except Exception as e:
            self._send_json({"ok": False, "error": f"复制失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已复制"})

    def _handle_api_files_write(self, data):
        rel = str((data or {}).get("path", "") or "")
        content = (data or {}).get("content")
        if not rel:
            self._send_json({"ok": False, "error": "缺少文件路径"}, 400)
            return
        if content is None:
            self._send_json({"ok": False, "error": "缺少内容"}, 400)
            return
        target, err = self._fs_abs(rel, need=False, want="file")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(target) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能写入服务器根目录本身"}, 400)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # ★ 原子写 + 退避重试：直接 write_text 在写入过程中崩溃会留下
            # 截断文件（配置文件写坏 = 服务器起不来），且 Windows 上目标被
            # 杀软/编辑器占用时会直接抛 PermissionError。
            if not atomic_write_text(str(target), str(content)):
                raise RuntimeError("原子写入失败（文件可能被占用）")
        except Exception as e:
            self._send_json({"ok": False, "error": f"保存失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已保存 " + target.name})

    def _handle_api_files_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "必须使用 multipart/form-data"}, 400)
            return
        bm = re.search(r'boundary=([^;\s]+)', content_type)
        if not bm:
            self._send_json({"ok": False, "error": "无效的 multipart 格式"}, 400)
            return
        boundary = bm.group(1).encode("ascii")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > self._FILES_UPLOAD_LIMIT:
            self._send_json({"ok": False, "error": "请求体过大或非法"}, 400)
            return
        body = self.rfile.read(length)
        self._body_consumed = True
        parts = body.split(b"--" + boundary)
        dir_rel = ""
        files = []
        for part in parts:
            if not part or part in (b"--\r\n", b"--"):
                continue
            h_end = part.find(b"\r\n\r\n")
            if h_end == -1:
                continue
            header = part[:h_end].decode("utf-8", errors="ignore")
            content = part[h_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if 'name="dir"' in header:
                dir_rel = content.decode("utf-8", errors="ignore").strip()
            if 'name="file"' in header and 'filename=' in header:
                fm = re.search(r'filename="([^"]*)"', header)
                fname = fm.group(1) if fm else ""
                if fname:
                    files.append((self._fs_basename(fname), content))
        if not files:
            self._send_json({"ok": False, "error": "未收到文件"}, 400)
            return
        dir_target, derr = self._fs_abs(dir_rel, need=False)
        if derr:
            self._send_json({"ok": False, "error": derr}, 400)
            return
        saved = []
        for fname, fdata in files:
            if not fname:
                continue
            dest = dir_target / fname
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(str(dest), "wb") as f:
                    f.write(fdata)
                saved.append(fname)
            except Exception as e:
                self.plugin.logger.error(f"[文件管理] 保存上传 {fname} 失败: {e}")
        if not saved:
            self._send_json({"ok": False, "error": "所有文件均保存失败"}, 500)
            return
        self._send_json({"ok": True, "message": f"已上传 {len(saved)} 个文件"})

    def _handle_api_permission(self, data):
        player_name = data.get('player_name')
        permission = data.get('permission')
        action = data.get('action', 'set')
        value = data.get('value', True)
        if not player_name or not permission:
            self._send_json({"error": "缺少 player_name 或 permission"}, 400)
            return
        if action not in ('set', 'unset'):
            self._send_json({"error": "action 必须是 'set' 或 'unset'"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(
                self._execute_permission, player_name, permission, action, bool(value)
            )
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"权限操作失败: {e}")
            self._send_json({"error": "权限操作失败"}, 500)

    def _execute_permission(self, player_name: str, permission: str, action: str, value: bool):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            att = self.plugin._perm_attachments.get(player_name)
            if att is not None:
                try:
                    if att.permissible is not target:
                        att = None
                except Exception:
                    att = None
            if action == "unset":
                if att is not None:
                    att.unset_permission(permission)
                target.recalculate_permissions()
                return True, f"已撤销 {player_name} 的权限 {permission}"
            if att is None:
                att = target.add_attachment(self.plugin)
                self.plugin._perm_attachments[player_name] = att
            att.set_permission(permission, value)
            target.recalculate_permissions()
            return True, f"已设置 {player_name} 的权限 {permission} = {value}"
        except Exception as e:
            return False, f"权限操作失败: {e}"

    def _handle_api_objective(self, data):
        action = data.get('action')
        name = data.get('name')
        if action not in ('add', 'remove', 'display'):
            self._send_json({"error": "action 必须是 'add'/'remove'/'display'"}, 400)
            return
        if not name:
            self._send_json({"error": "缺少 name"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(
                self._execute_objective, action, name,
                data.get('display_name'), data.get('render_type'), data.get('slot'), data.get('order')
            )
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"记分板目标操作失败: {e}")
            self._send_json({"error": "记分板目标操作失败"}, 500)

    def _execute_objective(self, action: str, name: str, display_name=None, render_type=None, slot=None, order=None):
        scoreboard = self.plugin.server.scoreboard
        if not scoreboard:
            return False, "记分板不可用"
        try:
            if action == "add":
                existing = scoreboard.get_objective(name)
                if existing is not None:
                    return False, f"目标 {name} 已存在"
                scoreboard.add_objective(name, _dummy_criteria(), display_name or name, _to_render(render_type))
                return True, f"已创建目标 {name}"
            objective = scoreboard.get_objective(name)
            if objective is None:
                return False, f"目标 {name} 不存在"
            if action == "remove":
                objective.unregister()
                return True, f"已删除目标 {name}"
            if action == "display":
                objective.set_display(_to_slot(slot), _to_order(order))
                return True, f"已设置目标 {name} 的显示位置"
            return False, "未知操作"
        except Exception as e:
            return False, f"操作失败: {e}"

    def _handle_api_kill(self, data):
        player_name = data.get('player_name')
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_kill, player_name)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"执行击杀失败: {e}")
            self._send_json({"error": "执行击杀失败"}, 500)

    def _execute_kill(self, player_name: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if target.perform_command("kill"):
            return True, f"已击杀 {player_name}"
        return False, "击杀命令执行失败"

    def _handle_api_message(self, data):
        player_name = data.get('player_name')
        message = data.get('message')
        if not player_name or message is None:
            self._send_json({"error": "缺少 player_name 或 message"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_message, player_name, message)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"发送消息失败: {e}")
            self._send_json({"error": "发送消息失败"}, 500)

    def _execute_message(self, player_name: str, message: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            target.send_message(message)
            return True, f"已向 {player_name} 发送消息"
        except Exception as e:
            return False, f"发送失败: {e}"

    def _handle_api_give(self, data):
        player_name = data.get('player_name')
        item_id = data.get('item_id')
        amount = data.get('amount', 1)
        if not player_name or not item_id:
            self._send_json({"error": "缺少 player_name 或 item_id"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_give, player_name, item_id, amount)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"给予物品失败: {e}")
            self._send_json({"error": "给予物品失败"}, 500)

    def _execute_give(self, player_name: str, item_id: str, amount: int):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if self.plugin._grant_item(player_name, item_id, amount):
            return True, f"已给予 {player_name} {amount} 个 {item_id}"
        return False, "给予物品命令执行失败"

    def _handle_api_health(self, data):
        player_name = data.get('player_name')
        operation = data.get('operation')
        value = data.get('value')
        if not player_name or operation not in ('set', 'add', 'sub') or value is None:
            self._send_json({"error": "参数错误，需要 player_name, operation(set/add/sub), value"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_health, player_name, operation, value)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"调整生命值失败: {e}")
            self._send_json({"error": "调整生命值失败"}, 500)

    def _execute_health(self, player_name: str, operation: str, value):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if hasattr(target, 'game_mode'):
            try:
                gm = target.game_mode
                if gm is not None and str(gm).lower() == 'creative':
                    return False, "玩家处于创造模式，无法修改生命值"
            except Exception:
                pass
        try:
            val = int(round(float(value)))
            if operation == 'set':
                target.health = min(target.max_health, max(0, val))
            elif operation == 'add':
                target.health = min(target.max_health, target.health + val)
            else:
                target.health = max(0, target.health - val)
            return True, f"已调整 {player_name} 的生命值，当前: {target.health}"
        except Exception as e:
            return False, f"调整失败: {e}"

    def _handle_api_tag(self, data):
        player_name = data.get('player_name')
        tag = data.get('tag')
        action = data.get('action')
        if not player_name or not tag or action not in ('add', 'remove'):
            self._send_json({"error": "需要 player_name, tag, action(add/remove)"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_tag, player_name, tag, action)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"标签操作失败: {e}")
            self._send_json({"error": "标签操作失败"}, 500)

    def _execute_tag(self, player_name: str, tag: str, action: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if not hasattr(target, 'add_scoreboard_tag') or not hasattr(target, 'remove_scoreboard_tag'):
            return False, "服务器不支持标签操作"
        try:
            if action == 'add':
                success = target.add_scoreboard_tag(tag)
            else:
                success = target.remove_scoreboard_tag(tag)
            if success:
                return True, f"已{action}标签 {tag} 给 {player_name}"
            return False, "标签操作失败"
        except Exception as e:
            return False, f"标签操作异常: {e}"

    def _handle_api_score(self, data):
        player_name = data.get('player_name')
        objective_name = data.get('objective')
        score = data.get('score')
        operation = data.get('operation', 'set')
        if not player_name or not objective_name or score is None:
            self._send_json({"error": "需要 player_name, objective, score"}, 400)
            return
        if operation not in ('set', 'add'):
            self._send_json({"error": "operation 必须是 'set' 或 'add'"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_score, player_name, objective_name, score, operation)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"设置记分板分数失败: {e}")
            self._send_json({"error": "设置分数失败"}, 500)

    def _execute_score(self, player_name: str, objective_name: str, score, operation: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        scoreboard = self.plugin.server.scoreboard
        if not scoreboard:
            return False, "记分板不可用"
        objective = scoreboard.get_objective(objective_name)
        if not objective:
            return False, f"记分板目标 {objective_name} 不存在"
        try:
            score_val = int(score)
            score_obj = objective.get_score(target)
            if score_obj is None:
                return False, "无法获取分数对象"
            if operation == 'set':
                score_obj.value = score_val
            else:
                score_obj.value += score_val
            return True, f"已{operation} {player_name} 的 {objective_name} 为 {score_obj.value}"
        except Exception as e:
            return False, f"设置分数失败: {e}"

    def _handle_api_kick(self, data):
        player_name = data.get('player_name')
        reason = data.get('reason', "被管理员踢出")
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_kick, player_name, reason)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"踢出玩家失败: {e}")
            self._send_json({"error": "踢出玩家失败"}, 500)

    def _execute_kick(self, player_name: str, reason: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            target.kick(reason)
            return True, f"已踢出 {player_name}，原因: {reason}"
        except Exception as e:
            return False, f"踢出失败: {e}"

    def _handle_api_op(self, data):
        player_name = data.get('player_name')
        value = data.get('value')
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        if value is None:
            self._send_json({"error": "缺少 value (true/false)"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_op, player_name, value)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"修改 OP 状态失败: {e}")
            self._send_json({"error": "修改 OP 状态失败"}, 500)

    def _execute_op(self, player_name: str, value):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"

        give = bool(value)
        cmd = "op" if give else "deop"
        try:
            ok = self.plugin.server.dispatch_command(
                self.plugin.server.command_sender, f"{cmd} {player_name}"
            )
        except Exception as e:
            return False, f"执行 {cmd} 命令异常: {e}"
        if ok:
            return True, f"已{'给予' if give else '移除'} {player_name} 的 OP 权限"
        return False, f"执行 {cmd} 命令失败"

    def _handle_api_teleport(self, data):
        player_name = data.get('player_name')
        x = data.get('x')
        y = data.get('y')
        z = data.get('z')
        dimension = data.get('dimension', 'overworld')
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        if x is None or y is None or z is None:
            self._send_json({"error": "缺少坐标 x, y, z"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_teleport, player_name, float(x), float(y), float(z), dimension)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"传送玩家失败: {e}")
            self._send_json({"error": "传送玩家失败"}, 500)

    def _execute_teleport(self, player_name: str, x: float, y: float, z: float, dimension_name: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            level = self.plugin.server.level
            if not level:
                return False, "无法获取服务器主世界"
            dim = level.get_dimension(dimension_name)
            if dim is None:
                alias = dimension_name.lower()
                if alias in ('overworld', 'world', '主世界'):
                    dim = level.get_dimension('overworld')
                elif alias in ('nether', '地狱', '下界'):
                    dim = level.get_dimension('nether')
                elif alias in ('the_end', 'end', '末地'):
                    dim = level.get_dimension('the_end')
                if dim is None:
                    return False, f"维度 {dimension_name} 不存在"
            from endstone.level import Location
            loc = Location(dim, x, y, z)
            target.teleport(loc)
            return True, f"已将 {player_name} 传送到 {dimension_name} ({x}, {y}, {z})"
        except Exception as e:
            return False, f"传送失败: {e}"

    def _handle_api_map(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_error("必须使用 multipart/form-data")
            return

        boundary_match = re.search(r'boundary=([^;\s]+)', content_type)
        if not boundary_match:
            self._send_error("无效的 multipart 格式")
            return
        boundary = boundary_match.group(1).encode('ascii')

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_error("请求体过大或非法")
            return
        body = self.rfile.read(length)
        self._body_consumed = True

        parts = body.split(b'--' + boundary)
        player_name = None
        image_data = None

        for part in parts:
            if not part or part == b'--\r\n' or part == b'--':
                continue
            header_end = part.find(b'\r\n\r\n')
            if header_end == -1:
                continue
            header = part[:header_end].decode('utf-8', errors='ignore')
            content = part[header_end + 4:]
            if content.endswith(b'\r\n'):
                content = content[:-2]

            if 'name="player"' in header:
                player_name = content.decode('utf-8', errors='ignore').strip()
            elif 'name="image"' in header or 'filename=' in header:
                image_data = content

        if not player_name:
            self._send_error("缺少玩家名")
            return
        if not image_data:
            self._send_error("未收到图片数据")
            return

        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_map, player_name, image_data)
            if success:
                self._send_success(msg)
            else:
                self._send_error(msg)
        except Exception as e:
            self.plugin.logger.error(f"生成地图失败: {e}")
            self._send_error(f"生成地图失败: {e}")

    def _execute_map(self, player_name: str, image_data: bytes):
        """转发到插件上的权威实现（``GreenMoonPlugin._execute_map``）。

        ⚠ 历史 bug 备忘：地图画的实现原先**只**长在本类（HTTP 处理器）上。
        面板自己调得到，但 ``gm/mapart.py`` 调的是 ``plugin._execute_map``，
        插件对象上压根没这个方法 —— 子插件一调用就报
        ``'GreenMoonPlugin' object has no attribute '_execute_map'``。

        实现在插件侧才是对的（它要用 server / scheduler / 玩家背包），
        所以搬到那儿去，这里只做转发，免得两边各留一份慢慢长歪。
        """
        plugin = getattr(self, "plugin", None)
        impl = getattr(plugin, "_execute_map", None)
        if impl is None:
            return False, "插件未提供地图生成实现（_execute_map 缺失）"
        return impl(player_name, image_data)

    def _execute_allowlist_add(self, name: str):

        try:
            ok = self.plugin.server.dispatch_command(self.plugin.server.command_sender, f"allowlist add {name}")
            return True if ok else "allowlist 命令未成功执行（请确认已开启 allow-list）"
        except Exception as e:
            return f"执行 allowlist 命令异常: {e}"

    def _handle_api_whitelist_apply(self, data):
        name = str(data.get("name", "")).strip()
        if not name:
            self._send_json({"error": "请填写游戏名"}, 400)
            return
        if len(name) > 64:
            self._send_json({"error": "游戏名过长"}, 400)
            return

        if not re.fullmatch(r"[0-9A-Za-z_\-\. ]+", name):
            self._send_json({"error": "游戏名包含非法字符"}, 400)
            return
        xuid = str(data.get("xuid", "")).strip()
        key = name.lower()
        with self.plugin._whitelist_lock:
            existing = self.plugin._whitelist_apps.get(key)
            if existing and existing.get("status") == "approved":
                self._send_json({"success": False, "message": "该游戏名已在白名单中"})
                return
        app = {
            "name": name,
            "xuid": xuid,
            "note": str(data.get("note", "")).strip(),
            "status": "pending",
            "applied_at": datetime.now().isoformat(),
            "reviewed_at": "",
            "reviewer": "",
        }
        with self.plugin._whitelist_lock:
            self.plugin._whitelist_apps[key] = app
        self.plugin._save_whitelist()
        self._send_json({"success": True, "message": "申请已提交，请等待管理员审核"})

    def _handle_api_whitelist_status(self, query):
        qs = urllib.parse.parse_qs(query)
        name = str(qs.get("name", [""])[0]).strip()
        if not name:
            self._send_json({"error": "缺少 name"}, 400)
            return
        with self.plugin._whitelist_lock:
            app = self.plugin._whitelist_apps.get(name.lower())
        # ★ 这个接口是公开的（_public_path 白名单里有），用于申请页自查进度。
        # 但原实现把整条申请记录原样返回，里面含 xuid、申请备注、审核人 ——
        # 而查询条件只有一个玩家名。于是任何知道别人 ID 的人都能拿它当
        # 「玩家名 → xuid」的查询器，配合白名单审核接口伪造申请。
        # 只回传申请页自己需要的最小字段集。
        if not isinstance(app, dict):
            self._send_json({"application": None})
            return
        self._send_json({"application": {
            "name": str(app.get("name", "") or ""),
            "status": str(app.get("status", "") or ""),
            "applied_at": str(app.get("applied_at", "") or ""),
            "reviewed_at": str(app.get("reviewed_at", "") or ""),
            "reason": str(app.get("reason", "") or ""),
        }})

    def _handle_api_whitelist_applications(self):
        try:
            apps = self.plugin._list_whitelist_apps()
            self._send_json({"applications": apps})
        except Exception as e:
            self.plugin.logger.error(f"获取白名单申请失败: {e}")
            self._send_json({"applications": []})

    def _handle_api_system_stats(self):

        try:
            stats = self.plugin.get_system_stats()
            self._send_json({"ok": True, "stats": stats})
        except Exception as e:
            self.plugin.logger.error(f"获取系统状态失败: {e}")
            self._send_json({"ok": False, "stats": {
                "cpu": 0, "mem": 0, "disk": 0,
                "mem_total_mb": 0, "mem_used_mb": 0,
                "disk_total_gb": 0, "disk_used_gb": 0, "uptime": 0}}, 500)

    def _handle_api_whitelist_review(self, data):
        name = str(data.get("name", "")).strip()
        action = str(data.get("action", "")).strip()
        note = str(data.get("note", "")).strip()
        if not name or action not in ("approve", "reject"):
            self._send_json({"error": "参数错误：需要 name 和 action(approve/reject)"}, 400)
            return
        key = name.lower()
        with self.plugin._whitelist_lock:
            app = self.plugin._whitelist_apps.get(key)
        if not app:
            self._send_json({"error": "未找到该申请"}, 404)
            return

        if action == "approve":
            stored_name = app.get("name") or name
            try:
                result = self.plugin._run_in_server_thread(self._execute_allowlist_add, stored_name, timeout=3.0)
            except Exception as e:
                self.plugin.logger.error(f"白名单审核异常: {e}")
                self._send_json({"error": "审核失败"}, 500)
                return
            if result is True:
                with self.plugin._whitelist_lock:
                    app["status"] = "approved"
                    app["reviewed_at"] = datetime.now().isoformat()
                    app["reviewer"] = "管理员"
                    app["note"] = note or app.get("note", "")
                    self.plugin._whitelist_apps[key] = app
                self.plugin._save_whitelist()
                self.plugin._log_admin_action(f"白名单通过: {name}")
                self.plugin._cross_push_whitelist()
                # 可信度联动：审核通过的玩家在档案里打上白名单标记，
                # 并把他申请时提交的设备指纹标为「信任设备」。
                # 为什么要这样：白名单申请是人工审核过的，这份信任应当折算成
                # 可信度加权，同时避免他刚进服就被设备突发规则误伤。
                try:
                    mgr = getattr(self.plugin, "trust", None)
                    if mgr is not None:
                        mgr.note_whitelist(
                            stored_name,
                            device_id=str(data.get("device_id") or "").strip(),
                        )
                except Exception as _e:
                    self.plugin.logger.warning(f"[可信度] 白名单联动失败: {_e}")
                self._send_json({"success": True, "message": f"已通过 {name} 的白名单申请"})
            else:
                msg = result if isinstance(result, str) else "allowlist 命令执行失败"
                self._send_json({"error": f"添加白名单失败: {msg}"})
            return

        with self.plugin._whitelist_lock:
            app["status"] = "rejected"
            app["reviewed_at"] = datetime.now().isoformat()
            app["reviewer"] = "管理员"
            app["note"] = note or app.get("note", "")
            self.plugin._whitelist_apps[key] = app
        self.plugin._save_whitelist()
        self.plugin._log_admin_action(f"白名单拒绝: {name}")
        self._send_json({"success": True, "message": f"已拒绝 {name} 的申请"})

    def _handle_api_properties_get(self):
        try:
            root, content = self.plugin._read_properties_raw()
            keys = {}
            for line in content.splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, _, v = s.partition("=")
                    keys[k.strip()] = v.strip()
            path_str = str(root / "server.properties") if root else ""
            self._send_json({"found": root is not None, "path": path_str, "raw": content, "keys": keys})
        except Exception as e:
            self.plugin.logger.error(f"读取 server.properties 失败: {e}")
            self._send_json({"found": False, "path": "", "raw": "", "keys": {}})

    def _handle_api_properties_save(self, data):
        content = data.get("content")
        if content is None:
            self._send_json({"error": "缺少 content"}, 400)
            return
        ok, msg = self.plugin._write_properties_raw(str(content))
        if ok:
            self._send_json({"success": True, "message": msg})
        else:
            self._send_json({"error": msg})

    def _handle_api_properties_set(self, data):
        key = str(data.get("key", "")).strip()
        if not key:
            self._send_json({"error": "缺少 key"}, 400)
            return
        value = data.get("value")
        value = "" if value is None else str(value)

        if "\n" in key or "\r" in key or "=" in key:
            self._send_json({"error": "key 包含非法字符"}, 400)
            return
        if "\n" in value or "\r" in value:
            self._send_json({"error": "value 包含非法字符"}, 400)
            return
        _root, content = self.plugin._read_properties_raw()
        content = self.plugin._set_property_value(content, key, value)
        ok, msg = self.plugin._write_properties_raw(content)
        if ok:
            self._send_json({"success": True, "message": f"已设置 {key} = {value}"})
        else:
            self._send_json({"error": msg})

    def _handle_api_worlds(self):
        try:
            worlds, current, root_path = self.plugin._list_worlds()
            self._send_json({"worlds": worlds, "current": current, "root": root_path})
        except Exception as e:
            self.plugin.logger.error(f"获取世界列表失败: {e}")
            self._send_json({"worlds": [], "current": "", "root": ""})

    def _handle_api_world_switch(self, data):
        world = str(data.get("world", "")).strip()
        if not world:
            self._send_json({"error": "缺少 world"}, 400)
            return
        worlds, current, _ = self.plugin._list_worlds()
        names = [w["name"] for w in worlds]
        if world not in names:
            self._send_json({"error": f"未找到名为 {world} 的世界存档，请刷新后重试"}, 400)
            return
        _, content = self.plugin._read_properties_raw()
        content = self.plugin._set_property_value(content, "level-name", world)
        ok, msg = self.plugin._write_properties_raw(content)
        if ok:
            self.plugin._log_admin_action(f"切换世界存档: {current} -> {world}（需重启生效）")
            self._send_json({"success": True, "message": f"已切换到存档 {world}，请重启服务器后生效", "restart_required": True})
        else:
            self._send_json({"error": msg})

    def _handle_api_console(self, query):
        qs = urllib.parse.parse_qs(query)
        try:
            after = int(qs.get("after", ["0"])[0])
        except Exception:
            after = 0
        try:
            logs, last_id = self.plugin._get_console_logs(after)
            self._send_json({"logs": logs, "last_id": last_id})
        except Exception as e:
            self.plugin.logger.error(f"获取控制台日志失败: {e}")
            self._send_json({"logs": [], "last_id": 0})

    def _execute_stop(self):
        try:
            self.plugin.server.dispatch_command(self.plugin.server.command_sender, "stop")
        except Exception as e:
            self.plugin.logger.error(f"执行 stop 命令失败: {e}")

    def _handle_api_restart(self, data):

        self._send_json({"success": True, "message": "已请求停止服务器；若未配置自动拉起，请手动重启服务器脚本"})
        self.plugin._log_admin_action("收到重启请求，即将停止服务器")

        def _later_stop():
            time.sleep(1.0)
            self.plugin._run_async_on_server_thread(self._execute_stop)

        threading.Thread(target=_later_stop, daemon=True).start()

    # ------------------------------------------------------------------
    # QQ 官方机器人 Web API
    #
    # 下面这几个 handler 都接受一个可选的 ``id`` 参数（GET 走 query string、
    # POST 走 body），用来指明「操作的是哪张机器人卡片」。多机器人共存时
    # 这张卡各自的群绑定码 / 群列表 / 配置必须严格隔离 —— 不传 id 时按
    # 主卡处理，仅为兼容旧前端。
    # ------------------------------------------------------------------
    @staticmethod
    def _qqbot_card_id_from_query(query: str) -> str:
        """从 query string 里取 id，取不到返回空串。"""
        try:
            from urllib.parse import parse_qs
            got = parse_qs(query or "", keep_blank_values=True).get("id") or []
            return str(got[0]).strip() if got else ""
        except Exception:
            return ""

    def _handle_api_qqbot_config(self, query=''):
        try:
            pl = self.plugin
            cid = self._qqbot_card_id_from_query(query)
            data = pl._qqbot_public_config(cid)
            data["status"] = pl._qqbot_status(cid)
            self._send_json({"ok": True, **data})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/config 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_status(self, query=''):
        try:
            cid = self._qqbot_card_id_from_query(query)
            self._send_json({"ok": True, **self.plugin._qqbot_status(cid)})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/status 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_group_new(self, query=''):
        try:
            # 必须传 cid：这里的 AppID/AppSecret 前置校验针对的是「当前这张
            # 卡」。若沿用主卡状态，A 卡没配 AppID 会把 B 卡的生成按钮误拦。
            cid = self._qqbot_card_id_from_query(query)
            st = self.plugin._qqbot_status(cid) or {}
            missing = [m for m in (st.get("missing") or []) if m in ("AppID", "AppSecret")]
            has_group = bool(str(st.get("group_openid") or "") or st.get("groups"))
            if missing and not has_group:
                self._send_json({"ok": False,
                                 "error": "请先完成机器人信息（AppID/AppSecret）配置并绑定主群，再来申请群绑定码"}, 400)
                return
            code = self.plugin._qqbot_new_group_code(cid)
            self._send_json({"ok": True, "code": code})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/group/new 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_group_rename(self, data):
        try:
            body = data or {}
            openid = str(body.get("openid", "") or "").strip()
            name = str(body.get("name", "") or "").strip()
            if not openid:
                self._send_json({"ok": False, "error": "缺少群 openid"}, 400)
                return
            cid = str(body.get("id", "") or "").strip()
            # 按 id 找对应网关，绝不回落到 plugin.qq_bot（那是主卡网关），
            # 否则改 B 卡群名会写到 A 卡的配置里。
            bot = None
            if cid:
                bot = self.plugin._hub_gateway_for(cid)
                if bot is None:
                    self._send_json({"ok": False, "error": "该机器人卡片未运行或不存在"}, 400)
                    return
            else:
                bot = getattr(self.plugin, "qq_bot", None)
            if bot is None:
                self._send_json({"ok": False, "error": "机器人未初始化"}, 500)
                return
            bot._rename_group(openid, name or openid)
            self._send_json({"ok": True, "openid": openid, "name": name or openid})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/group/rename 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ================================================== QQ 官方扫码登录
    #
    # 与 q.qq.com 的 lite 绑定接口对接：本地生成 bind_key 建任务 →
    # 前端把 connect.html 渲染成二维码 → 用户手机 QQ 扫码授权 →
    # 轮询拿 bot_encrypt_secret（AES-256-GCM，密钥是 bind_key）解密出 AppSecret。
    #
    # ⚠ 安全红线（改这段代码务必守住）：
    #   1. ``bind_key`` 是解密凭据，**只存内存**（_qr_tasks），绝不落盘、
    #      绝不下发给前端。落盘/下发都等于把 AppSecret 明文送出去。
    #   2. AppSecret **不通过任何接口回传**。前端只拿到 ``secret_len``
    #      与掩码，真值直接写进卡片配置。
    #   3. 建任务是会真的打到腾讯服务器上的外部调用，必须限流。

    def _handle_api_qqbot_qr_create(self):
        """GET /api/qqbot/qr/create —— 创建一个扫码绑定任务。

        成功返回 ``{ok, task_id, qrcode, interval}``。
        **不含 bind_key** —— 它留在服务端内存里，poll 时自己用。
        """
        try:
            ip = self._client_ip()
            now = time.time()
            with _QR_LOCK:
                # 逐出过期任务（>600s）+ 控制字典上限，防内存无限增长
                for key, item in list(_QR_TASKS.items()):
                    if now - float(item.get("created_at", 0)) > _QR_TASK_TTL:
                        _QR_TASKS.pop(key, None)
                while len(_QR_TASKS) > _QR_TASK_MAX:
                    oldest = min(_QR_TASKS, key=lambda k: float(_QR_TASKS[k].get("created_at", 0)))
                    _QR_TASKS.pop(oldest, None)
                # 同 IP 30s 内只允许建一次
                last = _QR_IP_LAST.get(ip, 0)
                if now - last < _QR_IP_COOLDOWN:
                    self._send_json({
                        "ok": False,
                        "error": f"请求过于频繁，请 {int(_QR_IP_COOLDOWN - (now - last)) + 1} 秒后重试",
                    }, 429)
                    return
                _QR_IP_LAST[ip] = now
                # IP 表自身也要清理，否则同一台机器长期运行会慢慢涨
                if len(_QR_IP_LAST) > 4096:
                    for key in [k for k, v in _QR_IP_LAST.items() if now - v > 3600]:
                        _QR_IP_LAST.pop(key, None)

            try:
                task = _qqofficial_bind.create_bind_task_sync()
            except Exception as e:
                self.plugin.logger.warning(f"[QQ扫码] 创建绑定任务失败: {e}")
                self._send_json({"ok": False, "error": f"向 QQ 申请二维码失败：{e}"}, 502)
                return

            task_id = str(task.get("task_id") or "")
            with _QR_LOCK:
                _QR_TASKS[task_id] = {
                    "bind_key": task.get("bind_key", ""),
                    "created_at": time.time(),
                    "ip": ip,
                }
            self.plugin.logger.info("[QQ扫码] 已创建扫码绑定任务")
            self._send_json({
                "ok": True,
                "task_id": task_id,
                "qrcode": task.get("qrcode", ""),
                "interval": task.get("interval", "2"),
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/qr/create 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_qr_poll(self, query=""):
        """GET /api/qqbot/qr/poll?task_id=... —— 轮询一次绑定结果。

        ``status``：``pending`` / ``created`` / ``expired`` / ``error``。

        ``created`` 时把 AppSecret 写进对应卡片并重启网关，
        但返回体里**只有** ``appid`` 和 ``secret_len``（等长掩码的信息量），
        绝不回传明文。
        """
        try:
            qs = urllib.parse.parse_qs(query or "")
            task_id = (qs.get("task_id") or [""])[0]
            card_id = (qs.get("card_id") or [""])[0]
            if not task_id:
                self._send_json({"ok": False, "error": "缺少 task_id"}, 400)
                return

            with _QR_LOCK:
                item = _QR_TASKS.get(task_id)
                if item is None:
                    self._send_json({"ok": False, "status": "expired",
                                     "error": "任务不存在或已过期，请重新获取二维码"})
                    return
                # 只允许创建者本人轮询，防 task_id 被猜到后他人截胡凭据
                if item.get("ip") and item.get("ip") != self._client_ip():
                    self._send_json({"ok": False, "error": "无权查询该任务"}, 403)
                    return
                bind_key = item.get("bind_key", "")

            try:
                result = _qqofficial_bind.poll_bind_task_sync(task_id, bind_key)
            except Exception as e:
                self.plugin.logger.warning(f"[QQ扫码] 轮询失败: {e}")
                self._send_json({"ok": False, "status": "error", "error": str(e)})
                return

            status = str(result.get("status") or "pending")
            if status != "created":
                if status in ("expired", "error"):
                    with _QR_LOCK:
                        _QR_TASKS.pop(task_id, None)
                payload = {"ok": True, "status": status}
                if result.get("message"):
                    payload["error"] = result["message"]
                self._send_json(payload)
                return

            appid = str(result.get("appid") or "")
            secret = str(result.get("secret") or "")
            if not appid or not secret:
                self._send_json({"ok": False, "status": "error", "error": "扫码成功但凭据不完整"})
                return

            # 写回卡片配置。card_id 为空时挑第一张 qqofficial 卡。
            saved, message = self.plugin._qqbot_apply_qr_credentials(card_id, appid, secret)
            with _QR_LOCK:
                _QR_TASKS.pop(task_id, None)

            # 等长掩码 + 长度，既让用户确认「确实拿到了」，又不泄漏任何字符
            self._send_json({
                "ok": bool(saved),
                "status": "created",
                "appid": appid,
                "secret_len": len(secret),
                "secret_mask": _qqofficial_bind.mask_secret(secret),
                "message": message,
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/qr/poll 异常: {e}")
            self._send_json({"ok": False, "status": "error", "error": str(e)}, 500)

    # ================================================== 正则触发引擎

    def _regex_engine(self):
        return getattr(self.plugin, "regex_engine", None)

    def _handle_api_regex_rules(self):
        """GET /api/qqbot/regex/rules —— 规则列表 + 可选值（供前端渲染下拉）。"""
        try:
            engine = self._regex_engine()
            if engine is None:
                self._send_json({"ok": False, "error": "正则引擎未加载", "rules": []})
                return
            from .regex_engine import EVENT_TYPES, ACTION_TYPES, _MAX_PATTERN_LEN
            self._send_json({
                "ok": True,
                "rules": engine.snapshot_rules(),
                "eventTypes": list(EVENT_TYPES),
                "actionTypes": list(ACTION_TYPES),
                "maxPatternLen": _MAX_PATTERN_LEN,
                "config": engine.conf(),
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/regex/rules 异常: {e}")
            self._send_json({"ok": False, "error": str(e), "rules": []}, 500)

    def _handle_api_regex_save(self, data):
        """POST /api/qqbot/regex/save —— 整表保存规则库。

        同时可以带上 ``config``（enable / only_on_main / admin_debug /
        command_timeout），存进 tools 配置。
        """
        try:
            engine = self._regex_engine()
            if engine is None:
                self._send_json({"ok": False, "error": "正则引擎未加载"}, 400)
                return
            payload = data or {}
            rules = payload.get("rules")
            if not isinstance(rules, list):
                self._send_json({"ok": False, "error": "rules 必须是数组"}, 400)
                return
            bad = _validate_regex_rules(rules)
            if bad:
                self._send_json({"ok": False, "error": bad}, 400)
                return
            count = engine.save_rules(rules)

            cfg = payload.get("config")
            if isinstance(cfg, dict):
                ok, msg = self.plugin._save_regex_config(cfg)
                if not ok:
                    self._send_json({"ok": False, "error": msg}, 400)
                    return
            self._send_json({"ok": True, "count": count, "message": f"已保存 {count} 条规则"})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/regex/save 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_regex_toggle(self, data):
        try:
            engine = self._regex_engine()
            if engine is None:
                self._send_json({"ok": False, "error": "正则引擎未加载"}, 400)
                return
            rule_id = str((data or {}).get("id") or "")
            if not rule_id:
                self._send_json({"ok": False, "error": "缺少规则 id"}, 400)
                return
            enabled = (data or {}).get("enabled")
            if not engine.toggle_rule(rule_id, enabled):
                self._send_json({"ok": False, "error": "规则不存在"}, 404)
                return
            self._send_json({"ok": True})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/regex/toggle 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_regex_delete(self, data):
        try:
            engine = self._regex_engine()
            if engine is None:
                self._send_json({"ok": False, "error": "正则引擎未加载"}, 400)
                return
            rule_id = str((data or {}).get("id") or "")
            if not rule_id:
                self._send_json({"ok": False, "error": "缺少规则 id"}, 400)
                return
            if not engine.delete_rule(rule_id):
                self._send_json({"ok": False, "error": "规则不存在"}, 404)
                return
            self._send_json({"ok": True})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/regex/delete 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_regex_test(self, data):
        """POST /api/qqbot/regex/test —— 网页「测试匹配」。

        必填 ``pattern``；``text`` 为空时返回正则是否合法（不判命中）。
        """
        try:
            engine = self._regex_engine()
            if engine is None:
                self._send_json({"ok": False, "error": "正则引擎未加载"}, 400)
                return
            payload = data or {}
            result = engine.test_match(
                str(payload.get("pattern") or ""),
                str(payload.get("flags") or ""),
                str(payload.get("text") or ""),
            )
            self._send_json(result)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/regex/test 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_list(self):
        try:
            self._send_json(self.plugin._bots_list())
        except Exception as e:
            self.plugin.logger.error(f"GET /api/bots/list 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_create(self, data):
        try:
            self._send_json(self.plugin._bots_create(data))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/create 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_update(self, data):
        try:
            adapter_id = str((data or {}).get("id", "") or "").strip()
            patch = (data or {}).get("patch") or {}
            if not isinstance(patch, dict):
                patch = {}
            self._send_json(self.plugin._bots_update(adapter_id, patch))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/update 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_toggle(self, data):
        try:
            adapter_id = str((data or {}).get("id", "") or "").strip()
            enabled = bool((data or {}).get("enabled", False))
            self._send_json(self.plugin._bots_toggle(adapter_id, enabled))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/toggle 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_delete(self, data):
        try:
            adapter_id = str((data or {}).get("id", "") or "").strip()
            self._send_json(self.plugin._bots_delete(adapter_id))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/delete 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_config_save(self, data):
        try:
            if not isinstance(data, dict):
                return self._send_json({"ok": False, "error": "数据格式不正确"}, 400)
            # id 既可能来自 body（前端统一调用约定），也可能来自 query
            cid = str(data.get("id", "") or "").strip() or self._qqbot_card_id_from_query(
                getattr(self, "_current_query", "") or "")
            ok, msg = self.plugin._qqbot_save_config(data, cid)
            self._send_json({"ok": bool(ok), "message": msg})
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/config/save 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _send_error(self, msg: str):
        # 同样必须带 Content-Length，理由见 _send_json
        body = f"错误: {msg}".encode()
        self.send_response(400)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_success(self, msg: str):
        body = msg.encode()
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

# ---- 子系统反向导入（在模块末尾，避免循环导入） ----
from .cross import CrossManager
from .qqbot import QQBotAPI, QQBotGateway, _TokenBucket, _GatewayLimiter
