# 前端第三方组件

面板 `static/` 里带了这些第三方前端资源，全部为宽松协议，允许商用与再分发。
三者都以源码形式内联进页面（见 `web_assets._build`），**不连任何 CDN，断网可用**。

| 组件 | 用途 | 协议 | 来源 |
| --- | --- | --- | --- |
| Tabler Icons（Outline） | 全面板图标（78 个，24×24 描边） | MIT © Paweł Kuna | https://github.com/tabler/tabler-icons |
| morphicons | 图标间的形状渐变（菜单⇄关闭、播放⇄暂停、软盘⇄对勾…） | MIT © Guillermo | https://github.com/guillermolg00/morphicons |
| QR Code Generator for JavaScript | 扫码登录 / 扫码绑定 | MIT © Kazuhiko Arase | http://www.d-project.com/ |

## 各自在哪

- **图标**：`static/admin.html` 的 `<style id="gm-icon-style">` 块。
  图标一律是 CSS mask（`-webkit-mask-image: url("data:image/svg+xml,...")`），
  一个 `<i>` 就能显示，**零 DOM 开销**。
- **形变库**：`static/admin.morph.js`（esbuild 打成 IIFE 单文件，gzip ~17 KB）。
- **二维码库**：`static/admin.extra.js`。

## 为什么要分两套形态

形变是逐帧改 `<path d>` 实现的，而 CSS mask 里的图形是一串**字符串**，没法插值。
所以只有真正会动的那几个图标才会被换成内联 `<svg><path>`（`.gm-ico-live`），
其余几百个静态图标继续走 mask —— 页面刚加载时一个都不升级。

## 重建方式（改了图标映射之后）

脚本都在 `static/tools/`，按顺序跑一遍即可（路径可用环境变量覆盖）：

1. 拿源码包：`npm pack @tabler/icons` / `npm pack morphicons`
2. `tools/build_morph.py` 的前置步骤 —— 把 morphicons 打成单文件：

   ```
   npm i morphicons
   echo 'export { createMorph } from "morphicons/dom";' > entry.js
   esbuild entry.js --bundle --format=iife --global-name=GMorph --minify -o /tmp/morph.min.js
   ```
3. `GM_TABLER=<tabler 的 icons/outline 目录> python3 tools/build_icons.py --apply`
   —— 内部图标名（如 `tri-r`）到 Tabler 图标名（如 `player-play`）的映射在这个脚本里；
   它会重写 `admin.html` 的图标样式块，并导出会形变的那些图标的 path。
4. `python3 tools/build_morph.py` —— 生成 `admin.morph.js`。
5. `python3 tools/build_preview.py` —— 生成那个可以点开验收的预览页。

## 动效原则

所有动画只碰 `transform` / `opacity`，刻意不用
`box-shadow` / `filter` / `width` / `top` / `left` 做动画 —— 前两者由合成器处理，
不触发重排与重绘。iOS 开关的滑块原本用 `left` 位移，已改为 `transform`。
系统开启「减弱动效」时全部降级为瞬切（含 morphicons 的 `reducedMotion: 'user'`）。
