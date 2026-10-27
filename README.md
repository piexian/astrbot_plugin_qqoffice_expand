# 官机扩展（astrbot_plugin_qqoffice_expand）

为 AstrBot 的 QQ 官方机器人适配器补充官方 API 与扩展事件，**供其他插件调用**。普通消息收发仍由 AstrBot 负责；本插件提供按实例路由的调用入口、频控与配额、扩展事件订阅和统一的错误处理，支持任意数量的适配器实例同时运行。

- [功能](#功能)
- [环境要求](#环境要求)
- [安装](#安装)
- [接入（供其他插件）](#接入供其他插件)
- [配置](#配置)
- [运行机制](#运行机制)
- [已知限制](#已知限制)
- [诊断与联调](#诊断与联调)
- [项目结构](#项目结构)

全部接口与调用示例见 [docs/API_COVERAGE.md](docs/API_COVERAGE.md)，多实例设计见 [docs/MULTI_INSTANCE_DESIGN.md](docs/MULTI_INSTANCE_DESIGN.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 功能

| 类别 | 内容 |
| --- | --- |
| 多实例路由 | `service.instance(id)` 按配置实例主动调用，`service.for_event(event)` 按事件来源回复；同 AppID 同环境的实例共享配额与去重，不同机器人完全隔离 |
| 群管理 | 成员列表与详情、批量移除、黑名单、撤回、禁言、入群审批、自动审批策略、群信息与机器人状态 |
| 单聊 | 独立流式分片、撤回、全参数发送、输入中状态、互动召回、富媒体上传 |
| 上传 | 群聊/单聊富媒体上传；分片预上传、分片确认与合并 |
| 频道 | 频道与子频道、成员、身份组、权限、禁言、消息与私信、公告、精华、日程、表情表态、音频、论坛、接口授权 |
| 管理 | 自定义菜单、指令面板、互动应答 |
| 富消息 | `send_rich`：markdown、按钮键盘、引用回复；上传与发送在同一操作上下文内完成 |
| 通用调用 | `call()` 访问任意官方端点：端点频控、主动消息配额、被动回复窗口与自动降级、token/频控自动重试、网关错误页识别、错误码排查建议 |
| 扩展事件 | 全局与实例两级订阅；事件带不可变来源；按钮/快捷菜单 3 秒内自动应答 |
| 引用索引 | REFIDX 按机器人身份持久化，全局容量上限 |
| 诊断 | `/qqoffice_status` 查看实例、身份、挂载、订阅、频控与事件处理状态 |

## 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | >= 3.12 | 与 AstrBot 基线一致 |
| AstrBot | 4.28.0-beta.1（commit `2aec78272cb8`）验证通过 | 多实例路由依赖 platform manager 的运行实例索引等内部结构；未验证的后续版本不作承诺 |
| botpy | 1.2.1 | AstrBot 自带，无需单独安装 |

仅支持 QQ 官方机器人适配器：`qq_official`（WebSocket）与 `qq_official_webhook`（Webhook），实例数量不限。

## 安装

在 AstrBot 插件页右下角点击加号，选择从链接安装：`https://github.com/piexian/astrbot_plugin_qqoffice_expand`。安装后无需重启 AstrBot。

### intents 与适配器重载

- 扩展事件需要的 intents 位只能随 WebSocket identify 下发。插件在适配器实例化时自动注入，新建或重载的适配器会直接带上。
- 已建立的 WebSocket 会话缺少所需位时，在管理面板重载对应适配器即可；Webhook 不受影响。
- 运行中首次订阅频道类事件，或修改 `enable_group_member_events` 后，也需要重载一次适配器。`on_any` 不会自动申请全部权限。
- 未在 QQ 开放平台开通对应权限、identify 被网关 4013/4014 拒绝时，插件会按实例剔除扩展位保住基础连接并记录日志，其他实例不受影响；开通权限后重载适配器即可。
- `/qqoffice_status` 中的 `verdict` 显示每个实例会话实际携带的 intents。

## 接入（供其他插件）

### 发现与绑定

消费方通过 `get_registered_star` 按插件名找到本插件，再取 `get_service(api_version=1)` 公开门面。AstrBot 不保证插件加载顺序，标准做法是在 `initialize()` 里先尝试绑定，再在加载广播里重试。**不要在 `initialize()` 里阻塞等待**，否则会卡住后续插件加载。

```python
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

PLUGIN = "astrbot_plugin_qqoffice_expand"

class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.qq = None          # 绑定的服务门面
        self._unsubs = []       # 本插件在服务上的订阅，卸载时解绑

    def _try_bind(self) -> bool:
        """绑定目标服务并注册订阅；重复调用（服务未变）幂等。"""
        meta = self.context.get_registered_star(PLUGIN)
        if not (meta and meta.activated and meta.star_cls):
            return False                    # missing / disabled / no_instance
        getter = getattr(meta.star_cls, "get_service", None)
        if not callable(getter):
            return False                    # unsupported_api：需升级提供者
        try:
            service = getter(api_version=1)   # 不兼容时抛 code=unsupported_version
        except RuntimeError as exc:
            logger.warning("无法接入 qqoffice SDK：%s", getattr(exc, "code", "sdk_error"))
            return False
        if not service.get_status()["ready"]:
            self._unbind()
            return False
        if self.qq is service:
            return True   # 同一服务实例已绑定，不重复订阅
        self._unbind()      # 服务重载后是新实例（instance_id 不同）：先解绑旧订阅
        self.qq = service
        self._unsubs.append(
            self.qq.on("INTERACTION_CREATE", self._on_button))  # 全局订阅
        return True

    def _unbind(self):
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        self.qq = None

    async def initialize(self):
        if not self._try_bind():
            logger.info("qqoffice_expand 尚未就绪，等待其加载广播（未安装则保持降级）")

    @filter.on_plugin_loaded()
    async def _on_qqoffice_loaded(self, metadata):
        """框架广播：任一插件 initialize 成功后触发，按名称识别目标。"""
        if getattr(metadata, "name", "") == PLUGIN:
            if self._try_bind():
                logger.info("已接入 qqoffice_expand")

    @filter.on_plugin_unloaded()
    async def _on_qqoffice_unloaded(self, metadata):
        if getattr(metadata, "name", "") == PLUGIN:
            self._unbind()   # 服务重载/卸载：解绑旧订阅，等下一次广播

    async def terminate(self):
        self._unbind()

    async def _on_button(self, ev):
        # 仅处理按钮/快捷菜单（type=11/12）；反馈、授权等互动不自动回复。
        interaction_type = ev.raw.get("type") or (ev.raw.get("data") or {}).get("type")
        if interaction_type not in (11, 12):
            return
        # 仅处理有群/C2C 回复目标的互动（频道按钮无此被动回复通道）。
        if ev.scene not in ("group", "c2c"):
            return
        if not (ev.interaction_id and (ev.group_openid or ev.user_openid)):
            return
        qq = self.qq.for_event(ev)   # 绑定来源机器人（代次核验 + 事件目标）
        await qq.send_rich(content="已收到按钮操作")
```

### 服务状态与生命周期

```python
status = service.get_status()
# {"api_version": 1, "instance_id": "…本次加载唯一", "state": "ready",
#  "ready": True, "reason": None,
#  "instances": {"qq_sales": {"adapter": "qq_official", "mode": "ws",
#      "appid": "…", "environment": "production", "generation": 1,
#      "transport_ready": True, "closing": False, …}}}
cap = service.capabilities()   # {"api_version": 1, "features": ["qq.instance", …]}
```

- `state` 取值为 initializing / ready / unavailable / closing / closed，`ready` 仅在 state 为 ready 时为真。服务就绪与平台连接就绪是两回事，平台是否可用看 `instances` 中的 `transport_ready` 和 `closing`。
- 状态查询只读本地快照：不联网、不刷新路由、不含 token/secret/配置。快照可能滞后，实际调用仍会核验实例与代次。
- `await service.wait_ready(timeout=5)` 成功时返回状态快照，超时抛 `TimeoutError`，服务正在关闭或已关闭时抛 `code="service_closed"` 的 RuntimeError。不要无限等待，也不要在 `initialize()` 里等。
- 插件重载后旧服务永久失效（closed），新服务的 `instance_id` 不同，可据此识别换代。
- 插件是否安装、是否启用、是否支持门面要分别判断，不能从服务 ready 推断。旧入口 `star_cls.ready` / `star_cls.status()` / `star_cls.wait_ready()` 保留兼容，新接入请使用门面。

### 主动调用：指定实例

```python
qq = service.instance("qq_sales")                    # AstrBot 平台配置 ID
await qq.group.send(group_openid, "通知")            # 主动消息
await qq.invoke("group.recall", openid, message_id)  # 按名称调用命名方法
resp = await qq.call("GET", "/v2/groups/{group_openid}/info",
                     path_params={"group_openid": openid}, scene="group")
await qq.c2c.stream_send(openid, "分片", msg_id=mid)
await qq.manage.me()
```

- 主动调用必须明确指定实例，没有「全局默认机器人」。
- `service.instance(id)` 要求该平台已在运行中，否则立即抛 `InstanceUnavailable`。插件 `initialize()` 早于平台实例化，请在 `@filter.on_platform_loaded()` 钩子、事件回调或后台任务里创建视图。平台已加载不代表 token 已登录，未登录时调用抛 `TransportNotReady`。
- 视图在创建时固定机器人身份：同一 ID 重载后自动跟随；同一 ID 改绑到另一个 AppID 后，旧视图调用抛 `InstanceIdentityChanged`，需要重新 `service.instance(id)`。

### 事件回复：绑定来源

```python
qq = service.for_event(astr_message_event)   # AstrBot 原生事件：核验 platform_id 与 event.bot
qq = service.for_event(qqoffice_event)       # 扩展事件：不可变来源 + 代次核验
await qq.send_rich(content="纯文本回复")      # 视图已持有事件目标
await qq.send_rich(markdown=service.md("签到成功"))
# 也可显式指定目标：qq.send_rich(scene="group", target_openid=..., content=...)
```

- `msg_id` 取事件消息 id（含频道消息），走被动回复窗口。
- `event_id` 只在事件类型属于官方允许集合（GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE / INTERACTION_CREATE / C2C_MSG_RECEIVE / FRIEND_ADD）时自动推断，并按场景校验；其他事件（如群成员进出、普通群消息）不伪造 `event_id`，按主动消息发送并消耗配额。
- 显式传入的参数始终覆盖自动推断的字段。
- 适配器已重载后再用旧事件回复抛 `StaleSourceEvent`。

### 富消息与引用回复

```python
qq = service.for_event(event)
await qq.send_rich(
    markdown=service.md("# 签到成功\n积分 **50**"),
    keyboard=service.kb().row(service.btn("签到", data="/签到", enter=True)).build(),
)
await qq.send_rich(content="收到",
                   reference=service.reference(service.ref_from_event(event)))
```

`ref_from_event` 也支持 AstrBot 原生消息：从本体保留的原始 payload（`message.raw_data`）提取 msg_idx，按机器人命名空间入库。适配器已重载的旧事件不会从新机器人的命名空间取引用。

### 订阅扩展事件

```python
unsub = service.on("GROUP_MEMBER_ADD", handler)                       # 全部实例
unsub = service.instance("qq_sales").on("GROUP_MEMBER_ADD", handler)  # 仅该实例
unsub = service.on_any(handler)                                       # 全部扩展事件
unsub()                                                               # 卸载时解绑
```

回调接收 `QQOfficeEvent`（`ev.type`、`ev.raw`、`ev.source`、`ev.scene`、`ev.group_openid` 等），同步或异步函数均可。回调异常只记录日志，不影响其他订阅者。实例级订阅随同身份重载继续生效，改绑身份后不会转给新机器人。

### 异常

异常均继承 `QQOfficeError`，可按类名判断：

| 异常 | 含义 | 建议 |
| --- | --- | --- |
| `InstanceUnavailable` | 实例被禁用、删除或尚未加载 | 不重试，降级处理 |
| `InstanceIdentityChanged` | 同一 ID 已改绑其他 AppID | 重新 `service.instance(id)` |
| `StaleSourceEvent` | 事件或操作属于已重载的旧代次 | 丢弃，不要重放 |
| `TransportNotReady` | 实例已加载但尚无可用 HTTP（如未登录） | 稍后再试 |
| `QQOfficeDuplicateAck` | 同一互动已应答或正在应答 | 不要重复应答 |
| `QQOfficeAPIError` | 官方接口返回业务错误，`code` 为错误码，`advice` 为排查建议 | 按 `code` 处理 |
| `QQOfficeGatewayError` | 平台网关错误页或 5xx；写操作结果未知，插件不自动重发 | 自行判断是否重试 |
| `QQOfficeNotSupported` | 当前场景不支持该能力，或参数不合法 | 降级或修正参数 |

前四项是路由错误：请求尚未进入 SDK 就被拒绝，插件不会换实例重放。门面自身的错误是带 `code` 的 RuntimeError，`service_closed` 表示服务已关闭，`unsupported_version` 表示 API 版本不兼容。

## 配置

**频控与回复**

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `certified_bot` | bool | false | 全局默认认证限额：群聊认证 60/分钟、未认证 30/分钟；C2C 认证 10/秒、未认证 5/秒且 30/分钟 |
| `certified_bots` | template_list | [] | 按 AppID 覆盖认证限额（每条 appid + certified）；未列出的用全局默认 |
| `auto_degrade_proactive` | bool | true | 被动回复窗口超时或超次时自动降级为主动消息；关闭后直接抛错 |
| `dot_replace` | bool | false | 群聊文本点号替换，规避 40054010 误拦 |
| `retry_max` | int | 3 | 频控自动等待重试上限；401/11244 的 token 重试固定 1 次 |

**事件与应答**

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `interaction_auto_ack` | bool | true | 按钮/快捷菜单（type=11/12）自动以 code=0 应答；需要自定义应答结果时关闭 |
| `interaction_ack_concurrency` | int | 128 | 同时进行的自动应答上限，超出时跳过并计数 |
| `event_handler_concurrency` | int | 16 | 同时运行的订阅回调上限，自动应答不占此名额 |
| `event_queue_size` | int | 1024 | 等待执行的回调上限，满时拒绝新增并计数 |
| `event_handler_timeout` | float | 0 | 单个异步回调的超时秒数，0 表示不限时；超时取消回调并计数 |
| `enable_group_member_events` | bool | true | 订阅群成员进出事件（intent 1<<24）；修改后需重载适配器 |

**引用索引**

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ref_ttl_days` | int | 7 | 引用索引本地保留天数；0 表示不落盘 |
| `ref_max_entries` | int | 50000 | 全插件容量上限，不随实例数增长 |

域名与沙箱环境跟随各适配器自身配置（Webhook 的 `is_sandbox`），插件不提供全局开关。

## 运行机制

### 机器人身份与多实例

- 每个 AstrBot 平台配置 ID 绑定一个机器人身份（AppID + 生产/沙箱）。同一身份的多个实例共享主动配额、被动回复计数、互动去重和引用缓存，合并计算；不同身份完全隔离。生产环境新旧域名别名不算不同身份。
- 实例重载或重连不会清零未过期的用量；实例被删除或禁用后，新请求立即被拒绝。
- 请求在限流等待后、每次重试前都会核验实例与代次，写操作不会重放到其他机器人或新代次。

### 被动回复与 msg_seq

- 被动回复窗口：群聊 5 分钟内 5 次，单聊 60 分钟内 4 次。超出后默认降级为主动消息，见 `auto_degrade_proactive`。
- 群聊/单聊的 `msg_seq` 按回复键（场景 + 目标 + msg_id）单调递增，同身份实例共享。每个回复键的首个序号取随机基数，避开 AstrBot 本体使用的 1~10000 随机序号。
- 发送明确失败时只退回额度，不回收序号；频控重试沿用原序号。显式传入的 `msg_seq` 原样使用并推进上界。
- 流式发送未传 `msg_seq` 时，首片按回复键分配，续片自动沿用首片序号。

### 互动应答

- 自动与手动应答共用 `(机器人身份, interaction_id)` 资格，同一互动只能应答一次，重复调用抛 `QQOfficeDuplicateAck`。
- 开启 `interaction_auto_ack` 时，按钮/快捷菜单的手动应答必然重复；需要自定义 code 请关闭自动应答。
- 官方明确拒绝时释放资格，可以重新应答；网络结果未知时保留去重，不会重复应答。

### 事件处理

- 每个回调在独立 Task 中运行，沿用投递事件时的上下文；同时运行数受 `event_handler_concurrency` 限制，超出的进入有界队列。
- 只等待协程回调。同步回调返回的 Task/Future 由回调自行管理，不占名额，也不会随超时被取消。
- 回调需要正常传播取消；同步阻塞的回调无法被超时中断。
- `/qqoffice_status` 显示运行中、排队、丢弃、超时的回调数，以及在途和被跳过的自动应答数。队列溢出的告警日志每 60 秒最多一次。

### token 刷新与热重载

- 同一 botpy Token 对象的过期刷新与 401 后的强制刷新会合并为一次；刷新完成后才到达的 401 不会再次触发刷新。不同适配器的 Token 互不合并。
- 响应捕获与 Token 补丁在插件重载间共享，按引用计数恢复；卸载时等待已在进行的刷新结束。
- 卸载只有在清理全部完成后才标记为 closed；清理失败或被取消时，下次 `terminate()` 会重试。

### 媒体来源

- `send_rich` 和上传接口的媒体支持 http(s) URL、base64/data URI、本地路径和 bytes。
- URL 只交给 QQ 文件接口，由官方服务器拉取，插件不在本机下载，因此不做地址校验；`ftp://`、`file://` 等其他协议会被拒绝。
- 本地文件与 base64 在本机读取后上传，相同内容按机器人身份缓存 `file_info`。频道与频道私信的 `send_rich` 只支持图片 URL。

## 已知限制

- 已进入 botpy 的在途 HTTP 请求，无法在实例被禁用的瞬间取消。
- 旧版本生成的无身份引用索引文件不会自动归属到任何机器人，也不会被删除；新记录写入独立的 v2 文件。
- 多实例路由依赖 AstrBot 内部结构，只在「环境要求」中的基线版本上验证过。

## 诊断与联调

向机器人发送指令：

```
/qqoffice_status
```

可以查看实例列表、机器人身份、补丁挂载、intents、订阅、频控以及事件处理状态。

联调清单：

| # | 能力 | 步骤 | 预期 |
| --- | --- | --- | --- |
| 1 | 实例路由 | 双实例环境分别调用 `service.instance(id).group.info(...)` | 各自返回对应机器人视角的群信息 |
| 2 | 事件来源 | 双实例各点一次按钮 | 订阅者收到 2 个事件，`source.platform_id` 不同，各自应答 |
| 3 | 禁用实例 | 在面板禁用一台后立即调用 | 立即抛 `InstanceUnavailable`，其他实例正常 |
| 4 | 同 ID 重载 | 重载适配器后用旧的长期视图调用 | 正常跟随；旧事件抛 `StaleSourceEvent` |
| 5 | 改绑 AppID | 同一 ID 改 AppID 后用旧视图调用 | 抛 `InstanceIdentityChanged` |
| 6 | 配额共享 | 同 AppID 两个实例交替发被动回复 | 群聊第 6 次降级，计数不因重载清零 |
| 7 | 富消息 | `send_rich` 发送 markdown + 键盘 | 群内渲染正常 |
| 8 | 引用回复 | `reference(service.ref_from_event(event))` | 引用气泡正常，按身份隔离 |
| 9 | 沙箱 | Webhook 配置 `is_sandbox` | 该实例身份为 sandbox，与生产实例配额分开 |
| 10 | intents 拒断 | 未开通权限时订阅成员事件 | 仅该实例降级保连，其他实例不受影响 |

## 项目结构

```
astrbot_plugin_qqoffice_expand/
├── main.py                # 插件入口：服务装配、实例与事件视图、send_rich、诊断指令
├── core/
│   ├── plugin_service.py  # SDK v1 公开门面：状态、生命周期、绑定入口
│   ├── routing.py         # 多实例路由：本体索引桥接、身份与代次、共享状态、被动窗口与序号
│   ├── client.py          # 调用通道：操作上下文、额度预留、频控、重试、错误标准化
│   ├── events.py          # 事件总线、适配器补丁挂载、自动应答
│   ├── ack.py             # 自动/手动互动应答共用资格
│   ├── token_guard.py     # botpy Token 刷新合并（跨重载）
│   ├── http_capture.py    # botpy 响应状态捕获（区分空响应与超时）
│   ├── ratelimit.py       # 端点令牌桶 + 主动消息三级配额
│   ├── refstore.py        # REFIDX 引用索引与 file_info 缓存
│   ├── media.py           # 媒体来源归一、AMR 剥头、图片尺寸解析
│   ├── builders.py        # md / kb / btn / reference / md_image 构建器
│   ├── registry.py        # 命名方法注册表
│   ├── errors.py          # 异常类型与错误码建议
│   ├── auth.py            # 适配器名称与域名解析
│   └── ready.py           # 就绪信号（旧入口兼容）
├── api/                   # group / c2c / guild / manage 命名空间（一方法一端点）；media.py 为群聊/单聊共用的分片上传
├── tests/                 # 离线测试：路由、契约、并发、intents、集成
├── docs/                  # API_COVERAGE.md、MULTI_INSTANCE_DESIGN.md
├── CHANGELOG.md
├── metadata.yaml
└── _conf_schema.json
```

__如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！__
