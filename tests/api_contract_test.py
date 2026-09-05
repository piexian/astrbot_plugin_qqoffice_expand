"""官方契约与补丁生命周期回归；所有 HTTP/网关输入均为本地桩。"""

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
from core.auth import find_qq_credentials, TokenManager
from core.client import QQOfficeClient, resolve_endpoint_key
from core.errors import QQOfficeAPIError, QQOfficeGatewayError
from core.events import AdapterPatcher, EVENT_SPECS, EventBus, normalize_event, _PLUGIN_INTENT_BITS
from core.ratelimit import ENDPOINT_LIMITS, RateLimiter, _ProactiveQuota
from core.refstore import RefStore
from core.registry import collect_methods


class Transport:
    appid = "test-app"

    def __init__(self):
        self.requests = []
        self.response = {}
        self.status = 200

    async def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        return self.status, self.response, {}


def client_for(transport=None):
    return QQOfficeClient(self_client=transport or Transport(), rate_limiter=RateLimiter(),
                          refstore=RefStore(), config={"retry_max": 0})


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_token_response_and_cache(self):
        response = types.SimpleNamespace(status_code=200, headers={},
            json=lambda: {"access_token": "test-token", "expires_in": 7200})
        http = types.SimpleNamespace(post=AsyncMock(return_value=response))
        context = AsyncMock()
        context.__aenter__.return_value = http
        with patch("httpx.AsyncClient", return_value=context):
            manager = TokenManager("test-app", "test-secret")
            self.assertEqual(await manager.get_token(), "test-token")
            self.assertEqual(await manager.get_token(), "test-token")
        self.assertEqual(http.post.await_count, 1)
        self.assertTrue(manager.state["cached"])

    async def test_credentials_use_astrbot_public_config_api(self):
        context = types.SimpleNamespace(get_config=lambda: {"platform": [
            {"type": "qq_official", "id": "offline", "appid": "test-id", "secret": "test-secret", "enable": False},
            {"type": "aiocqhttp", "appid": "other", "secret": "other"},
        ]})
        self.assertEqual(find_qq_credentials(context), [
            {"id": "offline", "appid": "test-id", "secret": "test-secret", "enable": False},
        ])

    async def test_group_pagination_and_partial_failure(self):
        transport = Transport()
        client = client_for(transport)
        group = GroupAPI(client)
        transport.response = {"members": [{"member_openid": "M1"}], "next_cursor": "page2"}
        first = await group.members("G1")
        await group.members("G1", cursor=first["next_cursor"])
        self.assertEqual(transport.requests[-1][1:],
                         ("/v2/groups/G1/members", {"params": {"cursor": "page2"}, "json": None, "timeout": 30.0}))
        transport.response = {"remove_members_result": "success", "add_to_member_blacklist_fail_openids": ["M2"]}
        result = await group.remove_members("G1", ["M1", "M2"], add_to_member_blacklist=True)
        self.assertEqual(result["add_to_member_blacklist_fail_openids"], ["M2"])
        self.assertEqual(transport.requests[-1][2]["json"]["member_openids"], ["M1", "M2"])
        transport.response = {"fail_openids": ["M2"]}
        self.assertEqual(await group.set_blacklist("G1", "del", ["M2"]), transport.response)
        await group.blacklist("G1", cursor="B2", limit=100)
        self.assertEqual(transport.requests[-1][2]["params"], {"cursor": "B2", "limit": 100})
        for key in ("group.members", "group.remove_members", "group.blacklist", "group.set_blacklist"):
            self.assertIn(key, client.limiter._buckets)

    async def test_guild_array_responses_and_pagination(self):
        transport = Transport()
        guild = GuildAPI(client_for(transport))
        transport.response = [{"id": "C1"}]
        self.assertIs(await guild.channels("G1"), transport.response)
        transport.response = [{"user": {"id": "U1"}}]
        page = await guild.members("G1", limit=400)
        transport.response = []
        self.assertEqual(await guild.members("G1", after=page[-1]["user"]["id"], limit=400), [])
        self.assertEqual(transport.requests[-1][2]["params"], {"after": "U1", "limit": 400})
        transport.response = [{"id": "S1"}]
        self.assertEqual(await guild.schedules("C1", since=0), [{"id": "S1"}])
        self.assertEqual(transport.requests[-1][2]["params"], {"since": 0})

    async def test_guild_write_contracts_and_dm_routing(self):
        transport = Transport()
        client = client_for(transport)
        guild = GuildAPI(client)
        transport.status, transport.response = 204, None
        self.assertEqual(await guild.member_role_add("G", "U", "5", channel_id="C"), {})
        self.assertEqual(transport.requests[-1][:2], ("PUT", "/guilds/G/members/U/roles/5"))
        self.assertEqual(transport.requests[-1][2]["json"], {"channel": {"id": "C"}})
        await guild.member_remove("G", "U", delete_history_msg_days=0)
        self.assertEqual(transport.requests[-1][2]["json"], {"add_blacklist": False, "delete_history_msg_days": 0})
        await guild.recall("C", "M", hidetip=True)
        self.assertEqual(transport.requests[-1][2]["params"], {"hidetip": "true"})
        self.assertIsNone(transport.requests[-1][2]["json"])
        await guild.mute_member("G", "U", mute_seconds="0")
        self.assertEqual(transport.requests[-1][2]["json"], {"mute_seconds": "0"})
        await guild.set_role_permissions("C", "R", add="0", remove="4")
        self.assertEqual(transport.requests[-1][2]["json"], {"add": "0", "remove": "4"})
        transport.status, transport.response = 200, {"user_ids": ["U1"]}
        self.assertEqual(await guild.mute("G", mute_seconds="60", user_ids=["U1", "U2"]), {"user_ids": ["U1"]})
        await guild.thread_create("C", "标题", "正文", format=3)
        self.assertEqual(transport.requests[-1][:2], ("PUT", "/channels/C/threads"))
        await guild.schedule_create("C", {"name": "日程", "start_timestamp": "1000"})
        self.assertIn("schedule", transport.requests[-1][2]["json"])
        await guild.dm_send("DM-G", "你好", msg_id="DM-M")
        self.assertEqual(transport.requests[-1][1], "/dms/DM-G/messages")
        self.assertNotIn("msg_type", transport.requests[-1][2]["json"])
        self.assertFalse(client.limiter.proactive._robot)
        self.assertFalse(client.limiter.c2c_proactive._robot)

    async def test_stream_fragments_share_one_reply(self):
        transport = Transport()
        transport.response = {"id": "STREAM", "remain_msg_len": 999}
        client = client_for(transport)
        api = C2CAPI(client)
        first = await api.stream_send("U", "a", msg_id="REQUEST")
        for index in range(1, 7):
            await api.stream_send("U", "b", msg_id="REQUEST", stream_msg_id=first["id"],
                                  index=index, input_state=10 if index == 6 else 1)
        self.assertEqual(client.windows._records["REQUEST"][1], 1)
        self.assertTrue(all(req[1] == "/v2/users/U/stream_messages" for req in transport.requests))
        self.assertTrue(all(req[2]["json"]["msg_seq"] == 1 for req in transport.requests))
        self.assertNotIn("msg_type", transport.requests[-1][2]["json"])
        self.assertEqual(transport.requests[-1][2]["json"]["input_state"], 10)
        self.assertFalse(client.limiter.c2c_proactive._robot)
        self.assertEqual(client.limiter._buckets["c2c.stream"].count, 50)

    async def test_expired_stream_does_not_silently_change_reply_mode(self):
        transport = Transport()
        client = client_for(transport)
        for _ in range(4):
            client.windows.record("c2c", "EXPIRED")
        with self.assertRaises(QQOfficeAPIError) as caught:
            await C2CAPI(client).stream_send("U", "first", msg_id="EXPIRED")
        self.assertEqual(caught.exception.code, 40034128)
        self.assertEqual(transport.requests, [])

    async def test_upload_control_contract_and_active_send_quota(self):
        transport = Transport()
        client = client_for(transport)
        api = C2CAPI(client)
        await api.upload_prepare("U", file_type=4, file_size=123, file_name="a.txt", md5="m", sha1="s", md5_10m="p")
        self.assertEqual(transport.requests[-1][1], "/v2/users/U/upload_prepare")
        self.assertEqual(transport.requests[-1][2]["json"]["file_size"], "123")
        await api.upload_part_finish("U", "UPLOAD", 0, block_size=123, md5="part")
        self.assertEqual(transport.requests[-1][2]["json"]["part_index"], 0)
        await api.upload_complete("U", "UPLOAD", srv_send_msg=True)
        self.assertEqual(len(client.limiter.c2c_proactive._robot), 1)
        self.assertIn("c2c.upload_prepare", client.limiter._buckets)
        self.assertIn("upload_complete", collect_methods(GroupAPI(client)))

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
            self.assertEqual(list(quota._rel_day["U"]), [101])

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
        transport = Transport()
        transport.status, transport.response = 400, {"code": 40034101, "message": "not a group member"}
        client = client_for(transport)
        client.config["retry_max"] = 3
        with self.assertRaises(QQOfficeAPIError):
            await GroupAPI(client).send("G", "hi", msg_id="IN")
        self.assertEqual(len(transport.requests), 1)

    async def test_interaction_types_and_button_modal(self):
        bus = EventBus()
        acked = []
        async def ack(iid):
            acked.append(iid)
        bus.set_ack_caller(ack)
        for kind in (11, 12, 13, 14, 15, 16, 18, 19, 20):
            event = normalize_event("interaction_create", {"d": {"id": str(kind), "type": kind}})
            await bus.emit(event)
        await asyncio.gather(*bus._tasks)
        self.assertEqual(sorted(acked), ["11", "12"])
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
        patcher = AdapterPatcher(None, EventBus())
        self.assertEqual(patcher._boot_bits(), (1 << 24) | (1 << 26) | (1 << 27))
        self.assertFalse(_PLUGIN_INTENT_BITS & ((1 << 12) | (1 << 25) | (1 << 30)))
        patcher.bus.on("GUILD_MEMBER_ADD", lambda ev: None)
        self.assertTrue(patcher._boot_bits() & (1 << 1))
        self.assertIn("AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER", EVENT_SPECS)

    async def test_cached_webhook_raw_payload_and_unmount(self):
        tasks = []
        client = types.SimpleNamespace(_connection=None)
        def dispatch(name, obj):
            tasks.append(asyncio.create_task(getattr(client, "on_" + name)(obj)))

        class State:
            def __init__(self):
                self._dispatch = dispatch
                self.parsers = {name[6:]: method for name, method in inspect.getmembers(self, callable)
                                if name.startswith("parse_")}
            def parse_channel_create(self, payload):
                # 模拟 botpy.Channel 只保留 id，丢掉 guild_id。
                self._dispatch("channel_create", types.SimpleNamespace(id=payload["d"]["id"]))

        state = State()  # 必须早于插件类级补丁创建
        original = state.parsers["channel_create"]
        botpy = types.ModuleType("botpy")
        connection = types.ModuleType("botpy.connection")
        connection.ConnectionState = State
        botpy.connection = connection
        bundle = types.SimpleNamespace(client=client, mode="webhook", name="qq_official_webhook", instance_id="WH",
            inst=types.SimpleNamespace(webhook_helper=types.SimpleNamespace(_connection=types.SimpleNamespace(state=state))))
        bus = EventBus()
        received = []
        bus.on_any(received.append)
        patcher = AdapterPatcher(None, bus)
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.connection": connection}):
            record = patcher.ensure_patched(bundle)
            size = len(record["parser_patches"])
            patcher.ensure_patched(bundle)
            self.assertEqual(len(record["parser_patches"]), size)
            self.assertIn("group_member_add", state.parsers)
            self.assertIn("audio_on_mic", state.parsers)
            for channel in ("C1", "C2"):
                state.parsers["channel_create"]({"id": "E-" + channel, "d": {"id": channel, "guild_id": "G", "name": channel}})
            state.parsers["group_member_add"]({"id": "E-M", "d": {"group_openid": "QG", "member_openid": "M"}})
            state.parsers["audio_on_mic"]({"id": "E-A", "d": {"guild_id": "G", "channel_id": "C"}})
            await asyncio.gather(*tasks)
            await asyncio.gather(*bus._tasks)
            channels = [event for event in received if event.type == "CHANNEL_CREATE"]
            self.assertEqual([(ev.guild_id, ev.channel_id, ev.payload_id) for ev in channels],
                             [("G", "C1", "E-C1"), ("G", "C2", "E-C2")])
            self.assertIsInstance(channels[0].obj, types.SimpleNamespace)
            self.assertTrue(any(event.type == "AUDIO_ON_MIC" for event in received))
            # 新 state 自动获得类级缺失 parser，卸载也必须清理。
            new_state = State()
            bundle.inst.webhook_helper._connection.state = new_state
            patcher.ensure_patched(bundle)
            await patcher._unmount_all()
            self.assertIs(state.parsers["channel_create"], original)
            self.assertNotIn("group_member_add", state.parsers)
            self.assertNotIn("group_member_add", new_state.parsers)
            self.assertFalse(hasattr(State, "parse_group_member_add"))

    async def test_adapter_lists_empty_success_and_timeout_are_distinct(self):
        botpy = types.ModuleType("botpy")
        module = types.ModuleType("botpy.http")
        class Route:
            def __init__(self, method, path):
                self.method, self.path = method, path
        class ForbiddenError(RuntimeError):
            pass
        ForbiddenError.__module__ = "botpy.errors"
        async def handle(response):
            if getattr(response, "body_timeout", False):
                raise asyncio.TimeoutError()
            if response.status == 403:
                raise ForbiddenError("permission denied")  # 模拟 SDK 丢弃业务 code
            return response.data
        module.Route, module._handle_response = Route, handle
        botpy.http = module
        class HTTP:
            async def request(self, route, **kwargs):
                await asyncio.sleep(0)
                if route.path == "/timeout":
                    return None
                if route.path == "/forbidden":
                    return await module._handle_response(types.SimpleNamespace(
                        status=403, headers={}, text=AsyncMock(return_value='{"code":11253,"message":"permission denied"}')))
                response = types.SimpleNamespace(status=204 if route.path == "/empty" else 200,
                                                  data=None if route.path == "/empty" else [{"id": "G"}],
                                                  body_timeout=route.path == "/body-timeout")
                try:
                    return await module._handle_response(response)
                except asyncio.TimeoutError:
                    return None  # botpy 1.2.1 会吞掉正文读取超时
        bundle = types.SimpleNamespace(get_api=lambda: types.SimpleNamespace(_http=HTTP()))
        client = QQOfficeClient(bundle=bundle, rate_limiter=RateLimiter(), refstore=RefStore(), config={"retry_max": 0})
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.http": module}):
            results = await asyncio.gather(client.call("DELETE", "/empty"), client.call("GET", "/list"),
                                           client.call("GET", "/timeout"), client.call("GET", "/body-timeout"),
                                           return_exceptions=True)
            self.assertEqual(results[:2], [{}, [{"id": "G"}]])
            self.assertIsInstance(results[2], QQOfficeGatewayError)
            self.assertIsInstance(results[3], QQOfficeGatewayError)
            with self.assertRaises(QQOfficeAPIError) as caught:
                await client.call("GET", "/forbidden")
            self.assertEqual(caught.exception.code, 11253)
            self.assertIn("白名单", caught.exception.advice)
            self.assertIs(module._handle_response, handle)

    async def test_send_rich_routes_native_channel_and_dm_events(self):
        # 加载真实 main 方法，仅 AstrBot 注册依赖使用桩；不启动服务或加载用户配置。
        package = types.ModuleType("qqoffice_contract_plugin")
        package.__path__ = [str(ROOT)]
        api = types.ModuleType("astrbot.api")
        api.AstrBotConfig, api.logger = dict, types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
        event_api = types.ModuleType("astrbot.api.event")
        event_api.AstrMessageEvent = object
        event_api.filter = types.SimpleNamespace(command=lambda *a, **k: lambda fn: fn)
        star_api = types.ModuleType("astrbot.api.star")
        class Star:
            def __init__(self, context, config=None):
                self.context = context
        star_api.Context = star_api.StarTools = object
        star_api.Star = Star
        with patch.dict(sys.modules, {"qqoffice_contract_plugin": package, "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": api, "astrbot.api.event": event_api, "astrbot.api.star": star_api}):
            main = importlib.import_module("qqoffice_contract_plugin.main")
        service = main.Main.__new__(main.Main)
        unavailable = main.Main(types.SimpleNamespace())
        self.assertEqual(len(unavailable.registry.names()), 96)
        with self.assertRaises(main.QQOfficeNotSupported):
            await unavailable.invoke("guild.members", "G")
        service.primary = types.SimpleNamespace(call=AsyncMock(return_value={"id": "OUT"}))
        for group_id, scene, target, path in (("C", "guild", "C", "/channels/{channel_id}/messages"),
                                             (None, "dm", "DM", "/dms/{guild_id}/messages")):
            event = types.SimpleNamespace(platform=types.SimpleNamespace(name="qq_official"), message_obj=types.SimpleNamespace(
                group_id=group_id, message_id="IN", raw_message=types.SimpleNamespace(guild_id="DM", channel_id="C")))
            await service.send_rich(event, markdown={"markdown": {"content": "hi"}})
            args, kwargs = service.primary.call.call_args
            self.assertEqual(args, ("POST", path))
            self.assertEqual((kwargs["scene"], kwargs["target_openid"]), (scene, target))
            self.assertNotIn("msg_type", kwargs["json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
