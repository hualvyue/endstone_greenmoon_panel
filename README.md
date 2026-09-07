# endstone_greenmoon_panel

GreenMoon 面板（发行名 `greenmoon-panel`）是一个基于 Endstone 的我的世界基岩版服务器管理面板，提供 Web 管理后台、机器人接入（QQ 官方 / OneBot v11）、跨服同步、云黑联合封禁与白名单等功能。

- 插件框架：[Endstone](https://endstone.dev)
- 运行环境：Python >= 3.11
- 当前版本：4.1.0

## 功能概览

- Web 管理后台：在线玩家、封禁、白名单、存档备份、模组与游戏规则管理。
- 机器人接入：QQ 官方机器人（网关鉴权）与 OneBot v11（WebSocket），支持群聊互通、进服/离服通知。
- 跨服同步：玩家数据、白名单与封禁记录的跨服同步。
- 云黑联合封禁（UniteBan）：进服自动查黑、封禁与解封自动上报。
- 控制台与 TPS/状态监控。

## 安装与使用

1. 将发行包放入服务端的 `plugins/` 目录。
2. 确认环境满足 `Python >= 3.11` 且已安装 `endstone`。
3. 启动服务器后，插件会自动创建 `greenmoon_panel` 文件夹，把离线依赖放入 `greenmoon_panel/libs` 文件夹。
4. 通过浏览器访问面板地址，在「机器人管理」「云黑名单」等板块完成配置。

## AI 辅助声明

本项目在开发过程中使用了 AI 辅助编程工具参与大部分代码生成与部分结构重构（约88%）。人工仅完成基本结构和部分逻辑的代码编写（约12%），功能实现几乎都是AI完成。可能包含由模型无意使用的GPL或其它协议的代码（部分代码按原样提交，作者将不对这部分代码的潜在第三方侵权问题负责），已知使用的开源项目请见下表。



## 第三方版权与许可声明

本项目在实现中参考、集成或捆绑了以下第三方项目。各项目的版权归其作者或贡献者所有，许可证声明如下：

| 项目 | 用途 | 许可证 | 版权声明 |
| --- | --- | --- | --- |
| [LumenBridge（明流桥）](https://github.com/gxh6438/LumenBridge) | 多适配器/机器人卡片架构的移植与设计对齐 | [MIT](https://www.opensource.org/licenses/mit-license.php) | Copyright (c) 2026 LumenBridge contributors |
| [Endstone](https://endstone.dev) | 插件开发框架（SDK 依赖） | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | The Endstone Authors |
| [requests](https://requests.readthedocs.io/) | 云黑接口等网络请求依赖 | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Kenneth Reitz |
| [websockets](https://websockets.readthedocs.io/) | WebSocket 通信（参考项目中捆绑） | [BSD-3-Clause](https://opensource.org/licenses/BSD-3-Clause) | Aymeric Augustin |
| [async-timeout](https://github.com/aio-libs/async-timeout) | 异步超时工具（捆绑） | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | The aio-libs team |
| [qrcode.js](https://github.com/kazuhikoarase/qrcode-generator) | 二维码生成前端库（参考项目 WebUI 捆绑） | [MIT](https://www.opensource.org/licenses/mit-license.php) | Copyright (c) 2009 Kazuhiko Arase |
| [Prism.js](https://prismjs.com/) | 代码高亮前端库（参考项目 WebUI 捆绑） | [MIT](https://www.opensource.org/licenses/mit-license.php) | Prism contributors |

使用与再分发上述第三方项目时，请一并保留其对应的版权声明与本许可说明。

## 许可证

本项目以 **MIT License** 开源，版权归所有Copyright (c) 2026 hualvyue。详见 [`LICENSE`](./LICENSE)。

## 说明

本插件发行包仅打包 `admin_web` 包及其前端资源，第三方库以依赖或参考形式引用，不在发行包内重复捆绑。
