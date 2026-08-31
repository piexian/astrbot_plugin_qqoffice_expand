# -*- coding: utf-8 -*-
"""intents 构造期注入 / 会话真值校验 / 4013/4014 自愈 的离线仿真测试。

用假的适配器模块（sys.modules 桩）模拟 botpy 时序：
client.intents 快照进 session['intent'] 后 identify 只读快照。
"""
import sys
import types

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.events import (  # noqa: E402
    _PLUGIN_INTENT_BITS,
    AdapterPatcher,
    EventBus,
)

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
        self._handlers = {}


class FakeAdapter:
    """模拟 QQOfficialPlatformAdapter.__init__：先建 Intents 再建 client。"""
    def __init__(self):
        self.intents = FakeIntents(BASE)
        self.client = FakeClient(self.intents.value)


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


def make_stub_module():
    """注册 astrbot.qqofficial 适配器模块桩，返回模块对象。"""
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


def make_patcher(logger=None):
    bus = EventBus({})
    return AdapterPatcher(
        platform_manager=None, bus=bus,
        config={"enable_group_member_events": True},
        logger=logger or LogSink(),
    )


def make_bundle(client):
    return types.SimpleNamespace(mode="ws", client=client, instance_id="官机")


# ---------- 1. 构造期注入：快照前完成 ----------
qoa = make_stub_module()
sink = LogSink()
patcher = make_patcher(sink)
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
# 热安装场景：client 未经构造注入（base 位），活跃会话已 identify
client = FakeClient(BASE)
conn = FakeConn()
pending = {"intent": BASE, "session_id": ""}
conn._session_list.append(pending)
client._connection = conn
active = {"intent": BASE, "session_id": "S1"}  # 已 identify，缺扩展位
client._active_websockets.add(FakeWS(session=active, client=client))

bundle = make_bundle(client)
record = {"bundle": bundle, "on_added": [], "parser_keys": [],
          "intents_applied": [], "original_intents": None, "verdict": None}
specs = [s for s in __import__("core.events", fromlist=["EVENT_SPECS"]).EVENT_SPECS.values()
         if s.intent in (1 << 24, 1 << 26, 1 << 27)]
warn_before = sink.count("warning")
patcher._apply_intents(bundle, record, specs)
t("runtime client.intents merged", client.intents & BOOT_BITS == BOOT_BITS)
t("pending session patched", pending["intent"] & BOOT_BITS == BOOT_BITS)
t("active session untouched", active["intent"] == BASE)  # 真值不被伪造
t("warn once", sink.count("warning") == warn_before + 1)
patcher._apply_intents(bundle, record, specs)  # 再次轮询不重复告警
t("warn dedup", sink.count("warning") == warn_before + 1)
t("verdict recorded", record["verdict"] == ("missing", BOOT_BITS))

# 活跃会话带位 → ok 且 info 一次
active["intent"] |= BOOT_BITS
patcher._apply_intents(bundle, record, specs)
t("verdict ok", record["verdict"] == "ok")
info_ok = [m for lv, m in sink.records if lv == "info" and "已含扩展位" in m]
t("ok info once", len(info_ok) == 1)

# ---------- 3. 4013/4014 自愈：剔位 + 进程内记忆 ----------
ws = FakeWS(session={"intent": BASE | BOOT_BITS, "session_id": "S2"}, client=client)
patcher._heal_intents_reject(ws, 4014, "disallowed intents")
t("heal strips session", ws._session["intent"] == BASE)
t("heal strips client", client.intents == BASE)
t("denied remembered", patcher._denied_bits == _PLUGIN_INTENT_BITS)
t("heal error logged", any("4014" in m for lv, m in sink.records if lv == "error"))
t("boot_bits excluded", patcher._boot_bits() == 0)

# 拒断后新适配器实例不再注入
adapter2 = FakeAdapter()
t("no reinject after denied", adapter2.client.intents == BASE)

# 非拒断码不触发自愈
ws3 = FakeWS(session={"intent": BASE | BOOT_BITS}, client=FakeClient(BASE | BOOT_BITS))
patcher._heal_intents_reject(ws3, 1006, "abnormal")
t("non-reject code ignored", ws3._session["intent"] == BASE | BOOT_BITS)

# ---------- 4. 卸载还原 ----------
patcher2 = make_patcher()  # denied=0 的新实例（模拟插件重载后权限已开通）
patcher2.uninstall_adapter_hooks()
adapter3 = FakeAdapter()
t("uninstall restores __init__", adapter3.client.intents == BASE)
t("on_closed restored", qoa.ManagedBotWebSocket.on_closed is FakeWS.on_closed)

print(f"\nall {ok} cases passed")
