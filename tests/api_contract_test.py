"""官方契约与补丁生命周期回归（N 实例版）；所有 HTTP/网关输入均为本地桩。

运行环境：bwrap 禁网沙箱，PYTHONPATH=/deps:/plugin（/deps 为本体 venv 只读依赖）。
"""

import asyncio
import importlib
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.c2c import C2CAPI
from api.group import GroupAPI
from api.guild import GuildAPI
from core.builders import btn
from core.client import generate_msg_seq, resolve_endpoint_key, execute_call
from core.errors import QQOfficeAPIError, QQOfficeGatewayError
from core.events import AdapterPatcher, EVENT_SPECS, EventBus, normalize_event, _PLUGIN_INTENT_BITS
from core.ratelimit import ENDPOINT_LIMITS, RateLimiter, _ProactiveQuota
from core.refstore import RefStore
from core.registry import collect_methods
from core.routing import BoundSource, RobotKey, RobotStates, RouteCore


class FakeMeta:
    def __init__(self, name, pid):
        self.name, self.id = name, pid


class FakeHttp:
    def __init__(self, appid, token=True):
        self.appid = appid
        self._token = object() if token else None
        self.requests = []
        self.response = {}
        self.status = 200

    async def request(self, route, **kwargs):
        self.requests.append((route.method, route.path, kwargs))
        if self.status >= 400:
            raise RuntimeError(f"ServerError: HTTP {self.status}")
        return self.response


class FakeApi:
    def __init__(self, http):
        self._http = http


class FakeClient:
    def __init__(self, appid, token=True):
        self.api = FakeApi(FakeHttp(appid, token))
        self.closed = False
        self.intents = 0

    def is_closed(self):
        return self.closed


class FakeAdapter:
    def __init__(self, pid, appid, mode="ws", sandbox=False):
        self.meta_obj = FakeMeta("qq_official_webhook" if mode == "webhook" else "qq_official", pid)
        self.config = {"id": pid, "appid": appid, "secret": "s", "is_sandbox": sandbox}
        self.appid = appid
        self.client = FakeClient(appid)
        self.client_self_id = f"uuid-{pid}"

    def meta(self):
        return self.meta_obj

    def get_client(self):
        return self.client


class FakeManager:
    def __init__(self):
        self._inst_map = {}


class RecordingSvc:
    """svc 桩：routes + refstore + config，HTTP 响应可编程。"""

    def __init__(self):
        self.manager = FakeManager()
        self.refstore = RefStore(None, ttl_days=0)
        self.config = {"retry_max": 0}
        self.states = RobotStates()
        self.routes = RouteCore(self.manager, self.states)

    def add(self, pid, appid, mode="ws"):
        ad = FakeAdapter(pid, appid, mode)
        self.manager._inst_map[pid] = {"inst": ad, "client_id": ad.client_self_id}
        self.routes.refresh_all()
        return ad

    def http_of(self, pid):
        return self.manager._inst_map[pid]["inst"].client.api._http


class _View:
    """最小 BoundView 桩：call 直连 execute_call（与 main.BoundView 一致）。"""
    def __init__(self, svc, bound):
        self._svc, self._bound = svc, bound

    async def call(self, method, path, **kwargs):
        return await execute_call(self._svc, self._bound, method, path, **kwargs)


def view_for(svc, pid, mode="instance"):
    route = svc.routes.route_of(pid)
    return _View(svc, BoundSource.for_instance(pid, route.robot_key))


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_route_to_matching_http_identity(self):
        svc = RecordingSvc()
        svc.add("a", "APP1")
        svc.add("b", "APP2", mode="webhook")
        a, b = view_for(svc, "a")._bound, view_for(svc, "b")._bound
        http_a, http_b = svc.http_of("a"), svc.http_of("b")
        http_a.response = {"id": "1"}
        http_b.response = {"id": "2"}
        self.assertEqual(await execute_call(svc, a, "GET", "/users/@me"), {"id": "1"})
        self.assertEqual(await execute_call(svc, b, "GET", "/users/@me"), {"id": "2"})
        self.assertEqual(http_a.requests[-1][1], "/users/@me")
        self.assertEqual(http_b.requests[-1][1], "/users/@me")

    async def test_group_pagination_and_partial_failure(self):
        svc = RecordingSvc()
        svc.add("g", "APP1")
        bound = view_for(svc, "g")
        http = svc.http_of("g")
        http.response = {"members": [{"member_openid": "M1"}], "next_cursor": "page2"}
        group = GroupAPI(bound)
        first = await group.members("G1")
        await group.members("G1", cursor=first["next_cursor"])
        self.assertEqual(http.requests[-1][1], "/v2/groups/G1/members")
        self.assertEqual(http.requests[-1][2]["params"], {"cursor": "page2"})
        http.response = {"fail_openids": ["M2"]}
        self.assertEqual(await group.set_blacklist("G1", "del", ["M2"]),
                         {"fail_openids": ["M2"]})
        await group.blacklist("G1", cursor="B2", limit=100)
        self.assertEqual(http.requests[-1][2]["params"], {"cursor": "B2", "limit": 100})
        state = svc.routes.state_for(svc.routes.route_of("g").robot_key)
        for key in ("group.members", "group.blacklist", "group.set_blacklist"):
            self.assertIn(key, state.rate._buckets)

    async def test_guild_array_responses_and_dm_routing(self):
        svc = RecordingSvc()
        svc.add("c", "APP1")
        bound = view_for(svc, "c")
        http = svc.http_of("c")
        guild = GuildAPI(bound)
        http.response = [{"id": "C1"}]
        self.assertEqual(await guild.channels("G1"), [{"id": "C1"}])
        await guild.dm_send("DM-G", "你好", msg_id="DM-M")
        self.assertEqual(http.requests[-1][1], "/dms/DM-G/messages")
        self.assertNotIn("msg_type", http.requests[-1][2]["json"])
        state = svc.routes.state_for(svc.routes.route_of("c").robot_key)
        self.assertFalse(state.rate.proactive._robot)

    async def test_stream_fragments_share_one_reply(self):
        svc = RecordingSvc()
        svc.add("u", "APP1")
        bound = view_for(svc, "u")
        http = svc.http_of("u")
        http.response = {"id": "STREAM", "remain_msg_len": 999}
        api = C2CAPI(bound)
        first = await api.stream_send("U", "a", msg_id="REQUEST")
        for index in range(1, 7):
            await api.stream_send("U", "b", msg_id="REQUEST",
                                  stream_msg_id=first["id"], index=index,
                                  input_state=10 if index == 6 else 1)
        state = svc.routes.state_for(svc.routes.route_of("u").robot_key)
        key = ("c2c", "U", "REQUEST")
        self.assertEqual(state.windows._records[key][1], 1)   # 预留 1 次，续片不扣
        self.assertTrue(all(req[1] == "/v2/users/U/stream_messages" for req in http.requests))
        self.assertTrue(all(req[2]["json"]["msg_seq"] == 1 for req in http.requests))
        self.assertFalse(state.rate.c2c_proactive._robot)
        self.assertEqual(state.rate._buckets["c2c.stream"].count, 50)

    async def test_passive_window_reserve_degrade_and_isolation(self):
        svc = RecordingSvc()
        svc.add("a", "APP1")
        svc.add("b", "APP2")
        http = svc.http_of("a")
        http.response = {"id": "ok"}
        group = GroupAPI(view_for(svc, "a"))
        for _ in range(5):
            await group.send("G1", "hi", msg_id="M1")
        # 第 6 次：窗口超限 → 自动降级主动消息（消耗配额，不带 msg_id）
        await group.send("G1", "hi again")
        req = http.requests[-1][2]["json"]
        self.assertNotIn("msg_id", req)
        state_a = svc.routes.state_for(svc.routes.route_of("a").robot_key)
        self.assertEqual(len(state_a.rate.proactive._robot), 1)   # 降级后消耗主动配额
        # 不同身份不共享窗口
        await GroupAPI(view_for(svc, "b")).send("G1", "hi", msg_id="M1")
        self.assertEqual(svc.http_of("b").requests[-1][2]["json"]["msg_id"], "M1")

    async def test_proactive_scene_and_daily_quotas(self):
        limiter = RateLimiter(certified_bot=True)
        self.assertEqual(limiter.c2c_proactive.robot_second, 10)
        self.assertIsNone(limiter.c2c_proactive.robot_minute)
        for i in range(10):
            await limiter.consume_proactive(str(i), scene="c2c")
        self.assertFalse(limiter.proactive._robot)
        await limiter.consume_proactive("0", scene="group")
        self.assertEqual(len(limiter.proactive._robot), 1)
        quota = _ProactiveQuota(certified=False, per_relation_day=2)
        with patch("core.ratelimit.time.time", return_value=86400 * 100 + 123):
            await quota.consume("U")
            await quota.consume("U")
            with self.assertRaises(QQOfficeAPIError):
                await quota.consume("U")
        with patch("core.ratelimit.time.time", return_value=86400 * 101):
            await quota.consume("U")
            self.assertEqual(quota._rel_day["U"], [86400 * 101 // 86400, 1])

    async def test_endpoint_dispatch_and_channel_limits(self):
        cases = [
            ("DELETE", "/v2/groups/G/messages/M", "group", "group.recall"),
            ("GET", "/v2/groups/G/members/M", "group", "group.member_info"),
            ("GET", "/v2/groups/G/member_blacklist", "group", "group.blacklist"),
            ("POST", "/v2/groups/G/member_blacklist", "group", "group.set_blacklist"),
            ("GET", "/v2/panels/P", None, "panels.get"),
            ("PUT", "/v2/panels/P/target", None, "panels.target"),
            ("GET", "/channels/C/messages", "guild", "default"),
        ]
        for method, path, scene, expected in cases:
            self.assertEqual(resolve_endpoint_key(method, path, scene), expected)
        self.assertEqual(ENDPOINT_LIMITS["group.member_info"], (30, 60.0))
        limiter = RateLimiter()
        await limiter.acquire("guild.send", target="C1")
        await limiter.acquire("guild.send", target="C2")
        self.assertEqual(len(limiter._buckets), 2)
        self.assertEqual(limiter._buckets[("guild.send", "C1")].count, 5)

    async def test_not_a_group_member_is_not_retried_as_rate_limit(self):
        svc = RecordingSvc()
        svc.add("a", "APP1")
        bound = view_for(svc, "a")._bound
        http = svc.http_of("a")

        class Route:
            def __init__(self, method, path):
                self.method, self.path = method, path

        botpy = types.ModuleType("botpy")
        module = types.ModuleType("botpy.http")
        module.Route = Route
        botpy.http = module
        async def boom(route, **kwargs):
            raise RuntimeError("ServerError: code=40034101 message=not a member")
        http.request = boom
        svc.config["retry_max"] = 3
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.http": module}):
            with self.assertRaises(QQOfficeAPIError) as caught:
                await GroupAPI(view_for(svc, "a")).send("G", "hi")
        self.assertEqual(caught.exception.code, 40034101)
        self.assertEqual(len(http.requests), 0)

    async def test_interaction_types_and_button_modal(self):
        bus = EventBus()
        acked = []
        async def ack(view, iid):
            acked.append(iid)
        bus.set_ack_caller(ack)
        for kind in (11, 12, 13, 14, 15, 16, 18, 19, 20):
            event = normalize_event("interaction_create", {"d": {"id": str(kind), "type": kind}})
            event.source = None   # 无来源时不自动应答（也不会崩）
            await bus.emit(event)
        await asyncio.gather(*bus._tasks)
        self.assertEqual(acked, [])   # 无来源不 ACK
        self.assertEqual(btn("删除", "delete", type=1, modal={"content": "确认删除？"})["action"]["modal"],
                         {"content": "确认删除？"})

    async def test_normalized_ids_and_default_intents(self):
        for name in ("group_member_add", "group_member_remove", "group_join_request"):
            self.assertEqual(normalize_event(name, {"group_openid": "G", "member_openid": "M"}).member_openid, "M")
        event = normalize_event("direct_message_delete", {"id": "EVENT", "d": {
            "message": {"id": "MESSAGE", "guild_id": "DM", "channel_id": "C", "author": {"id": "U"}},
            "op_user": {"id": "OP"}}})
        self.assertEqual((event.scene, event.guild_id, event.message_id, event.user_id, event.payload_id),
                         ("dm", "DM", "MESSAGE", "U", "EVENT"))
        self.assertIn("AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER", EVENT_SPECS)
        self.assertFalse(_PLUGIN_INTENT_BITS & ((1 << 12) | (1 << 25) | (1 << 30)))

    async def test_cached_webhook_raw_payload_and_unmount(self):
        tasks = []
        client = types.SimpleNamespace(_connection=None, intents=0, is_closed=lambda: False)
        def dispatch(name, obj):
            tasks.append(asyncio.create_task(getattr(client, "on_" + name)(obj)))

        class State:
            def __init__(self):
                self._dispatch = dispatch
                self.parsers = {name[6:]: method for name, method in inspect.getmembers(self, callable)
                                if name.startswith("parse_")}
            def parse_channel_create(self, payload):
                self._dispatch("channel_create", types.SimpleNamespace(id=payload["d"]["id"]))

        state = State()
        original = state.parsers["channel_create"]
        botpy = types.ModuleType("botpy")
        connection = types.ModuleType("botpy.connection")
        connection.ConnectionState = State
        botpy.connection = connection
        ad = FakeAdapter("WH", "APP1", mode="webhook")
        ad.client = client
        ad.webhook_helper = types.SimpleNamespace(
            _connection=types.SimpleNamespace(state=state))
        svc = RecordingSvc()
        svc.manager._inst_map["WH"] = {"inst": ad, "client_id": ad.client_self_id}
        svc.routes.refresh_all()   # 协调首次发现
        route = svc.routes.route_of("WH")
        bus = EventBus()
        received = []
        bus.on_any(received.append)
        patcher = AdapterPatcher(svc.routes, bus)
        received_sources = []
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.connection": connection}):
            patcher.refresh()
            size = len(patcher._patches[id(route)].parser_patches)
            patcher.refresh()   # 稳定：无新 wrapper
            self.assertEqual(len(patcher._patches[id(route)].parser_patches), size)
            self.assertIn("group_member_add", state.parsers)
            state.parsers["channel_create"]({"id": "E-C1", "d": {"id": "C1", "guild_id": "G", "name": "x"}})
            state.parsers["group_member_add"]({"id": "E-M", "d": {"group_openid": "QG", "member_openid": "M"}})
            await asyncio.gather(*tasks)
            await asyncio.gather(*bus._tasks)
            channels = [e for e in received if e.type == "CHANNEL_CREATE"]
            self.assertEqual([(ev.guild_id, ev.channel_id, ev.payload_id) for ev in channels],
                             [("G", "C1", "E-C1")])
            self.assertIsNotNone(channels[0].source)
            self.assertEqual(channels[0].source.generation, route.generation)
            self.assertTrue(any(e.type == "GROUP_MEMBER_ADD" for e in received))
            await patcher.stop()
            self.assertIs(state.parsers["channel_create"], original)
            self.assertNotIn("group_member_add", state.parsers)
            self.assertNotIn("parse_group_member_add",
                             [n for n in dir(connection.ConnectionState) if n.startswith("parse_")]
                             ) if hasattr(connection.ConnectionState, "parse_group_member_add") else None

    async def test_empty_success_timeout_and_forbidden_are_distinct(self):
        svc = RecordingSvc()
        svc.add("a", "APP1")
        bound = view_for(svc, "a")._bound
        http = svc.http_of("a")

        class Route:
            def __init__(self, method, path):
                self.method, self.path = method, path

        botpy = types.ModuleType("botpy")
        module = types.ModuleType("botpy.http")
        module.Route = Route
        botpy.http = module

        class ForbiddenError(RuntimeError):
            pass
        ForbiddenError.__module__ = "botpy.errors"

        async def handle(response):
            if response.status == 403:
                raise ForbiddenError(
                    'ServerError: code=11253 message="permission denied"'
                )
            return response.data

        module._handle_response = handle

        class ParsedHTTP(FakeHttp):
            async def request(self, route, **kwargs):
                if route.path == "/timeout":
                    await asyncio.sleep(0)
                    return None
                if route.path == "/forbidden":
                    # 模拟 SDK：_handle_response 读到 403 + 业务码后抛 RuntimeError 子类
                    await handle(types.SimpleNamespace(status=403, data=None))
                self.requests.append((route.method, route.path, kwargs))
                return {} if route.path == "/empty" else [{"id": "G"}]

        parsed = ParsedHTTP("APP1")
        old_api = svc.manager._inst_map["a"]["inst"].client.api
        svc.manager._inst_map["a"]["inst"].client.api = FakeApi(parsed)
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.http": module}):
            results = await asyncio.gather(
                execute_call(svc, bound, "DELETE", "/empty"),
                execute_call(svc, bound, "GET", "/list"),
                execute_call(svc, bound, "GET", "/timeout"),
                return_exceptions=True)
        self.assertEqual(results[0], {})
        self.assertEqual(results[1], [{"id": "G"}])
        self.assertIsInstance(results[2], QQOfficeGatewayError)
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.http": module}):
            with self.assertRaises(QQOfficeAPIError) as caught:
                await execute_call(svc, bound, "GET", "/forbidden")
        self.assertEqual(caught.exception.code, 11253)
        self.assertIn("白名单", caught.exception.advice)

    async def test_stale_generation_and_identity_rejected_mid_call(self):
        svc = RecordingSvc()
        svc.add("a", "APP1")
        bound = view_for(svc, "a")._bound
        http = svc.http_of("a")
        http.response = {"id": "1"}
        await execute_call(svc, bound, "GET", "/users/@me")
        # 长期视图（无来源约束）跟随同身份重载
        ad2 = FakeAdapter("a", "APP1")
        svc.manager._inst_map["a"] = {"inst": ad2, "client_id": ad2.client_self_id}
        svc.routes.refresh_all()
        ad2.client.api._http.response = {"id": "1"}
        self.assertEqual(await execute_call(svc, bound, "GET", "/users/@me"), {"id": "1"})
        # 改绑 AppID 后旧视图拒绝
        ad3 = FakeAdapter("a", "APP9")
        svc.manager._inst_map["a"] = {"inst": ad3, "client_id": ad3.client_self_id}
        svc.routes.refresh_all()
        with self.assertRaises(Exception):
            await execute_call(svc, bound, "GET", "/users/@me")

    async def test_refstore_namespace_and_capacity(self):
        rs = RefStore(None, ttl_days=0, max_entries=50000)
        rs.record_inbound("APP1@production|group", "G1", "M1",
                          {"message_scene": {"ext": ["msg_idx=REFIDX_a"]}})
        rs.record_inbound("APP2@production|group", "G1", "M1",
                          {"message_scene": {"ext": ["msg_idx=REFIDX_b"]}})
        self.assertEqual(rs.get_inbound("APP1@production|group", "G1", "M1"), "REFIDX_a")
        self.assertEqual(rs.get_inbound("APP2@production|group", "G1", "M1"), "REFIDX_b")
        self.assertEqual(rs._file_cache_max, 512)   # 全插件上限，不乘以实例数


if __name__ == "__main__":
    unittest.main(verbosity=2)
