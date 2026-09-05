# QQ 官方接口覆盖与调用约定

核对日期：2026-09-05。AstrBot 源码基线：`4.28.0-beta.1` / `2aec78272cb8`；SDK：`qq-botpy 1.2.1`。

插件提供其他插件可调用的命名接口和可卸载的适配器补丁。AstrBot 已有普通消息收发、富媒体上传、频道当前事件回复等能力；这些高层流程继续由 AstrBot 负责。SDK 已提供部分频道接口，本插件将它们统一到现有鉴权、错误处理和能力注册入口。

## 通用约定

- `svc.call()` 和命名方法保留官方响应对象或数组；成功的空响应统一为 `{}`。不能把数组接口的结果当成字典。
- `extra` 用于补充官方字段；GET 合并进查询参数，写接口合并进 JSON。频道撤回的 `extra` 合并进查询参数；`menu_put` 的 `extra` 沿用原约定合并进 `menu`。
- 不自动遍历所有分页、不自动拆分批量管理请求，也不把部分失败转换成全成功。调用方决定操作范围并检查返回结果。
- 所有公开方法自动注册为 `group.*`、`c2c.*`、`guild.*`、`manage.*`，可用 `svc.invoke()` 调用。
- 方法目录在插件构造时就注册；缺少 QQ 客户端时调用会明确报错。自建 HTTP 兜底通过 AstrBot 的 `Context.get_config()` 读取官方平台配置。
- 文档明确给出频率的接口使用对应限流。未注明频率的旧频道管理接口使用默认桶，仍由服务端权限和频控规则裁决。

## 私聊与群聊

| 能力 | 插件入口 | 契约与官方来源 |
| --- | --- | --- |
| 普通与富消息 | `c2c.send`、`group.send`、`svc.send_rich` | [C2C](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)、[群聊](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)：保留现有封装，支持 Markdown、键盘和引用字段 |
| 撤回 | `c2c.recall`、`group.recall` | [单聊撤回](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages_message_id.delete.html)、[群聊撤回](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages_message_id.delete.html)：10 QPS，2 分钟内；群管理员可撤普通成员消息 |
| 输入状态、互动召回 | `c2c.input_notify`、`c2c.wakeup` | 复用单聊发送接口；输入状态最长 60 秒，keepalive 句柄由调用方取消 |
| 独立流式分片 | `c2c.stream_send` | [stream_messages](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_stream_messages.post.html)：50 QPS，`index` 从 0 递增，结束片 `input_state=10`；首片响应 `id` 用作续片 `stream_msg_id` |
| 普通上传 | `c2c.upload`、`group.upload` | [单聊 files](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_files.post.html)、[群聊 files](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)：50 QPS；保留现有 URL/本地上传入口 |
| 分片上传控制 | 两个命名空间都有 `upload_prepare`、`upload_part_finish`、`upload_complete` | [单聊预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_id_upload_prepare.post.html)、[单聊分片确认](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_id_upload_part_finish.post.html)、[群预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.html)、[群分片确认](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_part_finish.post.html)：准备和确认 10 QPS，合并走 files |

AstrBot 基线已有 `send_streaming(generator)`，使用 `/messages` 上的旧 `stream` 字段。`c2c.stream_send` 补充新的 `/stream_messages` 协议，不替换 AstrBot 的生成器调度，也不据此认定旧协议失效。同一流的续片不重复消耗本插件的普通被动回复次数；`msg_seq` 默认 1，可显式设置，并在同一流内保持一致。首片被动窗口失效时直接报错，由调用方决定是否显式开启主动流，避免首片与续片使用不同的引用参数。

分片控制接口供需要控制上传流程的插件使用：计算校验值 → `upload_prepare` → 按返回的 `parts` 向预签名 URL 执行 PUT → 每片 `upload_part_finish` → `upload_complete`。大小字段按官方要求发送字符串，`md5_10m` 取前 **10002432 字节**。预签名 PUT、并发和重试由调用方依据 `upload_config` 管理；普通发送仍可使用 AstrBot 的现有完整分片上传流程。

主动配额按场景分开：[官方规则](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)中群聊认证主体 60/分钟、未认证 30/分钟；C2C 认证主体 10/秒、未认证 5/秒且 30/分钟。两种场景各有单关系 20/分钟、1000/天限制。频道不套用这些 QQ 私聊/群聊配额，文字子频道的 5/秒限制按子频道计数；频道可配置主动额度及私信每日额度由服务端裁决。

## 群管理

| 能力 | 插件入口 | 契约与官方来源 |
| --- | --- | --- |
| 群信息、机器人状态 | `group.info`、`group.bot_state` | [群信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html)、[群内状态](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_bot_state.get.html)，各 30 QPM |
| 申请列表、审批 | `group.join_requests`、`group.join_approve` | [申请列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_join_request_list.get.html) 30 QPM，`limit` 默认 20、最大 50；[审批](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_approval_join_request_member_openid.post.html) 60 QPM，回传对应 `join_request_id`，拒绝时可附理由和拉黑 |
| 禁言 | `group.get_mute`、`set_mute`、`mute_member`、`unmute_member` | [查询](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.get.html) 30 QPM；[设置](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.post.html) 60 QPM；时间为 RFC3339，最长 30 天 |
| 自动审批策略 | `strategy_list/create/update/delete/execute/whitelist` | [策略列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy.get.html)及其相邻五个接口均已封装；[更新策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id.patch.html)使用 `group_action`；[执行策略](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id_execute.post.html)为异步操作，返回成功不代表扫描完成 |
| 成员列表 | `group.members` | [官方文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_members.get.html)：60 QPM；仅 cursor 分页，每页最多 30 人，响应 `members/next_cursor` |
| 成员详情 | `group.member_info` | [官方文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_members_member_openid.get.html)：30 QPM，含昵称、角色、入群时间等 |
| 批量移除 | `group.remove_members` | [官方文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_batch_remove_members.post.html)：30 QPM，最多 20 人；检查 `add_to_member_blacklist_fail_openids` |
| 黑名单列表 | `group.blacklist` | [官方文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_member_blacklist.get.html)：30 QPM，`limit` 默认 20、最大 100，响应 `users/next_cursor` |
| 黑名单增删 | `group.set_blacklist` | [官方文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_member_blacklist.post.html)：60 QPM，`op=add/del`，最多 20 人；add 要求目标已不在群中，检查 `fail_openids` |

成员列表/详情、批量移除、黑名单两个接口目前标注内邀、白名单可用；未授权时保留 `11253` 错误。审批和禁言需要机器人是群管理员。

禁言批量上限存在 10/20 人的文档版本差异；插件不强制截断批量列表，调用方应以自己应用的实际权限和服务端结果为准。

## 频道管理

以下入口均位于 `svc.guild`；`manage.me` 为机器人全局信息。频道成员使用 `user_id`，与 QQ 群的 `member_openid` 分开。

| 能力 | 命名方法 | 主要契约与官方来源 |
| --- | --- | --- |
| 机器人信息、频道列表 | `manage.me`、`guilds` | [机器人信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/users_me.get.html)、[频道列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/users_me_guilds.get.html)：50 QPS；列表为数组，`before/after/limit`，limit 最大 100 |
| 频道/子频道查询 | `guild_info`、`channels`、`channel_info`、`online_nums` | [频道详情](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/guilds_guild_id.get.html)、[子频道列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/guilds_guild_id_channels.get.html)、[子频道详情](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/channels_channel_id.get.html)均 50 QPS；[在线人数](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/get_online_nums.html)用于音视频/直播子频道 |
| 子频道维护 | `channel_create/update/delete` | [创建](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/guilds_guild_id_channels.post.html)、[修改](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/channels_channel_id.patch.html)、[删除](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/channels_channel_id.delete.html)：50 QPS，私域、管理员 |
| 成员 | `members`、`member_info`、`member_remove`、`role_members` | [成员列表](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/member/get_members.html)使用 after=user.id、limit 1..400，数组为空才结束，跨页可能重复；[角色成员](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/member/get_role_members.html)使用 start_index，响应 data/next；[详情](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/member/get_member.html)、[移除](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/member/delete_member.html) |
| 身份组 | `roles`、`role_create/update/delete`、`member_role_add/remove` | [身份组列表](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/get_guild_roles.html)、[创建](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/post_guild_role.html)、[修改](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/patch_guild_role.html)、[删除](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/delete_guild_role.html)；[添加成员](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/put_guild_member_role.html)与[移除成员](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/delete_guild_member_role.html)在 role_id=5 时须传 channel_id |
| 子频道权限 | `member_permissions`、`set_member_permissions`、`role_permissions`、`set_role_permissions` | [用户权限](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/channel_permissions/put_channel_permissions.html)、[角色权限](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role-group/channel_permissions/put_channel_roles_permissions.html)：add/remove 为字符串位图，冲突时 remove 优先 |
| 禁言与消息设置 | `mute`、`mute_member`、`message_setting` | [全员](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/speak/patch_guild_mute.html)、[单人](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/speak/patch_guild_member_mute.html)、[批量](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/speak/patch_guild_mute_multi_member.html)：时间为秒数字符串，'0'解除；批量返回成功 user_ids；[消息设置](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/speak/setting/message_setting.html)可查询主动推送配置 |
| 消息与私信 | `send`、`recall`、`dm_create/send/recall` | [频道发送](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/send.html)、[撤回](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/recall.html)、[私信](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/dms.html)：dm_create 返回的 guild_id 为私信会话 ID，不能用 C2C OpenID 替代 |
| 公告 | `announce_create/delete` | [创建](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/announces/post_guild_announces.html)、[删除](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/announces/delete_guild_announces.html)：推荐子频道最多 3 个、整体替换；删除推荐公告用 message_id='all' |
| 精华 | `pins`、`pin_add/remove` | [查询](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/pins/get_pins_message.html)、[添加](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/pins/put_pins_message.html)、[移除](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/pins/delete_pins_message.html)：最多 20 条，移除时 'all' 清空 |
| 日程 | `schedules`、`schedule_info/create/update/delete` | [列表](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/schedule/get_schedules.html)为数组；[创建](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/schedule/post_schedule.html)和[修改](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/schedule/patch_schedule.html)使用 `{schedule: {...}}`，时间为毫秒；[详情](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/schedule/get_schedule.html)、[删除](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/schedule/delete_schedule.html) |
| 表情表态 | `reaction_add/remove/users` | [官方文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/emoji.html)：查询响应 users/cookie/is_end，limit 首次可设、最大 50 |
| 音频 | `audio_control`、`mic_on/off` | [控制](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/audio/audio_control.html)、[上麦](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/audio/put_mic.html)、[下麦](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/audio/delete_mic.html)：需音频权限 |
| 论坛 | `threads`、`thread_info/create/delete` | [列表](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/forum/get_threads_list.html)、[详情](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/forum/get_thread.html)、[发表](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/forum/put_thread.html)、[删除](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/forum/delete_thread.html)：私域；发表使用 PUT，format 1文本/2HTML/3Markdown/4JSON |
| 接口授权 | `api_permissions`、`api_permission_demand` | [查询](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/api_permissions/get_guild_api_permission.html)、[申请](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/api_permissions/post_api_permission_demand.html)：申请会发送授权链接，默认每频道每天 3 条 |

频道单条消息查询和历史列表本次仅找到[官方 SDK 文档](https://bot.q.qq.com/wiki/develop/pythonsdk/api/message/get_message.html)，没有据此推定当前开放范围或新增历史分页契约，因此未增加专用方法。已具备权限的调用方仍可使用 `svc.call()`。

## 机器人菜单、面板与互动

`manage.menu_get/menu_put` 已对应[全局菜单](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_menu.put.html)，仅 C2C；读 30 QPM、写 5 QPM。

`manage.panel_list/create/get/update/delete/target` 已对应[指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.post.html)：c2c/group/channel/dm 四种 scope，只有前两者能指定关联用户或群。读 30 QPM、写 10 QPM、修改关联对象 60 QPM。

`INTERACTION_CREATE` 完整保留消息反馈、授权、切换模型等字段。自动应答只针对 type=11/12；其余事件仍分发，按需由调用方通过 `manage.interaction_ack` 应答。[互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)、[响应接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/interactions_interaction_id.put.html)。按钮二次确认用 `svc.btn(..., modal={...})`，字段进入 `action.modal`；分组标识可通过 `group_id` 传入。

## 事件补丁与边界

- QQ 群加入、退出、申请事件的 `member_openid` 会归一到 `ev.member_openid`。
- 频道创建/变更/删除的 `d.id` 是频道 ID；子频道对应事件的 `d.id` 是子频道 ID。原始字段完整保存在 `ev.raw`，SDK 包装对象仍保留在 `ev.obj`。
- 频道用户归一为 `ev.user_id`；表态和撤回等目标消息归一为 `ev.message_id`。撤回从嵌套 `message` 读取频道、作者和消息 ID；网关最外层 ID 独立保留为 `payload_id`。
- 补丁同时覆盖已有 WS state 和 Webhook helper 缓存的 state；重复挂载不叠加，卸载恢复本插件拥有的处理器与 parser。
- 新增频道成员、普通频道消息/撤回、私信撤回、论坛、开放论坛、音频和音视频成员事件。频道扩展位按专属订阅者启用，`on_any` 不自动申请全部私域权限；首次订阅后需让 WS 重连或重载适配器，identify 才会携带新位。权限降级不剔除 AstrBot 已有的 1<<12、1<<25、1<<30。
- 事件位以[官方 Intents 总表](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html)为基线；[开放论坛](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/forum/open_forum.html)和[音视频成员事件](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/audio_or_live_channel_member.html)另由独立页面与 SDK 核对。
- `GROUP_JOIN_REQUEST` 的 1<<24/1<<25 标注存在文档版本差异，目前保留既有处理方式；默认群成员事件配置会另外启用 1<<24。
- 不替换 AstrBot 已有普通消息 handler，不迁移其持久会话格式。频道私信主动发送请明确使用 `guild.dm_send(dm_guild_id, ...)`；`send_rich(event, ...)` 可从当前事件识别频道/私信，避免误走 QQ 群/C2C。

## 调用示例

```python
async def list_group_members(svc, group_openid):
    cursor = ""
    while True:
        page = await svc.group.members(group_openid, cursor=cursor)
        for member in page["members"]:
            yield member
        cursor = page.get("next_cursor", "")
        if not cursor:
            break


async def remove_and_report(svc, group_openid, member_openids):
    result = await svc.group.remove_members(
        group_openid, member_openids, add_to_member_blacklist=True
    )
    return result["remove_members_result"], result.get("add_to_member_blacklist_fail_openids", [])


async def stream_reply(svc, user_openid, request_msg_id, first_text, remaining_text):
    first = await svc.c2c.stream_send(user_openid, first_text, msg_id=request_msg_id)
    return await svc.c2c.stream_send(
        user_openid, remaining_text, msg_id=request_msg_id,
        stream_msg_id=first["id"], index=1, input_state=10,
    )


async def reply_in_guild_dm(svc, dm_guild_id, message_id, content):
    return await svc.guild.dm_send(dm_guild_id, content, msg_id=message_id)
```

## 验证

在已安装插件依赖的 AstrBot Python 环境中执行：

```sh
python3 -B tests/offline_test.py
python3 -B tests/intents_patch_test.py
python3 -B tests/api_contract_test.py
```

这些是离线契约与生命周期检查，使用模拟 HTTP/网关输入，不代表真实机器人已取得接口权限或完成线上群管理联调。

2026-09-05 另在 Windows Launcher 的 AstrBot `4.28.0-beta.1`、Python `3.12.13`、botpy `1.2.1` 环境完成独立数据目录的真实启动验证：插件加载、96 个方法与诊断指令注册、WebUI 就绪、QQ 适配器构造期 intents 注入、插件热重载、后台任务停止与补丁还原均通过。业务 HTTP 使用模拟响应，未连接真实 QQ 机器人接口。

同日另用实例中已配置的真实机器人完成只读 API 联调：机器人信息、菜单、四种 scope 面板查询、群信息、机器人群内状态、禁言状态、入群申请、审批策略列表均成功。群成员列表、成员详情、黑名单查询返回 `11253`；自建 HTTP 与 botpy 路径一致，测试群中机器人角色为 admin。频道列表返回空数组，未继续验证需要频道目标的管理接口。测试未调用业务写接口；token 的 HTTP 200 成功响应误判为错误的问题已修正并补充缓存回归。
