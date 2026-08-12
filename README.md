# QQ Group Admin

面向 AstrBot 的 QQ 官方机器人群管理插件，直接适配 2026-08 新增的群禁言和入群申请接口。

## 功能

- `/禁言 @用户 [时间]`：时间可省略并使用配置中的默认禁言时长；支持 `30秒`、`10分`、`2小时`、`1天2小时`，纯数字按分钟，`0`、`解除`、`解禁`用于解除禁言。
- `qq_group_mute_member`：提供给大模型的群成员禁言/解禁工具。
- 自动转发 `GROUP_JOIN_REQUEST` 入群申请事件到对应群，展示昵称、申请时间、来源、邀请人、风险提示、验证消息和入群问答等接口实际返回的信息。
- 群管回复申请通知并发送 `同意`、`通过`、`拒绝` 或 `拒绝 理由` 即可审批。
- `qq_group_list_join_requests`：拉取当前群入群申请列表。
- `qq_group_review_join_request`：同意或拒绝指定申请。
- 普通成员进群时发送欢迎消息，普通成员退群时发送通知。
- `/添加群管 @用户`、`/删除群管 @用户`、`/群管列表`、`/群管帮助`：按群保存权限；AstrBot 管理员默认全局可用。
- 所有主要指令、通知和 LLM 工具均有独立配置开关。

## 安装

把整个 `astrbot_plugin_qq_group_admin` 目录放入 AstrBot 的 `data/plugins/`，然后重载插件或重启 AstrBot。插件不需要额外 Python 依赖。

QQ 官方平台必须满足：

1. 使用 `qq_official` 或 `qq_official_webhook` 适配器并启用群/C2C 事件。
2. 机器人已被设置为目标 QQ 群的管理员。
3. 机器人账号已获开放平台对应接口权限；否则 QQ 会返回无权限错误。

## 成员进退群通知

成员进群欢迎和退群通知分别提供开关与消息模板。为避免一堆实际上拿不到稳定数据的假占位符，模板只支持一个占位符：

| 占位符 | 进群事件 | 退群事件 |
|---|---|---|
| `{member_at}` | 生成 `<qqbot-at-user>`，通过 QQ Markdown 真正艾特新成员 | 仅显示成员 OpenID，无法艾特 |

推荐配置：

```text
成员进群：欢迎 {member_at} 加入群聊！
成员退群：有成员退出了群聊。
```

目前 `GROUP_MEMBER_REMOVE` 只提供 `group_openid`、`member_openid`、`op_member_openid` 和时间戳，没有昵称字段。新的群成员列表接口也只有成员 OpenID 与入群时间，且成员退群后已经不在群内，QQ 客户端无法再渲染对他的艾特。因此插件不提供昵称占位符，也不会假装能在退群通知中艾特对方。

WebSocket 模式会补充成员事件所需的 `GROUP_MEMBER` Intent（`1 << 24`）。如果 QQ 连接已经建立后才热重载插件，需要重载 QQ 平台或重启 AstrBot，才能让新的 Intent 生效；Webhook 模式还需在 QQ 开放平台订阅 `GROUP_MEMBER_ADD` 和 `GROUP_MEMBER_REMOVE`。

## 指令权限

| 指令/操作 | AstrBot 管理员 | 当前群的插件群管 | 普通成员 |
|---|---:|---:|---:|
| 添加/删除群管 | 是 | 否 | 否 |
| 查看群管列表 | 是 | 是 | 是 |
| 禁言/解禁 | 是 | 是 | 否 |
| 回复审批入群申请 | 是 | 是 | 否 |
| 群管理 LLM 工具 | 是 | 是 | 否 |

分群群管保存的是 QQ 官方接口提供的 `member_openid`，不是公开 QQ 号。

`default_mute_duration` 配置项控制省略时间时的默认禁言时长，初始值为 `10分`。发送 `/群管帮助` 可在群里查看完整指令、当前默认时长和入群申请回复审批方式。

## 入群申请可用信息

接口可能返回以下字段（没返回的字段会显示“未提供”或直接省略）：

- `username`：昵称
- `member_openid` / `union_openid`
- `join_request_id`
- `apply_at`：申请时间
- `apply_source`：来源
- `invited_by`：邀请人
- `risk_tips`：风险提示
- `verify_info.method`
- `verify_info.verify_message`
- `verify_info.review_qa_list[]`：问题与回答

回复审批依靠“申请通知消息 ID → 申请信息”的本地映射，默认保留 30 天，数据位于 AstrBot 的 `data/plugin_data/astrbot_plugin_qq_group_admin/state.json`。

## 接口说明

插件使用 QQ 新 OpenAPI 域名 `api.bot.qq.com`（旧的 `api.sgroup.qq.com` 已于 2026-08-10 下线），调用：

- `POST /v2/groups/{group_openid}/restrict_chat_setting`
- `GET /v2/groups/{group_openid}/join_request_list`
- `POST /v2/groups/{group_openid}/approval_join_request/{member_openid}`
- `GROUP_JOIN_REQUEST` 事件
- `GROUP_MEMBER_ADD` / `GROUP_MEMBER_REMOVE` 事件

禁言接口只能操作普通成员，无法禁言群主、群管理员或机器人；实际限制和权限以 QQ 开放平台为准。

## 开发

```bash
python -m compileall .
python -m unittest discover -s tests -v
```

仓库：[yun474/astrbot_plugin_qq_group_admin](https://github.com/yun474/astrbot_plugin_qq_group_admin)
