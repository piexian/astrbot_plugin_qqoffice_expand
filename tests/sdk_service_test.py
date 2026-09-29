# -*- coding: utf-8 -*-
"""SDK v1 公开门面回归（core/plugin_service.py + main.Main.get_service）。

按共享约定验证：原生发现协议、版本协商、状态快照无副作用、wait_ready
超时/关闭、terminate 生命周期、重载后旧服务失效、门面绑定路由与委托。
全部走真实 Main/BoundView/EventBus 装配（宿主注册依赖为桩，botpy 组件为
真实实现，HTTP 为内存桩），不执行真实 QQ HTTP。

运行：共享禁网 sandbox，独立运行（与既有脚本一致，避免跨文件 sys.modules
桩互相污染）：`test-isolated.sh <repo> tests/sdk_service_test.py`
或 `-m pytest tests/sdk_service_test.py -q -o asyncio_mode=auto`。
"""

import asyncio
import importlib
import inspect
import json
import sys
import types
from contextlib import asynccontextmanager

from botpy.client import Client
from botpy.connection import ConnectionState

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NS = types.SimpleNamespace

# —— AstrBot 注册依赖桩（与 main_assembly_test 相同的桩集合）——
_api = types.ModuleType("astrbot.api")
_api.AstrBotConfig = dict
_api.logger = NS(
    **{k: (lambda *a, **kw: None) for k in ("info", "warning", "error", "debug")}
)
_event = types.ModuleType("astrbot.api.event")
_event.AstrMessageEvent = object
_event.filter = NS(
    command=lambda *a, **kw: lambda fn: fn,
    on_platform_loaded=lambda *a, **kw: lambda fn: fn,
)
_star = types.ModuleType("astrbot.api.star")


class _Star:
    def __init__(self, context, config=None):
        self.context = context


_star.Star, _star.Context = _Star, object
_star.StarTools = NS(get_data_dir=lambda *a: None)
sys.modules.update(
    {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": _api,
        "astrbot.api.event": _event,
        "astrbot.api.star": _star,
    }
)
_package = types.ModuleType("qqoffice_sdk_test")
_package.__path__ = [str(ROOT)]
sys.modules["qqoffice_sdk_test"] = _package
M = importlib.import_module("qqoffice_sdk_test.main")
PS = importlib.import_module("qqoffice_sdk_test.core.plugin_service")
R = importlib.import_module("qqoffice_sdk_test.core.routing")
B = importlib.import_module("qqoffice_sdk_test.core.builders")

ok = 0


def t(name, cond, detail=None):
    global ok
    assert cond, f"FAIL: {name} ({detail!r})"
    ok += 1
    print(f"  ok  {name}")


class HTTP:
    def __init__(self, identity):
        self.identity = identity
        self.calls = []
        self._token = object()
        self.is_sandbox = False
        self.on_request = None
        self.response = {"id": "message", "file_info": "file", "ttl": 3600}

    async def request(self, route, **kwargs):
        self.calls.append((route.method, route.path, kwargs))
        if self.on_request:
            result = self.on_request(route, kwargs)
            if inspect.isawaitable(result):
                await result
        return self.response


def adapter(pid="A", appid="bot-A", identity=None):
    http = HTTP(identity or appid)
    client = Client.__new__(Client)
    client._closed = False
    client.loop = asyncio.get_running_loop()
    client.api = NS(_http=http)
    client.http = http
    client.intents = 1 << 30
    client._connection = NS(
        state=ConnectionState(client.ws_dispatch, client.api), _session_list=[]
    )
    return NS(
        appid=appid,
        config={"id": pid, "appid": appid},
        client=client,
        get_client=lambda: client,
        meta=lambda: NS(name="qq_official", id=pid),
        intents=NS(value=1 << 30),
    )


@asynccontextmanager
async def service(initial=True, **config):
    manager = NS(_inst_map={})
    inst = adapter() if initial else None
    if inst:
        manager._inst_map["A"] = {"inst": inst}
    svc = M.Main(NS(platform_manager=manager), {"retry_max": 0, **config})
    if initial:
        await svc.initialize()
    try:
        yield svc, manager, inst
    finally:
        if svc._service.state not in ("closed",):
            await svc.terminate()
        tasks = list(svc.event_bus._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def native_event(inst, pid="A", group="G", msg="M"):
    """模拟本体原生群消息事件（for_event 来源核验契约）。"""
    return NS(
        get_platform_id=lambda: pid,
        bot=inst.client,
        platform=NS(name="qq_official"),
        message_obj=NS(group_id=group, message_id=msg, raw_message={}),
    )


async def test_discovery_and_version():
    # 构造后（initialize 前）即可获取服务并查询状态
    async with service(initial=False) as (svc, pm, _):
        s1 = svc.get_service()
        t(
            "构造后即可取得服务且为同一对象",
            svc.get_service() is s1
            and svc.get_service(1) is s1
            and svc.get_service(api_version=1) is s1,
        )
        status = s1.get_status()
        t(
            "未初始化状态 initializing 且 ready=False",
            status["state"] == "initializing"
            and status["ready"] is False
            and status["reason"] is None,
            status,
        )
        t(
            "最低字段齐全（api_version/instance_id/state/ready/reason）",
            status["api_version"] == 1
            and isinstance(status["instance_id"], str)
            and status["instance_id"],
            status,
        )
        t("初始化前无实例快照", status["instances"] == {}, status["instances"])
        # 版本协商：仅真正的 int 1；bool/其他类型/其他值明确拒绝
        for bad in (2, 0, True, False, "1", 1.0, None):
            try:
                svc.get_service(bad)
                raised = False
            except PS.PluginServiceError as exc:
                raised = exc.code == "unsupported_version" and isinstance(
                    exc, RuntimeError
                )
            t(f"api_version={bad!r} 拒绝为 unsupported_version", raised)
        # capabilities：试点固定能力标识
        cap = s1.capabilities()
        t(
            "capabilities 为 api_version=1 + 固定 features",
            cap
            == {
                "api_version": 1,
                "features": [
                    "qq.instance",
                    "qq.events",
                    "qq.rich_message",
                    "qq.group",
                    "qq.c2c",
                    "qq.guild",
                    "qq.manage",
                ],
            },
            cap,
        )
        t(
            "capabilities 幂等且每次新列表（外部改不动常量）",
            s1.capabilities() == cap
            and s1.capabilities()["features"] is not cap["features"],
        )
        # 不透传任意属性：不泄露 config/HTTP/路由器/令牌，根服务无发送
        forbidden = (
            "config",
            "routes",
            "states",
            "patcher",
            "event_bus",
            "refstore",
            "registry",
            "client",
            "http",
            "token",
            "secret",
            "send_rich",
            "group",
            "c2c",
            "guild",
            "manage",
            "status",
            "recall",
        )
        leaks = [name for name in forbidden if hasattr(s1, name)]
        t("门面不透传内部对象与根级发送", not leaks, leaks)
        try:
            s1.anything_else
            no_attr = False
        except AttributeError:
            no_attr = True
        t("未知属性明确 AttributeError", no_attr)
    # 每次加载（新 Main 实例）instance_id 不同
    ids = set()
    for _ in range(2):
        async with service(initial=False) as (svc, pm, _):
            ids.add(svc.get_service().instance_id)
    t("不同加载 instance_id 不同", len(ids) == 2, ids)


async def test_state_lifecycle_and_wait_ready():
    async with service() as (svc, pm, inst):
        facade = svc.get_service()
        status = facade.get_status()
        t(
            "initialize 成功置 ready（reason=None）",
            status["state"] == "ready"
            and status["ready"] is True
            and status["reason"] is None,
            status,
        )
        # 状态查询无副作用：不 refresh 路由、不发请求、快照稳定
        svc.refresh()
        before_changes = len(svc.routes._changes)
        snap1 = facade.get_status()
        snap2 = facade.get_status()
        cap = facade.capabilities()
        t(
            "get_status/capabilities 不消费路由变更、快照稳定",
            len(svc.routes._changes) == before_changes
            and snap1 == snap2
            and cap == facade.capabilities(),
            {"changes": len(svc.routes._changes), "snap1": snap1, "snap2": snap2},
        )
        t("状态查询不触发网络", not inst.client.http.calls, inst.client.http.calls)
        t(
            "实例快照只含安全字段",
            all(
                set(v) <= set(PS._SAFE_INSTANCE_FIELDS)
                for v in snap1["instances"].values()
            )
            and snap1["instances"]["A"]["appid"] == "bot-A"
            and isinstance(snap1["instances"]["A"]["transport_ready"], bool),
            snap1["instances"],
        )
        dumped = json.dumps(snap1)
        t(
            "状态快照无 token/secret/config 字样",
            "token" not in dumped and "secret" not in dumped and "config" not in dumped,
        )
        # wait_ready 成功返回 get_status 同形快照
        waited = await facade.wait_ready(timeout=2)
        t(
            "wait_ready 返回同形快照",
            waited["ready"] is True
            and waited["instance_id"] == facade.instance_id
            and set(waited)
            >= {"api_version", "instance_id", "state", "ready", "reason"},
            waited,
        )
        # 兼容入口：Main.ready / Main.wait_ready 布尔语义
        t("旧 ready 属性与门面一致", svc.ready is True)
        t("旧 wait_ready 就绪返回 True", await svc.wait_ready(timeout=1) is True)
        # 根服务 ready ≠ 平台连接可用性分离：快照独立可得
        t(
            "根 ready 与实例快照并存",
            svc.get_service().get_status()["ready"]
            and "A" in svc.get_service().get_status()["instances"],
        )
    # 超时：未初始化的服务的门面 wait_ready 抛 TimeoutError
    async with service(initial=False) as (svc, pm, _):
        facade = svc.get_service()
        try:
            await facade.wait_ready(timeout=0.15)
            raised = False
        except TimeoutError:
            raised = True
        t("门面 wait_ready 超时抛 TimeoutError", raised)
        t(
            "旧 wait_ready 超时仍返回 False（布尔兼容）",
            await svc.wait_ready(timeout=0.15) is False,
        )
    # initialize 失败 → unavailable（不自动恢复，等超时）
    async with service(initial=False) as (svc, pm, _):

        async def boom():
            raise RuntimeError("config broken")

        svc._initialize = boom
        try:
            await svc.initialize()
            failed = False
        except RuntimeError:
            failed = True
        facade = svc.get_service()
        status = facade.get_status()
        t(
            "initialize 失败置 unavailable 并原样抛出",
            failed
            and status["state"] == "unavailable"
            and status["reason"] == "initialize_failed"
            and status["ready"] is False,
            status,
        )
        try:
            await facade.wait_ready(timeout=0.15)
            raised = False
        except TimeoutError:
            raised = True
        t("unavailable 的 wait_ready 走超时而非关闭", raised)


async def test_terminate_and_closed_refs():
    async with service() as (svc, pm, inst):
        facade = svc.get_service()
        view = facade.instance("A")
        await view.group.info("G")
        orig = svc._terminate

        async def probe():
            t(
                "terminate 一开始即 closing（拒绝新业务）",
                facade.state == "closing" and svc.ready is False,
                facade.state,
            )
            await orig()

        svc._terminate = probe
        await svc.terminate()
        t("terminate 完成后永久 closed", facade.state == "closed")
        status = facade.get_status()
        t(
            "关闭后 get_status 仍可查询（reason=service_closed）",
            status["state"] == "closed"
            and status["ready"] is False
            and status["reason"] == "service_closed",
            status,
        )
        t("关闭后 capabilities 仍可查询", facade.capabilities()["api_version"] == 1)
        # 新业务全部拒绝：稳定 code=service_closed
        for name, call in (
            ("instance", lambda: facade.instance("A")),
            ("for_event", lambda: facade.for_event(native_event(inst))),
            ("on", lambda: facade.on("GROUP_ADD_ROBOT", lambda ev: None)),
            ("on_any", lambda: facade.on_any(lambda ev: None)),
            ("ref_from_event", lambda: facade.ref_from_event(native_event(inst))),
        ):
            try:
                call()
                rejected = False
            except PS.PluginServiceError as exc:
                rejected = exc.code == "service_closed" and isinstance(
                    exc, RuntimeError
                )
            t(f"closed 后 {name} 拒绝（code=service_closed）", rejected)
        try:
            await facade.wait_ready(timeout=1)
            raised = False
        except PS.PluginServiceError as exc:
            raised = exc.code == "service_closed"
        t("closed 后 wait_ready 抛 service_closed（非超时）", raised)
        # 旧视图调用同样失效（路由层已停用，不触碰本体资源）
        before = len(inst.client.http.calls)
        try:
            await view.group.info("G")
            dead = False
        except Exception:
            dead = True
        t(
            "closed 前创建的旧视图调用失效",
            dead and len(inst.client.http.calls) == before,
        )


async def test_reload_new_service_identity():
    manager = NS(_inst_map={"A": {"inst": adapter()}})
    svc1 = M.Main(NS(platform_manager=manager), {"retry_max": 0})
    await svc1.initialize()
    old_view = svc1.get_service().instance("A")
    await svc1.terminate()
    # 重载：新 Main 实例 → 新服务、新 instance_id，旧服务保持失效
    svc2 = M.Main(NS(platform_manager=manager), {"retry_max": 0})
    await svc2.initialize()
    try:
        f1, f2 = svc1.get_service(), svc2.get_service()
        t(
            "重载后旧服务 closed、新服务 ready、instance_id 不同",
            f1.state == "closed"
            and f2.state == "ready"
            and f2.get_status()["ready"] is True
            and f1.instance_id != f2.instance_id,
            {"f1": f1.state, "f2": f2.state},
        )
        t(
            "旧服务对象不复活",
            svc1.get_service() is not f2 and f1.get_status()["state"] == "closed",
        )
        # 旧事件/旧视图不跨代次复活（同身份重载跟随属于新视图，不改造旧视图）
        try:
            await old_view.group.info("G")
            stale_rejected = False
        except Exception:
            stale_rejected = True
        t("重载前创建的旧视图不跟随复活", stale_rejected)
        new_view = f2.instance("A")
        await new_view.group.info("G")
        t("新服务视图正常（同身份跟随重载走新视图）", True)
    finally:
        await svc2.terminate()


async def test_facade_binding_routing():
    async with service() as (svc, pm, inst):
        facade = svc.get_service()
        # 主动调用：门面 instance → 视图 → 本体当前实例
        await facade.instance("A").group.info("G")
        t(
            "门面 instance 路由到本体当前机器人",
            inst.client.http.calls[-1][1] == "/v2/groups/G/info"
            and inst.client.http.calls[-1][0] == "GET",
        )
        # 事件回复：门面 for_event 自动携带事件目标
        await facade.for_event(native_event(inst)).send_rich(content="reply")
        _, path, kw = inst.client.http.calls[-1]
        t(
            "门面 for_event 视图 send_rich 自动携带 msg_id",
            path == "/v2/groups/G/messages" and kw["json"].get("msg_id") == "M",
            (path, kw),
        )
        # 改绑机器人：门面创建的旧视图明确拒绝，不发给新机器人
        old_view = facade.instance("A")
        other = adapter(appid="two")
        pm._inst_map["A"] = {"inst": other}
        try:
            await old_view.group.info("G")
            rejected = False
        except Exception:
            rejected = True
        t(
            "门面视图改绑 AppID 后拒绝且不发给新机器人",
            rejected and not other.client.http.calls,
        )
        # 同身份重载：门面新视图跟随
        reloaded = adapter(appid="bot-A")
        pm._inst_map["A"] = {"inst": reloaded}
        await facade.instance("A").group.info("G")
        t(
            "门面新视图跟随同身份重载",
            reloaded.client.http.calls[-1][1] == "/v2/groups/G/info",
        )
        # 订阅与解绑（订阅触发挂载刷新，事件从当前代次 client 进入）
        current = reloaded
        received, any_received = [], []
        unsub = facade.on("GROUP_ADD_ROBOT", received.append)
        unsub_any = facade.on_any(any_received.append)
        await current.client.on_group_add_robot({"id": "E", "group_openid": "G"})
        await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t(
            "门面 on/on_any 收到扩展事件",
            len(received) == 1 and len(any_received) == 1,
            (len(received), len(any_received)),
        )
        t(
            "事件携带不可变来源且身份正确",
            received[0].source.platform_id == "A"
            and received[0].source.robot_key.appid == "bot-A",
        )
        unsub()
        unsub_any()
        got = (len(received), len(any_received))
        await current.client.on_group_add_robot({"id": "E2", "group_openid": "G"})
        await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t(
            "解绑闭包生效",
            len(received) == got[0] and len(any_received) == got[1],
            (got, (len(received), len(any_received))),
        )
        # 构建器委托：与 core.builders 产物一致
        btn = facade.btn("签到", data="/签到", enter=True)
        t(
            "md/btn/reference 委托产物一致",
            facade.md("# 标题\n正文") == B.md("# 标题\n正文")
            and btn == B.btn("签到", data="/签到", enter=True)
            and facade.reference("REFIDX_x") == B.reference("REFIDX_x"),
        )
        t(
            "kb 链式构建可用",
            facade.kb().row(btn).build()
            == {"keyboard": {"content": {"rows": [{"buttons": [btn]}]}}},
        )
        # 引用：ref_from_event 按来源机器人命名空间入库
        event = NS(
            get_platform_id=lambda: "A",
            bot=current.client,
            platform=NS(name="qq_official"),
            message_obj=NS(
                group_id="G",
                message_id="M9",
                sender=None,
                raw_message=NS(
                    raw_data={
                        "id": "M9",
                        "group_openid": "G",
                        "message_scene": {"ext": ["msg_idx=REFIDX_SDK"]},
                    }
                ),
            ),
        )
        t(
            "门面 ref_from_event 从 raw_data 入库并返回",
            facade.ref_from_event(event) == "REFIDX_SDK",
        )


async def test_readme_binding_checks_service_readiness():
    """执行 README 接入示例，确认未就绪与已关闭服务不会被绑定。"""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    example = text.split("## 使用\n", 1)[1].split("```python\n", 1)[1]
    example = example.split("```", 1)[0]
    event_module = sys.modules["astrbot.api.event"]
    previous_filter = event_module.filter
    event_module.filter = NS(
        **vars(previous_filter),
        on_plugin_loaded=lambda *a, **kw: lambda fn: fn,
        on_plugin_unloaded=lambda *a, **kw: lambda fn: fn,
    )
    namespace = {}
    try:
        exec(compile(example, str(ROOT / "README.md"), "exec"), namespace)
    finally:
        event_module.filter = previous_filter
    async with service(initial=False) as (svc, pm, _):
        metadata = NS(activated=True, star_cls=svc)
        context = NS(get_registered_star=lambda name: metadata)
        consumer = namespace["MyPlugin"](context)
        t(
            "README 不绑定 initializing 服务",
            not consumer._try_bind() and consumer.qq is None,
        )
        await svc.initialize()
        t(
            "README 绑定真实就绪服务",
            consumer._try_bind() and consumer.qq is svc.get_service(),
        )
        t(
            "README 重复绑定不重复订阅",
            consumer._try_bind() and len(consumer._unsubs) == 1,
        )
        await svc.terminate()
        t(
            "README 释放已关闭服务",
            not consumer._try_bind() and consumer.qq is None and not consumer._unsubs,
        )


async def _main():
    await test_discovery_and_version()
    await test_state_lifecycle_and_wait_ready()
    await test_terminate_and_closed_refs()
    await test_reload_new_service_identity()
    await test_facade_binding_routing()
    await test_readme_binding_checks_service_readiness()


if __name__ == "__main__":
    asyncio.run(_main())
    print(f"\nALL {ok} CHECKS PASSED")
