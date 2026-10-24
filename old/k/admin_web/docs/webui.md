# 外部插件如何接入 GreenMoon 面板的 WebUI

> 适用版本：GreenMoon 面板 **10.2.3+**
>
> 一句话：**别的插件可以把自己的设置项挂进 GreenMoon 的网页后台**，服主在一个后台里配置所有插件，不必为每个插件各开一个端口、各记一套账号。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 两种接入方式怎么选](#2-两种接入方式怎么选)
- [3. 方式一：Python 子插件（推荐）](#3-方式一python-子插件推荐)
- [4. 字段类型参考](#4-字段类型参考)
- [5. 方式二：HTTP 远程注册（别的进程 / 别的语言）](#5-方式二http-远程注册别的进程--别的语言)
- [6. 安全边界](#6-安全边界)
- [7. 常见坑](#7-常见坑)
- [8. 完整可运行示例](#8-完整可运行示例)

---

## 1. 这是什么

GreenMoon 面板内置了一个**第三方插件面板注册中心**（`admin_web/panel.py`）。你把一组"字段声明"交给他，它会在**服主后台**渲染出一个表单；服主填完点保存，值就回调给你。

三个值得先知道的设计：

| 设计 | 说明 |
|------|------|
| **默认隐形** | 主导航里**没有**常驻入口。只有真的有插件注册了面板，左侧才会冒出「🧩 插件面板」这一项；一个都没接进来时，整个板块连同导航项一起不存在 —— 常年空白的入口比没有入口更让人困惑。 |
| **注册面板 ≠ 获得权限** | 面板是否对某个登录者可见，仍受该账号的 `scope` 约束（`/api/ext/` 需要 `ext` scope）。 |
| **值一律按声明的类型归一化** | 你声明 `number`，收到的就一定是数字（且被 `min`/`max` 夹取）；你声明 `select`，非法值直接丢弃。不需要自己在 `saver` 里做类型校验。 |

---

## 2. 两种接入方式怎么选

| | **Python 直连** | **HTTP 远程注册** |
|---|---|---|
| 适用 | 同一进程内的 Endstone 插件（**推荐**） | 别的进程 / 别的语言（Node、Go、Java…）写的插件 |
| 接入方式 | `from gm import panel` | `POST /api/ext/register` |
| 值怎么传 | 直接回调，无序列化开销 | 面板通过你给的 `endpoint` 回调你 |
| 需要凭据 | 不需要（靠子插件身份识别） | 需要 `ext_api_token` |
| 生命周期 | 插件卸载时自动摘除 | **每 900 秒要续约一次**，超时自动摘除 |
| 代价 | 必须先能拿到 `admin_web` 模块 | 需要 token 与心跳 |

> 两种路径走的是**同一个注册中心**，服主看到的界面完全一致。

---

## 3. 方式一：Python 子插件（推荐）

### 3.1 最小可运行示例

```python
# my_prefix_plugin/__init__.py
from gm import panel, config


def setup(plugin=None, gm=None):
    panel.register(
        plugin_id="my_prefix",          # 全局唯一，建议用你的 uuid
        title="我的前缀插件",
        icon="✨",
        description="自定义聊天与公告前缀",
        fields=[
            {"key": "prefix", "type": "text", "label": "聊天前缀",
             "default": "[玩家]", "hint": "显示在玩家名前面"},
            {"key": "enabled", "type": "bool", "label": "启用"},
            {"key": "mode", "type": "select", "label": "模式",
             "options": [{"value": "a", "label": "全部玩家"},
                         {"value": "b", "label": "仅管理员"}]},
            {"key": "secret", "type": "password", "label": "接口密钥"},
        ],
        loader=lambda: config.all(),    # 返回当前值
        saver=lambda values: (_write(values), "已保存"),
    )


def _write(values: dict) -> bool:
    cfg = config.all()
    cfg.update(values)
    config.save(cfg)
    return True
```

### 3.2 权限声明

子插件的 `manifest.json` 必须声明 `panel` 权限（通常还要 `config`）：

```json
{
  "type": "py",
  "entry": "__init__.py",
  "permissions": ["panel", "config"]
}
```

不声明会得到 `GmPermissionError`，报错里会明确告诉你该加哪个权限。

### 3.3 `register()` 参数

| 参数 | 必填 | 说明 |
|------|:--:|------|
| `plugin_id` | ✅ | 全局唯一。**重复注册会覆盖前一个**（方便热重载），所以插件更新后可以直接再注册一次。 |
| `title` | ✅ | 面板标题 |
| `fields` | ✅ | 字段声明列表，最多 **64** 个 |
| `description` | | 面板副标题 |
| `icon` | | 一个 emoji，默认 🧩 |
| `version` / `homepage` / `author` | | 展示用元信息 |
| `loader` | | 无参，返回当前值的 dict。服主**打开**面板时调用。 |
| `saver` | | 收一个 dict（只含服主改动过、且是你声明过的键），返回 `(ok, msg)` 或直接返回 `bool`。服主**点保存**时调用。 |

`register()` 返回 `(ok: bool, message: str)`，注册失败不会抛异常。

### 3.4 生命周期

- 用 `register()` 注册；插件卸载时主插件会**按 source（你的 uuid）自动整批摘除**，不必手动清理。
- 想提前摘掉就调 `panel.unregister("my_prefix")`。
- `panel.mine()` 列出**你自己**注册的面板（看不到别人的 —— 那是信息泄露）。
- `panel.available()` 判断面板功能是否就绪。

---

## 4. 字段类型参考

```python
{"key": "字段名", "type": "类型", "label": "显示名", ...}
```

| type | 渲染成 | 归一化规则 |
|------|--------|-----------|
| `text` | 单行输入框 | 转字符串，截断到 4096 字符 |
| `password` | 密码框 | 见下方 ⚠ |
| `number` | 数字输入框 | 转 float；超出 `min`/`max` 会被**夹取**（不是报错） |
| `bool` | 开关 | 接受 `1/true/yes/on/是/开`；其余为 False |
| `select` | 下拉框 | 值必须命中 `options` 之一，否则**丢弃** |
| `textarea` | 多行文本 | 同 `text` |
| `group` | 分组标题 | **不产生值**，只是一条分隔 |
| `note` | 说明文字 | **不产生值** |

### 字段的可选属性

| 属性 | 说明 |
|------|------|
| `label` | 显示名（默认取 `key`） |
| `hint` | 输入框下方的灰色提示 |
| `placeholder` | 占位文本 |
| `default` | 默认值（服主第一次打开时用） |
| `options` | 仅 `select`；`[{value, label}, ...]`，最多 50 项 |
| `min` / `max` | 仅 `number` |
| `readonly` | 只读（值会被拒绝写回） |
| `advanced` | 折叠到"高级"区 |
| `group` | 分组名 |

### ⚠ 两个必须知道的点

1. **`password` 是打码的。** 值回传时是 `"******"`（`MASK`）。如果服主**没改动**密码框，`saver` 里收到的就是 `"******"`，而这个值会被**自动丢弃** —— 也就是说 `saver` 收到的 dict 里**根本不含这个键**。你自己保留旧值即可：

    ```python
    def _saver(values):
        cfg = config.all()
        if "secret" in values:      # 没被丢弃 = 服主真的填了新值
            cfg["secret"] = values["secret"]
        ...
    ```

2. **`key` 必须是合法标识符**：`^[A-Za-z_][A-Za-z0-9_]{0,63}$`（不能数字开头、不能有中文和连字符）。不合法的字段会被**静默丢弃** —— 所以字段莫名不显示时，先查 `key`。

---

## 5. 方式二：HTTP 远程注册（别的进程 / 别的语言）

### 5.1 前置：服主必须先配置 token

远程注册**默认关闭**。未配置 token 时端点一律返回 403 —— 这是刻意的：这个端点能让外部进程往面板里塞内容，默认开放等于给服务器开一个匿名写入口子。

服主在面板配置文件 `config.json` 里加：

```json
{
  "ext_api_token": "你自己想一个足够长的随机串"
}
```

### 5.2 注册

```http
POST /api/ext/register
Content-Type: application/json

{
  "token":     "上面那个 token",
  "plugin_id": "my_node_plugin",
  "title":     "我的 Node 插件",
  "icon":      "🔧",
  "description": "用 Node 写的签到插件",
  "fields": [
    {"key": "interval", "type": "number", "label": "间隔（分钟）",
     "min": 1, "max": 1440, "default": 30},
    {"key": "welcome",  "type": "text",   "label": "欢迎语", "default": "欢迎！"}
  ],
  "endpoint": "http://127.0.0.1:18080",
  "ttl": 900,
  "version": "1.0.0",
  "homepage": "https://example.com",
  "author": "you"
}
```

响应：`{"ok": true, "message": "...", "pid": "my_node_plugin"}`

### 5.3 心跳续约（**必须**）

远程面板有存活时间（默认 900 秒，可调范围 30 ~ 86400）。**每隔 `ttl` 的一半时间重新 POST 一次 `/api/ext/register`**（同样的 `plugin_id` 会覆盖并刷新 TTL）。超时后面板自动消失，理由很实际：避免"插件明明已经卸载了，面板还挂着它的入口"。

```python
import threading, requests

def heartbeat():
    while True:
        requests.post(BASE + "/api/ext/register", json=REGISTER_BODY, timeout=8)
        threading.Event().wait(450)     # ttl 的一半
threading.Thread(target=heartbeat, daemon=True).start()
```

主动注销：

```http
POST /api/ext/unregister
{"token": "...", "plugin_id": "my_node_plugin"}
```

### 5.4 你要提供的两个回调（由 `endpoint` 拼出）

注册时给了 `endpoint`，面板会这样回调你：

| 时机 | 请求 | 你返回 |
|------|------|--------|
| 服主打开面板 | `POST {endpoint}/config`，body `{}` | `{"values": {"interval": 30, "welcome": "..."}}` |
| 服主点保存 | `POST {endpoint}/config/save`，body `{"plugin_id": "...", "values": {...}}` | `{"ok": true, "message": "已保存"}` |

> 超时分别是 6 秒（读）和 8 秒（写）。**别在这两个接口里做慢操作**，否则服主那边就是转圈。

### 5.5 前端只读接口（可选，用于自测）

```http
GET /api/ext/panels                 列出已接入的面板
GET /api/ext/panel?pid=xxx          读某个面板的字段与当前值
POST /api/ext/panel/save            保存（临时账号进不去，见下节）
```

---

## 6. 安全边界

| 边界 | 说明 |
|------|------|
| **注册面板不等于获得权限** | 面板对某个登录者是否可见，取决于该账号的 `scope`（`/api/ext/` 需要 `ext`）。临时账号看不到，也改不了。 |
| **保存要有管理员角色** | `/api/ext/panel/save` 额外过了 `_require_admin_role`。这等同于直接改服务器上的配置文件，临时账号一律拒绝。 |
| **`saver` 在主线程执行** | 面板会代为派发到服务器主线程（`_run_in_server_thread`）。这对你是好事 —— 你可以安全地改世界状态；但也意味着 **saver 必须快速返回**，慢操作请自己丢后台线程并立刻返回成功。 |
| **payload 有硬上限** | 最多 64 个字段、字符串截断到 4096、`options` 最多 50 项、`key` 必须是标识符。一个插件没法用超大 payload 把配置文件撑爆。 |
| **远程注册默认关闭** | 未配置 `ext_api_token` 时一律 403。 |

---

## 7. 常见坑

**Q：面板压根没出现在导航里？**
A：确认三件事 —— ① `register()` 返回的 `ok` 是 `True`；② 真的注册了（调 `panel.mine()` 看看）；③ 当前登录账号的 scope 里有 `ext`。

**Q：某个字段不显示？**
A：99% 是 `key` 不合法（比如 `"max-count"` 或中文 key）。`key` 必须匹配 `^[A-Za-z_][A-Za-z0-9_]{0,63}$`。非法字段被静默丢弃。

**Q：密码字段读出来永远是 `******`，改了老是不生效？**
A：这是设计。没改动时 `saver` 收到的 dict 里**没有这个键**。判一下 `if "secret" in values:` 再写。

**Q：保存没报错，但值没变？**
A：看响应的 `rejected` 字段。没通过类型归一化的键会被列在这里（`select` 值不在 `options` 里、字段是 `readonly`、密码是掩码值都会导致）。

**Q：远程面板一会儿就不见了？**
A：心跳没续上。远程面板默认 900 秒过期，记得定时重新注册。

**Q：想在面板上放个按钮，不只是填表单？**
A：目前面板只做**配置表单**（声明式字段 + loader/saver），不做自由布局的页面。需要交互按钮请用子插件的 `page` 文件（mod 页面）机制。

---

## 8. 完整可运行示例

### 8.1 Python 子插件版

```python
# my_prefix_plugin/__init__.py
"""给聊天消息加前缀 —— 配置放在 GreenMoon 面板里。"""
from gm import panel, config, event
from endstone.event import PlayerJoinEvent


def setup(plugin=None, gm=None):
    ok, msg = panel.register(
        plugin_id="my_prefix",
        title="聊天前缀",
        icon="✨",
        description="自定义聊天与进服欢迎语",
        version="1.0.0",
        fields=[
            {"key": "enabled", "type": "bool", "label": "启用本插件"},
            {"key": "prefix", "type": "text", "label": "聊天前缀",
             "placeholder": "[玩家]", "hint": "显示在玩家名前面"},
            {"key": "mode", "type": "select", "label": "生效范围",
             "options": [{"value": "a", "label": "全部玩家"},
                         {"value": "b", "label": "仅管理员"}]},
            {"key": "welcome", "type": "textarea", "label": "进服欢迎语"},
            {"key": "secret", "type": "password", "label": "接口密钥",
             "advanced": True},
        ],
        loader=config.all,
        saver=_save,
    )
    if not ok:
        print(f"[my_prefix] 面板注册失败：{msg}")

    @event.on(PlayerJoinEvent)
    def _on_join(ev):
        if not config.get("enabled", False):
            return
        welcome = config.get("welcome", "")
        if welcome:
            ev.player.send_message(welcome)


def _save(values: dict):
    """服主点保存时回调。只处理服主真正改过的键。"""
    cfg = config.all()
    for k in ("enabled", "prefix", "mode", "welcome"):
        if k in values:
            cfg[k] = values[k]
    # password 字段：saver 里没这个键 = 服主没改，保留旧值
    if "secret" in values:
        cfg["secret"] = values["secret"]
    config.save(cfg)
    return True, "已保存，立即生效"
```

> ⚠ **配置落地的两种方式，别踩坑**
>
> - **用 `gm.config`**：值存在你包内的 `config.json`，和面板「⚙ 设置」页读写的**是同一份文件** —— 服主在那边改，你这边立刻读得到。
>   但 `config.save()` 是**覆盖写**，并且**只接受你在 `settings.json` 里声明过的键**，未声明的会被拒（返回值里能看到 `rejected`）。所以包里必须带一份 `settings.json`：
>
>   ```json
>   {
>     "fields": [
>       {"key": "enabled", "type": "bool",   "label": "启用本插件", "default": true},
>       {"key": "prefix",  "type": "text",   "label": "聊天前缀",   "default": "[玩家]"},
>       {"key": "mode",    "type": "select", "label": "生效范围",
>        "options": [{"value": "a", "label": "全部玩家"},
>                    {"value": "b", "label": "仅管理员"}]},
>       {"key": "welcome", "type": "textarea", "label": "进服欢迎语", "default": "欢迎！"},
>       {"key": "secret",  "type": "password", "label": "接口密钥"}
>     ]
>   }
>   ```
>
>   注意三件事：**顶层必须是对象**、字段放在 **`fields` 数组**里（写成裸数组会整个不生效）；最多 64 个字段；单个字段写错不会让整个文件失效，只会丢弃那一条并在服务器日志里留一条 warning。
>
> - **自己写文件**：`gm.config` 只管它自己那份。你完全可以在 `saver` 里把 `values` 直接写进自己的 JSON —— 不必受 `settings.json` 约束，代价是服主在面板「⚙ 设置」页看不到，只能在你的「🧩 插件面板」里改。

`manifest.json`：

```json
{
  "name": "聊天前缀",
  "type": "py",
  "entry": "__init__.py",
  "permissions": ["panel", "config", "players"]
}
```

### 8.2 Node.js 远程版

```js
// ext-panel.js —— 一个 Node 插件把自己挂进 GreenMoon 面板
const express = require('express');

const PANEL = 'http://127.0.0.1:20091';      // GreenMoon 面板地址
const TOKEN = '你的 ext_api_token';
const SELF  = 'http://127.0.0.1:18080';      // 面板回调我用的地址

let values = { interval: 30, welcome: '欢迎！' };

const body = {
  token: TOKEN,
  plugin_id: 'my_node_plugin',
  title: '我的 Node 插件',
  icon: '🔧',
  description: '用 Node 写的签到插件',
  fields: [
    { key: 'interval', type: 'number', label: '签到间隔（分钟）',
      min: 1, max: 1440, default: 30 },
    { key: 'welcome',  type: 'text',   label: '欢迎语', default: '欢迎！' },
  ],
  endpoint: SELF,
  ttl: 900,
};

// 注册 + 心跳（ttl 的一半续一次）
async function register() {
  const r = await fetch(`${PANEL}/api/ext/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  console.log('register ->', await r.json());
}
register();
setInterval(register, 450_000);

// 面板回调：读当前值 / 保存新值
const app = express();
app.use(express.json());
app.post('/config', (req, res) => res.json({ values }));
app.post('/config/save', (req, res) => {
  Object.assign(values, req.body.values || {});
  console.log('保存后的配置：', values);
  res.json({ ok: true, message: '已保存' });
});
app.listen(18080, () => console.log('插件已就绪 :18080'));
```

---

## 附：相关源码位置

| 想看什么 | 文件 |
|----------|------|
| 注册中心、字段归一化、TTL | `admin_web/panel.py` |
| 子插件 API（`gm.panel`） | `admin_web/gm/panel.py` |
| HTTP 端点与 token 校验 | `admin_web/web.py` 的「第三方插件面板（/api/ext/*）」段 |
| 前端渲染 | `admin_web/static/admin.html` 的 `sec-ext` 板块 |
| 权限声明白名单 | `admin_web/mod_loader.py` 的 `KNOWN_PERMISSIONS` |
