# -*- coding: utf-8 -*-
"""多实例路由核心行为测试（零 astrbot 依赖，禁网沙箱运行）。

覆盖设计第 10 节中属于路由基础的场景：
N=1/2/10/50/100、任意插入顺序、非 QQ 平台混入、WS/Webhook 混用、
随机目标的请求严格命中对应 botpy HTTP 身份、同 AppID 共享/不同 AppID 隔离、
禁用/删除即时拒绝、同 ID 重载、改绑 AppID、旧事件与跨代次操作失败、
同 client 内 API 替换、空 token 未就绪、共享状态不因重载重置。
"""
import asyncio
import random
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import errors as qe
from core.routing import (
    AckTracker,
    BoundSource,
    OperationContext,
    PassiveWindows,
    PlatformIndex,
    RobotKey,
    RouteCore,
)

ok = 0
def t(name, cond):
    global ok
    assert cond, f"FAIL: {name}"
    ok += 1
    print(f"  ok  {name}")


# ---------------- 假本体 ----------------

class CountedMap(dict):
    """统计 get 次数的 _inst_map 桩：验证每次解析只查一次本体索引。"""
    def __init__(self):
        super().__init__()
        self.get_calls = 0

    def get(self, key, default=None):
        self.get_calls += 1
        return super().get(key, default)


class FakeHttp:
    def __init__(self, appid, token=True):
        self.appid = appid
        self._token = object() if token else None


class FakeApi:
    def __init__(self, http):
        self._http = http


class FakeClient:
    def __init__(self, appid, token=True):
        self.api = FakeApi(FakeHttp(appid, token))
        self.closed = False

    def is_closed(self):
        return self.closed


class FakeMeta:
    def __init__(self, name, pid):
        self.name = name
        self.id = pid


class FakeAdapter:
    """模拟 qq_official / qq_official_webhook 适配器实例。

    与真实适配器一致：appid 在构造时固化为实例属性；config 可被面板
    独立修改（inst.appid 不再变）。webhook 模式才消费 config['is_sandbox']。
    """
    def __init__(self, pid, appid, mode="ws", sandbox=False, with_client=True,
                 *, config_appid=None):
        self.meta_obj = FakeMeta("qq_official_webhook" if mode == "webhook" else "qq_official", pid)
        self.config = {"id": pid, "appid": config_appid if config_appid is not None else appid,
                       "secret": "s", "is_sandbox": sandbox}
        self.appid = appid
        self.client = FakeClient(appid) if with_client else None
        self.client_self_id = f"uuid-{pid}-{id(self)}"

    def meta(self):
        return self.meta_obj

    def get_client(self):
        return self.client


class FakeOtherAdapter(FakeAdapter):
    def __init__(self, pid):
        super().__init__(pid, "x")
        self.meta_obj = FakeMeta("aiocqhttp", pid)


class FakeManager:
    def __init__(self):
        self._inst_map = CountedMap()


class FakeClock:
    """可控 monotonic 时钟：patch core.routing.time 后验证到期/重入队时间线。"""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


def make_manager(specs):
    """specs: list[(pid, appid, mode, sandbox)] -> (manager, {pid: adapter})"""
    m = FakeManager()
    adapters = {}
    for pid, appid, mode, sandbox in specs:
        ad = FakeAdapter(pid, appid, mode, sandbox)
        adapters[pid] = ad
        m._inst_map[pid] = {"inst": ad, "client_id": ad.client_self_id}
    return m, adapters


def core_for(m):
    return RouteCore(m, logger=None)


# ---------------- 1. N 实例随机解析与 O(1) ----------------

def test_scale():
    for n in (1, 2, 10, 50, 100):
        random.seed(2026 + n)
        specs = [(f"qq_{i}", f"appid_{i}", random.choice(("ws", "webhook")), i % 7 == 0)
                 for i in range(n)]
        m, adapters = make_manager(specs)
        rc = core_for(m)
        views = {}
        for pid in adapters:
            route = rc.ensure_current_route(pid)
            views[pid] = BoundSource.for_instance(pid, route.robot_key)
        pids = list(views)
        random.shuffle(pids)
        m._inst_map.get_calls = 0
        for pid in pids * 3:  # 每个视图 3 次随机解析
            route, http = rc.resolve(views[pid])
            assert http.appid == adapters[pid].config["appid"], (pid, http.appid)
            assert route.platform_id == pid
            expected_mode = "webhook" if adapters[pid].meta_obj.name.endswith("_webhook") else "ws"
            assert route.mode == expected_mode, (pid, route.mode)
        t(f"N={n} 随机解析严格命中 HTTP 身份", True)
        # 每次 resolve（内含 ensure_current_route）只查询一次本体索引
        assert m._inst_map.get_calls == len(pids) * 3, m._inst_map.get_calls
        t(f"N={n} 每次解析恰好一次 _inst_map 查询（O(1)）", True)
        assert len(rc.states) == len({a.config["appid"] for a in adapters.values()})
        t(f"N={n} 机器人状态数 = 去重身份数", True)

def test_insert_order_and_mixed():
    random.seed(7)
    specs = [(f"p{i}", f"a{i}", random.choice(("ws", "webhook")), False) for i in range(20)]
    m, adapters = make_manager(specs)
    m._inst_map["webchat"] = {"inst": FakeOtherAdapter("webchat"), "client_id": "w"}  # 非 QQ 平台混入：仅作干扰项
    rc = core_for(m)
    order = list(adapters)
    random.shuffle(order)
    for pid in order:
        rc.ensure_current_route(pid)
    assert "webchat" not in rc.routes
    try:
        rc.resolve(BoundSource.for_instance("webchat", RobotKey("other")))
        raise AssertionError("webchat 不应可解析")
    except qe.InstanceUnavailable:
        pass
    t("非 QQ 平台混入被拒绝且不建路由", True)
    for pid in order:  # 干扰项不影响其他实例
        route, http = rc.resolve(BoundSource.for_instance(
            pid, RobotKey(adapters[pid].config["appid"])))
        assert http.appid == adapters[pid].config["appid"]
    t("非 QQ 平台不影响其余实例解析", True)


# ---------------- 2. 状态共享与隔离 ----------------

def test_state_sharing():
    m, adapters = make_manager([
        ("sales_a", "APP1", "ws", False),
        ("sales_b", "APP1", "webhook", False),   # 同 AppID 同环境，不同模式
        ("prod_x", "APP2", "ws", False),
        ("sand_x", "APP2", "webhook", True),     # 同 AppID 不同环境（webhook 才支持沙箱）
    ])
    rc = core_for(m)
    ra = rc.ensure_current_route("sales_a")
    rb = rc.ensure_current_route("sales_b")
    rp = rc.ensure_current_route("prod_x")
    rs = rc.ensure_current_route("sand_x")
    assert rc.state_for(ra.robot_key) is rc.state_for(rb.robot_key)
    t("同 AppID 同环境多实例共享 RobotState", True)
    assert rc.state_for(rp.robot_key) is not rc.state_for(ra.robot_key)
    t("不同 AppID 状态隔离", True)
    assert rc.state_for(rs.robot_key) is not rc.state_for(rp.robot_key)
    assert rs.robot_key.environment == "sandbox" and rp.robot_key.environment == "production"
    t("沙箱与生产身份分开", True)

    # 共享用量：任一实例的预留都记入同一状态
    state = rc.state_for(ra.robot_key)
    assert state.windows.reserve("group", "G1", "M1")[0]
    same = rc.state_for(rb.robot_key)
    assert same is state
    ok1, why = same.windows.check("group", "G1", "M1")
    assert ok1
    for _ in range(4):
        assert same.windows.reserve("group", "G1", "M1")[0]
    ok2, why2 = same.windows.reserve("group", "G1", "M1")
    assert not ok2 and "上限" in why2
    t("同身份实例共享被动回复计数", True)

    # 重载/替换/禁用不清除未过期用量
    ad_new = FakeAdapter("sales_a", "APP1", "ws", False)
    m._inst_map["sales_a"] = {"inst": ad_new, "client_id": ad_new.client_self_id}
    ra2 = rc.ensure_current_route("sales_a")
    assert ra2.generation != ra.generation
    assert rc.state_for(ra2.robot_key) is state
    ok3, why3 = state.windows.check("group", "G1", "M1")
    assert not ok3 and "上限" in why3  # 重载前已用满 5 次，计数跨重载保留
    t("重载后共享计数仍保留（未重置）", True)
    # 禁用后状态保留
    m._inst_map.pop("sales_a")
    try:
        rc.ensure_current_route("sales_a")
        raise AssertionError("禁用后应拒绝")
    except qe.InstanceUnavailable:
        pass
    assert rc.states.get(ra.robot_key) is state
    t("禁用实例后机器人状态保留", True)


# ---------------- 3. 重载 / 改绑 / 旧来源 ----------------

def test_reload_and_stale():
    m, adapters = make_manager([("sales", "APP1", "ws", False)])
    rc = core_for(m)
    route1 = rc.ensure_current_route("sales")
    view = BoundSource.for_instance("sales", route1.robot_key)
    src_client1 = adapters["sales"].client
    ctx1 = rc.operation(route1)
    src1 = rc.source_of(route1)

    # 同 ID 重载：新适配器对象 -> 新代次
    ad_new = FakeAdapter("sales", "APP1", "ws", False)
    m._inst_map["sales"] = {"inst": ad_new, "client_id": ad_new.client_self_id}
    route2 = rc.ensure_current_route("sales")
    assert route2.generation > route1.generation and route2.inst is ad_new
    # 长期视图（同身份、无来源约束）跟随重载继续可用
    r, http = rc.resolve(view)
    assert r.generation == route2.generation and http is ad_new.client.api._http
    t("长期实例视图跟随同身份重载", True)
    # 旧原生事件（event.bot 是旧 client）必须失败
    try:
        rc.resolve(BoundSource(view.platform_id, view.robot_key, source_client=src_client1))
        raise AssertionError("旧 event.bot 应失败")
    except qe.StaleSourceEvent:
        pass
    t("旧 event.bot 的原生事件被拒绝", True)
    # 旧代次的扩展事件与跨代次操作失败
    try:
        rc.resolve(BoundSource.from_event_source(src1))
        raise AssertionError("旧代次事件应失败")
    except qe.StaleSourceEvent:
        pass
    try:
        rc.check_context(ctx1)
        raise AssertionError("跨代次操作应失败")
    except qe.StaleSourceEvent:
        pass
    t("旧代次事件与操作上下文被拒绝", True)
    # 新代次操作上下文可用，并动态取当前 HTTP
    ctx2 = rc.operation(route2)
    assert rc.check_context(ctx2) is ad_new.client.api._http
    t("新代次操作上下文核验通过", True)

    # 同 ID 改绑另一 AppID：旧视图不得转给新机器人
    ad_other = FakeAdapter("sales", "APP2", "ws", False)
    m._inst_map["sales"] = {"inst": ad_other, "client_id": ad_other.client_self_id}
    try:
        rc.resolve(view)
        raise AssertionError("改绑后旧视图应失败")
    except qe.InstanceIdentityChanged:
        pass
    route3 = rc.ensure_current_route("sales")
    assert route3.robot_key.appid == "APP2"
    assert rc.states.get(route1.robot_key) is not None  # 旧身份状态保留
    t("改绑 AppID 后旧视图失败、旧状态保留", True)
    # 旧代次操作也不能重放到新机器人
    try:
        rc.check_context(ctx2)
        raise AssertionError("改绑后旧操作应失败")
    except qe.InstanceIdentityChanged:
        pass
    t("改绑后旧操作不跨机器人重放", True)


# ---------------- 4. 禁用 / 传输就绪 / API 替换 ----------------

def test_disable_and_transport():
    m, adapters = make_manager([
        ("gone", "A1", "ws", False),
        ("keep", "A2", "webhook", False),
    ])
    rc = core_for(m)
    rc.ensure_current_route("gone")
    keep_view = BoundSource.for_instance("keep", RobotKey("A2"))
    m._inst_map.pop("gone")
    try:
        rc.resolve(BoundSource.for_instance("gone", RobotKey("A1")))
        raise AssertionError("删除后应拒绝")
    except qe.InstanceUnavailable:
        pass
    route, http = rc.resolve(keep_view)
    t("删除实例即时拒绝，其余不受影响（无需轮询）", True)

    # Webhook 同 client 内替换 api/http：不增代次、动态取新 HTTP
    old_http = route.client.api._http
    new_http = FakeHttp("A2")
    adapters["keep"].client.api = FakeApi(new_http)
    route2, http2 = rc.resolve(keep_view)
    assert http2 is new_http and http2 is not old_http
    assert route2.generation == route.generation
    t("同 client 替换 API 后取新 HTTP 且不增代次", True)

    # 空 token / 无 API：明确未就绪
    adapters["keep"].client.api = FakeApi(FakeHttp("A2", token=False))
    try:
        rc.resolve(keep_view)
        raise AssertionError("空 token 应未就绪")
    except qe.TransportNotReady:
        pass
    adapters["keep"].client.api = None
    try:
        rc.resolve(keep_view)
        raise AssertionError("无 API 应未就绪")
    except qe.TransportNotReady:
        pass
    t("空 token / 无 API 返回 TransportNotReady", True)

    # client 关闭：不可用
    adapters["keep"].client.api = FakeApi(FakeHttp("A2"))
    adapters["keep"].client.closed = True
    try:
        rc.resolve(keep_view)
        raise AssertionError("关闭中的 client 应不可用")
    except qe.InstanceUnavailable:
        pass
    t("关闭中的 client 拒绝新请求", True)

    # 缺少 appid 的适配器：身份未知，拒绝
    m._inst_map["noappid"] = {"inst": FakeAdapter("noappid", "", "ws", False),
                              "client_id": "n"}
    try:
        rc.ensure_current_route("noappid")
        raise AssertionError("缺 appid 应拒绝")
    except qe.InstanceUnavailable:
        pass
    t("缺 appid 的适配器不可路由", True)

    # 缺少 _inst_map 的本体：明确不支持，不退回扫描
    try:
        PlatformIndex(object()).entry("x")
        raise AssertionError("应报不支持")
    except qe.QQOfficeNotSupported:
        pass
    t("本体缺 _inst_map 时明确报不支持", True)


# ---------------- 5. 全体差异刷新 ----------------

def test_refresh_diff():
    m, adapters = make_manager([("a", "A1", "ws", False)])
    rc = core_for(m)
    base = rc.refresh_all()  # 基线：协调任务首次发现
    assert [r.platform_id for r in base.added] == ["a"]
    m._inst_map["b"] = {"inst": adapters.setdefault("b", FakeAdapter("b", "A2", "webhook", False)),
                        "client_id": "b-self"}
    d1 = rc.refresh_all()
    assert [r.platform_id for r in d1.added] == ["b"] and len(d1.unchanged) == 1
    t("差异刷新：新增发现", True)
    d2 = rc.refresh_all()
    assert not (d2.added or d2.removed or d2.replaced) and len(d2.unchanged) == 2
    t("差异刷新：无变化为零改动", True)
    # 替换
    ad = FakeAdapter("a", "A1", "ws", False)
    m._inst_map["a"] = {"inst": ad, "client_id": ad.client_self_id}
    d3 = rc.refresh_all()
    assert len(d3.replaced) == 1 and d3.replaced[0][0].platform_id == "a"
    assert d3.replaced[0][1].generation > d3.replaced[0][0].generation
    t("差异刷新：同 ID 替换产生新代次", True)
    # 消失
    m._inst_map.pop("b")
    d4 = rc.refresh_all()
    assert [r.platform_id for r in d4.removed] == ["b"] and "b" not in rc.routes
    t("差异刷新：消失实例移除", True)
    # 请求侧 ensure 建立的首个路由（协调从未见过）在下轮巡检报告为 added
    m._inst_map["c"] = {"inst": FakeAdapter("c", "A3", "webhook", False), "client_id": "c-self"}
    rc.ensure_current_route("c")
    d5 = rc.refresh_all()
    assert [r.platform_id for r in d5.added] == ["c"]
    assert len(d5.unchanged) == 1   # 未变更的 a 正常进入 unchanged
    d6 = rc.refresh_all()
    assert not (d6.added or d6.removed or d6.replaced) and len(d6.unchanged) == 2
    t("请求侧首个路由作为 added 报告且不重复", True)


def test_coordinator_fold_cases():
    """协调差异收敛回归（对应 coordination_review_probe 场景）。"""
    # 场景 1：挂载 A → 请求侧 A→B → 本体 B→C → refresh：折叠为 replaced(A, C)，
    # 不挂载中间代次 B，无幽灵记录。
    m, adapters = make_manager([("a", "A1", "ws", False)])
    rc = core_for(m)
    base = rc.refresh_all()
    assert len(base.added) == 1
    ad_b = FakeAdapter("a", "A1", "ws", False)
    m._inst_map["a"] = {"inst": ad_b, "client_id": ad_b.client_self_id}
    rc.ensure_current_route("a")              # 请求侧先见 A→B（未挂载）
    ad_c = FakeAdapter("a", "A1", "ws", False)
    m._inst_map["a"] = {"inst": ad_c, "client_id": ad_c.client_self_id}
    d1 = rc.refresh_all()
    assert len(d1.replaced) == 1
    assert d1.replaced[0][0] is base.added[0] and d1.replaced[0][1] is rc.route_of("a")
    assert not d1.unchanged
    # 协调状态收敛到 1 份当前代次（无中间代次 B 的记录）
    assert len(rc._changes) == 0
    d2 = rc.refresh_all()
    assert not (d2.added or d2.removed or d2.replaced) and len(d2.unchanged) == 1
    t("请求侧 A→B + 巡检 B→C 折叠为单条 replaced(A, C)", True)

    # 场景 2：挂载 A → 请求侧 A→B → 本体删除 B → refresh：只恢复旧补丁，
    # 不挂载已删除的 B。
    m2, _ = make_manager([("a", "A1", "ws", False)])
    rc2 = core_for(m2)
    base2 = rc2.refresh_all()
    assert len(base2.added) == 1
    ad_b2 = FakeAdapter("a", "A1", "ws", False)
    m2._inst_map["a"] = {"inst": ad_b2, "client_id": ad_b2.client_self_id}
    rc2.ensure_current_route("a")
    m2._inst_map.pop("a")
    d3 = rc2.refresh_all()
    assert [r.platform_id for r in d3.removed] == ["a"]
    assert not (d3.added or d3.replaced) and not d3.unchanged
    assert "a" not in rc2.routes and len(rc2._changes) == 0
    t("请求侧 A→B 后本体删除：只恢复旧补丁，不挂中间代次", True)

    # 场景 3：本轮扫描期的新增+替换折叠（请求与巡检同轮多链）
    m3, _ = make_manager([("a", "A1", "ws", False), ("b", "A2", "ws", False)])
    rc3 = core_for(m3)
    base3 = rc3.refresh_all()
    assert len(base3.added) == 2
    # a: 请求侧两连跳；b: 仅巡检替换
    for _ in range(2):
        ad = FakeAdapter("a", "A1", "ws", False)
        m3._inst_map["a"] = {"inst": ad, "client_id": ad.client_self_id}
        rc3.ensure_current_route("a")
    ad = FakeAdapter("b", "A2", "ws", False)
    m3._inst_map["b"] = {"inst": ad, "client_id": ad.client_self_id}
    d4 = rc3.refresh_all()
    assert len(d4.replaced) == 2 and not d4.unchanged
    old_a = base3.added[0]
    rep_a = [r for r in d4.replaced if r[0] is old_a]
    assert rep_a and rep_a[0][1] is rc3.route_of("a")   # 两次跳变折叠为一条
    t("多实例混合 pending+巡检变更各自折叠为单条", True)


# ---------------- 6. 被动窗口 / ACK / 真实 botpy HTTP ----------------

def test_passive_windows():
    w = PassiveWindows()
    for _ in range(5):
        assert w.reserve("group", "G1", "M")
    ok1, why = w.reserve("group", "G1", "M")
    assert not ok1 and "上限" in why
    w.release("group", "G1", "M")
    assert w.reserve("group", "G1", "M")[0]
    t("预留-释放-确认语义", True)
    # 同 msg_id 不同 scene / target 不串计数
    assert w.check("c2c", "G1", "M")[0]          # scene 不同
    assert w.check("group", "G2", "M")[0]        # target 不同
    w.reserve("group", "G2", "M")
    assert w._records[PassiveWindows._key("group", "G2", "M")][1] == 1
    assert w._records[PassiveWindows._key("group", "G1", "M")][1] == 5
    t("同 msg_id 跨 scene/target 计数隔离", True)
    # 过期窗口不重置：官方会拒绝过期 msg_id
    rec = w._records[PassiveWindows._key("group", "G1", "M")]
    rec[0] -= 400
    ok2, why2 = w.reserve("group", "G1", "M")
    assert not ok2 and "过期" in why2
    assert rec[1] == 5  # 未被重置
    t("过期窗口拒绝且不重置", True)
    # check 保持只读
    assert w.check("c2c", "U", "X")[0]
    assert w._records.get(PassiveWindows._key("c2c", "U", "X")) is None
    t("check 不占用额度", True)

    # —— 清理队列按 scene 隔离：长窗口不阻塞短窗口（问题 2 回归）——
    w2 = PassiveWindows()
    base = time.monotonic()
    w2.reserve("c2c", "U", "long")      # 入队 base+3600
    w2.reserve("group", "G", "short")   # 入队 base+301
    for rec2 in w2._records.values():
        rec2[0] = base - 10               # 两个键的记录都已超窗
    removed = w2.prune_expired(base + 500, limit=10)   # 301 已到，3600 未到
    assert removed == 1
    assert w2._records.get(PassiveWindows._key("group", "G", "short")) is None
    assert w2._records.get(PassiveWindows._key("c2c", "U", "long")) is not None
    t("短窗口先于长窗口清理（不互相阻塞）", True)
    for rec2 in w2._records.values():
        rec2[0] = base - 10
    assert w2.prune_expired(base + 5000, limit=1) == 1
    assert w2.prune_expired(base + 5000, limit=1) == 0  # 队列已空，零开销
    t("全部到期后清完且空队列零开销", True)


def test_ack_tracker():
    a = AckTracker()
    assert a.try_reserve("I1") == "new"
    assert a.try_reserve("I1") == "duplicate"      # pending 期间不重答
    a.release("I1")                                 # 发送前失败 → 可重答
    assert a.try_reserve("I1") == "new"
    a.succeed("I1")
    assert a.try_reserve("I1") == "duplicate"
    assert a.state_of("I1") == "succeeded"
    assert a.try_reserve("I2") == "new"
    a.settle_unknown("I2")                          # 结果未知：按已应答，不重复应答
    assert a.try_reserve("I2") == "duplicate"
    assert a.state_of("I2") == "settled"
    a.release("I2")                                 # settled 不可 release
    assert a.try_reserve("I2") == "duplicate"
    t("ACK 去重 pending/succeeded/settled 生命周期", True)

    # —— 过期记录不永久残留（问题 1 回归）：succeed 按新时间重新入队 ——
    clk = FakeClock(start=0.0)
    with patch("core.routing.time", clk):
        b = AckTracker()
        b.try_reserve("I-A")                 # t=0，队列 deadline=600
        clk.now = 2
        b.succeed("I-A")                     # 记录与队列都按 t=2 重入队 deadline=602
        assert b.prune_expired(limit=10) == 0
        clk.now = 601
        assert b.prune_expired(limit=10) == 0   # 601-2 <= 600，不删
        assert b.state_of("I-A") == "succeeded"
        clk.now = 602.5
        assert b.prune_expired(limit=10) == 1   # 新到期时刻已过
        assert b.state_of("I-A") is None
        t("succeed 后按新到期时间清理，不永久残留", True)

        # release 后重新 reserve：旧队列项作废，不误删新记录
        c = AckTracker()
        c.try_reserve("I-B")                 # 队列项 v1，deadline=600
        clk.now = 1
        c.release("I-B")
        c.try_reserve("I-B")                 # 记录/队列项 v2，deadline=601
        clk.now = 600.5
        assert c.prune_expired(limit=10) == 0   # v1 项到点但版本作废；v2 未到
        assert c.state_of("I-B") == "pending"
        clk.now = 601.5
        assert c.prune_expired(limit=10) == 1
        assert c.state_of("I-B") is None
        t("release 后重新 reserve 的版本一致性", True)

        # 恰好到期边界（独立时钟：t0 reserve，deadline=t0+600）
        d = AckTracker()
        dclk = FakeClock(start=0.0)
        with patch("core.routing.time", dclk):
            d.try_reserve("I-C")                 # deadline=600
            dclk.now = 600
            assert d.prune_expired(limit=10) == 0   # 恰好到点未超时
            assert d.state_of("I-C") == "pending"
            dclk.now = 600.5
            assert d.prune_expired(limit=10) == 1
        t("恰好到期边界", True)

        # limit 约束检查量（含失效项），非删除量；旧队列项不阻塞
        e = AckTracker()
        for i in range(10):
            e.try_reserve(f"K{i}")
            e.succeed(f"K{i}")               # 每键 2 个队列项，前者失效
        clk.now += 10_000
        rounds = 0
        while e._records and rounds < 50:
            e.prune_expired(limit=3)
            rounds += 1
        assert not e._records and rounds <= 8    # 20 项 / limit 3 ≈ 7 轮内清完
        t("limit 约束检查量且旧队列项不阻塞清理", True)


def test_real_botpy_http_contract():
    """用真实 botpy BotHttp 核验 token None 判定与动态取 http。"""
    try:
        from botpy.http import BotHttp
    except Exception as exc:  # pragma: no cover
        print(f"  SKIP botpy 不可用: {exc}")
        return
    m = FakeManager()
    ad = FakeAdapter("sales", "APP1", "ws", False)
    m._inst_map["sales"] = {"inst": ad, "client_id": ad.client_self_id}
    rc = core_for(m)
    client = types.SimpleNamespace(api=types.SimpleNamespace(_http=BotHttp(timeout=30)))
    ad.client = client
    view = BoundSource.for_instance("sales", RobotKey("APP1"))
    try:
        rc.resolve(view)
        raise AssertionError("真实 BotHttp 未登录时 _token 应为 None")
    except qe.TransportNotReady:
        pass
    # 模拟 login() 后 token 就绪（不触网：直接设置 Token 对象）
    token = types.SimpleNamespace()  # 只要求非 None
    client.api._http._token = token
    route, http = rc.resolve(view)
    assert http is client.api._http and http._token is token
    t("真实 botpy BotHttp：token None 拒绝、就绪后放行", True)
    # 事件来源辅助
    src = rc.source_of(route)
    bound = BoundSource.from_event_source(src)
    assert bound.source_generation == route.generation and bound.source_client is None
    op = rc.operation(route)
    assert rc.check_context(op) is http
    ctx = OperationContext("sales", RobotKey("APP1"), route.generation)
    assert ctx == op
    t("EventSource/OperationContext 与路由一致", True)


def test_route_drop_keeps_state():
    m, adapters = make_manager([("a", "A1", "ws", False)])
    rc = core_for(m)
    route = rc.ensure_current_route("a")
    state = rc.state_for(route.robot_key)
    assert state.windows.reserve("group", "G1", "M1")[0]
    removed = rc.drop_route("a")
    assert removed is route and "a" not in rc.routes
    assert rc.states.get(route.robot_key) is state
    t("drop_route 不清除机器人状态", True)


def test_changes_consumed_by_coordinator():
    """问题 3 回归：请求侧先行变化必须由协调任务可靠消费。

    复现审查场景：先 refresh_all 建立协调基线，本体替换 A 后请求先于巡检
    更新路由，下一次 refresh_all 必须给出 replaced(old, new) 供补丁恢复。
    """
    m, adapters = make_manager([("a", "A1", "ws", False), ("b", "A2", "ws", False)])
    rc = core_for(m)
    base = rc.refresh_all()
    assert len(base.added) == 2
    old_a = rc.route_of("a")

    # 请求先于巡检看到替换
    ad_b = FakeAdapter("a", "A1", "ws", False)
    m._inst_map["a"] = {"inst": ad_b, "client_id": ad_b.client_self_id}
    rc.ensure_current_route("a")
    new_a = rc.route_of("a")
    d1 = rc.refresh_all()
    assert len(d1.replaced) == 1 and d1.replaced[0] == (old_a, new_a)
    assert not d1.added and not d1.removed and len(d1.unchanged) == 1  # b 未变
    t("请求侧先见的替换由 refresh_all 折叠报告（含旧记录，无重复工作）", True)

    # 无变更多次巡检不重复报告
    for _ in range(3):
        d = rc.refresh_all()
        assert not (d.added or d.removed or d.replaced) and len(d.unchanged) == 2
    t("无变更多次巡检零重复报告", True)

    # 连续 A→B→C 折叠为一次 replaced(first_old, last_new)
    old_for_fold = rc.route_of("a")
    ad_c = FakeAdapter("a", "A1", "ws", False)
    m._inst_map["a"] = {"inst": ad_c, "client_id": ad_c.client_self_id}
    rc.ensure_current_route("a")
    ad_d = FakeAdapter("a", "A1", "ws", False)
    m._inst_map["a"] = {"inst": ad_d, "client_id": ad_d.client_self_id}
    rc.ensure_current_route("a")
    d2 = rc.refresh_all()
    assert len(d2.replaced) == 1
    assert d2.replaced[0][0] is old_for_fold and d2.replaced[0][1] is rc.route_of("a")
    t("连续 A→B→C 折叠为 replaced(first, last)", True)

    # 请求侧先见的删除
    m._inst_map.pop("a")
    try:
        rc.ensure_current_route("a")
        raise AssertionError("删除后应拒绝")
    except qe.InstanceUnavailable:
        pass
    d3 = rc.refresh_all()
    assert [r.platform_id for r in d3.removed] == ["a"]
    t("请求侧先见的删除由 refresh_all 报告", True)

    # 一次性消费：consume_changes 取走后不再重复
    ad_e = FakeAdapter("b", "A2", "ws", False)
    m._inst_map["b"] = {"inst": ad_e, "client_id": ad_e.client_self_id}
    rc.ensure_current_route("b")
    consumed = rc.consume_changes()
    assert len(consumed) == 1 and consumed[0].kind == "replaced"
    assert rc.consume_changes() == []
    d4 = rc.refresh_all()  # 已消费，扫描不再重复
    assert not (d4.added or d4.removed or d4.replaced)
    t("consume_changes 一次性消费且不重复", True)

    # added 后立即 removed（协调从未见过）：折叠后事件消失
    m._inst_map["c"] = {"inst": FakeAdapter("c", "A3", "ws", False), "client_id": "c1"}
    rc.ensure_current_route("c")
    m._inst_map.pop("c")
    try:
        rc.ensure_current_route("c")
        raise AssertionError("c 删除后应拒绝")
    except qe.InstanceUnavailable:
        pass
    assert rc.consume_changes() == []
    d5 = rc.refresh_all()
    assert not (d5.added or d5.removed or d5.replaced)
    t("added→removed 折叠后事件消失", True)


def test_robot_identity_semantics():
    """问题 4 回归：身份取 inst.appid；环境按传输形态；关闭状态语义。"""
    # WS 本体不传 is_sandbox：config 任意值都不改 production 身份
    m = FakeManager()
    ad_ws = FakeAdapter("w1", "A1", "ws", sandbox=True)  # config 置 True 干扰项
    m._inst_map["w1"] = {"inst": ad_ws, "client_id": "1"}
    rc = core_for(m)
    route = rc.ensure_current_route("w1")
    assert route.robot_key.environment == "production"
    t("WS 模式忽略 config['is_sandbox']（本体 botClient 不传）", True)

    # Webhook：helper 读取 config['is_sandbox']，沙箱标记成立
    m._inst_map.clear()
    ad_wh = FakeAdapter("w2", "A1", "webhook", sandbox=True)
    m._inst_map["w2"] = {"inst": ad_wh, "client_id": "2"}
    rc2 = core_for(m)
    route2 = rc2.ensure_current_route("w2")
    assert route2.robot_key.environment == "sandbox"
    t("Webhook 沙箱配置生效", True)

    # 身份取 inst.appid 而非 config['appid']：面板改 config 未重建客户端时
    m._inst_map.clear()
    ad_cfg = FakeAdapter("w3", "A-REAL", "ws", config_appid="A-EDITED")
    m._inst_map["w3"] = {"inst": ad_cfg, "client_id": "3"}
    rc3 = core_for(m)
    route3 = rc3.ensure_current_route("w3")
    assert route3.robot_key.appid == "A-REAL"
    t("身份取 inst.appid 而非可能已修改的 config 字段", True)

    # config 改 appid 不影响已建路由的运行身份，也不误报路由错误
    view = BoundSource.for_instance("w3", route3.robot_key)
    ad_cfg.config["appid"] = "A-EDITED2"
    r, http = rc3.resolve(view)
    assert r.robot_key.appid == "A-REAL" and r.generation == route3.generation
    t("config 字段变化不影响运行身份与代次", True)

    # client_is_closing：识别 WS 本体 is_shutting_down（先于 is_closed）
    class ShuttingClient(FakeClient):
        def __init__(self):
            super().__init__("A")
            self._shutting_down = False

        @property
        def is_shutting_down(self):
            return self._shutting_down or self.is_closed()

    from core.routing import client_is_closing
    c = ShuttingClient()
    assert not client_is_closing(c)
    c._shutting_down = True
    assert client_is_closing(c) and not c.is_closed()
    t("is_shutting_down 先于 is_closed 被识别", True)

    class CallableClosing(FakeClient):   # callable 形态仅防御兼容
        def __init__(self):
            super().__init__("A")

        def is_shutting_down(self):
            return True
    assert client_is_closing(CallableClosing())
    assert not client_is_closing(FakeClient("A"))  # 无该属性的 client 行为不变
    t("无 is_shutting_down 属性的 client 行为不变", True)


async def _main():
    test_scale()
    test_insert_order_and_mixed()
    test_state_sharing()
    test_reload_and_stale()
    test_disable_and_transport()
    test_refresh_diff()
    test_coordinator_fold_cases()
    test_passive_windows()
    test_ack_tracker()
    test_real_botpy_http_contract()
    test_route_drop_keeps_state()
    test_changes_consumed_by_coordinator()
    test_robot_identity_semantics()

if __name__ == "__main__":
    asyncio.run(_main())
    print(f"\nALL {ok} CHECKS PASSED")
