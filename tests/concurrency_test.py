"""并发发送、ACK、事件积压与卸载的行为回归。"""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "assembly_fixture", ROOT / "tests/main_assembly_test.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
M, C, R = fixture.M, fixture.C, fixture.R
E = __import__("qqoffice_main_test.core.events", fromlist=["EventBus"])


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_survives_gc_before_task_start(self):
        async with fixture.service() as (svc, _, inst):
            key = svc.routes.route_of("A").robot_key
            old = svc.states.get(key)
            event = {"id": "E", "d": {"id": "I", "type": 11, "user_openid": "U"}}
            await inst.client.on_interaction_create(event)
            svc._prune_idle_states()
            await inst.client.on_interaction_create(event)
            await asyncio.gather(*list(svc.event_bus._tasks))
            self.assertIs(svc.states.get(key), old)
            self.assertEqual(len(inst.client.http.calls), 1)
            self.assertEqual(old.acks.state_of("I"), "succeeded")
            self.assertFalse(old.in_use)

    async def test_auto_ack_wins_without_second_manual_request(self):
        async with fixture.service() as (svc, _, inst):
            failures = []

            async def manual(ev):
                try:
                    await svc.for_event(ev).manage.interaction_ack(
                        ev.interaction_id, code=1
                    )
                except Exception as exc:
                    failures.append(type(exc).__name__)

            svc.on("INTERACTION_CREATE", manual)
            await inst.client.on_interaction_create({"id": "I", "type": 11})
            await asyncio.gather(*list(svc.event_bus._tasks))
            self.assertEqual(
                [c[2]["json"]["code"] for c in inst.client.http.calls], [0]
            )
            self.assertEqual(failures, ["QQOfficeDuplicateAck"])

    async def test_manual_ack_concurrent_and_terminal(self):
        async with fixture.service(interaction_auto_ack=False) as (svc, _, inst):
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold(*args):
                entered.set()
                await release.wait()

            inst.client.http.on_request = hold
            view = svc.instance("A")
            first = asyncio.create_task(view.manage.interaction_ack("I", code=1))
            await entered.wait()
            with self.assertRaises(C.qo_errors.QQOfficeDuplicateAck):
                await view.manage.interaction_ack("I")
            release.set()
            await first
            with self.assertRaises(C.qo_errors.QQOfficeDuplicateAck):
                await view.call("PUT", "/interactions/I", json={"code": 0})
            self.assertEqual(len(inst.client.http.calls), 1)

    async def test_manual_ack_rejection_releases_unknown_keeps(self):
        for exception, expected in (
            (C.QQOfficeAPIError(400, "rejected"), None),
            (RuntimeError("unknown result"), "settled"),
        ):
            async with fixture.service() as (svc, _, inst):

                def fail(*args):
                    raise exception

                inst.client.http.on_request = fail
                with self.assertRaises(type(exception)):
                    await svc.instance("A").manage.interaction_ack("I")
                state = svc.states.get(svc.routes.route_of("A").robot_key)
                self.assertEqual(state.acks.state_of("I"), expected)
                inst.client.http.on_request = None
                if expected is None:
                    await svc.instance("A").manage.interaction_ack("I")
                    self.assertEqual(state.acks.state_of("I"), "succeeded")
                else:
                    with self.assertRaises(C.qo_errors.QQOfficeDuplicateAck):
                        await svc.instance("A").manage.interaction_ack("I")

    async def test_cancel_auto_ack_before_start_returns_ownership(self):
        async with fixture.service() as (svc, _, inst):
            await inst.client.on_interaction_create({"id": "I", "type": 11})
            state = svc.states.get(svc.routes.route_of("A").robot_key)
            await svc.event_bus.stop()
            self.assertFalse(state.in_use)
            self.assertIsNone(state.acks.state_of("I"))
            self.assertEqual(inst.client.http.calls, [])

    async def test_sequences_shared_monotonic_and_not_reused(self):
        async with fixture.service() as (svc, pm, inst):
            second = fixture.adapter(pid="B")
            pm._inst_map["B"] = {"inst": second}
            svc.refresh()
            views = [svc.instance("A"), svc.instance("B")]
            with patch.object(C.time, "time", return_value=1800000000.0):
                await asyncio.gather(
                    *(
                        views[i % 2].group.send("G", str(i), msg_id="M")
                        for i in range(5)
                    )
                )
            seqs = [
                c[2]["json"]["msg_seq"]
                for ad in (inst, second)
                for c in ad.client.http.calls
            ]
            self.assertEqual(sorted(seqs), [1, 2, 3, 4, 5])
        async with fixture.service() as (svc, _, inst):

            def reject(*args):
                raise C.QQOfficeAPIError(400, "rejected")

            inst.client.http.on_request = reject
            with self.assertRaises(C.QQOfficeAPIError):
                await svc.instance("A").group.send("G", "first", msg_id="M", msg_seq=9)
            inst.client.http.on_request = None
            await svc.instance("A").group.send("G", "second", msg_id="M")
            self.assertEqual(
                [c[2]["json"]["msg_seq"] for c in inst.client.http.calls], [9, 10]
            )

    async def test_initialize_failure_releases_installed_token_guard(self):
        from botpy.robot import Token

        baseline = Token.check_token
        svc = M.Main(fixture.NS(platform_manager=fixture.NS(_inst_map={})), {})
        with patch.object(
            M.AdapterPatcher, "refresh", side_effect=RuntimeError("initialize failed")
        ):
            with self.assertRaises(RuntimeError):
                await svc.initialize()
        self.assertIs(Token.check_token, baseline)
        self.assertTrue(svc.event_bus._stopping)
        self.assertTrue(svc.patcher._stopping)
        self.assertEqual(svc._service.state, "unavailable")
        await svc.terminate()

    async def test_invalid_sequence_does_not_consume_window(self):
        async with fixture.service() as (svc, _, inst):
            view = svc.instance("A")
            with self.assertRaises(C.QQOfficeNotSupported):
                await view.group.send("G", "reply", msg_id="M", msg_seq="1")
            state = svc.states.get(view.robot_key)
            self.assertEqual(state.windows.stats()["tracked_msg_ids"], 0)
            self.assertFalse(state.in_use)
            self.assertEqual(inst.client.http.calls, [])

    async def test_manual_ack_cancel_after_dispatch_keeps_dedup(self):
        async with fixture.service() as (svc, _, inst):
            entered = asyncio.Event()

            async def hold(*args):
                entered.set()
                await asyncio.Event().wait()

            inst.client.http.on_request = hold
            task = asyncio.create_task(svc.instance("A").manage.interaction_ack("I"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            state = svc.states.get(svc.routes.route_of("A").robot_key)
            self.assertEqual(state.acks.state_of("I"), "settled")
            self.assertFalse(state.in_use)
            with self.assertRaises(C.qo_errors.QQOfficeDuplicateAck):
                await svc.instance("A").manage.interaction_ack("I")
            self.assertEqual(len(inst.client.http.calls), 1)

    async def test_token_expiry_and_late_401_use_one_refresh(self):
        from botpy.robot import Token

        helper_spec = importlib.util.spec_from_file_location(
            "token_fixture", ROOT / "tests/token_guard_test.py"
        )
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        transport = helper.Transport()
        transport.same_value = True
        transport.gate.set()
        expired = asyncio.Event()
        late_rejection = asyncio.Event()
        refreshed_retry = asyncio.Event()
        async with fixture.service() as (svc, _, inst):
            token = Token("mock-app", "mock-secret")
            inst.client.http._token = token
            attempts = {}

            async def request(route, **kwargs):
                await token.check_token()
                target = kwargs["json"]["content"]
                attempts[target] = attempts.get(target, 0) + 1
                if target == "expiry" and attempts[target] == 1:
                    expired.set()
                    await late_rejection.wait()
                    raise C.QQOfficeAPIError(401, "rejected refreshed token")
                if target == "force" and attempts[target] == 1:
                    await expired.wait()
                    raise C.QQOfficeAPIError(401, "rejected same generation")
                if target == "force":
                    refreshed_retry.set()
                return {"id": "ok"}

            inst.client.http.request = request
            with patch("botpy.robot.aiohttp.ClientSession", transport.session):
                view = svc.instance("A")
                first = asyncio.create_task(view.group.send("G", "expiry", msg_id="M"))
                await expired.wait()
                second = asyncio.create_task(view.group.send("G", "force", msg_id="M"))
                await refreshed_retry.wait()
                late_rejection.set()
                await asyncio.gather(first, second)
            self.assertEqual(transport.calls, 2)
            self.assertEqual(transport.closed, 2)
            self.assertEqual(attempts, {"expiry": 2, "force": 2})

    async def test_token_refresh_cancel_or_reload_does_not_dispatch(self):
        from botpy.robot import Token

        helper_spec = importlib.util.spec_from_file_location(
            "token_fixture", ROOT / "tests/token_guard_test.py"
        )
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        for mode in ("cancel", "reload"):
            transport = helper.Transport()
            async with fixture.service() as (svc, pm, inst):
                inst.client.http._token = Token("mock-app", "mock-secret")
                with patch("botpy.robot.aiohttp.ClientSession", transport.session):
                    task = asyncio.create_task(
                        svc.instance("A").group.send("G", "reply", msg_id="M")
                    )
                    await transport.started.wait()
                    if mode == "cancel":
                        task.cancel()
                    else:
                        pm._inst_map["A"] = {"inst": fixture.adapter()}
                    transport.gate.set()
                    with self.assertRaises(
                        asyncio.CancelledError
                        if mode == "cancel"
                        else M.QQOfficeRoutingError
                    ):
                        await task
                self.assertEqual(inst.client.http.calls, [])
                self.assertEqual(transport.active, 0)
                state = svc.states.get(svc.routes.route_of("A").robot_key)
                self.assertEqual(state.windows._records[("group", "G", "M")][1], 0)

    async def test_ack_limit_keeps_ownership_and_counts_overflow(self):
        async with fixture.service(interaction_ack_concurrency=1) as (svc, _, inst):
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold(*args):
                entered.set()
                await release.wait()

            inst.client.http.on_request = hold
            await inst.client.on_interaction_create({"id": "I1", "type": 11})
            await entered.wait()
            await inst.client.on_interaction_create({"id": "I2", "type": 11})
            self.assertEqual(svc.event_bus.status()["pending_acks"], 1)
            self.assertEqual(svc.event_bus.status()["dropped_acks"], 1)
            release.set()
            await asyncio.gather(*list(svc.event_bus._tasks))
            await inst.client.on_interaction_create({"id": "I2", "type": 11})
            await asyncio.gather(*list(svc.event_bus._tasks))
            self.assertEqual(len(inst.client.http.calls), 2)
            self.assertEqual(svc.event_bus.status()["pending_acks"], 0)

    async def test_ack_before_dispatch_cancel_releases(self):
        async with fixture.service() as (svc, _, inst):
            entered = asyncio.Event()
            state = svc.states.get(svc.routes.route_of("A").robot_key)

            async def hold(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            state.rate.acquire = hold
            task = asyncio.create_task(svc.instance("A").manage.interaction_ack("I"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNone(state.acks.state_of("I"))
            self.assertFalse(state.in_use)
            self.assertEqual(inst.client.http.calls, [])

    async def test_sequence_retry_keeps_same_value(self):
        async with fixture.service(retry_max=1) as (svc, _, inst):

            def reject_once(*args):
                if len(inst.client.http.calls) == 1:
                    raise C.QQOfficeAPIError(429, "retry")

            inst.client.http.on_request = reject_once
            with patch.object(C.asyncio, "sleep", return_value=None):
                await svc.instance("A").group.send("G", "reply", msg_id="M")
            self.assertEqual(
                [call[2]["json"]["msg_seq"] for call in inst.client.http.calls], [1, 1]
            )

    async def test_cancelled_handler_does_not_strand_queued_jobs(self):
        bus = E.EventBus({"event_handler_concurrency": 1, "event_queue_size": 3})
        started, completed = asyncio.Event(), asyncio.Event()
        received = []

        async def handler(ev):
            if ev.raw["id"] == "first":
                started.set()
                await asyncio.Event().wait()
            received.append(ev.raw["id"])
            completed.set()

        bus.on("GROUP_MEMBER_ADD", handler)
        await bus.emit(E.normalize_event("group_member_add", {"id": "first"}))
        await started.wait()
        await bus.emit(E.normalize_event("group_member_add", {"id": "queued"}))
        worker = next(iter(bus._workers))
        worker.cancel()
        await asyncio.wait_for(completed.wait(), timeout=1)
        self.assertEqual(received, ["queued"])
        await bus.stop()

    async def test_concurrent_terminate_from_handlers_and_external(self):
        async with fixture.service() as (svc, _, inst):
            finished = asyncio.Event()
            entered, release = asyncio.Event(), asyncio.Event()
            original_stop = svc.event_bus.stop

            async def delayed_stop():
                entered.set()
                await release.wait()
                await original_stop()

            async def close(ev):
                await svc.terminate()
                finished.set()

            svc.event_bus.stop = delayed_stop
            svc.on("GROUP_MEMBER_ADD", close)
            await inst.client.on_group_member_add({"id": "E", "group_openid": "G"})
            await entered.wait()
            external = asyncio.create_task(svc.terminate())
            second = asyncio.create_task(svc.terminate())
            release.set()
            await asyncio.wait_for(
                asyncio.gather(external, second, finished.wait()), timeout=1
            )
            self.assertEqual(svc._service.state, "closed")
            self.assertTrue(svc.patcher._stopping)

    async def test_event_backlog_bounded_and_counted(self):
        bus = E.EventBus({"event_handler_concurrency": 2, "event_queue_size": 3})
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        peak = 0
        completed = []

        async def slow(ev):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                completed.append(ev.raw["id"])
            finally:
                active -= 1

        bus.on("GROUP_MEMBER_ADD", slow)
        for i in range(2):
            await bus.emit(E.normalize_event("group_member_add", {"id": i}))
        await entered.wait()
        for i in range(2, 12):
            await bus.emit(E.normalize_event("group_member_add", {"id": i}))
        status = bus.status()
        self.assertLessEqual(len(bus._tasks), 2)
        self.assertEqual(status["queued_handlers"], 3)
        self.assertEqual(status["dropped_handlers"], 7)
        release.set()
        await asyncio.gather(*list(bus._tasks))
        self.assertEqual(peak, 2)
        self.assertEqual(sorted(completed), [0, 1, 2, 3, 4])
        await bus.stop()

    async def test_handler_timeout_frees_worker_and_ack_is_independent(self):
        async with fixture.service(
            event_handler_concurrency=1, event_queue_size=2, event_handler_timeout=0.02
        ) as (svc, _, inst):
            started = asyncio.Event()

            async def slow(ev):
                started.set()
                await asyncio.Event().wait()

            svc.on("GROUP_MEMBER_ADD", slow)
            await inst.client.on_group_member_add({"id": "E", "group_openid": "G"})
            await started.wait()
            await inst.client.on_interaction_create({"id": "I", "type": 11})
            await asyncio.gather(*list(svc.event_bus._tasks))
            self.assertEqual(len(inst.client.http.calls), 1)
            self.assertEqual(svc.event_bus.status()["handler_timeouts"], 1)

    async def test_terminate_inside_handler_finishes_cleanup(self):
        async with fixture.service() as (svc, _, inst):
            finished = asyncio.Event()

            async def close(ev):
                await svc.terminate()
                finished.set()

            svc.on("GROUP_MEMBER_ADD", close)
            await inst.client.on_group_member_add({"id": "E", "group_openid": "G"})
            await asyncio.wait_for(finished.wait(), timeout=1)
            self.assertEqual(svc._service.state, "closed")
            self.assertTrue(svc.patcher._stopping)
            self.assertEqual(svc.event_bus.status()["queued_handlers"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
