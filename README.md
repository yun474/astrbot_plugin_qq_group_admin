# QQ Group Admin

面向 AstrBot 的 QQ 官方机器人群管理插件，直接适配 2026-08 新增的群禁言和入群申请接口。

## 功能

- `/禁言 @用户 [时间]`：时间可省略并使用配置中的默认禁言时长；支持 `30秒`、`10分`、`2小时`、`1天2小时`，纯数字按分钟，`0`、`解除`、`解禁`用于解除禁言。
- `/解禁 @用户`：解除一个或多个被艾特成员的禁言，与 `/禁言 @用户 解除` 等效。
- `qq_group_mute_member`：提供给大模型的群成员禁言/解禁工具。
- `qq_group_unmute_member`：提供给大模型的独立群成员解禁工具。
- `/禁言状态`：查看全员禁言模式、定时/周期规则及当前被禁言成员（含昵称、OpenID 和到期时间）。
- `qq_group_get_mute_status`：提供给大模型的群禁言状态查询工具。
- 自动转发 `GROUP_JOIN_REQUEST` 入群申请事件到对应群，展示昵称、申请时间、来源、邀请人、风险提示、验证消息和入群问答等接口实际返回的信息。
- 群管回复申请通知并发送 `同意`、`通过`、`拒绝` 或 `拒绝 理由` 即可审批。
- `qq_group_list_join_requests`：拉取当前群入群申请列表。
- `qq_group_review_join_request`：同意或拒绝指定申请。
- 普通成员进群时发送欢迎消息，普通成员退群时发送通知。
- `/添加群管 @用户`、`/删除群管 @用户`、`/群管列表`、`/群管帮助`：按群保存权限；AstrBot 管理员默认全局可用。
- 所有主要指令、通知和 LLM 工具均有配置开关；禁言和解禁 LLM 工具可分别开关。

LLM 群管工具以机器人自身权限调用 QQ 接口。工具本身不直接发送固定成功通知，而是始终把执行结果交回 LLM，让 Agent Loop 继续并由模型自然回复。开启 `silent_mute_success_notice` 后，仅关闭 `/禁言`、`/解禁` 指令直接产生的“已禁言/已解禁”消息；错误提示和 LLM 自然回复不受影响。

插件兼容 AstrBot 4.26.0 的 `ContextWrapper` 工具调用上下文，同时保留旧版直接传入消息事件的调用方式。

`/禁言` 不依赖 AstrBot 的位置参数绑定，而是从消息组件和文本中自行识别目标与时长。它兼容人工艾特以及 LLM Executor 生成的 At 组件、`@member_openid` 文本，不限制 AstrBot 版本。

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

`default_mute_duration` 配置项控制省略时间时的默认禁言时长，初始值为 `1分`。发送 `/群管帮助` 可通过 Markdown 帮助卡片查看完整指令、当前默认时长和入群申请回复审批方式；Markdown 发送失败时 AstrBot 会自动降级为纯文本。

QQ 当前的禁言状态查询接口会返回全员禁言规则，但对应的设置接口只支持成员级禁言，暂不支持由机器人修改全员禁言模式或定时/周期规则。本插件因此只展示这些规则，不提供无法真正生效的规则配置项。

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
