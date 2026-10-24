#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一个可以直接双击打开的预览页，用来验收图标与动效。"""
import io
import os
import re

STATIC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("GM_PREVIEW_OUT", "/workspace/GreenMoon_图标与动效预览.html")

html = io.open(os.path.join(STATIC, "admin.html"), encoding="utf-8").read()
css = io.open(os.path.join(STATIC, "admin.css"), encoding="utf-8").read()
morph = io.open(os.path.join(STATIC, "admin.morph.js"), encoding="utf-8").read()

# 图标样式块
m = re.search(r'<style id="gm-icon-style">(.*?)</style>', html, re.S)
icons = m.group(1)
# 动效章节（从 admin.css 里单独拎出来，预览页能直观看到这段）
i = css.find("/* ---------- 15. 动效")
motion = css[i:css.rfind("</style>")] if i >= 0 else ""
# 主体样式（去掉动效章节，其余照抄，保证和面板视觉一致）
i2 = css.find("<style>")
i3 = css.rfind("</style>")
base = css[i2 + 7:i] if i >= 0 else css[i2 + 7:i3]

names = sorted(set(re.findall(r"gm-ico\[data-ic=([a-z0-9-]+)\]", html)))

DEMO = """
/* 预览页自己的排版 */
.gm-demo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
.gm-demo-cell {
    display: flex; flex-direction: column; align-items: center; gap: 6px;
    padding: 10px 4px; border: 1px solid var(--gm-border); border-radius: var(--gm-r-md);
    background: var(--gm-glass); font-size: 11px; color: var(--gm-text-dim);
    transition: transform .2s cubic-bezier(.2, .8, .2, 1);
}
.gm-demo-cell:hover { transform: translate3d(0, -3px, 0); color: var(--gm-accent-fg); }
.gm-demo-cell > .gm-ico, .gm-demo-cell > .gm-ico-live { font-size: 22px; }
#rise-box > .card { animation: gmRise .32s cubic-bezier(.2, .8, .2, 1); }
"""

JS = """
/* 预览页的演示逻辑：全部走真实 API，与面板里跑的完全一样 */
function ico(sel) {
    var e = document.querySelector(sel);
    return e ? e.querySelector('.gm-ico-live,.gm-ico') : null;
}
var open = false;
document.getElementById('btn-menu').onclick = function () {
    open = !open;
    gmMorph.to(ico('#btn-menu'), open ? 'x' : 'menu');
};
document.getElementById('btn-save').onclick = function () {
    var e = gmMorph.to(ico('#btn-save'), 'check');
    if (!e) return;
    clearTimeout(e._t);
    e._t = setTimeout(function () { gmMorph.to(e, 'disk'); }, 1100);
};
var playing = false;
document.getElementById('btn-play').onclick = function () {
    playing = !playing;
    gmMorph.to(ico('#btn-play'), playing ? 'pause' : 'tri-r');
};
var rec = false;
document.getElementById('btn-rec').onclick = function () {
    rec = !rec;
    gmMorph.to(ico('#btn-rec'), rec ? 'stop' : 'tri-r');
};
document.getElementById('btn-refresh').onclick = function () {
    var i = ico('#btn-refresh');
    i.classList.add('gm-spin');
    clearTimeout(i._t);
    i._t = setTimeout(function () { i.classList.remove('gm-spin'); }, 780);
};
document.getElementById('btn-rise').onclick = function () {
    var cards = document.querySelectorAll('#rise-box > *');
    for (var n = 0; n < cards.length; n++) {
        cards[n].style.animation = 'none';
        void cards[n].offsetWidth;
        cards[n].style.animation = '';
    }
};
document.getElementById('btn-theme').onclick = function () {
    var dark = !document.body.classList.contains('gm-light');
    document.body.classList.toggle('gm-light', dark);
    gmMorph.to(ico('#btn-theme'), dark ? 'sun' : 'moon');
};
"""

body = []
body.append('<h2>图标（%d 个 · Tabler Icons Outline · MIT）</h2>' % len(names))
body.append('<div class="gm-demo-grid">')
for n in names:
    body.append('<div class="gm-demo-cell"><i class=gm-ico data-ic=%s></i><span>%s</span></div>' % (n, n))
body.append('</div>')

body.append('''
<h2 class="mt-lg">动效</h2>
<div class="card">
  <div class="row">
    <button id="btn-menu"><i class=gm-ico data-ic=menu></i> 形变：菜单 ⇄ 关闭</button>
    <button id="btn-play"><i class=gm-ico data-ic=tri-r></i> 形变：播放 ⇄ 暂停</button>
    <button id="btn-rec"><i class=gm-ico data-ic=tri-r></i> 形变：录制 ⇄ 停止</button>
    <button id="btn-save" class="ok"><i class=gm-ico data-ic=disk></i> 保存（成功后变对勾）</button>
    <button id="btn-refresh"><i class=gm-ico data-ic=refresh></i> 刷新（转圈）</button>
    <button id="btn-theme"><i class=gm-ico data-ic=moon></i> 主题：月亮 ⇄ 太阳</button>
    <label class="ios"><input type="checkbox" checked><i></i>
      <span class="ios-text">开关滑块改用 transform（原来是 left）</span></label>
  </div>
  <p class="hint mt">箭头类图标 hover 会顺着方向滑：
    <button><i class=gm-ico data-ic=arrow-r></i> 向右</button>
    <button><i class=gm-ico data-ic=arrow-l></i> 向左</button>
    <button><i class=gm-ico data-ic=arrow-u></i> 向上</button>
    <button><i class=gm-ico data-ic=arrow-d></i> 向下</button>
  </p>
</div>

<h3 class="mt-lg">卡片 hover 抬升 · 切板块淡入上移</h3>
<div class="grid-3" id="rise-box">
  <div class="card">卡片一 · 鼠标移上来会轻抬 2px</div>
  <div class="card">卡片二 · 只动 transform，不触发重排</div>
  <div class="card">卡片三 · 老机器上也是合成器在处理</div>
  <div class="card">卡片四</div>
  <div class="card">卡片五</div>
  <div class="card">卡片六</div>
</div>
<p class="mt-sm"><button id="btn-rise"><i class=gm-ico data-ic=refresh></i> 重播入场动画</button></p>

<h3 class="mt-lg">侧栏导航项（把鼠标移上去）</h3>
<div class="card" style="max-width:320px">
  <a class="gm-nav" href="javascript:void(0)"><i class=gm-ico data-ic=server></i> 服务器信息</a>
  <a class="gm-nav" href="javascript:void(0)"><i class=gm-ico data-ic=players></i> 在线玩家</a>
  <a class="gm-nav gm-nav-active" href="javascript:void(0)"><i class=gm-ico data-ic=toolbox></i> 操作控制台</a>
  <a class="gm-nav" href="javascript:void(0)"><i class=gm-ico data-ic=ban></i> 封禁管理</a>
</div>
''')

page = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GreenMoon 面板 · 图标与动效预览</title>
<style>
%(base)s
%(motion)s
%(demo)s
</style>
<style id="gm-icon-style">
%(icons)s
</style>
<style>
.gm-nav {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    color: var(--gm-text); text-decoration: none; font-size: 14px;
    border-radius: var(--gm-r-md); margin: 2px 0; position: relative;
    transition: background .2s ease, color .2s ease, transform .2s cubic-bezier(.2,.8,.2,1);
}
.gm-nav:hover { background: var(--gm-glass-hover); transform: translate3d(2px, 0, 0); }
.gm-nav::before {
    content: ''; position: absolute; left: 3px; top: 9px; bottom: 9px; width: 3px;
    border-radius: 3px; background: var(--gm-accent); opacity: 0; transform: scaleY(0);
    transition: transform .24s cubic-bezier(.2,.8,.2,1), opacity .24s ease;
}
.gm-nav:hover::before { opacity: .45; transform: scaleY(.42); }
.gm-nav-active::before { opacity: 1; transform: scaleY(1); }
</style>
</head>
<body>
<div class="layout"><div class="main">
<div class="section">
<h1>GreenMoon 面板 · 图标与动效预览</h1>
<p class="hint">图标：Tabler Icons Outline（MIT）· 形变：morphicons（MIT，打包成本地单文件，离线可用）</p>
%(body)s
</div>
</div></div>
%(morph)s
<script>
%(js)s
</script>
</body>
</html>
""" % {
    "base": base,
    "motion": motion,
    "demo": DEMO,
    "icons": icons,
    "body": "\n".join(body),
    "morph": morph,
    "js": JS,
}

io.open(OUT, "w", encoding="utf-8").write(page)
print("预览页 %d 字节，图标 %d 个" % (len(page.encode()), len(names)))
