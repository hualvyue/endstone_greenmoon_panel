#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 morphicons（MIT）打成面板能直接内联的 admin.morph.js。

它的形态是「自带 <script> 包裹的第三方库文件」—— web_assets 会把它拼到
admin.js **之前**，所以这里定义好的全局，业务脚本里可以直接用。
"""
import gzip
import os

MORPH_MIN = os.environ.get("GM_MORPH_MIN", "/tmp/morph.min.js")
DATA = os.environ.get("GM_ICON_DATA", "/tmp/out_icon_data.js")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin.morph.js")

ADAPTER = r"""
/* ---------------------------------------------------------------------------
 * GM 图标形变适配层
 * ---------------------------------------------------------------------------
 * 面板的图标默认是 CSS mask（``-webkit-mask-image: url("data:image/svg+xml,...")``）：
 * 一个 <i> 就能显示，零 DOM 开销。代价是它只是一串**字符串**，没法逐帧插值，
 * 所以形状变化只能是硬切。
 *
 * 只有真的要动的那几个图标才升级成内联 <svg><path>：把 <i> 换成等尺寸的
 * <svg class="gm-ico-live">，剩下的交给 morphicons 逐帧改 d。
 * 静态图标继续走 mask —— 老机器上几百个 <i> 也不会掉帧。
 *
 * 对外只有两个方法，都很便宜：
 *   gmMorph.to(el, 'pause')   形变过去（有弹簧动画）
 *   gmMorph.set(el, 'pause')  直接换（无动画，用于 DOM 重建后的初始状态）
 */
(function () {
    'use strict';
    var M = window.GMorph;
    if (!M || !M.createMorph) return;
    var NS = 'http://www.w3.org/2000/svg';
    var D = window.GM_MORPH_D || {};
    var live = new WeakMap();            // path 元素 -> morph 实例

    function pathOf(el) {
        if (!el) return null;
        var tag = (el.tagName || '').toLowerCase();
        if (tag === 'svg') return el.firstChild || null;
        if (tag !== 'i') return null;
        // <i class="gm-ico" data-ic="x"> -> <svg class="gm-ico-live" data-ic="x">
        var ic = el.getAttribute('data-ic') || '';
        var d = D[ic];
        if (!d) return null;             // 没有路径数据的图标不升级，保持 mask
        var svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('class', 'gm-ico-live');
        svg.setAttribute('data-ic', ic);
        svg.setAttribute('viewBox', '0 0 24 24');
        var p = document.createElementNS(NS, 'path');
        p.setAttribute('d', d);
        svg.appendChild(p);
        if (el.parentNode) el.parentNode.replaceChild(svg, el);
        return p;
    }

    function morphOf(p) {
        var m = live.get(p);
        if (!m) {
            // 首次：以「当前显示的样子」为起点，绝不凭空闪一下
            m = M.createMorph(p, p.getAttribute('d'), { reducedMotion: 'user' });
            live.set(p, m);
        }
        return m;
    }

    function apply(el, ic, animate) {
        if (!el || !ic) return el;
        var d = D[ic];
        if (!d) return el;               // 目标没数据：保持原样，不做半吊子动画
        // 已经是这个形状就连 DOM 都不碰 —— 页面初始化会跑一遍开合/切换，
        // 没有这道闸的话，几百个静态图标会被白升级成内联 svg。
        if (el.getAttribute && el.getAttribute('data-ic') === ic) return el;
        var p = pathOf(el);
        if (!p) return el;
        if (p.getAttribute('d') === d) return p.parentNode || el;
        if (animate) {
            morphOf(p).morphTo(d);
        } else {
            var m = live.get(p);
            if (m) m.set(d); else p.setAttribute('d', d);
        }
        if (p.parentNode && p.parentNode.setAttribute) p.parentNode.setAttribute('data-ic', ic);
        return p.parentNode || el;
    }

    window.gmMorph = {
        to: function (el, ic) { return apply(el, ic, true); },
        set: function (el, ic) { return apply(el, ic, false); },
        has: function (ic) { return !!D[ic]; }
    };
})();
"""


def main():
    lib = open(MORPH_MIN, encoding="utf-8").read().strip()
    data = open(DATA, encoding="utf-8").read().strip()
    body = "\n".join([
        "<script>",
        "/* morphicons — MIT © Guillermo (https://github.com/guillermolg00/morphicons)",
        "   esbuild --bundle --format=iife 打成单文件，离线可用、不连 CDN。 */",
        lib,
        data,
        ADAPTER.strip(),
        "</script>",
    ]) + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(body)
    raw = len(body.encode())
    gz = len(gzip.compress(body.encode(), 9))
    print("admin.morph.js  %d 字节（gzip %d 字节）" % (raw, gz))


if __name__ == "__main__":
    main()
