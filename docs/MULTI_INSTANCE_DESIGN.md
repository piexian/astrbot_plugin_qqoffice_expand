# 多实例路由与状态管理设计

设计基线：插件 `e18a23b`（实施前），AstrBot `2aec78272cb8` / `4.28.0-beta.1`，botpy `1.2.1`。
本文描述的算法已实施；实施确定的行为细则见第 11 节，验证证据见第 12 节。

## 1. 目标与本体边界

- 支持 N 个 QQ 官方实例，覆盖 1、2、10、50、100 个实例的算法验证，不写双实例特例。
- 预览阶段直接调整接口，不保留无来源的 `primary` 调用或旧接口兼容层。
- AstrBot 管理平台配置、创建/重载/关闭适配器及 botpy 鉴权；插件复用当前适配器的 API。
- 插件管理扩展 API 路由、扩展事件订阅、配额与引用状态，不接管原生消息管线。
- 禁用、删除或重载期间不存在当前运行实例时，拒绝新的扩展请求。移除凭旧凭据绕过本体禁用状态的自建 HTTP 兜底。
- 原生消息 handler 不覆盖、不重复执行；扩展 parser 和 handler 的恢复继续遵守补丁所有权。

## 2. 本体已核实的契约

| 契约 | 源码依据 | 对设计的影响 |
| --- | --- | --- |
| `event.get_platform_id()` 返回配置实例 ID | `astrbot/core/platform/astr_message_event.py:133`；两适配器 `meta()` | 唯一公开路由入口使用本体 ID，不另造 bot 编号 |
| 本体可能把 ID 中的 `:`、`!` 改为 `_` | `astrbot/core/platform/manager.py:110` | 使用加载后的 ID，不自行再规范化 |
| `self_id` 不保证是 AppID；`client_self_id` 是每次构造的 UUID | QQ 消息解析；`astrbot/core/platform/platform.py:45` | 身份、配置 ID、运行代次必须分开 |
| WS/Webhook 的原生事件均保存 `event.bot` | `qqofficial_message_event.py:99`；`qo_webhook_event.py:8` | 可以识别同 ID 重载后仍在排队的旧消息 |
| 平台加载通知不传实例，也不保证登录完成 | `astrbot/core/platform/manager.py:218` | 加载通知只触发索引刷新，不直接标记 HTTP 已就绪 |
| 没有平台卸载通知；关闭前先从 `_inst_map` 移除实例 | `astrbot/core/platform/manager.py:275` | 请求前读取本体当前实例，轮询只负责清理 |
| Webhook 初始化会替换同一个 client 的 `api/http` | `qqofficial_webhook/qo_webhook_server.py:86,113,122` | 每次尝试动态取得 API，不能缓存首次发现的空 token HTTP |

本体公开 `context.get_platform_inst(id)` 当前是 O(N) 遍历。为避免每个扩展请求都遍历全部平台，读取 `_inst_map` 的逻辑集中在一个小型本体访问模块中；只读该映射，不写入、复制替代或补丁包装平台管理器。以目标源码契约测试约束这处私有依赖，依赖变化时明确报不支持，不退回可能过期的本地缓存。

## 3. 两类标识、三组索引

```text
platform_id = AstrBot 加载后的配置 ID
robot_key   = (appid, environment)  # production / sandbox，来自当前适配器

routes[platform_id] -> RouteRecord
robots[robot_key]   -> RobotState
patches[client_generation] -> ClientPatchRecord
```

`RouteRecord` 保存本体适配器对象、botpy client 对象、`robot_key` 和递增运行代次。只有当前适配器或 client 对象发生变化时才更换代次；Webhook 在同一个 client 内替换 API 不构成新代次。

`RobotState` 保存机器人级频控、被动回复计数及互动去重。同 AppID、同环境的不同实例共享这些状态；token、session 仍由各自的本体适配器持有。引用与媒体缓存使用同一 `robot_key` 作为键前缀。

`ClientPatchRecord` 保存该代次自己的 parser/handler 补丁、扩展 intent 状态和拒绝权限记录。某个连接被网关拒绝，不修改其他连接的拒绝记录。

生产环境的新旧域名别名不能被当作两个机器人，导致配额被重复发放。域名和沙箱请求行为继续服从本体当前 HTTP 客户端。

## 4. 新调用接口

```python
# 主动调用：明确配置实例 ID。
qq = svc.instance("qq_sales")
await qq.group.send(group_openid, "通知")
await qq.invoke("manage.me")

# 原生 AstrMessageEvent：自动使用本体平台 ID 与 event.bot。
await svc.for_event(event).send_rich(content="收到")

# 扩展事件：每个事件携带不可变的 source。
async def on_button(ev):
    qq = svc.for_event(ev)
    # qq 已绑定来源；低层 call 不隐式补充消息回复参数。

# 全实例订阅与单实例订阅语义明确。
unsubscribe_all = svc.on("INTERACTION_CREATE", on_button)
unsubscribe_one = svc.instance("qq_sales").on("INTERACTION_CREATE", on_button)
```

根服务只暴露实例查询、事件订阅、构建器和状态查询，不暴露无目标的 `call/group/c2c/guild/manage/send_rich`。即使当前只有一个实例，主动调用也明确选实例，后续添加机器人不会改变原调用含义。

`svc.instance(id)` 返回轻量视图，绑定配置 ID 与创建视图时的机器人身份，不持有固定 HTTP 连接。每次调用重新解析当前代次：同身份重载后可继续使用；ID 改绑另一 AppID、环境改变、ID 被删除时，旧视图明确失败，调用方重新获取视图。

`svc.for_event(event)` 则绑定来源代次。旧事件不随重载自动切换到新 client；原生事件用 `event.bot is route.client` 核验，扩展事件用 `source.generation` 核验。若本体改变同一配置对象的 AppID，也必须核验来源身份。

四个 API 命名空间接收绑定视图的路由客户端。能力目录只构建一次，保存方法描述及未绑定方法；`invoke` 在自己的命名空间上调用，避免为每个实例或每次刷新重新反射、注册所有方法。

## 5. 请求路由算法

```python
def resolve(bound):
    entry = core.current_platform(bound.platform_id)  # _inst_map.get，O(1)
    if entry is None:
        raise InstanceUnavailable(bound.platform_id)

    # 同步、无 await；必要时只更新这个实例的路由记录。
    route = ensure_current_route(entry.inst)
    if route.robot_key != bound.robot_key:
        raise InstanceIdentityChanged(bound.platform_id)
    if bound.source_client is not None and bound.source_client is not route.client:
        raise StaleSourceEvent(bound.platform_id)
    if bound.source_generation is not None and bound.source_generation != route.generation:
        raise StaleSourceEvent(bound.platform_id)
    if client_is_closing(route.client):
        raise InstanceUnavailable(bound.platform_id)

    # Webhook 会替换这些对象，所以不能保存在长期视图里。
    api = route.client.api
    http = api._http
    if http._token is None:
        raise TransportNotReady(bound.platform_id)
    return route, http
```

`TransportNotReady` 只表示尚不能进入 SDK 请求，不虚构登录成功状态。token 存在时可以尝试请求，凭据无效、鉴权失败仍通过现有错误通道返回。本体 `RUNNING`、加载 hook、HTTP 对象存在都不能替代这个判断。

每次业务调用建立一个操作上下文，固定 `platform_id + robot_key + generation`。富消息的上传、发送，以及重试前都核验该上下文；同一操作不允许在 await 之后悄悄换到新代次。旧的长期实例视图可以在下一次独立调用开始时解析到新代次。

执行顺序：

1. 解析来源并建立操作上下文。
2. 在所属 `RobotState` 申请需要的配额或回复计数预留。
3. 限流等待结束后，再核验本体当前实例与代次，并动态取得 HTTP。
4. 调用该 HTTP；响应、错误、缓存写入始终归属于操作上下文中的机器人。
5. 重试仍使用同一来源和代次；代次失效就结束，不跨实例、跨通道重发。

本体没有提交锁或关闭前插件通知，所以只能拒绝失效后的新尝试，不能承诺已经进入 SDK 的在途 HTTP 在禁用后绝不发送。对已发送但响应不明的写请求，不以重载或传输异常为由自动重放。

不增加全局请求锁。路由映射更新和操作计数预留在同一 asyncio 事件循环中无 await 完成；网络与限流等待发生在它们之外。不同机器人之间不共享配额锁或排队链。

请求超时服从选中适配器当前的 `http.timeout`：本体基线 WS 为 20 秒，正常初始化后的 Webhook 为 300 秒。若扩展操作需要更短的总截止时间，只在操作外层施加截止时间；不向 SDK 重复传 `timeout`，也不承诺把原生 HTTP 超时延长到 120 秒。

## 6. 事件与自动应答

扩展事件增加不可变来源：

```text
source = {platform_id, appid, environment, generation}
```

来源在挂载处理器时从实际适配器记录绑定，不从官方 payload 中猜测、不用 `self_id` 推导。收到回调时验证本体当前实例；失效代次的回调不再向订阅者投递。

订阅按事件类型及来源建立索引：

```text
all_subs[event_type]
scoped_subs[(platform_id, robot_key, event_type)]
all_any
scoped_any[(platform_id, robot_key)]
```

单事件只合并上述四组的相关订阅者，开销为 O(H)，H 是实际要调用的订阅者数，不遍历其他机器人。实例级订阅随同身份重载继续生效，改绑身份后不转移给新机器人；全局订阅接收全部带来源的事件。

自动 ACK 使用事件来源，不经过全局管理客户端；去重键为 `(robot_key, interaction_id)`。同机器人双连接收到相同互动只应答一次，不同机器人使用独立去重空间。状态区分 pending/succeeded，不能在找不到来源或发送前失败时提前永久占用去重项；网络结果未知时也不能无条件清除并重复应答。

ACK 延续独立任务及短截止时间，不等待普通订阅者执行完毕。所有扩展任务记录来源，插件卸载统一停止；不会关闭由本体拥有的 botpy client/session。

## 7. 刷新与补丁算法

单一协调任务：每 15 秒低频巡检，`on_platform_loaded` 钩子即时触发差异刷新；不为每个实例启动 watcher。

一次刷新在没有 await 的阶段读取本体实例快照，按 `platform_id` 对比：

- 新增：建立路由及机器人状态引用，挂载这个 client 的扩展。
- 相同实例/client：复用记录，检查延迟出现或替换的 WS/Webhook state，并按固定事件表重跑 intents 需求校验（稳定状态不新建 wrapper，告警按状态去重）。
- 同 ID 替换：旧路由立即失效，建立新代次；旧 parser/handler 按所有权恢复。
- 消失：失效并清理这个实例的补丁和索引；不选择另一机器人作为替代。

请求不必等巡检发现新增/替换：`resolve` 可以从本体索引同步刷新单个路由。删除/禁用在本体移除索引后立即被下一次请求看到。更新路由时，把需要恢复的旧补丁记录加入待清理集合，同时标记新代次待挂载，不能先丢弃旧记录。协调任务合并处理这些变化；异步清理阶段不能阻挡新索引发布。

parser 挂载记录改为 `state -> event_name -> {original, wrapper}` 的字典。重复挂载直接比较当前 wrapper，避免目前每个 parser 都在线性补丁列表中执行 `any(...)`，把一次全体 state 检查从 O(N·E²) 降到 O(N·E)。E 为固定事件种类数；只有新增或变化的 state 真正创建包装器。

恢复时只改回仍是自己 wrapper 的属性；其他插件已替换的属性保持原状。失效 wrapper 即使仍被第三方引用，也只透传原 parser，不再发出本插件事件。不能仅删除记录而把原包装器留在对象上。

扩展 intent 按实例计算，来源于启用功能、该实例订阅和全局订阅。`on_any` 不申请全部频道权限。网关拒绝状态归属单个运行代次；降级仅移除本插件为该实例新增的位，保留本体与其他功能已存在的位。已 identify 的连接不由插件擅自重连；新位何时生效遵从本体现有重连/重载流程。

## 8. 状态隔离与高频路径优化

| 状态 | 隔离/共享规则 | 清理规则 |
| --- | --- | --- |
| botpy API、token、HTTP session | 本体适配器持有；插件每次动态取得 | 本体关闭，插件不另建身份或关闭原生资源 |
| 端点频控、主动配额 | 按 `robot_key` 共享；频道等端点继续细分目标 | 保留未过期用量，不能通过换实例或重载清零 |
| 被动回复计数 | 按 `robot_key + scene + target + msg_id` | 窗口过期清理；同身份多连接共享 |
| 引用与媒体缓存 | 全局存储，键前缀含 `robot_key` | 全插件缓存上限与 TTL，不把现有上限乘以实例数 |
| ACK 去重 | `robot_key + interaction_id` | pending/succeeded 生命周期与 TTL |
| intent 拒绝、补丁、连接状态 | 按运行代次 | 实例替换/移除后清理，不传播到其他实例 |

现有频控清理在每次请求遍历所有关系，回复计数也会反复扫描整个表。调整为请求只裁剪当前机器人和当前目标的时间窗口；后台协调任务增量回收闲置键。关系记录用可轮转的有序映射保存，每轮巡检只检查限定数量的键，把仍存活的记录移到尾部；请求更新已有记录不重复追加清理项。每日关系用量用 `(day, count)` 表示，保持现有按天计数语义，不保存同一天重复的 1000 个日期值。

认证主体等限额配置按 `robot_key` 设置，同身份多实例只读取一份配置。全局值仅作为默认值，不要求几十个机器人都具有相同认证状态。

被动回复额度先预留、后确认，预留计入并发占用，避免两个实例同时通过 `check` 后都发送。发送前取消可释放预留；发送结果未知按已消耗处理，不能凭超时重新发放额度。网络等待不持有整机器人锁。

缓存可按 LRU 淘汰；未过期配额、预留和 ACK pending 不按 LRU 丢弃。机器人暂时没有运行实例时，仍保留未过期的用量状态，直到自然过期，防止重新添加同 AppID 后获得第二份配额。

旧引用文件没有 AppID，无法可靠归属；新格式使用独立文件和显式机器人前缀，不自动把旧记录分配给首个机器人，也不删除旧文件。

## 9. 复杂度与性能验收

设 N 为运行实例数，B 为不同机器人身份数，E 为固定扩展事件种类数，H 为当前事件的实际订阅者数，R 为未过期的配额/回复状态数量，K 为受限缓存条目数，I 为在途操作/清理记录数。

| 操作 | 目标复杂度 | 边界 |
| --- | --- | --- |
| 请求选实例、代次核验 | 平均 O(1) | 常数次字典查询与对象身份比较，不扫描 N |
| 单个实例变更的请求侧更新 | O(1) 路由更新 | 补丁工作交给协调任务，不混入每次请求 |
| 全体发现与差异计算 | O(P + N + C) | P 是本体全部平台数，C 为本轮累计变更数；只在通知/巡检发生 |
| 全体 parser 状态检查 | O(N·E) | 不反复遍历历史补丁；稳定状态不创建新 wrapper，但仍做固定事件表校验 |
| 单事件分发 | O(H) | 不扫描全部实例及无关订阅 |
| scoped intent 需求 | O(E) | 按 (pid, 身份) 索引直查，不随订阅数 S 扫描 |
| 配额窗口裁剪 | 摊还 O(1) | 每条时间记录只入队、出队一次；不扫描其他目标 |
| 后台清理（桶/配额/窗口） | O(预算) 检查数 | popitem 轮转、检查名额与删除数分离、跨调用指针防饥饿；堆操作 O(log R) |
| 管理状态内存 | O(N·E + B + R + K + S + I) | S 为实际订阅数；缓存上限为全插件共享；另计消息及上传内容字节数 |

不凭 N 推断网络吞吐，也不承诺 50 个机器人意味着固定 QPS；QQ 限额、SDK HTTP 与订阅者代码仍决定实际吞吐。性能验证首先检查操作计数、稳定状态零新增包装器、任务和内存回收，再测本地路由耗时。

## 10. 验收场景

- N=1/2/10/50/100，任意插入顺序，非 QQ 平台混入，WS/Webhook 混用。
- 随机目标的 GET、发送、管理、上传和 ACK 使用严格对应的 botpy HTTP 身份。
- 同 AppID 两个实例共享用量、引用和去重；不同 AppID 不共享；沙箱与生产分开。
- 其中一个实例禁用、删除、同 ID 重载、改绑 AppID，其余实例行为不变。
- 延迟加载与 Webhook 同 client 内替换 API：不使用构造时的空 token HTTP。
- 保存的实例视图经过同身份重载可继续使用；保存的旧事件和跨代次操作明确失败。
- 上传结束前后、限流等待中、重试前发生实例变化：不向另一实例发出后续写操作。
- 一个实例收到 4013/4014 后，其他实例的 intent 和后续重载不受影响。
- 连续无变更巡检、反复重载、插件卸载后，parser 包装层数、记录数与后台任务不增长。
- 故意构造相同目标 ID、消息 ID、互动 ID，确认机器人命名空间确实隔离。

## 11. 行为细则

以下细则与第 5-8 节设计一致，为实施时依据本体源码确定的具体行为：

- 环境判定按本体实际传输形态：WS 适配器构造 botClient 不传 is_sandbox，恒为 production；只有 Webhook helper 读取 config['is_sandbox']（qo_webhook_server.py）。身份的 appid 只取 inst.appid 实例属性，忽略可能已被面板修改但未重建客户端的 config 字段。
- 客户端关闭识别同时检查 botpy is_closed 与 AstrBot WS 适配器 botClient 公开的 is_shutting_down（本体 shutdown 前置为 True）。
- 被动窗口键为 (scene, target, msg_id)（robot_key 由 RobotState 隔离承担），过期窗口拒绝且不重置；预留/确认语义，发送结果未知按已消耗。
- ACK 阶段语义：官方明确拒绝可重答；结果未知或进入 SDK 后取消按已应答处理（settle），不重答同一 id；发送前确定性失败回收预留。
- intents 所有权按实际对象身份记录：构造期逐 client pending（owned/denied），挂载收编本 client；拒断剔位时同步还原本插件改写的 adapter.intents.value 位；类级 hook/parser 所有权用保存的安装引用做对象身份比较（functools.wraps 复制标签不可信），卸载保留第三方 wrapper 但自身停用透传，新插件实例可穿过保留的第三方链重新安装；无 original 的类补齐 parser 卸载后不再主动分发。
- 差异收敛：请求侧与巡检变更在同一 `_changes` 日志折叠，每实例一条 first_old→last_new；changed 与 unchanged 互斥；中间代次不挂载；请求侧变化不安装补丁。
- 后台清理有界：桶/配额/窗口各队列 popitem 轮转、检查名额与删除数分离、跨调用轮转指针防饥饿；在途操作/限流等待者以引用计数保护 RobotState 与 bucket；当日额度不因闲置删除；`idle()` 为 O(1) 结构空判定。
- 引用查询按事件来源核验：原生事件用 platform_id+event.bot 对本体当前实例校验，过期来源不返回新机器人命名空间的引用；扩展事件按不可变 source 命名空间。原生消息缓存未命中时从本体保留的 raw_data（PatchedMessage.raw_data / AstrBotMessage.raw_message）提取 msg_idx 入库，不接管原生 parser。
- 引用持久化 JSONL 压缩：触发时只写未过期、容量内的每 key 最新记录；压缩后下一次触发阈值按压缩后实际体积翻倍（与保底阈值取 max），长期追加的全量扫描次数有界；旧无身份文件不认领不删除。
- 写操作经 `retry_time=2` 限制 botpy 内部 ConnectionReset 递归重试（不修改共享 http.request）；已进入 SDK 的请求无法在本体卸载瞬间撤回，此为准确边界。
- 扩展事件自动回复字段：msg_id 取消息 id（频道消息同样保留）；event_id 仅在事件类型属于 EVENT_ID_SCOPES 允许集合时从 payload 事件 id 推断，并携带事件类型作为 event_id_source 走与显式传参相同的校验；不在允许集合的事件（如 GROUP_MEMBER_ADD、普通群消息）不伪造 event_id，也不因此跳过主动消息配额。

## 12. 验证记录

全部在 bwrap 禁网沙箱、本体 .venv 只读依赖、无生产数据下执行：

- `tests/routing_test.py`：N 档随机解析严格命中 HTTP 身份、每请求恰一次本体索引查询、折叠消费（pending+扫描组合、删除恢复）、真实 botpy BotHttp token 契约、身份语义（WS 忽略 sandbox 配置、Webhook 沙箱、inst.appid）。
- `tests/api_contract_test.py`：真实接口形状与端点限流、跨身份调用隔离、流式续片不重复扣窗口、被动窗口降级与身份隔离、429/401 语义、空响应/超时/403+11253 区分、webhook 补丁挂载/来源绑定/卸载恢复、重载跟随与改绑拒绝、refstore 命名空间。
- `tests/intents_patch_test.py`：构造期注入时序、会话真值校验与告警去重、4013/4014 按实例剔位记忆、实例隔离、非 owner 不卸载、owner 卸载还原。
- `tests/integration_test.py`：N 档混合 WS/Webhook 随机调用身份、扩展事件不可变来源与实例作用域订阅、自动 ACK 来源路由/幂等/阶段语义、在途请求不虚假中断、429 等待后重载不跨代次重放、权限拒绝按实例隔离、连续巡检任务/补丁/wrapper 零增长。
- `tests/offline_test.py`：构建器/错误码/端点表/被动窗口/引用命名空间/配额增量回收/媒体/注册表/事件分发/就绪原语。
- `tests/main_assembly_test.py`（真实 Main 装配）：视图创建时固定身份/改绑拒绝/插件卸载后旧视图失效、真实命名空间 invoke、自动回复字段契约、GET 不耗配额、上传-发送同代次、发送阶段预留语义、未知写结果不重放不退款、botpy 内部重置不重放写、缓存命中 await 后核验、ACK 接线与阶段语义、本体删除后旧 handler 不投递、scoped 订阅按 (pid, 身份) 绑定/按需 intents/解绑与改绑不继承、空闲状态回收、旧 state 引用清理、类级 parser 残留清理、事件任务取消、构造期 intents 还原、第三方 hook 保留与重载恢复、拒断 adapter.value 两条时序还原、协调折叠两场景、N 档随机调用身份。
- 独立审查探针（主代理维护）：来源/结果语义、边界行为、协调折叠、规模路由、所有权（functools.wraps 形态）、清理有界性、事件回复字段与引用来源。
- 真实 AstrBot 主进程完整启动冒烟（禁网临时目录、无生产凭据）：非空 template_list 配置经真实 AstrBotConfig 解析与 dashboard.validate_config 校验通过，按 AppID 限额覆盖生效（30/60/默认 60）；插件 loaded/ready、新入口齐备、initialize/协调任务运行；SIGINT 后 terminate 完成且插件任务归零、进程退出码 0。

未覆盖：真实 QQ 官方侧联调（真实 identify 载荷、真实网关 4013/4014、真实互动应答时延）需要连接真实凭据与生产环境，按验证边界不在本轮执行。

