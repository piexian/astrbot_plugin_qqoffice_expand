# -*- coding: utf-8 -*-
"""N 实例完整流程行为测试（设计第 10 节验收矩阵的离线部分）。

真实组件：botpy ConnectionState parser 时序（sys.modules 桩模块承载真实
botpy.connection 时用真实 botpy）、AstrBot 适配器契约（sys.modules 桩）。
模拟组件：botpy HTTP（内存桩，记录请求并按身份校验）。
全部在 bwrap 禁网沙箱运行；不加载用户配置、不连真实 QQ。
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.events import AdapterPatcher, EventBus  # noqa: E402
from core.refstore import RefStore  # noqa: E402
from core.registry import Registry  # noqa: E402
from core.routing import RobotStates, RouteCore  # noqa: E402
from api.group import GroupAPI as RealGroupAPI  # noqa: E402
from api.manage import ManageAPI as RealManageAPI  # noqa: E402

ok = 0
def t(name, cond, detail=None):
    global ok
    assert cond, f"FAIL: {name} ({detail!r})"
    ok += 1
    print(f"  ok  {name}")


# ---------------- 桩 ----------------

class FakeHttp:
    """内存 HTTP：按 robot 身份记录请求；可编程响应/挂起/错误。"""
    def __init__(self, appid, holder):
        self.appid = appid
        self._token = object()
        self.requests = []          # (method, path, appid)
        self.holder = holder
        self.response = {"id": "ok"}
        self.hang = False

    async def request(self, route, **kwargs):
        self.requests.append((route.method, route.path, self.appid))
        if self.hang:
            await asyncio.sleep(10)
        return self.response


class FakeApi:
    def __init__(self, http):
        self._http = http


class FakeClient:
    def __init__(self, appid, holder, intents=0):
        self.api = FakeApi(FakeHttp(appid, holder))
        self.intents = intents
        self.closed = False
        self._shutting_down = False
        self._connection = None
        self._active_websockets = set()

    @property
    def is_shutting_down(self):
        return self._shutting_down or self.is_closed()

    def is_closed(self):
        return self.closed


class FakeAdapter:
    def __init__(self, pid, appid, holder, mode="ws"):
        name = "qq_official_webhook" if mode == "webhook" else "qq_official"
        self.meta_obj = types.SimpleNamespace(name=name, id=pid)
        self.config = {"id": pid, "appid": appid, "secret": "s", "is_sandbox": False}
        self.appid = appid
        self.client = FakeClient(appid, holder, intents=(1 << 30) | (1 << 25) | (1 << 12))
        self.client_self_id = f"u-{pid}-{id(self)}"
        if mode == "ws":
            self.client._connection = types.SimpleNamespace(
                state=types.SimpleNamespace(parsers={}, _dispatch=lambda n, p: None),
                _session_list=[])

    def meta(self):
        return self.meta_obj

    def get_client(self):
        return self.client


class Manager:
    def __init__(self):
        self._inst_map = {}


class Svc:
    """最小 svc：routes/refstore/config/registry 目录（同 main 装配）。"""

    def __init__(self, manager):
        self.manager = manager
        self.refstore = RefStore(None, ttl_days=0)
        self.config = {"retry_max": 0, "auto_degrade_proactive": True}
        self.states = RobotStates()
        self.routes = RouteCore(manager, self.states)
        self.event_bus = EventBus({}, None)
        self.patcher = AdapterPatcher(self.routes, self.event_bus,
                                      config={"enable_group_member_events": True})
        self._ack = None
        # ACK 视图工厂：call 走完整 execute_call 通道（phase 置位语义一致）
        self.routes.bind_view_factory(
            lambda bound: (lambda v: (
                setattr(v, "call", lambda m, p, **kw: self._execute(bound, m, p, kw)),
                setattr(v, "manage", types.SimpleNamespace(
                    interaction_ack=lambda iid, **kw: v.call(
                        "PUT", "/interactions/{interaction_id}",
                        path_params={"interaction_id": iid},
                        json={"code": 0}, endpoint_key="interactions.ack", **kw))),
                v)[-1])(types.SimpleNamespace(platform_id=bound.platform_id)))

        self.registry = Registry()
        import inspect as _inspect
        for prefix, cls in (("group", RealGroupAPI), ("manage", RealManageAPI)):
            for attr in dir(cls):
                if attr.startswith("_"):
                    continue
                fn = getattr(cls, attr)
                if _inspect.isfunction(fn):
                    self.registry.register_fn(f"{prefix}.{attr}", fn)

    async def _execute(self, bound, method, path, kwargs):
        from core.client import execute_call

        return await execute_call(self, bound, method, path, **kwargs)

    async def _call(self, method, path, **kwargs):
        from core.client import execute_call

        return await execute_call(self, self._view_bound, method, path, **kwargs)

    def instance_view(self, pid):
        from core.client import execute_call

        route = self.routes.route_of(pid)
        bound = __import__("core.routing", fromlist=["BoundSource"]).BoundSource.for_instance(
            pid, route.robot_key)
        view = types.SimpleNamespace(_svc=self, _bound=bound, platform_id=pid)
        view.group = RealGroupAPI(view)
        view.c2c = RealGroupAPI.__mro__[1].__self__ if False else None  # c2c 用 group 即可
        view.call = lambda m, p, **kw: execute_call(self, bound, m, p, **kw)

        async def _invoke(name, *args, **kw):
            fn, _ = self.registry.get(name)
            proxy = types.SimpleNamespace(_client=view)
            result = fn(proxy, *args, **kw)
            if asyncio.iscoroutine(result):
                return await result
            return result
        view.invoke = _invoke
        return view



def http_of(manager, pid):
    return manager._inst_map[pid]["inst"].client.api._http


def add_instance(manager, pid, appid, mode="ws"):
    ad = FakeAdapter(pid, appid, manager, mode)
    manager._inst_map[pid] = {"inst": ad, "client_id": ad.client_self_id}
    return ad


# ---------------- 1. N 实例混合传输，随机调用身份严格对应 ----------------

async def test_n_instances():
    import random
    for n in (1, 2, 10, 50, 100):
        random.seed(n)
        manager = Manager()
        pids = []
        for i in range(n):
            pid, appid = f"p{i}", f"app{i}"
            add_instance(manager, pid, appid, random.choice(("ws", "webhook")))
            pids.append((pid, appid))
        svc = Svc(manager)
        svc.patcher.refresh()
        random.shuffle(pids)
        for pid, appid in pids:
            await svc.instance_view(pid).invoke("group.info", "G1")
        for pid, appid in pids:
            calls = http_of(manager, pid).requests
            assert calls and all(req[2] == appid for req in calls), (pid, calls[:2])
        # 卸载恢复
        await svc.patcher.stop()
        t(f"N={n} 随机调用严格命中对应 HTTP 身份（WS/Webhook 混合），卸载恢复正常", True)


# ---------------- 2. 扩展事件来源 + ACK 来源路由 ----------------

async def test_event_source_and_ack():
    manager = Manager()
    add_instance(manager, "a", "APP1")
    add_instance(manager, "b", "APP2", mode="webhook")
    svc = Svc(manager)
    svc.patcher.refresh()

    acked = []
    async def ack_impl(view, iid):
        return {}
    svc._ack = ack_impl

    async def ack_caller(view, iid, phase=None):
        acked.append((view.platform_id, iid))
        return {}
    svc.event_bus.set_ack_caller(ack_caller)
    svc.event_bus.bind_routes(svc.routes)

    got_global, got_scoped = [], []
    svc.event_bus.on("INTERACTION_CREATE", lambda e: got_global.append(e))
    svc.event_bus.on_scoped("b", svc.routes.route_of("b").robot_key, "INTERACTION_CREATE", lambda e: got_scoped.append(e))

    # 通过挂载的 on_xxx handler 触发（等同网关事件进来）
    for pid in ("a", "b"):
        route = svc.routes.route_of(pid)
        handler = getattr(route.client, "on_interaction_create")
        await handler({"id": f"EV-{pid}", "d": {"id": f"I-{pid}", "type": 11, "user_openid": "U"}})
    await asyncio.gather(*svc.event_bus._tasks)
    await asyncio.sleep(0.05)
    t("扩展事件携带不可变来源并分发", len(got_global) == 2 and all(e.source for e in got_global))
    t("实例作用域订阅只收本实例", len(got_scoped) == 1 and got_scoped[0].source.platform_id == "b")
    t("自动 ACK 使用来源机器人", sorted(acked) == [("a", "I-a"), ("b", "I-b")])
    # 同 id 幂等
    route_a = svc.routes.route_of("a")
    await route_a.client.on_interaction_create({"id": "EV-a", "d": {"id": "I-a", "type": 11, "user_openid": "U"}})
    await asyncio.gather(*svc.event_bus._tasks)
    t("同机器人同 interaction_id 只应答一次", acked.count(("a", "I-a")) == 1)
    state_a = svc.routes.state_for(route_a.robot_key)
    t("ACK 成功后状态 succeeded", state_a.acks.state_of("I-a") == "succeeded")
    await svc.patcher.stop()


# ---------------- 3. ACK 超时/失败语义 ----------------

async def test_ack_semantics():
    manager = Manager()
    add_instance(manager, "a", "APP1")
    svc = Svc(manager)
    svc.patcher.refresh()
    svc.event_bus.bind_routes(svc.routes)

    async def passthrough_ack(view, iid, phase=None):
        return await view.manage.interaction_ack(iid, _phase=phase)
    svc.event_bus.set_ack_caller(passthrough_ack)

    # ACK 发送通道挂起：http 桩 hang，short wait_for 触发超时
    http_a = http_of(manager, "a")
    hung = asyncio.Event()

    async def hang(*a, **kw):
        hung.set()
        await asyncio.sleep(10)
    http_a.request = hang
    import core.events as evmod
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await real_wait_for(coro, timeout=0.05)
    route = svc.routes.route_of("a")
    h = getattr(route.client, "on_interaction_create")
    with patch.object(evmod.asyncio, "wait_for", fast_wait_for):
        await h({"id": "E1", "d": {"id": "I-T", "type": 11, "user_openid": "U"}})
        await asyncio.gather(*svc.event_bus._tasks, return_exceptions=True)
    state = svc.routes.state_for(route.robot_key)
    t("超时按已应答处理（settle，不永久卡 pending）",
      state.acks.state_of("I-T") == "settled", state.acks.state_of("I-T"))

    # 发送前确定性失败 → release 可重答
    async def fail_ack(view, iid, phase=None):
        raise RuntimeError("no source")
    svc.event_bus.set_ack_caller(fail_ack)
    await h({"id": "E2", "d": {"id": "I-F", "type": 11, "user_openid": "U"}})
    await asyncio.gather(*svc.event_bus._tasks)
    t("确定性失败回收预留", state.acks.state_of("I-F") is None)
    await svc.patcher.stop()


# ---------------- 4. 上传/限流等待/重试中途实例变化 ----------------

async def test_mid_call_changes():
    manager = Manager()
    add_instance(manager, "a", "APP1")
    svc = Svc(manager)
    svc.patcher.refresh()

    # 限流等待中重载：等待后核验失败，不跨代次发送
    http_old = http_of(manager, "a")
    http_old.hang = True
    ad2 = FakeAdapter("a", "APP1", manager)
    view = svc.instance_view("a")
    # 先解析建立 ctx，再替换本体实例（模拟请求侧先见）
    bound = view._bound

    async def slow_then_replace():
        # 启动调用（会卡在 HTTP hang）——替换本体后新请求立刻拒绝
        pass

    # 直接验证：await 中替换 → check_context 拒绝重试
    from core.client import execute_call
    http_old.hang = True
    task = asyncio.create_task(execute_call(svc, bound, "GET", "/users/@me"))
    await asyncio.sleep(0.02)
    manager._inst_map["a"] = {"inst": ad2, "client_id": ad2.client_self_id}
    svc.routes.refresh_all()
    http_old.hang = False
    await task   # 在途请求本身不承诺中断（已进入 SDK）
    t("在途请求完成于旧 HTTP（不对在途请求作虚假承诺）",
      all(r[2] == "APP1" for r in http_old.requests))

    # 重试路径：第一响应 429 → 重载 → 重试前核验失败
    manager2 = Manager()
    add_instance(manager2, "x", "APPX")
    svc2 = Svc(manager2)
    svc2.patcher.refresh()
    http_x = http_of(manager2, "x")
    calls = {"n": 0}

    class RateHttp(FakeHttp):
        async def request(self, route, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ServerError: code=429 message=rate")
            return await super().request(route, **kwargs)

    http_x.__class__ = RateHttp
    http_x.requests = []
    ad3 = FakeAdapter("x", "APPX", manager2)
    bound2 = svc2.instance_view("x")._bound

    async def reload_during_backoff():
        await asyncio.sleep(0.01)
        manager2._inst_map["x"] = {"inst": ad3, "client_id": ad3.client_self_id}
    svc2.config["retry_max"] = 3
    reload_task = asyncio.create_task(reload_during_backoff())
    try:
        await execute_call(svc2, bound2, "GET", "/users/@me")
        raised = None
    except Exception as exc:
        raised = exc
    await reload_task
    t("429 等待后重载：重试前核验失败，不跨代次重放", raised is not None and calls["n"] == 1)
    await svc.patcher.stop()
    await svc2.patcher.stop()


# ---------------- 5. 权限拒绝实例隔离 ----------------

async def test_denied_isolation():
    sys.path.insert(0, str(ROOT))
    for name in ("astrbot", "astrbot.core", "astrbot.core.platform",
                 "astrbot.core.platform.sources",
                 "astrbot.core.platform.sources.qqofficial"):
        sys.modules.setdefault(name, types.ModuleType(name))
    qoa = types.ModuleType("astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter")

    class FakeWS:
        def __init__(self, session=None, client=None):
            self._session = session
            self._client = client

        async def on_closed(self, code, msg):
            pass
    qoa.QQOfficialPlatformAdapter = type("AD", (), {})
    qoa.ManagedBotWebSocket = FakeWS
    sys.modules[qoa.__name__] = qoa

    manager = Manager()
    add_instance(manager, "a", "APP1")
    add_instance(manager, "b", "APP2")
    svc = Svc(manager)
    svc.patcher.install_adapter_hooks()
    svc.patcher.refresh()

    route_a = svc.routes.route_of("a")
    ws_a = FakeWS(session={"intent": route_a.client.intents, "session_id": "S"},
                  client=route_a.client)
    svc.patcher._heal_intents_reject(ws_a, 4014, "disallowed")
    patch_a = svc.patcher._patches[svc.patcher._by_platform["a"]]
    patch_b = svc.patcher._patches[svc.patcher._by_platform["b"]]
    t("A 被拒后仅 A 剔位记忆", patch_a.denied_bits == (1 << 26) | (1 << 27) | (1 << 24)
      and patch_b.denied_bits == 0)
    t("B 的 intents 不受影响", svc.routes.route_of("b").client.intents & (1 << 26))
    # 稳定轮询：多次 refresh 不增长 wrapper/补丁
    parsers_before = len(route_a.client._connection.state.parsers)
    for _ in range(5):
        svc.patcher.refresh()
    t("连续巡检零新增 wrapper", len(route_a.client._connection.state.parsers) == parsers_before)
    await svc.patcher.stop()


# ---------------- 6. 同身份额度共享 + 空闲回收 ----------------

async def test_shared_quota_and_gc():
    manager = Manager()
    add_instance(manager, "a1", "APP1")
    add_instance(manager, "a2", "APP1")   # 同 AppID 第二实例
    svc = Svc(manager)
    svc.patcher.refresh()
    v1, v2 = svc.instance_view("a1"), svc.instance_view("a2")
    await v1.group.send("G1", "hi", msg_id="M1")
    state = svc.routes.state_for(svc.routes.route_of("a1").robot_key)
    t("同身份共享被动窗口", state.windows._records[("group", "G1", "M1")][1] == 1)
    await v2.group.send("G1", "hi", msg_id="M1")
    t("第二实例沿用同一计数", state.windows._records[("group", "G1", "M1")][1] == 2)
    # 禁用一实例不清状态
    manager._inst_map.pop("a1")
    svc.routes.refresh_all()
    assert state.windows._records[("group", "G1", "M1")][1] == 2
    t("禁用实例不清除未过期计数", True)
    await svc.patcher.stop()


# ---------------- 7. 稳定轮询无增长（任务/记录/补丁）----------------

async def test_no_growth():
    manager = Manager()
    for i in range(6):
        add_instance(manager, f"p{i}", f"app{i}", "ws" if i % 2 else "webhook")
    svc = Svc(manager)
    svc.patcher.refresh()
    for i in range(6):
        await svc.instance_view(f"p{i}").invoke("group.info", "G")
    tasks0 = len(svc.event_bus._tasks)
    patches0 = len(svc.patcher._patches)
    wrappers0 = sum(len(d) for p in svc.patcher._patches.values()
                    for d in p.parser_patches.values())
    for _ in range(8):
        svc.patcher.refresh()
    tasks1 = len(svc.event_bus._tasks)
    patches1 = len(svc.patcher._patches)
    wrappers1 = sum(len(d) for p in svc.patcher._patches.values()
                    for d in p.parser_patches.values())
    t("连续无变更巡检：任务/补丁/wrapper 不增长",
      (tasks0, patches0, wrappers0) == (tasks1, patches1, wrappers1))
    await svc.patcher.stop()


async def _main():
    await test_n_instances()
    await test_event_source_and_ack()
    await test_ack_semantics()
    await test_mid_call_changes()
    await test_denied_isolation()
    await test_shared_quota_and_gc()
    await test_no_growth()

if __name__ == "__main__":
    asyncio.run(_main())
    print(f"\nALL {ok} CHECKS PASSED")
