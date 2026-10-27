# 更新日志

本文件记录各版本的主要变化，版本号与 `metadata.yaml` 一致，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.3.3] - 2026-10-28

### 变更

- 媒体 URL 不再在本机做 DNS 解析与公网地址校验。URL 来源只交给 QQ 文件接口，由官方服务器拉取，插件不在本机下载；旧校验在异步上传路径中同步解析 DNS，会阻塞事件循环，还会拒绝本机解析不了但官方能拉取的链接。`ftp://`、`file://` 等非 http(s) 来源仍被拒绝。（[#4](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/4)）
- README 重新组织结构，新增本更新日志。（[#4](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/4)）

### 移除

- 未被使用的本机下载函数 `download_url` 及其 `httpx` 依赖（删除 `requirements.txt`）；URL 来源调用 `load_bytes()` 时直接报错。（[#4](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/4)）

## [0.3.2] - 2026-10-28

### 新增

- 配置项 `event_handler_concurrency`、`event_queue_size`、`event_handler_timeout`、`interaction_ack_concurrency`；`/qqoffice_status` 显示回调运行、排队、丢弃、超时数以及自动应答的在途与跳过数。
- 异常 `QQOfficeDuplicateAck`：同一互动重复应答时抛出。
- `metadata.yaml` 增加显示名「官机扩展」与简介。

### 变更

- 自动与手动互动应答共用 `(机器人身份, interaction_id)` 资格；开启 `interaction_auto_ack` 时，按钮/快捷菜单的手动应答会抛 `QQOfficeDuplicateAck`，需要自定义 code 请关闭自动应答。
- 群聊/单聊 `msg_seq` 改为按回复键单调递增并由同身份实例共享，首个序号取随机基数以避开本体的 1~10000 随机序号；`c2c.stream_send` 的 `msg_seq` 默认改为不传，首片自动分配、续片沿用。
- 事件回调改为并发上限 + 有界队列，每个回调独立 Task 并沿用投递时的上下文，只等待协程回调；`event_handler_timeout` 默认 0（不限时）。
- 最低 Python 版本改为 3.12，与 AstrBot 验证基线一致。

### 修复

- 自动应答排队期间机器人状态被回收，导致同一互动重复应答。
- 同一 botpy Token 并发过期检查与 401 刷新造成刷新风暴；刷新完成后才到达的 401 不再重复刷新。
- 并发发送时 `msg_seq` 碰撞。
- 事件回调任务数量无上限。
- 插件热重载后残留旧的 HTTP 响应捕获补丁。
- 回调内卸载插件时等待自身；卸载失败或被取消后无法重试；初始化失败时清理不完整。

（[#3](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/3)）

## [0.3.1] - 2026-09-30

### 新增

- 版本化公开门面 `get_service(api_version=1)`：状态快照、能力列表、`wait_ready` 与稳定错误码；通过 `instance` / `for_event` 绑定视图，开放事件订阅与消息构建器。

### 变更

- 服务关闭或插件重载后旧门面永久失效，关闭状态不可逆；状态查询不联网、不刷新路由、不输出凭据。
- 旧入口 `ready` / `status` / `wait_ready` 保留兼容。

（[#2](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/2)）

## [0.3.0] - 2026-09-06

### 新增

- N 实例路由：每个 AstrBot 平台配置 ID 绑定一个机器人身份（AppID + 生产/沙箱），`instance(id)` 主动调用、`for_event(event)` 按来源回复；同身份实例共享配额、被动回复计数、互动去重与引用缓存，不同身份隔离。
- 实例级事件订阅；事件携带不可变来源，旧代次事件明确失败。
- 配置项 `certified_bots`（按 AppID 覆盖认证限额）与 `ref_max_entries`（引用索引全局上限）。
- 设计文档 `docs/MULTI_INSTANCE_DESIGN.md`。

### 移除

- 自建 HTTP/token 兜底路径，请求一律走本体适配器当前的 botpy HTTP。
- 配置项 `prefer_new_domain`、`sandbox`；域名与环境改由各适配器自身配置决定。

（[#1](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/1)）

## [0.2.0] - 2026-09-05

### 新增

- 群成员列表与详情、批量移除、群黑名单。
- 单聊独立流式分片（`/stream_messages`），群聊/单聊分片上传控制（预上传、分片确认、合并）。
- 频道管理：频道与子频道、成员、身份组、权限、禁言、消息与私信、公告、精华、日程、表情表态、音频、论坛、接口授权；频道与私信的富消息（图片 URL）和按钮二次确认。
- 更多互动、群、频道、论坛、音频、审核、消息与成员事件，保留原始 payload 并规范化标识符。
- 接口覆盖文档 `docs/API_COVERAGE.md`。

### 修复

- 鉴权响应、配置读取、限流、原始事件保留与补丁生命周期问题。
- 保留 SDK 的成功空响应与数组响应，区分网关错误与超时。

（[#1](https://github.com/piexian/astrbot_plugin_qqoffice_expand/pull/1)）

## [0.1.5] - 2026-08-31

### 修复

- 自动审批策略改用官方 `group_action` 嵌套结构。
- FRIEND_ADD 等事件补充 openid 兜底。
- `send_rich` 强制 markdown 与 content 互斥，避免 22006。
- 补充 8 个错误码的排查建议。

## [0.1.4] - 2026-08-31

### 修复

- `payload_id` 取错字段导致 `event_id` 被动回复被拒，改为取网关事件 id。

## [0.1.3] - 2026-08-31

### 修复

- 按钮回调事件丢失 `data.resolved`，订阅方拿不到按钮数据。

## [0.1.2] - 2026-08-31

### 修复

- 经适配器发起的调用全部失败：不再透传 per-request timeout（与 botpy 内部注入的 timeout 冲突）。

## [0.1.1] - 2026-08-31

### 变更

- intents 扩展位改为在适配器构造时注入，identify 直接携带，启动与重载自动生效。
- 按会话实际 intents 判断缺位并去重告警；identify 被 4013/4014 拒绝时自动剔除扩展位，保住基础连接。

### 修复

- 追加 intents 位时误报「已在会话」告警。

## [0.1.0] - 2026-08-31

### 新增

- 首个版本，供其他 AstrBot 插件调用的 QQ 官方扩展能力：
  - 命名方法：群撤回、禁言、入群审批、自动审批策略；单聊撤回、发送、输入中状态、互动召回；自定义菜单、指令面板；频道消息。
  - 通用 `call()` 通道：表驱动频控、主动消息配额、`msg_seq` 生成、被动窗口自动降级、token/频控自动重试、HTML 网关页识别、错误码排查建议。
  - 扩展事件订阅与按钮自动应答，REFIDX 引用索引与 `file_info` 缓存，`send_rich` 富消息，诊断指令 `/qqoffice_status`。
