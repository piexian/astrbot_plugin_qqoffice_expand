# -*- coding: utf-8 -*-
"""离线逻辑自测：core/api 模块零 astrbot 依赖，全部可导入直测。"""
import asyncio
from pathlib import Path
import base64
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\GitHub\astrbot_plugin_qqoffice_expand")

from core import builders, errors, media
from core.auth import QQClientBundle, SelfClient, TokenManager, resolve_domain, find_qq_credentials
from core.client import PassiveWindowTracker, QQOfficeClient, generate_msg_seq, resolve_endpoint_key
from core.ratelimit import RateLimiter
from core.refstore import RefStore, parse_scene_ext
from core.registry import Registry, collect_methods
from core.events import EVENT_SPECS, EventBus, QQOfficeEvent, normalize_event
from api.group import GroupAPI
from api.c2c import C2CAPI
from api.guild import GuildAPI
from api.manage import ManageAPI

ok = 0
def t(name, cond):
    global ok
    assert cond, f"FAIL: {name}"
    ok += 1
    print(f"  ok  {name}")

# ---------- builders ----------
t("md content", builders.md("# 标题") == {"markdown": {"content": "# 标题"}})
t("md template", builders.md(template_id="tpl_1", params={"title": "hi"}) ==
  {"markdown": {"custom_template_id": "tpl_1", "params": [{"key": "title", "values": ["hi"]}]}})
b = builders.btn("确认", data="/ok", enter=True, button_id="b1")
t("btn shape", b["action"]["data"] == "/ok" and b["render_data"]["label"] == "确认" and b["action"]["enter"] is True)
kb = builders.Keyboard().row(b, builders.btn("取消", data="/no")).build()
t("kb rows", len(kb["keyboard"]["content"]["rows"][0]["buttons"]) == 2)
kb2 = builders.Keyboard().template("tpl").build()
t("kb template", kb2 == {"keyboard": {"id": "tpl"}})
t("reference", builders.reference("REFIDX_x") == {"message_reference": {"message_id": "REFIDX_x"}})
t("md_image explicit", builders.md_image("https://a.com/1.png", width=100, height=50) == "![#100px #50px](https://a.com/1.png)")

png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 640, 320) + b"\x00" * 16
t("md_image parse png", builders.md_image("https://a.com/1.png", source=png) == "![#640px #320px](https://a.com/1.png)")

# ---------- errors ----------
e = errors.from_http_response(400, {"code": 40034005, "message": "msg_id expired"})
t("err code map", isinstance(e, errors.QQOfficeAPIError) and e.code == 40034005 and e.advice is not None)
e2 = errors.from_http_response(502, "<html>bad gateway</html>", {"content-type": "text/html", "x-tps-trace-id": "t-123"})
t("err html", isinstance(e2, errors.QQOfficeGatewayError) and e2.trace_id == "t-123")
e3 = errors.from_http_response(401, {"code": 11244, "message": "invalid token"})
t("err token", e3.code == 11244)
e4 = errors.from_http_response(400, {"code": 40093002, "message": "limit"})
t("err upload daily", isinstance(e4, errors.UploadDailyLimitExceeded))
e5 = errors.from_http_response(429, "rate limited")
t("err 429 wait", e5.code == 429)
e6 = errors.from_exception(Exception("ServerError: code=40034100 message=quota"))
t("err botpy text", e6 is not None and e6.code == 40034100)
t("err pass-through", errors.from_exception(errors.QQOfficeNotSupported("x")) is not None)
t("err non-target", errors.from_exception(ValueError("plain")) is None)

# ---------- msg_seq / endpoint key / window / chunk ----------
seqs = {generate_msg_seq() for _ in range(200)}
t("msg_seq stateless", all(0 <= s < 100_000_000 + 65536 for s in seqs) and len(seqs) > 190)
t("ep send", resolve_endpoint_key("POST", "/v2/groups/{group_openid}/messages", "group") == "group.send")
t("ep recall", resolve_endpoint_key("DELETE", "/v2/groups/{group_openid}/messages/{message_id}", "group") == "group.recall")
t("ep files", resolve_endpoint_key("POST", "/v2/users/{user_openid}/files", "c2c") == "c2c.files")
t("ep mute get/set", resolve_endpoint_key("GET", "/v2/groups/{g}/restrict_chat_setting", "group") == "group.mute_get"
  and resolve_endpoint_key("POST", "/v2/groups/{g}/restrict_chat_setting", "group") == "group.mute_set")
t("ep menu", resolve_endpoint_key("GET", "/v2/menu", None) == "menu.get" and resolve_endpoint_key("PUT", "/v2/menu", None) == "menu.put")
t("ep panels", resolve_endpoint_key("PUT", "/v2/panels/{panel_id}/target", None) == "panels.target")
t("ep interactions", resolve_endpoint_key("PUT", "/interactions/{interaction_id}", None) == "interactions.ack")
t("ep default", resolve_endpoint_key("GET", "/some/new/endpoint", None) == "default")

w = PassiveWindowTracker()
t("window fresh", w.check("group", "m1")[0])
for _ in range(5):
    w.record("group", "m1")
ok1, why1 = w.check("group", "m1")
t("window count cap", not ok1 and "上限" in why1)
w.record("group", "m2")
# 过期窗口：直接造一条超窗记录
w._records["m3"] = (w._records["m2"][0] - 400, 1, w._records["m2"][2], "group")
ok3, why3 = w.check("group", "m3")
t("window expired", not ok3 and "过期" in why3)
t("window c2c limit", PassiveWindowTracker.LIMITS["c2c"] == (3600.0, 4))
t("chunk", QQOfficeClient.chunk_text("a\nb\nc", limit=3) == ["a\nb", "c"]
  and len(QQOfficeClient.chunk_text("abcd\nef", limit=3)) == 3
  and QQOfficeClient.chunk_text("", limit=10) == [])

# ---------- refstore ----------
with tempfile.TemporaryDirectory() as td:
    rs = RefStore(Path(td), ttl_days=7)
    rs.store("in:group:g1:m1", "REFIDX_a")
    t("ref roundtrip", rs.get("in:group:g1:m1") == "REFIDX_a")
    rs.record_inbound("group", "g1", "m2", {"message_scene": {"ext": ["msg_idx=REFIDX_b", "x=1"]},
                                            "msg_elements": []})
    t("ref inbound scene ext", rs.get_inbound("group", "g1", "m2") == "REFIDX_b")
    rs.record_inbound("group", "g1", "m3", {"msg_elements": [{"msg_idx": "REFIDX_c"}]})
    t("ref inbound elements fallback", rs.get_inbound("group", "g1", "m3") == "REFIDX_c")
    rs.record_outbound("group", "g1", {"ext_info": {"ref_idx": "REFIDX_o1"}}, local_key="s1")
    t("ref outbound latest", rs.get_outbound_latest("group", "g1") == "REFIDX_o1")
    t("ref outbound keyed", rs.get(f"out:group:g1:s1") == "REFIDX_o1")
    t("parse_scene_ext", parse_scene_ext(["a=1", "b=2"]) == {"a": "1", "b": "2"})
    rs.cache_file_info("h1", "group", "g1", 1, "FI", ttl=120)
    t("file cache hit", rs.get_file_info("h1", "group", "g1", 1) == "FI")
    rs.cache_file_info("h2", "group", "g1", 1, "FI2", ttl=30)
    t("file cache ttl floor", rs._file_cache[RefStore.file_cache_key("h2", "group", "g1", 1)][0] - __import__("time").monotonic() <= 30)
    t("file cache miss", rs.get_file_info("hx", "group", "g1", 1) is None)
    rs2 = RefStore(Path(td), ttl_days=7)
    t("ref persisted reload", rs2.get("in:group:g1:m1") == "REFIDX_a")
    t("content hash", len(rs.content_hash(b"abc")) == 32)

# ---------- ratelimit ----------
async def rl_test():
    rl = RateLimiter(certified_bot=False)
    await rl.acquire("group.send")
    await rl.acquire("group.send")
    t("rl unknown default", await rl.acquire("brand.new") == 0.0)
    w0 = asyncio.get_event_loop().time()
    await rl.consume_proactive("t1")
    await rl.consume_proactive("t1")
    # 未认证 30/分钟，前 20 条单关系窗口内应即时放行
    for _ in range(18):
        await rl.consume_proactive("t1")
    t("rl proactive relation window", True)
    try:
        rl.proactive._rel_day.setdefault("t2", __import__("collections").deque()).append(12345)  # 伪造非今日 → 会被 prune
        await rl.consume_proactive("t2")
        t("rl daily prune", True)
    except errors.QQOfficeAPIError as e:
        raise AssertionError(f"daily quota misfired: {e}")
    snap = rl.snapshot()
    t("rl snapshot", snap["proactive"]["robot_per_minute"] == 30)
    rl2 = RateLimiter(certified_bot=True)
    t("rl certified 60", rl2.snapshot()["proactive"]["robot_per_minute"] == 60)
asyncio.run(rl_test())

# ---------- media ----------
up = media.to_uploadable("https://example.com/a.png")
t("up url", up.kind == "url" and up.filename == "a.png")
up2 = media.to_uploadable(base64.b64encode(b"hello world payload 0123456789abcdefghij" * 3).decode())
t("up b64", up2.kind == "base64")
up3 = media.to_uploadable(b"\x00\x01\x02")
t("up bytes", up3.kind == "bytes" and up3.size == 3)
try:
    media.to_uploadable("https://127.0.0.1/x.png")
    t("ssrf loopback", False)
except errors.QQOfficeAPIError:
    t("ssrf loopback", True)
try:
    media.to_uploadable("ftp://example.com/x")
    t("ssrf scheme", False)
except errors.QQOfficeAPIError:
    t("ssrf scheme", True)
t("image_size gif", media.image_size(b"GIF89a" + struct.pack("<HH", 300, 200) + b"\x00" * 10) == (300, 200))
t("image_size default", media.image_size(b"not-an-image") == (512, 512))
amr = b"#!AMR\nSILK_PAYLOAD"
t("amr strip", media.strip_amr_header(amr) == (b"SILK_PAYLOAD", True))
t("amr passthrough", media.strip_amr_header(b"RAW") == (b"RAW", False))
t("content hash len", len(media.hashlib_sha256(b"x")) == 64)

# ---------- registry / api ----------
reg = Registry()
ns = ManageAPI(None)
for name, fn in collect_methods(ns).items():
    reg.register_fn(f"manage.{name}", fn)
t("registry count", len(reg.names()) == 9 and "manage.interaction_ack" in reg.names())
async def _reg_unknown():
    try:
        await reg.invoke("manage.nope")
        return False
    except errors.QQOfficeNotSupported:
        return True
t("registry unknown", asyncio.run(_reg_unknown()))

class _Cap:
    def __init__(self):
        self.calls = []
    async def call(self, *a, **k):
        self.calls.append((a, k))
        return {"resp": True}
    async def upload_media(self, *a, **k):
        return {"file_info": "FI"}

cap = _Cap()
g = GroupAPI(cap)
asyncio.run(g.recall("G1", "M1"))
a, k = cap.calls[-1]
t("group.recall", a[0] == "DELETE" and a[1] == "/v2/groups/{group_openid}/messages/{message_id}"
  and k["path_params"] == {"group_openid": "G1", "message_id": "M1"})
asyncio.run(g.mute_member("G1", "M1", "2026-08-31T00:00:00+08:00"))
a, k = cap.calls[-1]
t("group.mute", k["json"]["members"][0]["op"] == "add")
asyncio.run(g.join_approve("G1", "M1", op="decline", reject_reason="r", add_to_member_blacklist=True))
a, k = cap.calls[-1]
t("group.approve", k["json"] == {"op": "decline", "reject_reason": "r", "add_to_member_blacklist": True})
asyncio.run(g.strategy_whitelist("S1", "add", ["123"]))
a, k = cap.calls[-1]
t("group.whitelist", a[1] == "/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users")

c = C2CAPI(cap)
asyncio.run(c.wakeup("U1", "hi"))
a, k = cap.calls[-1]
t("c2c.wakeup", k["json"]["is_wakeup"] is True and "msg_id" not in k["json"])
asyncio.run(c.send("U1", "hi", msg_id="M"))
a, k = cap.calls[-1]
t("c2c.send", k["scene"] == "c2c" and k["msg_id"] == "M")

m = ManageAPI(cap)
asyncio.run(m.menu_put([{"type": "send_message", "name": "帮助", "send_message": "/help"}]))
a, k = cap.calls[-1]
t("menu_put body", k["json"]["menu"]["items"][0]["send_message"] == "/help")
asyncio.run(m.panel_target("P1", "add", group_openids=["G"]))
a, k = cap.calls[-1]
t("panel_target", a[1] == "/v2/panels/{panel_id}/target" and k["json"]["op"] == "add")

# ---------- events ----------
t("specs three groups",
  EVENT_SPECS["GROUP_JOIN_REQUEST"].needs_parser and EVENT_SPECS["GROUP_JOIN_REQUEST"].intent is None
  and EVENT_SPECS["GROUP_MEMBER_ADD"].intent == 1 << 24 and EVENT_SPECS["GROUP_MEMBER_ADD"].needs_parser
  and EVENT_SPECS["INTERACTION_CREATE"].intent == 1 << 26 and not EVENT_SPECS["INTERACTION_CREATE"].needs_parser
  and EVENT_SPECS["GROUP_ADD_ROBOT"].intent is None and not EVENT_SPECS["GROUP_ADD_ROBOT"].needs_parser
  and EVENT_SPECS["GUILD_CREATE"].intent == 1 << 0)

ev = normalize_event("group_add_robot", {"id": "E1", "d": {"group_openid": "G", "op_member_openid": "M"}})
t("normalize dict", ev.type == "GROUP_ADD_ROBOT" and ev.scene == "group" and ev.group_openid == "G"
  and ev.member_openid == "M" and ev.payload_id == "E1")

class _Wrap:
    def __init__(self):
        self.id = "E2"
        self.group_openid = "G2"
        self.author = type("A", (), {"member_openid": "M2"})()
ev2 = normalize_event("group_del_robot", _Wrap())
t("normalize wrapper", ev2.scene == "group" and ev2.member_openid == "M2" and ev2.payload_id == "E2")

async def bus_test():
    bus = EventBus({"interaction_auto_ack": True}, None)
    got, any_got = [], []
    unsub = bus.on("INTERACTION_CREATE", lambda e: got.append(e))
    bus.on_any(lambda e: any_got.append(e))
    acked = []
    async def fake_ack(iid):
        acked.append(iid)
    bus.set_ack_caller(fake_ack)
    ev = normalize_event("interaction_create", {"d": {"id": "I-1", "user_openid": "U"}})
    await bus.emit(ev)
    await bus.emit(ev)  # 同 id 第二次：不应重复 ack，但事件仍分发
    await asyncio.sleep(0.05)
    t("bus sub+any", len(got) == 2 and len(any_got) == 2)
    t("bus auto-ack once", acked == ["I-1"])
    unsub()
    await bus.emit(normalize_event("interaction_create", {"d": {"id": "I-2"}}))
    await asyncio.sleep(0.05)
    t("bus unsub", len(got) == 2 and len(any_got) == 3)
    t("bus counts", bus.counts["INTERACTION_CREATE"] == 3)
asyncio.run(bus_test())

# ---------- auth / client helpers ----------
t("domain default", resolve_domain() == "api.sgroup.qq.com")
t("domain new", resolve_domain(prefer_new_domain=True) == "api.bot.qq.com")
t("domain sandbox", resolve_domain(sandbox=True) == "sandbox.api.sgroup.qq.com")
tm = TokenManager("a", "s")
t("token state", tm.state == {"cached": False, "remaining_seconds": 0.0})
sc = SelfClient("a", "s")
t("selfclient domain", sc.domain == "api.sgroup.qq.com" and sc.upload_timeout == 120.0)

class _Bundle:
    pass
bd = _Bundle()
bundle = QQClientBundle(inst=bd, name="qq_official", mode="ws", appid="A", secret="S", client=object(), instance_id="i1")
t("bundle", bundle.get_api() is None and bundle.is_connected() in (True, False))

class _PrimCap(_Cap):
    def __init__(self):
        super().__init__()
        self.prepared = []
    async def _prepare_message_payload(self, payload, scene, target, msg_id, event_id, msg_seq):
        self.prepared.append((payload, scene, target, msg_id, event_id, msg_seq))
        payload = dict(payload)
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = 7
        return payload, False

pc = _PrimCap()
client = QQOfficeClient(bundle=None, self_client=SelfClient("a", "s"), rate_limiter=RateLimiter(),
                        refstore=RefStore(None, ttl_days=0), config={})
t("client mode", client.mode == "self")

import core.client as cl
t("safe dict", cl._SafeDict({"a": 1})["b"] == "{b}" and cl._SafeDict({"a": 1})["a"] == 1)

# ---------- ready ----------
from core.ready import ReadySignal, wait_for_star

rs = ReadySignal()
t("ready initial", rs.is_ready is False)

async def ready_test():
    t("ready wait timeout", await rs.wait(0.05) is False)
    rs.set()
    t("ready wait ok", await rs.wait(0.05) is True)
    t("ready flag", rs.is_ready is True)
asyncio.run(ready_test())

class _Meta:
    def __init__(self, activated=True, cls=None):
        self.activated = activated
        self.star_cls = cls

class _Svc:
    def __init__(self, ready):
        self._r = ready
    @property
    def ready(self):
        return self._r

async def star_wait_test():
    t("wait none", await wait_for_star(lambda: None, timeout=0.1, interval=0.02) is None)
    svc = _Svc(False)
    meta = _Meta(True, svc)
    async def flip():
        await asyncio.sleep(0.05)
        svc._r = True
    task = asyncio.create_task(flip())
    t("wait flips ready", await wait_for_star(lambda: meta, timeout=2, interval=0.02) is svc)
    await task
    t("wait inactive", await wait_for_star(lambda: _Meta(False, _Svc(True)), timeout=0.1, interval=0.02) is None)
    def boom():
        raise RuntimeError("x")
    t("wait boom", await wait_for_star(boom, timeout=0.1, interval=0.02) is None)
asyncio.run(star_wait_test())

print(f"\nALL {ok} CHECKS PASSED")
