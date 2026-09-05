# -*- coding: utf-8 -*-
"""真实 Main/BoundView/EventBus 装配的行为回归（审查 probe 的仓库版）。

与 integration_test.py 的 Svc 桩不同：这里直接实例化 main.Main（仅 AstrBot
注册依赖为桩），确保装配接线与生产一致。botpy Client/ConnectionState 为
真实组件，HTTP 为内存桩。
"""
import asyncio
import importlib
import inspect
import sys
import types
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from botpy.client import Client
from botpy.connection import ConnectionState

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NS = types.SimpleNamespace

# —— AstrBot 注册依赖桩（真实 main.py 只需要这些）——
_api = types.ModuleType("astrbot.api")
_api.AstrBotConfig = dict
_api.logger = NS(**{k: (lambda *a, **kw: None) for k in ("info", "warning", "error", "debug")})
_event = types.ModuleType("astrbot.api.event")
_event.AstrMessageEvent = object
_event.filter = NS(
    command=lambda *a, **kw: (lambda fn: fn),
    on_platform_loaded=lambda *a, **kw: (lambda fn: fn),
)
_star = types.ModuleType("astrbot.api.star")


class _Star:
    def __init__(self, context, config=None):
        self.context = context


_star.Star, _star.Context = _Star, object
_star.StarTools = NS(get_data_dir=lambda *a: None)
sys.modules.update({
    "astrbot": types.ModuleType("astrbot"), "astrbot.api": _api,
    "astrbot.api.event": _event, "astrbot.api.star": _star,
})
_package = types.ModuleType("qqoffice_main_test")
_package.__path__ = [str(ROOT)]
sys.modules["qqoffice_main_test"] = _package
M = importlib.import_module("qqoffice_main_test.main")
R = importlib.import_module("qqoffice_main_test.core.routing")
C = importlib.import_module("qqoffice_main_test.core.client")

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
    client._connection = NS(state=ConnectionState(client.ws_dispatch, client.api),
                            _session_list=[])
    return NS(appid=appid, config={"id": pid, "appid": appid}, client=client,
              get_client=lambda: client, meta=lambda: NS(name="qq_official", id=pid),
              intents=NS(value=1 << 30))


@asynccontextmanager
async def service(initial=True, **config):
    manager = NS(_inst_map={})
    inst = adapter() if initial else None
    if inst:
        manager._inst_map["A"] = {"inst": inst}
    svc = M.Main(NS(platform_manager=manager), {"retry_max": 0, **config})
    await svc.initialize()
    try:
        yield svc, manager, inst
    finally:
        await svc.terminate()
        tasks = list(svc.event_bus._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_bindings():
    async with service(False) as (svc, pm, _):
        try:
            svc.instance("A")
            raised = False
        except Exception:
            raised = True   # 平台不可用时创建即拒绝，视图永远不会改绑身份
        t("实例不存在时创建视图即拒绝（不改绑身份）", raised)
    async with service() as (svc, pm, inst):
        # 创建时固定身份（bot-A）：跟随同身份重载
        view = svc.instance("A")
        reloaded = adapter(appid="bot-A")
        pm._inst_map["A"] = {"inst": reloaded}
        await view.manage.me()
        t("长期视图跟随同身份重载", len(reloaded.client.http.calls) == 1)
        # 改绑 AppID：旧视图拒绝，不发给新机器人
        second = adapter(appid="two")
        pm._inst_map["A"] = {"inst": second}
        try:
            await view.manage.me()
            rejected = False
        except Exception:
            rejected = True
        t("同 ID 改绑 AppID 后旧视图拒绝", rejected and not second.client.http.calls)
        # 重新绑定到 two（新视图），验证 guild.channels / 复合 helper / 继承 MediaAPI
        view = svc.instance("A")
        await view.invoke("guild.channels", "guild")
        await view.invoke("group.mute_member", "G", "M", "2026-09-06T00:00:00Z")
        t("invoke 覆盖 guild/复合 group/继承方法", True)
        current = svc.routes.route_of("A")
        event = NS(get_platform_id=lambda: "A", bot=current.client,
                   platform=NS(name="qq_official"),
                   message_obj=NS(group_id="G", message_id="M", raw_message={}))
        await svc.for_event(event).send_rich(content="reply")
        t("for_event 视图 send_rich 自动携带事件目标",
          current.client.http.calls[-1][1] == "/v2/groups/G/messages"
          and current.client.http.calls[-1][2]["json"]["msg_id"] == "M")


async def test_requests():
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        await view.group.info("G")
        quota = svc.states.get(view.robot_key).rate.proactive
        t("GET 不消耗主动消息配额", not quota._rel_day, dict(quota._rel_day))
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        next_inst = adapter(identity="new-generation")

        def swap(route, kw):
            pm._inst_map.update({"A": {"inst": next_inst}})
        inst.client.http.on_request = swap
        error = None
        try:
            await view.send_rich(scene="group", target_openid="G", content="hi",
                                 media="data:image/png;base64,bG9jYWwtdGVzdC1pbWFnZQ==",
                                 msg_id="M")
        except Exception as exc:
            error = type(exc).__name__
        t("上传后发送共用同一代次（不跨代次）",
          bool(inst.client.http.calls) and not next_inst.client.http.calls
          and error == "StaleSourceEvent",
          {"error": error, "old": [x[1] for x in inst.client.http.calls]})
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        state = svc.states.get(view.robot_key)

        async def stop_before_send(*a, **kw):
            pm._inst_map.clear()
        state.rate.acquire = stop_before_send
        try:
            await view.group.send("G", "reply", msg_id="M")
        except Exception:
            pass
        count = state.windows._records.get(("group", "G", "M"), [0, 0])[1]
        t("发送前失败释放被动预留", count == 0, count)
    async with service(retry_max=1) as (svc, pm, inst):
        view = svc.instance("A")
        inst.client.http.response = None
        with patch.object(C.asyncio, "sleep", AsyncMock()):
            try:
                await view.group.send("G", "reply", msg_id="M")
            except Exception:
                pass
        state = svc.states.get(view.robot_key)
        count = state.windows._records.get(("group", "G", "M"), [0, 0])[1]
        t("未知写结果不重放不退款",
          len(inst.client.http.calls) == 1 and count == 1,
          {"attempts": len(inst.client.http.calls), "reserved": count})


async def test_events():
    async with service() as (svc, pm, inst):
        handler = inst.client.on_interaction_create
        await handler({"id": "I", "type": 11})
        results = await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t("真实 Main 自动 ACK 已接线",
          bool(inst.client.http.calls)
          and not [x for x in results if isinstance(x, Exception)],
          [str(x) for x in results if isinstance(x, Exception)])
    async with service() as (svc, pm, inst):
        received = []
        svc.on("GROUP_ADD_ROBOT", received.append)
        handler = inst.client.on_group_add_robot
        pm._inst_map.clear()
        await handler({"id": "E", "group_openid": "G"})
        await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t("本体删除实例后旧 handler 不投递", not received, len(received))
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        view.on("MESSAGE_CREATE", lambda ev: None)
        svc.refresh()
        t("实例作用域订阅补 intents 位", bool(inst.client.intents & (1 << 9)),
          inst.client.intents)
        received = []
        view.on("GROUP_ADD_ROBOT", received.append)
        other = adapter(appid="different-app")
        pm._inst_map["A"] = {"inst": other}
        svc.refresh()
        await other.client.on_group_add_robot({"id": "E", "group_openid": "G"})
        await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t("作用域订阅按身份绑定（改绑不转移）", not received,
          [ev.source.robot_key.appid for ev in received])


async def test_cleanup():
    async with service() as (svc, pm, inst):
        state = svc.states.get(svc.instance("A").robot_key)
        with patch.object(R.time, "monotonic", return_value=0):
            state.acks.try_reserve("I")
        with patch.object(R.time, "monotonic", return_value=10000):
            svc._prune_idle_states()
        t("协调任务清理过期且非空状态", not state.acks.stats()["total"], state.acks.stats())
    async with service() as (svc, pm, inst):
        for _ in range(3):
            inst = adapter()
            pm._inst_map["A"] = {"inst": inst}
            svc.refresh()
        t("重载后不残留旧 state 引用", len(svc.patcher._states_by_id) == 1,
          len(svc.patcher._states_by_id))
        state = inst.client._connection.state
        await svc.terminate()
        t("卸载后类级 parser 不残留于既有 state", "group_member_add" not in state.parsers)
    async with service() as (svc, pm, inst):
        started = asyncio.Event()

        async def slow(ev):
            started.set()
            await asyncio.Event().wait()
        svc.on("GROUP_ADD_ROBOT", slow)
        await inst.client.on_group_add_robot({"id": "E", "group_openid": "G"})
        await started.wait()
        tasks = list(svc.event_bus._tasks)
        await svc.terminate()
        await asyncio.sleep(0)
        t("卸载取消并等待事件任务", all(task.done() for task in tasks))
    async with service(False) as (svc, pm, _):
        inst = adapter()
        base = inst.client.intents
        svc.patcher._inject_construction_intents(inst)
        pm._inst_map["A"] = {"inst": inst}
        svc.refresh()
        await svc.terminate()
        t("构造期注入的 intents 卸载时还原", inst.client.intents == base,
          {"base": base, "after": inst.client.intents})


async def test_edge_semantics():
    # —— 身份创建时固定（审查二轮 #1）——
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        other = adapter(appid="different")
        pm._inst_map["A"] = {"inst": other}
        try:
            await view.manage.me()
            rejected = False
        except M.QQOfficeRoutingError:
            rejected = True
        t("视图创建时固定身份（改绑后拒绝且不发给新机器人）",
          rejected and not other.client.http.calls)
    # —— 卸载后旧视图失效（#2）——
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        await view.manage.me()
        before = len(inst.client.http.calls)
        await svc.terminate()
        try:
            await view.manage.me()
            rejected = False
        except Exception:
            rejected = True
        t("插件卸载后旧视图立即失效", rejected and len(inst.client.http.calls) == before)
    # —— 扩展事件携带回复目标（#3）——
    async with service() as (svc, pm, inst):
        ev = M.QQOfficeEvent(
            type="GROUP_AT_MESSAGE_CREATE", name="group_at_message_create",
            payload_id="E", raw={"id": "M", "group_openid": "G"},
            scene="group", group_openid="G", message_id="M",
            source=svc.routes.source_of(svc.routes.ensure_current_route("A")))
        await svc.for_event(ev).send_rich(content="reply")
        _, path, kw = inst.client.http.calls[-1]
        t("扩展事件视图 send_rich 自动携带 msg_id（普通消息不伪造 event_id）",
          path.endswith("/G/messages") and kw["json"].get("msg_id") == "M"
          and "event_id" not in kw["json"], (path, kw))
        # 允许集合事件（GROUP_ADD_ROBOT）自动带 event_id + source，走同一校验
        ev_ok = M.QQOfficeEvent(
            type="GROUP_ADD_ROBOT", name="group_add_robot", payload_id="E2",
            raw={"id": "E2", "group_openid": "G"},
            scene="group", group_openid="G",
            source=svc.routes.source_of(svc.routes.ensure_current_route("A")))
        await svc.for_event(ev_ok).send_rich(content="welcome")
        kw2 = inst.client.http.calls[-1][2]["json"]
        t("允许集合事件自动 event_id 走 EVENT_ID_SCOPES 校验",
          kw2.get("event_id") == "E2" and "msg_id" not in kw2, kw2)
        # 不在允许集合的群事件（GROUP_MEMBER_ADD）：无 event_id，纯主动配额生效
        ev_mem = M.QQOfficeEvent(
            type="GROUP_MEMBER_ADD", name="group_member_add", payload_id="E3",
            raw={"id": "E3", "group_openid": "G"},
            scene="group", group_openid="G",
            source=svc.routes.source_of(svc.routes.ensure_current_route("A")))
        state_a = svc.states.get(svc.routes.route_of("A").robot_key)
        before_quota = len(state_a.rate.proactive._robot)
        await svc.for_event(ev_mem).send_rich(content="hi")
        kw3 = inst.client.http.calls[-1][2]["json"]
        t("非允许事件不带 event_id 且消耗主动配额",
          "event_id" not in kw3 and len(state_a.rate.proactive._robot) == before_quota + 1,
          kw3)
        # 频道扩展事件（MESSAGE_CREATE）补齐 msg_id 被动回复字段
        ev_g = M.QQOfficeEvent(
            type="MESSAGE_CREATE", name="message_create", payload_id="E4",
            raw={"id": "E4", "guild_id": "DM", "channel_id": "C"},
            scene="guild", guild_id="DM", channel_id="C", message_id="MC",
            source=svc.routes.source_of(svc.routes.ensure_current_route("A")))
        await svc.for_event(ev_g).send_rich(content="频道回复")
        path4 = inst.client.http.calls[-1][1]
        kw4 = inst.client.http.calls[-1][2]["json"]
        t("频道扩展事件保留 message_id 作 msg_id",
          path4 == "/channels/C/messages" and kw4.get("msg_id") == "MC", (path4, kw4))


async def test_dispatch_outcomes():
    # 被动回复自动生成 msg_seq
    async with service() as (svc, pm, inst):
        with patch.object(C, "generate_msg_seq", return_value=4242):
            await svc.instance("A").group.send("G", "reply", msg_id="M")
        payload = inst.client.http.calls[-1][2]["json"]
        t("被动回复自动生成 msg_seq", payload.get("msg_seq") == 4242, payload)
    # 不篡改共享 HTTP 方法
    async with service() as (svc, pm, inst):
        retained = []

        def observe_request(*a):
            retained.append(
                getattr(inst.client.http.request, "__func__", None) is HTTP.request)
        inst.client.http.on_request = observe_request
        await svc.instance("A").group.send("G", "reply", msg_id="M")
        t("不替换本体共享的 HTTP 方法", all(retained), retained)
    # 进入 SDK 后取消/未知异常保留消费
    for name, exception in (("cancel", asyncio.CancelledError()),
                            ("opaque", RuntimeError("after request started"))):
        async with service() as (svc, pm, inst):
            view = svc.instance("A")

            def abort(*a):
                raise exception
            inst.client.http.on_request = abort
            try:
                await view.group.send("G", "hello", msg_id="M")
            except BaseException:
                pass
            state = svc.states.get(view.robot_key)
            count = state.windows._records.get(("group", "G", "M"), [0, 0])[1]
            t(f"进入 SDK 后 {name} 保留预留不退款",
              len(inst.client.http.calls) == 1 and count == 1, count)
    # 未知结果写请求不重放（ACK PUT 与消息 POST）
    async with service(retry_max=1) as (svc, pm, inst):
        inst.client.http.response = None
        with patch.object(C.asyncio, "sleep", AsyncMock()):
            try:
                await svc.instance("A").manage.interaction_ack("I")
            except Exception:
                pass
        t("未知结果 ACK 不重试", len(inst.client.http.calls) == 1, len(inst.client.http.calls))
    # 自动 ACK：未知结果保留去重
    async with service() as (svc, pm, inst):
        inst.client.http.response = None
        handler = inst.client.on_interaction_create
        for _ in range(2):
            await handler({"id": "I", "type": 11})
            await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t("未知结果自动 ACK 不重复应答", len(inst.client.http.calls) == 1,
          len(inst.client.http.calls))
    # 明确拒绝的 ACK 可重答
    async with service() as (svc, pm, inst):
        def reject_once(*args):
            if len(inst.client.http.calls) == 1:
                raise C.QQOfficeAPIError(400, "explicit rejection")
        inst.client.http.on_request = reject_once
        for _ in range(2):
            await inst.client.on_interaction_create({"id": "I", "type": 11})
            await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t("明确拒绝的 ACK 释放后可重答", len(inst.client.http.calls) == 2,
          len(inst.client.http.calls))
    # 拒绝后重试等待中取消：释放
    async with service(retry_max=1) as (svc, pm, inst):
        def reject_first(*args):
            if len(inst.client.http.calls) == 1:
                raise C.QQOfficeAPIError(429, "rate rejection")

        async def cancel_retry_wait(*args):
            raise asyncio.CancelledError()
        inst.client.http.on_request = reject_first
        with patch.object(C.asyncio, "sleep", cancel_retry_wait):
            await inst.client.on_interaction_create({"id": "I", "type": 11})
            await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        await inst.client.on_interaction_create({"id": "I", "type": 11})
        await asyncio.gather(*list(svc.event_bus._tasks), return_exceptions=True)
        t("拒绝后取消的 ACK 释放且可重答", len(inst.client.http.calls) == 2,
          len(inst.client.http.calls))
    # 新尝试重置前次拒绝：第二次未知结果保留
    async with service(retry_max=1) as (svc, pm, inst):
        view = svc.instance("A")
        state = svc.states.get(view.robot_key)

        def reject_then_unknown(*args):
            if len(inst.client.http.calls) == 1:
                raise C.QQOfficeAPIError(429, "rate rejection")
            raise RuntimeError("second write outcome unknown")
        inst.client.http.on_request = reject_then_unknown
        with patch.object(C.asyncio, "sleep", AsyncMock()):
            try:
                await view.group.send("G", "reply", msg_id="M")
            except Exception:
                pass
        count = state.windows._records.get(("group", "G", "M"), [0, 0])[1]
        t("重试新尝试开始重置前次拒绝语义",
          count == 1 and len(inst.client.http.calls) == 2,
          (count, len(inst.client.http.calls)))
    # 明确拒绝 + 重试等待中禁用：退预留
    async with service(retry_max=1) as (svc, pm, inst):
        view = svc.instance("A")
        state = svc.states.get(view.robot_key)

        def rejected(*args):
            raise C.QQOfficeAPIError(429, "explicit rate rejection")
        inst.client.http.on_request = rejected

        async def retire_during_retry(*args):
            pm._inst_map.clear()
        with patch.object(C.asyncio, "sleep", retire_during_retry):
            try:
                await view.group.send("G", "reply", msg_id="M")
            except Exception:
                pass
        count = state.windows._records.get(("group", "G", "M"), [0, 0])[1]
        t("明确拒绝后路由失效：退预留不重放", count == 0 and len(inst.client.http.calls) == 1,
          count)
    # 真实 botpy BotHttp 发送后连接重置：不重放写操作
    from botpy.http import BotHttp
    async with service() as (svc, pm, inst):
        attempts = []

        class ResetResponse:
            async def __aenter__(self):
                attempts.append("sent")
                raise ConnectionResetError("connection reset after send")

            async def __aexit__(self, *args):
                pass
        native = BotHttp(timeout=20)
        native._token = object()
        native._session = NS(closed=True, request=lambda **kw: ResetResponse())
        native.check_session = AsyncMock()
        inst.client.api._http = inst.client.http = native
        try:
            await svc.instance("A").group.send("G", "reply", msg_id="M")
        except Exception:
            pass
        t("botpy 发送后连接重置不重放写操作", len(attempts) == 1, len(attempts))
    # 缓存命中在 await 后仍核验上下文
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        source = "data:image/png;base64,bG9jYWwtdGVzdC1pbWFnZQ=="
        await view.upload_media("group", "G", source)
        media_module = importlib.import_module("qqoffice_main_test.core.media")
        up = media_module.to_uploadable(source)
        data = await up.load_bytes()

        async def load_and_disable():
            pm._inst_map.clear()
            return data
        fake_up = NS(kind="base64", load_bytes=load_and_disable)
        with patch.object(media_module, "to_uploadable", return_value=fake_up):
            try:
                await view.upload_media("group", "G", source)
                rejected = False
            except M.QQOfficeRoutingError:
                rejected = True
        t("缓存命中也在 await 后核验同一上下文", rejected)


async def test_intent_ownership():
    # 逐 client 构造期还原
    async with service(False) as (svc, pm, _):
        instances = [adapter(pid=f"A{i}") for i in range(3)]
        bases = [x.client.intents for x in instances]
        for inst in instances:
            svc.patcher._inject_construction_intents(inst)
            pm._inst_map[inst.config["id"]] = {"inst": inst}
        svc.refresh()
        await svc.terminate()
        actual = [x.client.intents for x in instances]
        t("逐 client 构造期注入各自还原", actual == bases, {"before": bases, "after": actual})
    # 挂载前拒断按 client 记忆，不重申被拒位
    async with service(False) as (svc, pm, _):
        inst = adapter()
        native_bits = (1 << 30) | (1 << 24)
        inst.client.intents = native_bits
        inst.intents.value = native_bits
        svc.patcher._inject_construction_intents(inst)
        ws = NS(_client=inst.client, _session={"intent": inst.client.intents})
        svc.patcher._heal_intents_reject(ws, 4014, "test")
        after_reject = inst.client.intents
        pm._inst_map["A"] = {"inst": inst}
        svc.refresh()
        after_mount = inst.client.intents
        t("挂载前拒断按 client 记忆且不重申", after_reject == native_bits
          and after_mount == native_bits,
          {"base": native_bits, "rejected": after_reject, "mounted": after_mount})
    # scoped 订阅只刷新来源实例
    async with service() as (svc, pm, inst):
        other = adapter(pid="B")
        pm._inst_map["B"] = {"inst": other}
        svc.refresh()
        svc.instance("A").on("MESSAGE_CREATE", lambda ev: None)
        t("scoped 订阅只给来源实例补位",
          bool(inst.client.intents & (1 << 9)) and not (other.client.intents & (1 << 9)),
          {"A": inst.client.intents, "B": other.client.intents})


async def test_bounded_cleanup():
    from collections import OrderedDict, deque
    L = importlib.import_module("qqoffice_main_test.core.ratelimit")

    class Counted(OrderedDict):
        inspected = 0

        def __iter__(self):
            for key in super().__iter__():
                self.inspected += 1
                yield key

        def keys(self):
            return iter(self)

    quota = L._ProactiveQuota(certified=False)
    quota._rel_minute = Counted()
    quota._rel_day = Counted()
    today = int(L.time.time()) // 86400
    for i in range(1000):
        quota._rel_minute[str(i)] = deque([L.time.monotonic()])
        quota._rel_day[str(i)] = [today, 1]
    quota.prune_idle(limit=7)
    inspected = quota._rel_minute.inspected + quota._rel_day.inspected
    t("配额清理有界（不复制全表）", inspected <= 14, inspected)
    limiter = L.RateLimiter()
    limiter._buckets = Counted((str(i), L._Bucket(1, 60)) for i in range(1000))
    for b in limiter._buckets.values():
        b.events.append(L.time.monotonic())
    limiter.prune_idle(limit=7)
    t("桶清理有界", limiter._buckets.inspected <= 7, limiter._buckets.inspected)
    quota_rb = L._ProactiveQuota(certified=False)
    with patch.object(L.time, "monotonic", return_value=0):
        await quota_rb.consume("G")
    with patch.object(L.time, "monotonic", return_value=10000):
        quota_rb.prune_idle(limit=7)
    t("闲置清理裁剪机器人队列", not quota_rb._robot and not quota_rb._robot_second,
      (list(quota_rb._robot), list(quota_rb._robot_second)))
    # 公平轮转：minute 表有活跃目标时，纯 day 过期目标仍能被清理
    quota2 = L._ProactiveQuota(certified=False)
    quota2._rel_minute["active"] = deque([L.time.monotonic()])
    quota2._rel_day["active"] = [today, 1]
    quota2._rel_day["retired"] = [today - 3, 5]
    for _ in range(8):
        quota2.prune_idle(limit=2)
    t("day 过期目标不被活跃 minute 饿死", "retired" not in quota2._rel_day,
      dict(quota2._rel_day))
    # idle() 为结构空判定（不遍历关系表）
    quota3 = L._ProactiveQuota(certified=False)
    quota3._rel_day["old"] = [today - 3, 5]
    t("idle 用非空结构判定（当日记录保留则非 idle）", not quota3.idle())
    quota3.prune_idle(limit=8)
    t("有界轮次清完后可回收", quota3.idle(), dict(quota3._rel_day))
    # 限流等待者持有 bucket：不被清理回收重建（主代理 cleanup probe 已验证模式）
    async with service() as (svc, pm, inst):
        # 停用协调任务：避免它在 sleep 桩下无限刷新
        if svc._coordinator and not svc._coordinator.done():
            svc._coordinator.cancel()
            try:
                await svc._coordinator
            except (asyncio.CancelledError, Exception):
                pass
            svc._coordinator = None
        view = svc.instance("A")
        state = svc.states.get(view.robot_key)
        state.rate.register_limit("bot.me", 1, 1.0)
        now = [0.0]
        real_sleep = asyncio.sleep
        permits: asyncio.Queue = asyncio.Queue()
        entered = asyncio.Event()

        async def paced_sleep(seconds):
            # 逐次放行：每个 await sleep 消耗一个许可；许可耗尽时挂起。
            entered.set()   # 等待者已到达本桶的 sleep（供测试精确等待）
            await permits.get()

        with patch.object(L.time, "monotonic", lambda: now[0]), \
                patch.object(L.asyncio, "sleep", paced_sleep):
            await view.manage.me()              # t=0：第一次成功（无等待）
            assert len(inst.client.http.calls) == 1
            task = asyncio.create_task(view.manage.me())
            await entered.wait()                # 第二个等待者已进入本桶 sleep
            t("第二次请求进入同一桶等待（唯一等待者，_users==1）",
              state.rate._buckets["bot.me"]._users == 1,
              state.rate._buckets["bot.me"]._users)
            bucket_before = state.rate._buckets["bot.me"]
            now[0] = 2.0
            svc._prune_idle_states()            # 清理轮：不得回收被等待的桶
            bucket = state.rate._buckets.get("bot.me")
            t("限流等待中的 bucket 不被后台清理",
              bucket is not None and bucket._users > 0,
              {"bucket": bucket is not None})
            # 第三个请求：旧窗口已过（t=2 > 0+1s），同一桶内直接获得新额度
            await view.manage.me()             # 同一桶实例上成功（第 2 次 HTTP）
            t("新请求与等待者共用同一桶（不另建限流状态）",
              state.rate._buckets["bot.me"] is bucket_before)
            # 放一个许可：被限流的第二个请求醒来，但时间未推进（t=2，
            # events=[2.0]）→ 仍未获得额度，继续挂起，同窗口无额外放行
            permits.put_nowait(None)
            await real_sleep(0)
            await real_sleep(0)
            sent_now = sum(1 for c in inst.client.http.calls if c[1] == "/users/@me")
            t("同窗口没有额外放行（累计 2 次发送）", sent_now == 2, sent_now)
            t("被限流者仍挂起且持有同一桶", not task.done()
              and state.rate._buckets["bot.me"] is bucket_before)
            # 推进时间到 t=4，再给许可：被限流的请求完成（第 3 次 HTTP）
            now[0] = 4.0
            permits.put_nowait(None)
            await real_sleep(0)
            await real_sleep(0)
            await task
            sent_final = sum(1 for c in inst.client.http.calls if c[1] == "/users/@me")
            t("时间推进后等待者完成（最终 3 次发送）", sent_final == 3, sent_final)
        await svc.terminate()   # 测试内已停 coordinator，正常收尾其他资源
    # PassiveWindows 全局预算：一次 limit=1 至多检查/删除一项
    from core.routing import PassiveWindows
    pw = PassiveWindows()
    t0 = R.time.monotonic()
    with patch.object(R.time, "monotonic", return_value=t0):
        pw.reserve("group", "G", "M1")      # 队列到期 t0+300
        pw.reserve("c2c", "U", "M2")        # 队列到期 t0+3600
    # 群窗口到期：第一轮只清 group（全局预算 1）
    with patch.object(R.time, "monotonic", return_value=t0 + 400):
        removed = pw.prune_expired(limit=1)
        t("PassiveWindows 全局检查预算（limit=1 最多清 1 项）", removed == 1, removed)
        t("轮转保证另一 scene 未被同轮清除",
          pw._records.get(PassiveWindows._key("c2c", "U", "M2")) is not None)
    # c2c 窗口到期（3600s）：下一轮轮转清它
    with patch.object(R.time, "monotonic", return_value=t0 + 4000):
        removed2 = pw.prune_expired(limit=1)
        t("后续轮次清理另一 scene", removed2 == 1
          and pw._records.get(PassiveWindows._key("c2c", "U", "M2")) is None,
          removed2)
    store = M.RefStore(None)
    try:
        for i in range(513):
            store.cache_file_info(str(i), "group", "G", 1, "file", 3600)
    except Exception as exc:
        t("file 缓存全插件容量上限", False, repr(exc))
    else:
        t("file 缓存全插件容量上限", len(store._file_cache) <= 512, len(store._file_cache))
    # LRU recency：填满 512 → 访问最旧的 seed0 → 加 new：
    # seed0 因命中更新而保留，seed1（次旧、未访问）被淘汰，总数仍 512。
    fresh = M.RefStore(None)
    for i in range(512):
        fresh.cache_file_info(f"seed{i}", "group", "G", 1, "F", 3600)
    assert fresh.get_file_info("seed0", "group", "G", 1) == "F"   # seed0 命中 → 移到尾部
    fresh.cache_file_info("new", "group", "G", 1, "FN", 3600)
    t("LRU：命中过的 seed0 保留",
      fresh.get_file_info("seed0", "group", "G", 1) == "F")
    t("LRU：未命中的次旧 seed1 被淘汰",
      fresh.get_file_info("seed1", "group", "G", 1) is None)
    t("LRU：新条目存在且总数不超容量",
      fresh.get_file_info("new", "group", "G", 1) == "FN"
      and len(fresh._file_cache) == 512)


async def test_ref_source_binding():
    """引用查询按事件来源核验（改绑后旧事件不读新机器人命名空间）。"""
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        # 旧机器人（bot-A）写入 G/M 引用
        svc.refstore.record_inbound(
            f"{view.robot_key.prefix()}|group", "G", "M",
            {"message_scene": {"ext": ["msg_idx=REF_OLD"]}})
        # 本体改绑新 AppID；新机器人恰好有相同 G/M 记录
        other = adapter(appid="bot-B")
        pm._inst_map["A"] = {"inst": other}
        svc.refresh()
        svc.refstore.record_inbound(
            f"{svc.routes.route_of('A').robot_key.prefix()}|group", "G", "M",
            {"message_scene": {"ext": ["msg_idx=REF_NEW"]}})
        stale_event = NS(get_platform_id=lambda: "A", bot=inst.client,
                         platform=NS(name="qq_official"),
                         message_obj=NS(group_id="G", message_id="M", raw_message={}))
        t("过期原生事件引用查询返回 None（不读新机器人命名空间）",
          svc.ref_from_event(stale_event) is None)
        fresh_event = NS(get_platform_id=lambda: "A", bot=other.client,
                         platform=NS(name="qq_official"),
                         message_obj=NS(group_id="G", message_id="M", raw_message={}))
        t("新事件按当前机器人命名空间取引用",
          svc.ref_from_event(fresh_event) == "REF_NEW")


async def test_native_reference_ingest():
    """原生消息引用：ref_from_event 从本体 raw_data 入库并返回（正向）。"""
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        prefix = view.robot_key.prefix()
        for scene, raw, openid, msg_id in (
            ("group", {"id": "M", "group_openid": "G",
                       "message_scene": {"ext": ["msg_idx=REFIDX_G"]}}, "G", "M"),
            ("c2c", {"id": "M2", "user_openid": "U",
                     "msg_elements": [{"msg_idx": "REFIDX_U"}]}, "U", "M2"),
        ):
            # 模拟本体原生事件对象：message_obj.raw_message 带 raw_data；
            # C2C 事件本体填 sender.user_id（_resolve_event_target 契约）。
            event = NS(
                get_platform_id=lambda: "A", bot=inst.client,
                platform=NS(name="qq_official"),
                message_obj=NS(group_id=openid if scene == "group" else None,
                               message_id=msg_id,
                               sender=NS(user_id=openid if scene == "c2c" else None),
                               raw_message=NS(raw_data=raw)))
            ref = svc.ref_from_event(event)
            t(f"原生 {scene} 消息引用从 raw_data 入库并返回",
              ref == f"REFIDX_{'G' if scene == 'group' else 'U'}", ref)
            t(f"原生 {scene} 已按机器人命名空间缓存",
              svc.refstore.get_inbound(f"{prefix}|{scene}", openid, msg_id) == ref)
        # 过期来源不得把旧 raw_data 写入新机器人：改绑后未缓存事件返回 None
        other = adapter(appid="bot-B")
        pm._inst_map["A"] = {"inst": other}
        svc.refresh()
        stale = NS(get_platform_id=lambda: "A", bot=inst.client,
                   platform=NS(name="qq_official"),
                   message_obj=NS(group_id="G", message_id="M3",
                                  raw_message=NS(raw_data={
                                      "id": "M3", "group_openid": "G",
                                      "message_scene": {"ext": ["msg_idx=REFIDX_X"]}})))
        t("过期来源不入库不返回", svc.ref_from_event(stale) is None)
        t("新机器人命名空间无该记录",
          svc.refstore.get_inbound(
              f"{svc.routes.route_of('A').robot_key.prefix()}|group", "G", "M3")
          is None)


async def test_ownership_and_hooks():
    """intent 所有权与类级 hook 所有权（第三轮审查项）。"""
    # 逐 client 构造期注入 + 卸载还原 adapter.intents.value
    async with service(False) as (svc, pm, _):
        inst = adapter()
        base_value = inst.intents.value
        svc.patcher._inject_construction_intents(inst)
        pm._inst_map["A"] = {"inst": inst}
        svc.refresh()
        await svc.terminate()
        t("卸载还原 client.intents 与 adapter.intents.value",
          inst.client.intents == 1 << 30 and inst.intents.value == base_value,
          (inst.client.intents, inst.intents.value))

    # 卸载不覆盖后来第三方 __init__/on_closed wrapper；
    # 被第三方保留的本插件 wrapper 已停用（调用只透传本体行为）。
    for name in ("astrbot", "astrbot.core", "astrbot.core.platform",
                 "astrbot.core.platform.sources",
                 "astrbot.core.platform.sources.qqofficial"):
        sys.modules.setdefault(name, types.ModuleType(name))
    qoa = types.ModuleType(
        "astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter")

    class CoreAdapter:
        def __init__(self):
            self.__dict__.update(vars(adapter()))

    class WS:
        async def on_closed(self, code, msg):
            self.called = True

    qoa.QQOfficialPlatformAdapter = CoreAdapter
    qoa.ManagedBotWebSocket = WS
    sys.modules[qoa.__name__] = qoa
    with patch.dict(sys.modules, {qoa.__name__: qoa}):
        async with service(False) as (svc, pm, _):
            owned_init, owned_close = CoreAdapter.__init__, WS.on_closed

            def third_init(inst):
                owned_init(inst)

            async def third_close(ws, code, msg):
                await owned_close(ws, code, msg)
            CoreAdapter.__init__ = third_init
            WS.on_closed = third_close
            await svc.terminate()
            t("卸载不覆盖第三方 hook 属性",
              CoreAdapter.__init__ is third_init and WS.on_closed is third_close)
            after = CoreAdapter.__new__(CoreAdapter)
            third_init(after)
            t("被第三方保留的本插件 ctor wrapper 已停用（不再注入）",
              after.client.intents == 1 << 30, after.client.intents)

    # scoped 订阅解绑后不再算需求；改绑新身份不继承旧订阅位
    async with service() as (svc, pm, inst):
        view = svc.instance("A")
        unsub = view.on("MESSAGE_CREATE", lambda ev: None)
        unsub()
        bits = svc.event_bus.scoped_intent_bits("A", view.robot_key.prefix())
        t("解绑后空订阅不再申请 intents 位", not bits & (1 << 9), bits)
        view.on("MESSAGE_CREATE", lambda ev: None)
        new = adapter(appid="new-app")
        pm._inst_map["A"] = {"inst": new}
        svc.refresh()
        t("改绑新身份不继承旧订阅 intents 位",
          not new.client.intents & (1 << 9), new.client.intents)

    # 第三方包裹 → 旧插件卸载 → 新插件加载：构造注入恢复且能正常卸载
    with patch.dict(sys.modules, {qoa.__name__: qoa}):
        svc1 = M.Main(NS(platform_manager=NS(_inst_map={})), {"retry_max": 0})
        await svc1.initialize()
        svc1.patcher.install_adapter_hooks()

        owned_init2 = CoreAdapter.__init__

        def third_init2(inst):
            owned_init2(inst)

        CoreAdapter.__init__ = third_init2
        await svc1.terminate()   # 保留 third_init2，但清理自己的标记
        assert CoreAdapter.__init__ is third_init2

        svc2 = M.Main(NS(platform_manager=NS(_inst_map={})), {"retry_max": 0})
        await svc2.initialize()   # 新实例应能包裹 third_init2 并注入
        t("重载后新插件可穿过保留的第三方 wrapper 安装",
          CoreAdapter.__init__ is not third_init2
          and svc2.patcher._hooks_installed)
        fresh = CoreAdapter.__new__(CoreAdapter)
        CoreAdapter.__init__(fresh)
        t("重载后构造注入恢复（含 bit24）",
          bool(fresh.client.intents & (1 << 24)), fresh.client.intents)
        await svc2.terminate()
        t("新插件卸载恢复正常（穿过第三方链）",
          CoreAdapter.__init__ is third_init2)

    # 拒断还原 adapter.intents.value：挂载前 / 挂载后两条时序
    for timing in ("pre_mount", "post_mount"):
        async with service(False) as (svc, pm, _):
            inst = adapter()
            base_value = inst.intents.value
            svc.patcher._inject_construction_intents(inst)
            if timing == "post_mount":
                pm._inst_map["A"] = {"inst": inst}
                svc.refresh()
            ws = NS(_client=inst.client, _session={"intent": inst.client.intents})
            svc.patcher._heal_intents_reject(ws, 4014, "test")
            if timing == "pre_mount":
                pm._inst_map["A"] = {"inst": inst}
                svc.refresh()
            await svc.terminate()
            t(f"拒断后 adapter.intents.value 还原（{timing}）",
              inst.intents.value == base_value and inst.client.intents == 1 << 30,
              (inst.intents.value, inst.client.intents))


async def test_coordination_convergence():
    """协调差异收敛（真实 Main 装配，对应 coordination_review_probe）。"""
    # 场景 1：挂载 A → 请求侧 A→B（未挂载）→ 本体 B→C → refresh：
    # 折叠为单条 replaced(A, C)，不挂载中间代次，无幽灵。
    async with service() as (svc, pm, first):
        view = svc.instance("A")
        middle = adapter(identity="middle")
        pm._inst_map["A"] = {"inst": middle}
        await view.manage.me()          # 请求侧先见 first→middle
        final = adapter(identity="final")
        pm._inst_map["A"] = {"inst": final}
        svc.refresh()                    # 巡检见 middle→final，折叠消费
        patches = list(svc.patcher._patches.values())
        t("请求侧 A→B + 巡检 B→C 只挂最终代次",
          len(patches) == 1 and patches[0].route.inst is final
          and len(svc.patcher._states_by_id) == 1
          and not hasattr(middle.client, "on_group_add_robot"),
          {"patches": len(patches), "states": len(svc.patcher._states_by_id)})
        svc.refresh()
        t("下一轮巡检无幽灵残留",
          len(svc.patcher._patches) == 1 and len(svc.patcher._states_by_id) == 1)
    # 场景 2：挂载 A → 请求侧 A→B → 本体删除 B → refresh：只恢复旧补丁。
    async with service() as (svc, pm, first):
        view = svc.instance("A")
        middle = adapter(identity="middle")
        pm._inst_map["A"] = {"inst": middle}
        await view.manage.me()
        pm._inst_map.clear()
        svc.refresh()
        t("pending 中删除的实例不被挂载、旧补丁恢复",
          not svc.patcher._patches and not svc.patcher._states_by_id,
          {"patches": len(svc.patcher._patches),
           "states": len(svc.patcher._states_by_id)})


async def test_main_scale():
    """真实 Main N 规模路由（对应 scale_review_probe 的核心行为）。"""
    import random
    for n in (1, 2, 10, 50, 100):
        random.seed(n)
        async with service(False) as (svc, pm, _):
            pids = []
            for i in range(n):
                pid, appid = f"p{i}", f"app{i}"
                mode = "webhook" if i % 2 else "ws"
                inst = adapter(pid=pid, appid=appid)
                if mode == "ws":
                    inst.meta = lambda p=pid, a=appid: NS(name="qq_official", id=p)
                else:
                    inst.meta = lambda p=pid, a=appid: NS(name="qq_official_webhook", id=p)
                pm._inst_map[pid] = {"inst": inst}
                pids.append((pid, appid))
            svc.refresh()
            order = pids[:]
            random.shuffle(order)
            ok_calls = 0
            for pid, appid in order:
                await svc.instance(pid).call("GET", "/users/@me")
                ok_calls += 1
            identity_ok = True
            for pid, appid in pids:
                http = pm._inst_map[pid]["inst"].client.api._http
                if not http.calls or http.identity != appid:
                    identity_ok = False
            t(f"真实 Main N={n} 随机调用身份正确", identity_ok and ok_calls == n)
            await svc.terminate()


async def _main():
    t("on_platform_loaded 为异步钩子",
      inspect.iscoroutinefunction(M.Main._on_platform_loaded))
    await test_bindings()
    await test_requests()
    await test_events()
    await test_cleanup()
    await test_edge_semantics()
    await test_dispatch_outcomes()
    await test_intent_ownership()
    await test_bounded_cleanup()
    await test_ownership_and_hooks()
    await test_coordination_convergence()
    await test_main_scale()
    await test_ref_source_binding()
    await test_native_reference_ingest()


if __name__ == "__main__":
    asyncio.run(_main())
    print(f"\nALL {ok} CHECKS PASSED")
