# -*- coding: utf-8 -*-
"""intents 构造期注入 / 会话真值校验 / 4013/4014 自愈 / 实例隔离 的离线仿真测试。

用假的适配器模块（sys.modules 桩）模拟 botpy 时序：
client.intents 快照进 session['intent'] 后 identify 只读快照。
N 实例版：denied_bits 归运行实例（_InstancePatch），A 被拒不影响 B。
"""
import sys
import types

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.events import AdapterPatcher, EventBus  # noqa: E402
from core.routing import RobotStates, RouteCore  # noqa: E402

BASE = (1 << 30) | (1 << 25) | (1 << 12)   # AstrBot 适配器基础 intents
BOOT_BITS = (1 << 24) | (1 << 26) | (1 << 27)  # 默认构造期注入（guild_p2 无订阅）

ok = 0
def t(name, cond):
    global ok
    assert cond, f"FAIL: {name}"
    ok += 1
    print(f"  ok  {name}")


class FakeIntents:
    def __init__(self, value):
        self.value = value


class FakeClient:
    """botpy Client 关键语义：intents 为 int；_connection/_active_websockets。"""
    def __init__(self, intents_value):
        self.intents = intents_value
        self._connection = None
        self._active_websockets = set()
        self.closed = False

    def is_closed(self):
        return self.closed


class FakeAdapter:
    """模拟 QQOfficialPlatformAdapter.__init__：先建 Intents 再建 client。"""
    def __init__(self, pid="官机"):
        self.meta_obj = types.SimpleNamespace(name="qq_official", id=pid)
        self.config = {"id": pid, "appid": "A1", "secret": "s"}
        self.appid = "A1"
        self.intents = FakeIntents(BASE)
        self.client = FakeClient(self.intents.value)

    def meta(self):
        return self.meta_obj

    def get_client(self):
        return self.client


class FakeWS:
    """ManagedBotWebSocket 桩。"""
    def __init__(self, session=None, client=None):
        self._session = session
        self._client = client
        self.closed_args = None

    async def on_closed(self, code, msg):
        self.closed_args = (code, msg)


class FakeConn:
    def __init__(self):
        self._session_list = []
        self.state = None


class LogSink:
    def __init__(self):
        self.records = []

    def _add(self, level, msg):
        self.records.append((level, msg))

    def info(self, msg):
        self._add("info", msg)

    def warning(self, msg):
        self._add("warning", msg)

    def error(self, msg):
        self._add("error", msg)

    def count(self, level):
        return sum(1 for lv, _ in self.records if lv == level)


def make_stub_module():
    for name in (
        "astrbot", "astrbot.core", "astrbot.core.platform",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.qqofficial",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    qoa = types.ModuleType(
        "astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter"
    )
    qoa.QQOfficialPlatformAdapter = FakeAdapter
    qoa.ManagedBotWebSocket = FakeWS
    sys.modules[qoa.__name__] = qoa
    return qoa


def make_routes(logger=None):
    manager = types.SimpleNamespace(_inst_map={})
    routes = RouteCore(manager, RobotStates(), logger=logger)
    return manager, routes


def make_patcher(routes, logger=None):
    bus = EventBus({})
    return AdapterPatcher(routes, bus,
                          config={"enable_group_member_events": True},
                          logger=logger or LogSink())


def register(manager, adapter, pid="官机"):
    manager._inst_map[pid] = {"inst": adapter, "client_id": "u"}
    return adapter


# ---------- 1. 构造期注入：快照前完成 ----------
qoa = make_stub_module()
sink = LogSink()
manager, routes = make_routes(sink)
patcher = make_patcher(routes, sink)
patcher.install_adapter_hooks()
t("hooks installed", patcher._hooks_installed is True)

adapter = FakeAdapter()  # 适配器实例化（注入应已发生）
t("ctor inject client.intents", adapter.client.intents == BASE | BOOT_BITS)
t("ctor inject adapter.intents.value", adapter.intents.value == BASE | BOOT_BITS)

session = {"intent": adapter.client.intents, "session_id": ""}  # _pool_init 快照
t("identify snapshot carries bits", session["intent"] & BOOT_BITS == BOOT_BITS)
t("ctor inject log", any("构造期注入" in m for lv, m in sink.records if lv == "info"))

# 幂等：重复安装不叠加包装
orig_init = qoa.QQOfficialPlatformAdapter.__init__
patcher.install_adapter_hooks()
t("install idempotent", qoa.QQOfficialPlatformAdapter.__init__ is orig_init)

# ---------- 2. 运行时后补：活跃会话告警一次，待连会话直接补位 ----------
client = FakeClient(BASE)
conn = FakeConn()
pending = {"intent": BASE, "session_id": ""}
conn._session_list.append(pending)
client._connection = conn
active = {"intent": BASE, "session_id": "S1"}  # 已 identify，缺扩展位
client._active_websockets.add(FakeWS(session=active, client=client))

adapter2 = FakeAdapter()
adapter2.client = client
register(manager, adapter2)
patcher.refresh()             # 建路由 + 挂载 + 第一次真值校验（活跃缺位 → 告警 1 次）
pid = adapter2.meta_obj.id
t("runtime client.intents merged", client.intents & BOOT_BITS == BOOT_BITS)
t("pending session patched", pending["intent"] & BOOT_BITS == BOOT_BITS)
t("active session untouched", active["intent"] == BASE)  # 真值不被伪造
warn_before = sink.count("warning")
assert warn_before >= 1
patcher.refresh()             # 再刷：不重复告警
patcher.refresh()
t("warn dedup", sink.count("warning") == warn_before)
patch_rec = next(iter(patcher._patches.values()))
t("verdict recorded", patch_rec.verdict == ("missing", BOOT_BITS))

# 活跃会话带位 → ok 且 info 一次
active["intent"] |= BOOT_BITS
patcher.refresh()
t("verdict ok", patch_rec.verdict == "ok")
info_ok = [m for lv, m in sink.records if lv == "info" and "已含扩展位" in m]
t("ok info once", len(info_ok) == 1)

# ---------- 3. 4013/4014 自愈：剔位 + 实例内记忆；实例隔离 ----------
ws = FakeWS(session={"intent": BASE | BOOT_BITS, "session_id": "S2"}, client=client)
patcher._heal_intents_reject(ws, 4014, "disallowed intents")
t("heal strips session", ws._session["intent"] == BASE)
t("heal strips client", client.intents == BASE)
applied_mask = 0
for _b in patch_rec.intents_applied:
    applied_mask |= _b
t("denied remembered on instance", patch_rec.denied_bits == applied_mask == ((1 << 24) | (1 << 26) | (1 << 27)))
t("heal error logged", any("4014" in m for lv, m in sink.records if lv == "error"))
t("boot_bits excluded", patcher._boot_bits(patch_rec) == 0)

# 实例隔离：另一实例 B 不受 A 拒绝影响；A 同 ID 替换恢复旧补丁
manager._inst_map["官机"] = {"inst": FakeAdapter("官机"), "client_id": "u"}
manager._inst_map["B机"] = {"inst": FakeAdapter("B机"), "client_id": "u2"}
patcher.refresh()
patch_b = [p for p in patcher._patches.values() if p.route.platform_id == "B机"][0]
t("instance B unaffected", patch_b.denied_bits == 0 and patch_b.route.robot_key.appid == "A1")
t("A patch removed on replace", all(p.route.platform_id != "官机" for p in patcher._patches.values())
  or [p for p in patcher._patches.values() if p.route.platform_id == "官机"][0].denied_bits == 0)

# 无本插件 owned 的 client（本体自有位被拒）：不剔位、不记忆（二轮 #8）
ws3 = FakeWS(session={"intent": BASE | BOOT_BITS}, client=FakeClient(BASE | BOOT_BITS))
patcher._heal_intents_reject(ws3, 4013, "invalid intents")
t("non-attributed heal does not strip", ws3._session["intent"] == BASE | BOOT_BITS)

# 非拒断码不触发自愈
ws4 = FakeWS(session={"intent": BASE | BOOT_BITS}, client=FakeClient(BASE | BOOT_BITS))
patcher._heal_intents_reject(ws4, 1006, "abnormal")
t("non-reject code ignored", ws4._session["intent"] == BASE | BOOT_BITS)

# ---------- 4. 卸载还原 ----------
# 从所有权标记取 previous（真正本体原始）与 installed（本插件安装的函数）
meta = getattr(qoa.QQOfficialPlatformAdapter, patcher._CTOR_MARK, None)
original_init = meta.get("previous") if isinstance(meta, dict) else None
installed_init = meta.get("installed") if isinstance(meta, dict) else None
meta_ws = getattr(qoa.ManagedBotWebSocket, patcher._ONCLOSED_MARK, None)
original_closed = meta_ws.get("previous") if isinstance(meta_ws, dict) else None
installed_closed = meta_ws.get("installed") if isinstance(meta_ws, dict) else None
assert original_init is not None and installed_init is not None
# 非 owner 的 patcher 不得卸载活跃 patcher 的 hook（所有权规则）
patcher2 = make_patcher(make_routes()[1])
patcher2.uninstall_adapter_hooks()
t("non-owner uninstall keeps hooks",
  qoa.QQOfficialPlatformAdapter.__init__ is installed_init
  and qoa.ManagedBotWebSocket.on_closed is installed_closed)
# 实际 owner 卸载并还原
patcher.uninstall_adapter_hooks()
adapter3 = FakeAdapter()
t("uninstall restores __init__", adapter3.client.intents == BASE
  and qoa.QQOfficialPlatformAdapter.__init__ is original_init
  and qoa.ManagedBotWebSocket.on_closed is original_closed)

print(f"\nall {ok} cases passed")
