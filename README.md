# QQ 官方机器人能力扩展 (astrbot_plugin_qqoffice_expand)

通过可卸载的猴子补丁和**按实例绑定**的调用入口，补充 AstrBot（`qq_official` / `qq_official_webhook`，支持**任意 N 个实例同时运行**）的扩展事件与官方 API 能力，供**其他插件**调用。普通消息收发继续由 AstrBot 负责；已有 SDK 能力统一纳入鉴权、频控和错误处理。

多实例语义：

- 每个 AstrBot 配置实例（平台 ID）绑定到一个机器人身份（AppID + 生产/沙箱）；同 AppID 同环境的多实例共享配额、被动回复计数、ACK 去重与引用缓存，不同身份完全隔离。
- 主动调用必须**明确选实例**（`svc.instance(id)`）；事件回复通过 `svc.for_event(event)` 自动绑定来源机器人。没有"全局默认机器人"。
- 实例重载/换连接不清零未过期用量；实例被删除/禁用后新请求立即拒绝（不等轮询）；旧事件与跨代次的进行中操作明确失败，不会把消息发到另一台机器人。

> **intents 生效时机**：扩展 intents 位只能随 WS identify 下发。插件会在适配器实例化时自动注入扩展位（首次构造即生效）；**已建立的 WS 会话若缺少所需 intents，请在管理面板重载对应的 QQ 官方 WS 适配器**，让新 identify 携带扩展位。Webhook 不依赖 WS identify，不因此要求重启。若扩展位未在 QQ 开放平台开通权限导致 identify 被网关 4013/4014 拒断，插件会**按实例**剔除扩展位保住基础连接并日志提示（其他实例不受影响）；开通权限后重载相关适配器即可重新申请本代次权限。

## 环境要求

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| Python | >= 3.10 |  |
| AstrBot | 4.28.0-beta.1（commit `2aec78272cb8`）验证通过 | 多实例路由依赖 platform manager 的运行实例索引等内部结构，以该基线核实；未验证的未来版本不承诺 |
| botpy | 1.2.1 | AstrBot 环境自带，无需单独安装 |

__平台支持__: 仅 QQ 官方机器人适配器（`qq_official` websocket 模式 / `qq_official_webhook` webhook 模式），数量不限。

## 功能

- **N 实例路由** - `svc.instance(id)` / `svc.for_event(event)` 绑定视图；平均 O(1) 定位本体当前实例与代次；同 AppID 共享、跨身份隔离
- 群管理命名方法 - 成员列表与详情、批量移除、群黑名单、撤回、禁言、入群审批、自动审批策略 6 件套、群信息与机器人状态
- 单聊命名方法 - 独立流式分片、撤回、全参数发送、输入中状态、互动召回、富媒体上传
- 分片上传控制 - 群聊/C2C 的预上传、分片确认和合并接口
- 频道管理 - 频道与子频道、成员、身份组、权限、禁言、消息与私信、公告、精华、日程、表情表态、音频、论坛和接口授权
- [接口覆盖与调用示例](docs/API_COVERAGE.md)
- `send_rich` 富消息 - markdown + 按钮键盘 + 引用回复，上传与发送在同一操作上下文完成（中途重载不跨代次发送）
- 通用 `call()` 通道（视图方法）- 任意官方端点：频控令牌桶、主动消息配额、无状态 msg_seq、被动窗口自动降级、token/频控自动重试（重试前核验同一来源）、HTML 网关页识别、错误码 → 排查建议
- 扩展事件订阅 - 全局与实例作用域两级；事件携带不可变来源；按钮/快捷菜单 3 秒时限自动应答（按来源机器人、同 id 一次）
- REFIDX 引用索引 - 按机器人身份命名空间持久化（全局容量上限，不随实例数增长）
- 诊断指令 `/qqoffice_status` - 实例/身份/挂载/订阅/频控状态一览

## 安装

从仓库链接安装：`https://github.com/piexian/astrbot_plugin_qqoffice_expand`
（AstrBot 插件界面右下角加号 → 从链接安装）。

安装后无需强制重启本体；仅当已建立的 QQ 官方 WS 会话缺少所需 intents 时，
重载对应适配器即可（见顶部「intents 生效时机」）。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `certified_bot` | bool | false | 全局默认认证限额（群聊 60/分钟、未认证 30/分钟；C2C 认证 10/秒、未认证 5/秒且 30/分钟），可被 `certified_bots` 按 AppID 覆盖 |
| `certified_bots` | template_list | [] | 按机器人 AppID 覆盖认证限额（每条 appid + certified）；同身份多实例只读一份，未列出的用全局默认 |
| `auto_degrade_proactive` | bool | true | 被动回复窗口超窗/超次时自动降级为主动消息；关闭后直接抛错 |
| `dot_replace` | bool | false | 群聊文本点号替换，规避 40054010 误拦；默认关闭 |
| `interaction_auto_ack` | bool | true | 仅按钮/快捷菜单（type=11/12）按来源机器人在 3 秒内自动应答 |
| `enable_group_member_events` | bool | true | 订阅群成员进出事件（intent 1<<24）；修改后需重载适配器生效 |
| `ref_ttl_days` | int | 7 | 引用索引本地留存天数；0 表示不落盘 |
| `ref_max_entries` | int | 50000 | 引用索引全插件容量上限（不随实例数增长） |
| `retry_max` | int | 3 | 频控自动等待重试上限；401/11244 token 重试固定 1 次 |

> 注：旧版本的 `prefer_new_domain` / `sandbox` 全局域名开关已移除。域名与环境由每个适配器自身的配置决定（Webhook 的 `is_sandbox`；生产新旧域名别名不构成不同机器人身份）。

## 使用

消费方是其他 AstrBot 插件：通过 `get_registered_star` 拿到 `star_cls` 作为 svc。
加载顺序不可控（AstrBot 无插件排序能力），标准接法是「initialize 内先试绑定
+ 收加载广播再绑定」，**不要在 initialize() 里阻塞等待**：

```python
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

PLUGIN = "astrbot_plugin_qqoffice_expand"

class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.qq = None          # 绑定的服务实例
        self._unsubs = []       # 本插件在服务上的订阅，卸载时解绑

    def _try_bind(self) -> bool:
        """绑定目标服务并注册订阅；重复调用（服务未变）幂等。"""
        meta = self.context.get_registered_star(PLUGIN)
        if not (meta and meta.activated and meta.star_cls
                and getattr(meta.star_cls, "ready", False)):
            return False
        if self.qq is meta.star_cls:
            return True   # 同一服务实例已绑定，不重复订阅
        self._unbind()      # 服务重载后是新实例：先解绑旧订阅
        self.qq = meta.star_cls
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

### 主动调用：明确选实例

```python
qq = svc.instance("qq_sales")            # AstrBot 平台配置 ID
await qq.group.send(group_openid, "通知")            # 纯主动消息
await qq.invoke("group.recall", openid, message_id)  # 命名方法泛化调用
resp = await qq.call("GET", "/v2/groups/{group_openid}/info",
                     path_params={"group_openid": openid}, scene="group")

# 视图上也有完整命名空间与实例级订阅
await qq.c2c.stream_send(openid, "分片", msg_id=mid)
await qq.manage.me()
unsub = qq.on("GROUP_MEMBER_ADD", handler)           # 只收这台机器人的事件
```

使用时机与身份固定：AstrBot 中插件 initialize 先于平台实例化，因此
`svc.instance(id)` 要求目标平台已在运行索引中（不可用 ID 立即抛
`InstanceUnavailable`），并在**创建时即固定机器人身份**：同 ID 改绑另一
AppID 后旧视图调用抛 `InstanceIdentityChanged`（需重新 `svc.instance(id)`）；
同身份重载则自动跟随。平台未就绪时，请在 `@filter.on_platform_loaded()`
钩子或事件回调中创建视图（平台加载不代表 token 已登录，请求就绪由传输
检查判定，未登录时报 `TransportNotReady`）。

### 事件回复：自动绑定来源

```python
qq = svc.for_event(astr_message_event)   # 原生事件：platform_id + event.bot 核验
qq = svc.for_event(qqoffice_event)       # 扩展事件：不可变来源 + 代次核验
await qq.send_rich(markdown=svc.md("签到成功"))   # 视图持有事件目标，不必再传 event
await qq.send_rich(content="纯文本回复")
# 也可显式指定目标：qq.send_rich(scene="group", target_openid=..., content=...)
```

自动回复字段契约：msg_id 取消息 id（含频道消息，走被动回复窗口）；event_id
仅在事件类型属于官方允许集合（GROUP_ADD_ROBOT/GROUP_MSG_RECEIVE/
INTERACTION_CREATE/C2C_MSG_RECEIVE/FRIEND_ADD）时自动推断，并按与显式
传参相同的 EVENT_ID_SCOPES 校验；其余事件（如群成员进出、普通群消息）
不伪造 event_id，纯主动发送会正常消耗配额。显式传参始终覆盖自动字段。

旧事件（适配器已重载/替换）再调用会抛 `StaleSourceEvent`；实例被删除/禁用立即抛 `InstanceUnavailable`；传输未就绪（登录中/token 为空）抛 `TransportNotReady`。

### 富消息 / 引用回复

```python
qq = svc.for_event(event)
await qq.send_rich(
    markdown=svc.md("# 签到成功\n积分 **50**"),
    keyboard=svc.kb().row(svc.btn("签到", data="/签到", enter=True)).build(),
)
await qq.send_rich(content="收到",
                   reference=svc.reference(svc.ref_from_event(event)))
```

原生 AstrBot 消息的引用同样可用：`ref_from_event` 从本体保留的原始
payload（`message.raw_data`）提取 msg_idx 并按机器人命名空间入库；过期
事件（适配器已重载）不会从新机器人命名空间取引用。

### 诊断指令

```
/qqoffice_status
```

## 使用前提与已知约束

1. 扩展 intents 位仅随 ws identify 下发；构造期注入保证启动/重载适配器自动携带；运行中改配置需重载一次适配器。`/qqoffice_status` 的 `verdict` 显示每个实例的会话真值。
2. 已进入 botpy 的在途 HTTP 请求无法在实例禁用瞬间取消；插件保证的是"重试前与等待后核验，不跨机器人/代次重放写操作"。
3. 主动配额、被动回复窗口、互动去重、引用缓存按机器人身份共享：同 AppID 多实例合计计算，重载/换连接不清零。
4. 首次订阅频道类扩展事件后，需重连或重载适配器让新 intents 位生效；`on_any` 不自动申请全部权限。
5. 旧版本的无身份引用索引文件不会被自动归属到任何机器人（也不会删除）；新记录写入独立的 v2 文件。

## 实测联调清单

环境自检：QQ 群里发 `/qqoffice_status`，确认实例列表、机器人身份、挂载与 intents 状态正常。

| # | 能力 | 步骤 | 预期 |
| --- | --- | --- | --- |
| 1 | 实例路由 | 双实例环境分别 `svc.instance(id).group.info(...)` | 各自返回对应机器人视角的群信息 |
| 2 | 事件来源 | 双实例各点一次按钮 | 订阅者收到 2 个事件且 `source.platform_id` 不同，各自 ACK |
| 3 | 禁用实例 | 面板禁用一台后立即调用 | 立即抛 `InstanceUnavailable`，其他实例正常 |
| 4 | 同 ID 重载 | 重载适配器后用旧长期视图调用 | 正常（跟随同身份重载）；旧事件抛 `StaleSourceEvent` |
| 5 | 改绑 AppID | 同 ID 改 AppID 后用旧视图 | 抛 `InstanceIdentityChanged` |
| 6 | 配额共享 | 同 AppID 两实例交替发被动回复 | 第 6 次（群）降级，计数不因重载清零 |
| 7 | 富消息 | `send_rich` markdown+键盘 | 群内渲染正常 |
| 8 | 引用回复 | `reference(svc.ref_from_event(event))` | 引用气泡正常（身份前缀隔离） |
| 9 | 沙箱 | Webhook 配置 `is_sandbox` | 该实例身份为 sandbox，与生产实例配额分开 |
| 10 | intents 拒断 | 未开权限时订阅成员事件 | 仅该实例降级保连，其他实例不受影响 |

## 项目结构

```
astrbot_plugin_qqoffice_expand/
├── main.py              # Star 入口：svc 装配、实例/事件视图、send_rich、诊断指令
├── core/
│   ├── routing.py       # N 实例路由核心：本体索引桥接、身份/代次、机器人状态
│   ├── auth.py          # 适配器名称与 botpy 域名改写（自建 HTTP 兜底已移除）
│   ├── client.py        # 绑定视图调用通道：操作上下文/预留/频控/重试/错误标准化
│   ├── events.py        # EventBus（全局+实例订阅）、适配器协调挂载、自动 ACK
│   ├── ratelimit.py     # 表驱动令牌桶 + 主动消息三级配额（按机器人身份）
│   ├── refstore.py      # REFIDX 引用索引（身份命名空间 + 全局容量上限）
│   ├── media.py         # to_uploadable / SSRF 防护 / AMR 剥头 / 图片尺寸解析
│   ├── builders.py      # md / kb / btn / reference / md_image 构建器
│   ├── registry.py      # 命名方法注册表（目录构建一次）
│   ├── errors.py        # 错误标准化 + 路由错误类型
│   ├── http_capture.py  # botpy 空响应/超时区分
│   └── ready.py         # 就绪信号与依赖方等待原语
├── api/                 # group/c2c/guild/manage 命名空间（一方法一端点）
├── tests/               # 离线沙箱测试（路由/契约/intents/集成/离线逻辑）
├── docs/                # API_COVERAGE.md、MULTI_INSTANCE_DESIGN.md
├── metadata.yaml
├── _conf_schema.json
└── requirements.txt
```

__如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！__
