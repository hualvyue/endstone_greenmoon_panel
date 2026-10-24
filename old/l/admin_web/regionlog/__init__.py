"""区块日志与恢复系统（``regionlog``）。

给 AI 的行为记录系统补上「世界发生了什么变化 / 能不能退回去」这一半。

背景
----
``behaviors.py`` 记录的是**玩家做了什么**（聊天、爆炸物、方块增删……），
它是一条只追加的 JSONL 流水，回答"最近发生了什么"。

但"把这个区域恢复到一小时前"需要的是另一份数据：

* 按坐标建索引（JSONL 做不到）；
* 每条记录带"是否已被回档"标记；
* 回档前留下现场快照，好让回档本身可以被撤销。

这就是 ``regionlog`` 存在的原因。它是行为记录系统的**世界侧**。

对外脉络
--------

    behaviors.py  玩家行为流水（JSONL，AI 查询用）
          |
          +-- regionlog/   方块变更流水 + 回档 + 撤销（SQLite）
                    |
                    +-- api.RegionAPI      ← 唯一入口（人 / AI / 插件共用）
                    +-- web                ← HTTP 路由（在 core 里登记）
                    +-- panel              ← 第三方插件挂载点

原游戏内 UI（木斧选区 + 表单）**没有**随移植进来 —— 面板化之后，
区域由坐标显式给出，进度实时推到浏览器 / AI 工具返回值里。
"""

from .api import (
    RegionAPI, RegionConfig, ApiResult,
    CALLER_AI, CALLER_HUMAN, CALLER_PLUGIN,
)
from .store import RegionStore

__all__ = [
    "RegionAPI", "RegionConfig", "ApiResult", "RegionStore",
    "CALLER_HUMAN", "CALLER_AI", "CALLER_PLUGIN",
]

API_VERSION = "1.0"
