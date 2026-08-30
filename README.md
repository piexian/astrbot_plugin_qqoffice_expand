# QQ 官方机器人能力扩展 (astrbot_plugin_qqoffice_expand)

把 QQ 官方开放平台在 AstrBot 本体（`qq_official` / `qq_official_webhook` 适配器）中缺失的能力封装成稳定接口，供**其他插件**调用，避免各自裸调 `event.bot.api`、重复处理鉴权/频控/错误码。

> ⚠️ **安装本插件后必须重启一次 AstrBot，否则无法注入。**
> 事件挂载与 intents 置位必须发生在 QQ 官方适配器实例化之前；WebUI 热安装时适配器早已连接，intents 未随 identify 下发，按钮回调/群成员/入群申请等事件一条都收不到。重启一次即恢复。AstrBot 当前不支持调整插件加载顺序，无法用排序绕开此时序。

## 环境要求

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| Python | >= 3.10 |  |
| AstrBot | >= v4.27.4 | 适配器行为与插件广播机制以此基线源码核实 |
| botpy | 1.2.1 | AstrBot 环境自带，无需单独安装 |
| httpx | >= 0.27 | 自建 HTTP 兜底路径使用，见 requirements.txt |

__平台支持__: 仅 QQ 官方机器人适配器（`qq_official` websocket 模式 / `qq_official_webhook` webhook 模式）

## 功能

- 群管理命名方法 - 撤回、成员禁言（≤10 人/次、≤30 天）、入群审批（通过/拒绝/拉黑）、自动审批策略 6 件套、群信息与机器人状态
- 单聊命名方法 - 撤回、全参数发送、输入中状态（`input_second` ≤60s，keepalive 每 50s 自动续发）、互动召回（30 天 4 周期）、富媒体上传（语音自动剥 AMR 头）
- `send_rich` 富消息 - markdown + 按钮键盘 + 引用回复，绕开本体消息序列化对 keyboard/reference 的丢弃；markdown × reference 互斥内建裁决
- 通用 `call()` 通道 - **任意官方端点当天可用**：频控令牌桶、主动消息配额、无状态 msg_seq、被动窗口自动降级、token/频控自动重试、HTML 网关页识别、错误码 → 排查建议
- 扩展事件订阅 - 按钮回调（3 秒时限自动应答、同 id 一次）、群管理 8 事件、入群申请、群成员进出、审核事件、频道事件；未登记事件走 `on_any` 透传
- REFIDX 引用索引 - 入站 msg_idx / 出站 ref_idx 本地持久化（JSONL + 7 天 TTL + LRU 5 万条），发送引用回复的前置依赖
- 诊断指令 `/qqoffice_status` - 适配器发现/挂载/intents/订阅/频控状态一览

## 安装

### 两种方式

1. 在 AstrBot 插件市场搜索 `qqoffice_expand` 点击安装
2. 在插件界面右下角点击加号选择从链接安装输入 `https://github.com/piexian/astrbot_plugin_qqoffice_expand`

**安装完成后必须重启一次 AstrBot**（原因见顶部警告）。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `prefer_new_domain` | bool | false | 使用统一域名 `api.bot.qq.com`（会同步改写 botpy Route 域名，影响该适配器全部请求）；旧域名 `api.sgroup.qq.com` 当前仍可用，官方下线旧域名时再开启 |
| `sandbox` | bool | false | 使用沙箱域名 `sandbox.api.sgroup.qq.com`，仅联调测试时开启 |
| `certified_bot` | bool | false | 机器人是否为认证主体，影响主动消息每分钟配额档位（认证 60 条/分钟，未认证 30 条/分钟） |
| `auto_degrade_proactive` | bool | true | 被动回复窗口（群 5 分钟/5 次、C2C 60 分钟/4 次）超窗/超次时自动降级为主动消息；关闭后直接抛错 |
| `dot_replace` | bool | false | 群聊文本中把「字母/汉字.字母/汉字」替换为下划线，规避平台 40054010「不允许发送URL」误拦；默认关闭（会改变文本语义） |
| `interaction_auto_ack` | bool | true | 收到 INTERACTION_CREATE 后先自动 `PUT /interactions/{id}` code=0（3 秒时限）再分发给订阅者 |
| `enable_group_member_events` | bool | true | 订阅群成员进出事件（intent 1<<24）；修改后需重载 QQ 官方平台适配器才生效 |
| `ref_ttl_days` | int | 7 | 引用索引（msg_idx / ref_idx）本地留存天数；0 表示不落盘（仅内存） |
| `retry_max` | int | 3 | 命中频控（429/40034100/40034101）时的自动等待重试次数上限；401/11244 的 token 重试固定 1 次 |

## 使用

本插件不面向终端用户，**消费方是其他 AstrBot 插件**：通过 `get_registered_star` 拿到 `star_cls` 作为 svc。加载顺序不可控（AstrBot 无插件排序能力），标准接法是「先试绑定 + 收加载广播再绑定」，**不要在 initialize() 里阻塞等待**（插件加载循环是串行 await 的，会卡死后续插件）：

```python
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

PLUGIN = "astrbot_plugin_qqoffice_expand"

class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.qq = None
        self._unsub = None

    def _try_bind(self) -> bool:
        # ready=True 表示对方 initialize 已完成（svc 完全可用），可放心绑定
        meta = self.context.get_registered_star(PLUGIN)
        if not (meta and meta.activated and meta.star_cls and getattr(meta.star_cls, "ready", False)):
            return False
        self.qq = meta.star_cls
        # 事件订阅必须在拿到 svc 后程序式注册（类体装饰器不可行）
        self._unsub = self.qq.on("INTERACTION_CREATE", self._on_button)
        return True

    async def initialize(self):
        # 本插件先加载 → 此处直接绑定成功；否则等它的加载广播，保持降级不阻塞
        if not self._try_bind():
            logger.info("qqoffice_expand 尚未就绪，等待其加载广播（未安装则保持降级）")

    @filter.on_plugin_loaded()
    async def _on_qqoffice_loaded(self, metadata):
        """框架广播：任一插件 initialize 成功后触发，metadata 是该插件的 StarMetadata。"""
        if self.qq is None and getattr(metadata, "name", "") == PLUGIN:
            if self._try_bind():
                logger.info("已接入 qqoffice_expand（收到加载成功广播）")

    @filter.on_plugin_unloaded()
    async def _on_qqoffice_unloaded(self, metadata):
        # 本插件被重载/卸载时旧 svc 对象失效，解绑并等待下一次加载广播
        if getattr(metadata, "name", "") == PLUGIN and self._unsub:
            self._unsub()
            self.qq = None
            self._unsub = None

    async def _on_button(self, data):   # data: QQOfficeEvent（.raw/.scene/.member_openid/.interaction_id）
        ...
```

约定：返回值统一为官方原始响应 dict；错误统一抛 `QQOfficeAPIError(code, message, advice)`（`QQOfficeNotSupported` 表示当前场景不支持，调用方据此降级）。也可在后台任务里轮询（`await svc.wait_ready(timeout)` / `svc.ready`），但不建议在 initialize 里轮询。

### 通用通道（官方上新接口当天可用）

```python
await svc.call(
    "POST",
    "/v2/groups/{group_openid}/messages",
    path_params={"group_openid": openid},
    json={"msg_type": 2, "markdown": {...}, "keyboard": {...}},
    scene="group", target_openid=openid, msg_id=mid,
)
```

### 富消息 / 引用回复

```python
await svc.send_rich(
    event,
    markdown=svc.md("# 签到成功\n积分 **50**"),
    keyboard=svc.kb().row(
        svc.btn("签到", data="/签到", enter=True),
        svc.btn("帮助", data="/help", enter=True),
    ).build(),
)

# 引用回复：REFIDX 从事件（ref_from_event）或机器人出站消息（ref_outbound）取
await svc.send_rich(event, content="收到", reference=svc.reference(svc.ref_from_event(event)))
```

### 诊断指令

```
/qqoffice_status
```

## 使用前提与已知约束

1. 需先安装本插件；依赖方接入模板见「使用」小节。
2. **群成员事件（1<<24）仅在 ws identify 时生效**：重启安装后自动生效；运行中开启/修改该配置后，需在管理面板**重载 QQ 官方平台适配器**（或重启 AstrBot），`qqoffice_status` 会提示。
3. 插件加载顺序不可调：依赖方按「使用」小节的绑定模板接入，本插件 initialize 成功时框架广播且 `svc.ready` 已置位，两种加载顺序都能自动接上。注意：该方案解决的是"拿不到 svc"，不改变热安装需重启的 intents 时序（见顶部警告）。
4. `on_any` 覆盖的是**已挂载**事件类型；官方全新事件在网关层会被 botpy 丢弃，需按 EVENT_SPECS 补一行（设计上的兜底边界）。
5. 大文件分片上传不复用本插件：AstrBot 自带 `qqofficial_chunked_upload.py` 已处理 40093001 持续重试与 prepare 字符串字段转换，本插件只提供 ≤单请求的 `upload_media`。

## 实测联调清单

环境自检先行：QQ 群里发 `/qqoffice_status`，确认 primary=adapter、适配器实例已列出（connected=True）、intents 位已列、订阅为空。

| # | 能力 | 步骤 | 预期 |
| --- | --- | --- | --- |
| 1 | call 通道 | `svc.call("GET", "/v2/groups/{openid}/info", path_params=..., scene="group")` | 返回群信息 dict |
| 2 | 群撤回 | 发消息后 2 分钟内 `group.recall` | 消息消失 |
| 3 | 禁言 | `group.mute_member(openid, member, "+10min的RFC3339")` → `get_mute` 查询 | 列表含该成员；`unmute_member` 解除 |
| 4 | 入群审批 | 邀请测试号进群 → `join_requests` → `join_approve`（approve/decline 各一） | 状态变更，decline 理由可见 |
| 5 | 策略件套 | `strategy_create(group_openids=[...])` → `strategy_list` → `whitelist add` → `strategy_execute` → `strategy_delete` | 全部 2xx，列表增减一致 |
| 6 | 富消息 | `send_rich(event, markdown=md(...), keyboard=kb().row(btn...).build())` | 群内渲染 markdown+按钮 |
| 7 | 按钮事件 | 点击按钮 | 订阅者收到 INTERACTION_CREATE，客户端按钮变为已响应（自动应答生效） |
| 8 | 互斥 | send_rich 同时给 markdown+reference | 日志告警，消息正常（默认丢引用）；`on_mutex="text_reference"` 时引用生效 |
| 9 | 引用回复 | `send_rich(event, content="..", reference=svc.reference(svc.ref_from_event(event)))` | 引用气泡展示 |
| 10 | 被动窗口 | 同一 msg_id 连发 6 次 | 第 6 次降级主动并告警（或未开配置时抛 40034005 类错误） |
| 11 | C2C input_notify | 处理耗时任务前 `c2c.input_notify(openid, keepalive=True)`，完事 `handle.cancel()` | 对方看到输入中约 50s+；不 cancel 会在 60s 后自然停 |
| 12 | C2C wakeup | `c2c.wakeup(openid, "召回")` | 用户收到召回消息 |
| 13 | 富媒体 | `upload_media("group", openid, url_or_path, 1)` → media 消息 | 返回 file_info，消息带图 |
| 14 | 入群申请事件 | 触发进群申请 | on_any/GROUP_JOIN_REQUEST 订阅者收到 |
| 15 | 群成员事件 | 成员进/退群（先重载适配器确认 1<<24 生效） | GROUP_MEMBER_ADD/REMOVE 到达 |
| 16 | 菜单/面板 | `manage.menu_put([...])` → `menu_get`；`panel_create(scope="group",...)` → `panel_list` → `panel_target` → `panel_delete` | 配置生效（面板查 QQ 群聊侧） |
| 17 | 沙箱/域名 | 开 `sandbox` 实测；开 `prefer_new_domain` 观察 botpy 请求域名 | 请求落到对应域名 |
| 18 | 错误标准化 | 故意传过期 msg_id | 抛 `QQOfficeAPIError(40034005, advice=...)` |
| 19 | 加载广播 | 依赖方名称字母序排在本插件之前 → 重启 | 依赖方 initialize 时空手而归，收到广播后日志显示自动绑定成功 |

## 项目结构

```
astrbot_plugin_qqoffice_expand/
├── main.py              # Star 入口：组装 svc、诊断指令 qqoffice_status
├── core/
│   ├── auth.py          # 适配器发现（路径 A）与自建 HTTP（路径 B）
│   ├── client.py        # 通用 call 通道：频控/msg_seq/被动窗口/重试/错误标准化
│   ├── ratelimit.py     # 表驱动令牌桶 + 主动消息三级配额
│   ├── refstore.py      # REFIDX 引用索引持久化 + file_info TTL 缓存
│   ├── media.py         # to_uploadable / SSRF 防护 / AMR 剥头 / 图片尺寸解析
│   ├── builders.py      # md / kb / btn / reference / md_image 构建器
│   ├── events.py        # EventBus + 适配器延迟挂载 + INTERACTION 自动应答
│   ├── registry.py      # 命名方法注册表
│   ├── errors.py        # 错误标准化（错误码 → 排查建议）
│   └── ready.py         # 就绪信号与依赖方等待原语
├── api/
│   ├── group.py         # 群聊接口：撤回/禁言/入群审批/自动审批策略/群信息，一方法一端点
│   ├── c2c.py           # 单聊接口：撤回/发送/输入中状态/互动召回/富媒体上传
│   ├── guild.py         # 频道接口：频道消息发送、频道/子频道信息
│   └── manage.py        # 全局管理接口：自定义菜单、指令面板、互动应答
├── tests/
│   └── offline_test.py  # 离线逻辑自测（89 项，零 astrbot 依赖）
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项 Schema
└── requirements.txt
```

__如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！__
