<div align="center">

# 🛡️ QQ 官方群管理

**入群有人迎，申请点一下，群聊管理轻松一点。**

✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) · QQ 官方机器人 · WebSocket / Webhook ✨

[![版本 2.6.0](https://img.shields.io/badge/版本-2.6.0-89b4fa.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-a3be8c.svg)](LICENSE)
[![作者 yun474](https://img.shields.io/badge/作者-yun474-f5b7c7.svg)](https://github.com/yun474)

<img src="https://count.getloli.com/@yun474_qq_group_admin?name=yun474_qq_group_admin&amp;theme=booru-lewd&amp;padding=7&amp;offset=0&amp;align=top&amp;scale=1&amp;pixelated=1&amp;darkmode=auto" alt="访问计数" />

[功能亮点](#features) · [安装使用](#usage) · [入群审批](#review) · [权限配置](#permissions) · [进退群通知](#notices)

</div>

---

<a id="features"></a>

## ✨ 群里的小帮手

| | 功能 |
| --- | --- |
| 🔇 **禁言与解禁** | 多成员操作、组合时长、默认时长与禁言状态查询 |
| 📨 **入群审批** | Markdown 申请卡片，回调按钮一键处理，也支持引用通知回复 |
| 👋 **进退群通知** | 自定义欢迎与退群文案，支持成员艾特和头像 |
| 🗝️ **分群管理** | 本群插件群管、全局或分群功能开关、群环境白名单 |
| 🤖 **LLM 工具** | 禁言、解禁、状态查询、申请列表与审批，可选严格权限审查 |

<a id="usage"></a>

## 🚀 安装与使用

在 AstrBot 插件管理中通过[仓库链接](https://github.com/yun474/astrbot_plugin_qq_group_admin)安装，或导入插件 ZIP。也可将插件目录放入 `data/plugins/` 后重载，无需额外 Python 依赖。

使用前请确认：

1. 使用 `qq_official` 或 `qq_official_webhook` 适配器，启用群/C2C 事件。
2. 机器人是目标 QQ 群的管理员，并已获得对应群管理接口权限。
3. 使用审批按钮需具备自定义 Markdown/按钮权限；Webhook 需订阅 `INTERACTION_CREATE`。

| 指令 | 用途 |
| --- | --- |
| `/禁言 @用户 [时间]` | 禁言一个或多个成员，省略时间默认 **1 分钟** |
| `/解禁 @用户` | 解除成员禁言 |
| `/禁言状态` | 查看全员禁言规则及被禁言成员 |
| `/添加群管 @用户` / `/删除群管 @用户` | 管理本群插件群管 |
| `/群管列表` / `/群管帮助` | 查看群管名单与使用帮助 |
| `/群管功能` | 查看功能开关；点击状态填入修改指令，发送后生效 |

时间支持 `30秒`、`10分`、`2小时`、`1天2小时`，纯数字按分钟；`0`、`解除`、`解禁`表示解除禁言。默认时长可在配置中修改。

> 禁言仅适用于普通成员；全员禁言规则目前只提供查询，不提供设置。

<a id="review"></a>

## 📨 入群申请：两种人工审批方式

申请卡片展示昵称、时间、来源、风险提示和验证内容，隐藏成员 OpenID 与申请 ID。

### ① 点击回调按钮

**点击后直接审批并在群内反馈，不需要再发送指令。**

| 按钮 | 谁能用 |
| --- | --- |
| 第一行「同意 / 拒绝」 | QQ 原生群主、群管理员 |
| 第二行「授权群管同意 / 授权群管拒绝」 | AstrBot 管理员、当前群插件群管；未配置时不显示此行 |

点击「拒绝」不填写理由。成功处理后旧按钮失效；处理中的重复点击不会重复提交。

### ② 引用通知回复

回复**机器人发出的原申请通知**，发送 `同意`、`通过`、`拒绝` 或 `拒绝 理由`，例如 `拒绝 未完成入群验证`。

> 引用审批需要 QQ 提供原通知消息 ID。若客户端未提供，请使用回调按钮或 QQ 原生群管理。按钮发送失败时通知会降级为纯文本。

2.6.0 已移除申请编号、编号审批指令和 `/群申请归零`；旧通知上的编号按钮不再生效。LLM 申请审批工具仍单独保留。

<details>
<summary>按钮权限与事件订阅</summary>

- QQ 原生群管按钮由 QQ 平台限制为管理员可点击；授权群管按钮限制指定用户，后台还会检查当前权限名单。
- 新增授权群管不会更新已发送按钮的用户列表；已撤销权限的用户会被后台拒绝。
- WebSocket 自动补充相关 Intents；Webhook 需订阅 `GROUP_JOIN_REQUEST`、`GROUP_MEMBER_ADD`、`GROUP_MEMBER_REMOVE`、`INTERACTION_CREATE`。
- 按钮遵循 [QQ 官方回调协议](https://github.com/tencent-connect/bot-docs/blob/645787a45937e5d9c4f0f61afefdffde0f38696e/docs/develop/api-v2/server-inter/message/trans/msg-btn.md)，使用随机标识绑定申请，并校验平台、群和有效期。

</details>

<a id="permissions"></a>

## 🗝️ 权限与开关

| 操作 | AstrBot 管理员 | 插件群管 | QQ 群主/管理员 | 普通成员 |
| --- | :---: | :---: | :---: | :---: |
| 禁言、解禁、状态查询、人工审批 | ✅ | ✅ | ✅ | — |
| 添加/删除插件群管 | ✅ | — | 可配置，默认关闭 | — |
| 查看/修改本群功能（分群模式） | ✅ | ✅ | ✅ | — |
| 修改全局功能开关 | ✅ | — | — | — |
| 查看群管名单与帮助 | ✅ | ✅ | ✅ | ✅ |

插件群管按群保存，使用 QQ 官方成员 OpenID，不是公开 QQ 号。

### LLM 唤醒人权限

「⑥ LLM 群管工具 → **严格审查唤醒人权限**」默认**关闭**：

- **关闭**：由模型决定调用，以机器人权限执行；不按唤醒人身份拦截。
- **开启**：五个工具均校验真实唤醒人的群管权限，普通成员无法调用，查询工具也不例外。

该开关为全局设置。工具结果交回模型自然回复；“关闭禁言和解禁的固定成功提示”只影响人工指令的成功提示，不影响错误提示和模型回复。

### 全局与分群配置

- **默认全局模式**：由 AstrBot 管理员统一设置；开启“分群功能管理”后，各群可覆盖功能开关，未覆盖项继承全局值。
- **修改开关**：`/群管功能 功能名 开启` 或 `关闭`。全局/分群模式本身在配置面板切换。
- **限制启用群**：在目标群发送 `/sid`，将 UMO 填入 `enabled_group_umos`；留空表示所有群启用，白名单始终优先。

<a id="notices"></a>

## 👋 进退群通知

欢迎和退群通知分别提供开关与模板：

| 占位符 | 进群 | 退群 |
| --- | --- | --- |
| `{member_at}` | 艾特新成员 | 显示 OpenID，无法艾特 |
| `{member_avatar}` | 显示成员头像 | 显示成员头像 |

欢迎文案可以写成 `欢迎 {member_at} 加入群聊！`；退群文案可以搭配 `{member_avatar}` 与 `有成员退出了群聊。`。头像不可用时会省略，Markdown 发送失败时降级为普通文本。

<details>
<summary>🧰 数据备份与开发验证</summary>

群管、分群开关和待审映射保存在 `data/plugin_data/astrbot_plugin_qq_group_admin/state.json`。待审映射默认有效期 30 天，审批前会清理过期项；备份时保存该文件即可。

已在 AstrBot **4.26.8** 与 **4.28.1 源码环境**通过单元测试，尚未完成真实 QQ 群联调；测试版本不等于最低兼容版本。旧版适配器可能缺少原生群管角色字段。

在插件父目录运行：

```bash
python -m unittest discover -s astrbot_plugin_qq_group_admin/tests -v
ruff check astrbot_plugin_qq_group_admin
ruff format --check astrbot_plugin_qq_group_admin
```

</details>

---

<div align="center">

喜欢的话，给云云点一颗 ⭐ 吧！

[更新日志](CHANGELOG.md) · [反馈问题](https://github.com/yun474/astrbot_plugin_qq_group_admin/issues) · [MIT License](LICENSE)

</div>
