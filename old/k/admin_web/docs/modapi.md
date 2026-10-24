# 子插件 API（`gm` 模块）

> 适用版本：GreenMoon 面板 **10.2.3+**

子插件（`.gmmod` / py 子插件）在自己的代码里 `from gm import ...`，就能拿到面板已经封装好的能力 —— 服务器、玩家、机器人、配置、AI、区块回档、面板挂载，都在里面。

---

## 1. 30 秒上手

```python
# my_mod/__init__.py
from gm import server, players, config, event
from endstone.event import PlayerJoinEvent


def setup(plugin=None, gm=None):
    """子插件入口。主插件加载子插件时调用它。"""
    server.broadcast("§a我的插件已启动")

    @event.on(PlayerJoinEvent)
    def _on_join(ev):
        ev.player.send_message("§e欢迎回来！")

    print("当前在线：", players.names())
```

`manifest.json`：

```json
{
  "name": "我的插件",
  "type": "py",
  "entry": "__init__.py",
  "permissions": ["players", "config"]
}
```

**不声明 `permissions` 默认只能拿到游戏侧能力**（`server` / `player` / `event`）。凡是碰面板数据的模块都要显式声明。

---

## 2. 模块清单

| 模块 | 干什么 | 需要的权限 | 主要函数 |
|------|--------|:----------:|---------|
| `gm.server` | Endstone Server 透传 + 主线程包装 | — | `get` `raw` `run` `broadcast` `dispatch` `online_players` `player_list` `player_count` |
| `gm.player` | 单个玩家（原生 `Player` 对象） | — | `find` `raw` `all` `run_on` `names` `tell` `kick` `teleport` `health` `game_mode` |
| `gm.players` | 在线玩家快照（纯读，不碰服务器线程） | `players` | `snapshot` `names` `get` `count` `exists` `field` |
| `gm.admin` | 面板状态直读（系统指标 / 玩家 / 消息） | — | `system_stats` `players_snapshot` `online_names` `online_count` `messages` `state` `cache` |
| `gm.event` | 注册游戏事件 | — | `on` `off` `clear` `handlers` `supported` |
| `gm.console` | 控制台日志与指令 | `console` | `logs` `run` `command` `tail` `clear` |
| `gm.bot` | QQ 机器人（多卡片维度） | `bots` | `list` `get` `groups` `send_text` `send_group` `primary` … |
| `gm.bindings` | 玩家 QQ 账户绑定数据 | `bindings` | `all` `by_qq` `by_mc` `get` `exists` `count` `pairs` |
| `gm.cross` | 跨服玩家数据 | `cross` | `enabled` `role` `get_player` `put_player` `status` … |
| `gm.mapart` | 把图片画到地图上 | `players` | `available` `diagnose` `create` `give` `to_pixels` … |
| `gm.config` | 读写**自己包内**的 `config.json` | `config` | `all` `get` `save` `update` `keys` `reset` `path` |
| `gm.trust` | 玩家可信度（加权安全系统） | `trust` | `get` `score` `all_scores` `set` `add` `is_trusted_person` … |
| `gm.whitelist` | 服务器白名单读写 | `whitelist` | `list` `add` `remove` `has` `snapshot` |
| `gm.ban` | 服务器封禁列表读写 | `bans` | `list` `add` `remove` `has` `snapshot` |
| `gm.mods` | 子插件管理（清点 / 启停 / 安装） | `mods` | `list` `info` `enabled` `enable` `disable` `install` `uninstall` `reload` |
| `gm.ai` | 面板 AI 能力（问答 / 注册工具 / 接管级别） | `ai` `ai:control` `ai:irreversible` | `available` `status` `ask` `register_tool` `autonomy` `set_autonomy` |
| **`gm.region`** | **区块日志与区域回档** | `region` | `enabled` `status` `recent` `at` `sessions` `preview` `rollback` `undo` `subscribe` |
| **`gm.panel`** | **把自己的设置面板挂进主面板** | `panel` | `register` `unregister` `mine` `available` |

想在面板里挂自己的设置项请看 [《外部插件接入 WebUI 指南》](#)（同目录下的 `webui.md`）。

---

## 3. 权限

`manifest.json` 的 `permissions` 是一个字符串数组，写 `"*"` 表示全部。

**可选值**：`players` `logs` `console` `detail` `bots` `bindings` `cross` `mods` `backup` `cloud` `gamerules` `worlds` `perm` `scoreboard` `gametools` `files` `diag` `account` `trust` `config` `whitelist` `bans` `ai` `ai:control` `ai:irreversible` `region` `panel`

### ⚠ 几个高危权限，申请前请想清楚

| 权限 | 它能做什么 |
|------|-----------|
| `trust` | 改任意玩家的可信度（含「信任的人」＝ 永久满分、免疫扣分） |
| `whitelist` | 决定**谁能进服** |
| `bans` | 决定**谁进不来** |
| `mods` | 启停 / 安装 / 卸载其它子插件 ⇒ 事实上的**任意代码执行** |
| `region` | **直接改写世界方块**（回档 / 撤销），并能拉出完整的方块变更流水（含隐私） |
| `panel` | 让用户在面板里帮你填参数，值会喂给你的 `saver` |
| `ai:control` | 改 AI 接管级别 —— 等于决定"AI 能不能自己动手删档" |
| `ai:irreversible` | 注册不可逆等级的工具（影响面与内置的删档工具同级） |

> `ai` 拆成三级（`ai` / `ai:control` / `ai:irreversible`）是刻意的：
> 一个只想"问 AI 几个问题"的子插件，不该顺手获得"把服务器推进完全放开模式"的能力。

---

## 4. 常用示例

### 4.1 配置读写（`gm.config`）

```python
from gm import config

who = config.get("bot_id", "")      # 读单个键
cfg = config.all()                  # 读全量 dict
config.update(bot_id="123456")      # 增量合并（推荐）
config.save({"bot_id": "123456"})   # 覆盖写
```

**两个坑**：

1. `config.save()` 是**覆盖写**，并且**只接受你在包内 `settings.json` 里声明过的键**，未声明的会被拒（返回值里能看到 `rejected`）。

   `settings.json`（随包分发，**顶层必须是对象、字段放在 `fields` 数组里**）：

   ```json
   {
     "fields": [
       {"key": "bot_id", "type": "text", "label": "机器人 QQ 号", "default": ""},
       {"key": "groups", "type": "textarea", "label": "播报群号", "default": ""}
     ]
   }
   ```

2. `config.json` 和面板「⚙ 设置」页读写的**是同一份文件** —— 服主在网页上改，你这边立刻读得到。

### 4.2 注册事件（`gm.event`）

```python
from gm import event
from endstone.event import PlayerChatEvent, PlayerJoinEvent

@event.on(PlayerChatEvent)
def on_chat(ev):
    if "spawn" in ev.message:
        ev.player.send_message("§e坐标：0 64 0")

@event.on(PlayerJoinEvent)
def on_join(ev):
    ev.player.send_message("§a欢迎回来！")

event.off(PlayerChatEvent, on_chat)     # 取消注册
```

Endstone 的事件注册是**插件级、面向插件自己方法**的，子插件无法直接注册 —— 所以这里由主插件预先挂好转发方法，再按类型分发给你的函数。子插件卸载时它注册的所有 handler 会被**自动清理**。

### 4.3 区块回档（`gm.region`）

```python
from gm import region

# ① 先看影响面（只读，**务必先做**）
info = region.preview(p1=(100, 60, 200), p2=(130, 80, 230),
                      dimension="overworld", minutes=60)
print(info["records_hit"], info["by_actor"])

# ② 执行回档（**异步**：返回只代表"已受理"）
res = region.rollback(p1=(100, 60, 200), p2=(130, 80, 230),
                      dimension="overworld", minutes=60,
                      actor="my_plugin", reason="清理炸服建筑")

# ③ 结果靠订阅事件拿（回档是分批跑的，可能持续数秒）
def _on(ev, payload):
    if ev == "done":
        print("回档完成:", payload.get("applied"))
off = region.subscribe(_on)
# ... 不用了记得 off()

# ④ 撤销
region.undo(session_id=res["session_id"], actor="my_plugin")
```

**要点**：

- `actor` **必填** —— 回档是破坏性操作，必须留下"谁干的"。用你插件的名字，别填玩家名。
- 回档**异步分批**执行，函数返回 ≠ 完成。想知道结果请 `subscribe()`，或事后查 `sessions()`。
- 时间窗支持 `minutes` / `seconds` / `hours` / `days`，**四选一**（同时传按 minutes → seconds → hours → days 取第一个）。
- `region.at(x, y, z)` 的 `dimension` **留空 = 服务端默认维度**，不是"不限维度"；`region.recent()` 的 `dimension` 才是过滤器（留空 = 不限）。同名不同义，别搞混。
- `region.undo()` 不传 `session_id` = **撤销最近一次回档**，谨慎。

### 4.4 挂自己的设置面板（`gm.panel`）

```python
from gm import panel, config

def setup(plugin=None, gm=None):
    panel.register(
        plugin_id="my_prefix",
        title="我的前缀插件",
        icon="✨",
        fields=[
            {"key": "prefix",  "type": "text", "label": "聊天前缀", "default": "[玩家]"},
            {"key": "enabled", "type": "bool", "label": "启用"},
        ],
        loader=config.all,                       # 服主打开面板时读当前值
        saver=lambda v: (_write(v), "已保存"),    # 服主点保存时回调
    )
```

面板出现在主面板「🧩 插件面板」板块。字段类型、`password` 打码规则等见 `webui.md`。

---

## 5. 线程

- **`gm.server` / `gm.player` 的写入类操作**会自动派发到服务器主线程，你不用自己切线程。
- **但 `loader` / `saver`（`gm.panel`）由主线程调用** —— 它们**必须快速返回**，不能读网络、不能等锁。慢操作请在 `saver` 里丢后台线程并立刻返回成功。
- `gm.region` 的回档本身在主线程分批执行；回调（`subscribe` 的函数）也在**发起事件的那条线程**上跑（回档时就是主线程），**回调里绝不能做慢操作**，否则直接卡 tick。

---

## 6. 常见报错

| 报错 | 原因 | 怎么办 |
|------|------|--------|
| `GmNotReady` | 主插件还没挂载 `gm` | 在 `setup()` 里调用，别在模块顶层就调 |
| `GmPermissionError` | 缺少权限 | 在 `manifest.json` 的 `permissions` 加上它报的那个 scope |
| `save()` 的返回值里 `rejected` 非空 | 键没在 `settings.json` 声明，或类型不符 | 补声明 / 改类型 |
| 面板字段不显示 | `key` 不合法，或类型不支持 | `key` 须匹配 `^[A-Za-z_][A-Za-z0-9_]{0,63}$` |
| 装不上，报"permissions 含未知权限" | 写了清单里没有的 scope | 对照上面第 3 节的可选值 |
