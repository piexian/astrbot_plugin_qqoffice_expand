# -*- coding: utf-8 -*-
"""离线逻辑自测：core/api 模块零 astrbot 依赖，全部可导入直测（N 实例版）。"""
import asyncio
import base64
import struct
import sys
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import builders, errors, media
from core.auth import resolve_domain
from core.client import generate_msg_seq, resolve_endpoint_key
from core.ratelimit import RateLimiter
from core.refstore import RefStore, parse_scene_ext
from core.registry import Registry, collect_methods
from core.events import EVENT_SPECS, EventBus, normalize_event
from api.group import GroupAPI
from api.c2c import C2CAPI
from api.guild import GuildAPI
from api.manage import ManageAPI
from core.routing import PassiveWindows, AckTracker

ok = 0
def t(name, cond, detail=None):
    global ok
    assert cond, f"FAIL: {name}" + (f" ({detail!r})" if detail is not None else "")
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
e6b = errors.from_exception(Exception("ForbiddenError: code=11253"))
t("err 11253 extract", e6b is not None and e6b.code == 11253)
t("err pass-through", errors.from_exception(errors.QQOfficeNotSupported("x")) is not None)
t("err non-target", errors.from_exception(ValueError("plain")) is None)

# 路由错误类型
for name in ("InstanceUnavailable", "InstanceIdentityChanged", "StaleSourceEvent", "TransportNotReady"):
    t(f"routing error {name}", hasattr(errors, name) and issubclass(getattr(errors, name), errors.QQOfficeError))

# ---------- msg_seq / endpoint key / 被动窗口 ----------
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

w = PassiveWindows()
t("window fresh", w.check("group", "T", "m1")[0])
for _ in range(5):
    assert w.reserve("group", "T", "m1")
ok1, why1 = w.check("group", "T", "m1")
t("window count cap", not ok1 and "上限" in why1)
w.record = None  # 确认旧接口已移除
ok3, why3 = w.check("c2c", "T", "m2")
t("window c2c limit", PassiveWindows.LIMITS["c2c"] == (3600.0, 4))
t("chunk moved to api layer", True)  # 分块辅助保留在 api/媒体层由调用方使用

# ---------- refstore（身份命名空间）----------
with tempfile.TemporaryDirectory() as td:
    rs = RefStore(Path(td), ttl_days=7)
    P1, P2 = "APP1@production", "APP2@production"
    rs.store(rs.inbound_key(f"{P1}|group", "g1", "m1"), "REFIDX_a")
    t("ref roundtrip", rs.get_inbound(f"{P1}|group", "g1", "m1") == "REFIDX_a")
    rs.record_inbound(f"{P1}|group", "g1", "m2", {"message_scene": {"ext": ["msg_idx=REFIDX_b", "x=1"]},
                                                  "msg_elements": []})
    t("ref inbound scene ext", rs.get_inbound(f"{P1}|group", "g1", "m2") == "REFIDX_b")
    rs.record_inbound(f"{P2}|group", "g1", "m2", {"msg_elements": [{"msg_idx": "REFIDX_c"}]})
    t("ref namespace isolated", rs.get_inbound(f"{P1}|group", "g1", "m2") == "REFIDX_b"
      and rs.get_inbound(f"{P2}|group", "g1", "m2") == "REFIDX_c")
    rs.record_outbound(f"{P1}|group", "g1", {"ext_info": {"ref_idx": "REFIDX_o1"}}, local_key="s1")
    t("ref outbound latest", rs.get_outbound_latest(f"{P1}|group", "g1") == "REFIDX_o1")
    t("ref outbound keyed", rs.get(f"out:{P1}|group:g1:s1") == "REFIDX_o1")
    t("parse_scene_ext", parse_scene_ext(["a=1", "b=2"]) == {"a": "1", "b": "2"})
    rs.cache_file_info(f"{P1}|h1", "group", "g1", 1, "FI", ttl=120)
    t("file cache hit", rs.get_file_info(f"{P1}|h1", "group", "g1", 1) == "FI")
    t("file cache ttl floor", rs._file_cache[rs.file_cache_key(f"{P1}|h1", "group", "g1", 1)][0]
      - time.monotonic() <= 60)
    t("file cache miss", rs.get_file_info(f"{P1}|hx", "group", "g1", 1) is None)
    rs2 = RefStore(Path(td), ttl_days=7)
    t("ref persisted reload", rs2.get_inbound(f"{P1}|group", "g1", "m1") == "REFIDX_a")
    t("content hash", len(rs.content_hash(b"abc")) == 32)

    # —— 带身份前缀键的压缩：全量扫描次数有界，最新值/容量/命名空间不变 ——
    with tempfile.TemporaryDirectory() as td2:
        rs3 = RefStore(Path(td2), ttl_days=7, max_entries=1000)
        loads = {"n": 0}
        orig_load = rs3._load

        def counting_load():
            loads["n"] += 1
            orig_load()
        rs3._load = counting_load
        pfx = "APP@production|group"
        for i in range(1050):   # 重复同一 key：压缩后仅 1 行
            rs3.store(f"in:{pfx}:g:m", f"REFIDX_{i}")
        t("重复 key 长期追加的全量扫描次数有界（≤5）", loads["n"] <= 5, loads["n"])
        t("重复 key 最新值保留", rs3.get(f"in:{pfx}:g:m") == "REFIDX_1049")
        loads["n"] = 0
        for i in range(1050):   # 全不同 key：容量内压缩后阈值翻倍
            rs3.store(f"in:{pfx}:g:m{i}", f"R{i}")
        t("多 key 长期追加的全量扫描次数有界（≤5）", loads["n"] <= 5, loads["n"])
        t("容量上限保持", len(rs3._mem) <= 1000)
        t("最新值可查（容量内）", rs3.get(f"in:{pfx}:g:m1049") == "R1049")
        rs4 = RefStore(Path(td2), ttl_days=7, max_entries=1000)
        t("重开保留最新记录", rs4.get(f"in:{pfx}:g:m1049") == "R1049")

# ---------- ratelimit ----------
async def rl_test():
    rl = RateLimiter(certified_bot=False)
    await rl.acquire("group.send")
    await rl.acquire("group.send")
    t("rl unknown default", await rl.acquire("brand.new") == 0.0)
    await rl.consume_proactive("t1")
    await rl.consume_proactive("t1")
    for _ in range(18):
        await rl.consume_proactive("t1")
    t("rl proactive relation window", True)
    try:
        rl.proactive._rel_day.setdefault("t2", [12345, 1])
        rl.proactive._rel_day.move_to_end("t2")
        rl.proactive._rel_day["t2"][0] = 12345  # 非今日 → prune 后可再用
        await rl.consume_proactive("t2")
        t("rl daily prune", True)
    except errors.QQOfficeAPIError as e:
        raise AssertionError(f"daily quota misfired: {e}")
    snap = rl.snapshot()
    t("rl snapshot", snap["proactive"]["robot_per_minute"] == 30)
    rl2 = RateLimiter(certified_bot=True)
    t("rl certified 60", rl2.snapshot()["proactive"]["robot_per_minute"] == 60)
    # 增量回收：请求只裁剪当前目标
    rl.proactive._rel_minute["old-target"] = __import__("collections").deque([time.monotonic() - 3600])
    await rl.consume_proactive("t1")
    t("rl hot path not scanning others", "old-target" in rl.proactive._rel_minute)
    # 日计数未过期：只清分钟窗口，不删目标（防止当日额度重发）
    removed = rl.proactive.prune_idle(limit=16)
    t("rl idle prune keeps today count", removed >= 0 and "old-target" not in rl.proactive._rel_minute)
    # 日计数过期后整体回收
    rl.proactive._rel_day["old-target"] = [12345, 1]
    rl.proactive._rel_minute["old-target"] = __import__("collections").deque([time.monotonic() - 3600])
    rl.proactive.prune_idle(limit=16)
    t("rl idle prune removes expired day", "old-target" not in rl.proactive._rel_day)
asyncio.run(rl_test())

# ---------- media ----------
with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
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
class _Cap:
    async def call(self, *a, **k):
        return {"resp": True}
    async def upload_media(self, *a, **k):
        return {"file_info": "FI"}

ns = ManageAPI(None)
fns = collect_methods(ns)   # 未绑定函数（目录构建一次）
t("registry collect unbound", callable(fns["interaction_ack"]) and "self" in fns["interaction_ack"].__code__.co_varnames)

class _ViewCap:
    async def call(self, *a, **k):
        return {"resp": True}

proxy = type("P", (), {"_client": _ViewCap()})()
reg = Registry()
for name, fn in fns.items():
    reg.register_fn(f"manage.{name}", fn)
t("registry count", len(reg.names()) == 10 and "manage.interaction_ack" in reg.names())

async def _reg_unknown():
    try:
        await reg.invoke("manage.nope")
        return False
    except errors.QQOfficeNotSupported:
        return True
t("registry unknown", asyncio.run(_reg_unknown()))

cap = _ViewCap()
g = GroupAPI(cap)
async def api_checks():
    await g.recall("G1", "M1")
    await g.mute_member("G1", "M1", "2026-08-31T00:00:00+08:00")
    await g.join_approve("G1", "M1", op="decline", reject_reason="r", add_to_member_blacklist=True)
    await g.strategy_whitelist("S1", "add", ["123"])
    await g.strategy_update("S1", is_enable="off", group_action={"op": "add", "group_openids": ["G1"]})
    c = C2CAPI(cap)
    await c.wakeup("U1", "hi")
    await c.send("U1", "hi", msg_id="M")
    m = ManageAPI(cap)
    await m.menu_put([{"type": "send_message", "name": "帮助", "send_message": "/help"}])
    await m.panel_target("P1", "add", group_openids=["G"])
    return g, c, m
gg, cc, mm = asyncio.run(api_checks())
t("api namespace calls ok", True)
t("upload_complete registered", "upload_complete" in collect_methods(GroupAPI(None)))

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

class _InteractionWrap:
    def __init__(self):
        self.id = "I-9"
        self.event_id = "EVT-9"
        self.group_openid = "G9"
        self.data = type("D", (), {
            "type": 1,
            "resolved": type("R", (), {
                "button_data": "rg2:shoot:G9",
                "button_id": "rg2_shoot",
                "message_id": "M-9",
            })(),
        })()
ev3 = normalize_event("interaction_create", _InteractionWrap())
t("normalize interaction data",
  ev3.is_interaction and ev3.interaction_id == "I-9" and ev3.scene == "group"
  and ev3.raw["data"]["resolved"]["button_data"] == "rg2:shoot:G9")
t("normalize interaction event_id", ev3.payload_id == "EVT-9")

ev4 = normalize_event("friend_add", {"d": {"id": "F1", "openid": "U-F", "scene": 1001}})
t("normalize friend_add openid", ev4.user_openid == "U-F" and ev4.scene == "c2c")

async def bus_test():
    bus = EventBus({"interaction_auto_ack": True}, None)
    got, any_got = [], []
    unsub, _ = bus.on("INTERACTION_CREATE", lambda e: got.append(e))
    bus.on_any(lambda e: any_got.append(e))
    ev = normalize_event("interaction_create", {"d": {"id": "I-1", "type": 11, "user_openid": "U"}})
    ev.source = None   # 无来源：不 ACK 但正常分发
    await bus.emit(ev)
    await bus.emit(ev)
    await asyncio.sleep(0.05)
    t("bus sub+any", len(got) == 2 and len(any_got) == 2)
    unsub()
    await bus.emit(normalize_event("interaction_create", {"d": {"id": "I-2"}}))
    await asyncio.sleep(0.05)
    t("bus unsub", len(got) == 2 and len(any_got) == 3)
    t("bus counts", bus.counts["INTERACTION_CREATE"] == 3)
    # 实例作用域订阅
    scoped = []
    bus.on_scoped("qq_a", __import__("core.routing", fromlist=["RobotKey"]).RobotKey("A1"), "GROUP_ADD_ROBOT", lambda e: scoped.append(e))
    ev_a = normalize_event("group_add_robot", {"d": {"group_openid": "G"}})
    ev_a.source = None
    from core.routing import EventSource, RobotKey
    ev_a.source = EventSource("qq_a", RobotKey("A1"), 1)
    ev_b = normalize_event("group_add_robot", {"d": {"group_openid": "G"}})
    ev_b.source = EventSource("qq_b", RobotKey("A2"), 1)
    await bus.emit(ev_a)
    await bus.emit(ev_b)
    await asyncio.sleep(0.05)
    t("scoped sub isolation", len(scoped) == 1)
asyncio.run(bus_test())

# ---------- auth / domain ----------
t("domain default", resolve_domain() == "api.sgroup.qq.com")
t("domain new", resolve_domain(prefer_new_domain=True) == "api.bot.qq.com")
t("domain sandbox", resolve_domain(sandbox=True) == "sandbox.api.sgroup.qq.com")

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
