"""用内存 aiohttp 响应验证 botpy 1.2.1 Token 刷新并发。"""

import asyncio
import gc
import importlib.util
import sys
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from botpy.robot import Token

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def generation(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "core/token_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Transport:
    def __init__(self):
        self.calls = 0
        self.closed = 0
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.failure = False
        self.same_value = False
        self.active = 0
        self.close_started = asyncio.Event()
        self.close_gate = None

    def session(self):
        transport = self

        class Session:
            def post(self, **kwargs):
                return self

            async def __aenter__(self):
                transport.calls += 1
                transport.active += 1
                transport.started.set()
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                await transport.gate.wait()
                if transport.failure:
                    raise RuntimeError("mock failure")
                value = "fixed" if transport.same_value else f"mock-{transport.calls}"
                return {"access_token": value, "expires_in": "3600"}

            async def close(self):
                transport.close_started.set()
                if transport.close_gate is not None:
                    await transport.close_gate.wait()
                await asyncio.sleep(0)
                transport.closed += 1
                transport.active -= 1

        return Session()


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old = generation("token_old")
        self.new = generation("token_new")
        self.original_check = Token.check_token
        self.original_update = Token.update_access_token
        self.transport = Transport()
        self.mock = patch("botpy.robot.aiohttp.ClientSession", self.transport.session)
        self.mock.start()
        self.guards = [self.old.TokenRefreshGuard(), self.new.TokenRefreshGuard()]
        for guard in self.guards:
            guard.install()

    async def asyncTearDown(self):
        self.transport.gate.set()
        for guard in self.guards:
            await guard.stop()
        self.mock.stop()
        self.assertEqual(self.transport.active, 0)
        self.assertIs(Token.check_token, self.original_check)
        self.assertIs(Token.update_access_token, self.original_update)

    def token(self, fresh=False):
        token = Token("mock-app", "mock-secret")
        if fresh:
            token.access_token = "fixed"
            token.expires_in = int(time.time()) + 3600
        return token

    async def pending(self, *coroutines):
        tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
        await self.transport.started.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return tasks

    async def test_concurrent_expiry_and_recheck(self):
        token = self.token()
        tasks = await self.pending(*(token.check_token() for _ in range(10)))
        self.assertEqual(self.transport.calls, 1)
        self.transport.gate.set()
        await asyncio.gather(*tasks)
        await token.check_token()
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.transport.closed, 1)

    async def test_force_and_expiry_share_refresh(self):
        token = self.token()
        snapshot = self.old.token_snapshot(token)
        tasks = await self.pending(
            token.check_token(),
            token.update_access_token(),
            self.new.refresh_access_token(token, snapshot),
        )
        self.assertEqual(self.transport.calls, 1)
        self.transport.gate.set()
        await asyncio.gather(*tasks)
        await token.update_access_token()
        self.assertEqual(self.transport.calls, 2)

    async def test_one_cancelled_waiter_does_not_poison_others(self):
        token = self.token()
        tasks = await self.pending(*(token.update_access_token() for _ in range(3)))
        tasks[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tasks[0]
        self.assertEqual(self.transport.closed, 0)
        self.transport.gate.set()
        await asyncio.gather(*tasks[1:])
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.transport.closed, 1)

    async def test_all_cancelled_waiters_close_owned_task(self):
        token = self.token()
        tasks = await self.pending(*(token.update_access_token() for _ in range(3)))
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(
            all(isinstance(result, asyncio.CancelledError) for result in results)
        )
        self.assertEqual(self.transport.closed, 1)
        entry = Token._qqoffice_token_guard["tokens"][token]
        self.assertIsNone(entry["task"])
        self.assertEqual(entry["waiters"], 0)
        self.transport.gate.set()
        await token.update_access_token()
        self.assertEqual(self.transport.calls, 2)

    async def test_new_waiter_during_cancel_cleanup_starts_fresh_refresh(self):
        token = self.token()
        self.transport.close_gate = asyncio.Event()
        tasks = await self.pending(token.check_token())
        tasks[0].cancel()
        await self.transport.close_started.wait()
        newcomer = asyncio.create_task(token.check_token())
        await asyncio.sleep(0)
        self.transport.close_gate.set()
        self.transport.gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await tasks[0]
        await newcomer
        self.assertEqual(self.transport.calls, 2)
        self.assertEqual(self.transport.closed, 2)

    async def test_failure_is_shared_and_recovery_is_possible(self):
        token = self.token()
        self.transport.failure = True
        tasks = await self.pending(*(token.update_access_token() for _ in range(4)))
        self.transport.gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        self.assertEqual(self.transport.calls, 1)
        self.transport.failure = False
        await token.check_token()
        self.assertEqual(self.transport.calls, 2)

    async def test_late_401_same_token_value_changed_revision(self):
        token = self.token(fresh=True)
        self.transport.same_value = True
        before = self.old.token_snapshot(token)
        self.assertEqual(repr(before), "<TokenSnapshot>")
        self.transport.gate.set()
        self.assertTrue(await self.new.refresh_access_token(token, before))
        self.assertTrue(await self.old.refresh_access_token(token, before))
        self.assertEqual(self.transport.calls, 1)
        after = self.new.token_snapshot(token)
        self.assertTrue(await self.old.refresh_access_token(token, after))
        self.assertEqual(self.transport.calls, 2)

    async def test_external_value_change_and_wrong_token_snapshot(self):
        token = self.token(fresh=True)
        before = self.old.token_snapshot(token)
        token.access_token = "changed"
        await self.new.refresh_access_token(token, before)
        await self.new.refresh_access_token(self.token(), before)
        self.assertEqual(self.transport.calls, 0)

    async def test_separate_tokens_do_not_serialize(self):
        first, second = self.token(), self.token()
        tasks = await self.pending(first.check_token(), second.check_token())
        self.assertEqual(self.transport.calls, 2)
        self.assertEqual(self.transport.active, 2)
        self.transport.gate.set()
        await asyncio.gather(*tasks)

    async def test_owners_stop_in_both_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                for guard in self.guards:
                    guard.install()
                    guard.install()
                token = self.token()
                tasks = await self.pending(token.check_token())
                first, last = self.guards[::-1] if reverse else self.guards
                await first.stop()
                self.assertIsNot(Token.check_token, self.original_check)
                stopping = asyncio.create_task(last.stop())
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                self.assertIs(Token.check_token, self.original_check)
                self.assertEqual(self.transport.closed, self.transport.calls - 1)
                self.transport.gate.set()
                await asyncio.gather(*tasks, stopping)
                self.transport.gate.clear()
                self.transport.started.clear()

    async def test_simultaneous_stop_and_third_party_top_wrapper(self):
        captured_check, captured_update = Token.check_token, Token.update_access_token
        calls = []

        async def third_check(token):
            calls.append("check")
            return await captured_check(token)

        async def third_update(token):
            calls.append("update")
            return await captured_update(token)

        Token.check_token, Token.update_access_token = third_check, third_update
        try:
            await asyncio.gather(*(guard.stop() for guard in self.guards))
            self.assertIs(Token.check_token, third_check)
            self.assertIs(Token.update_access_token, third_update)
            for guard in self.guards:
                guard.install()
            token = self.token()
            tasks = await self.pending(token.check_token(), token.update_access_token())
            self.transport.gate.set()
            await asyncio.gather(*tasks)
            self.assertEqual(self.transport.calls, 1)
            await asyncio.gather(*(guard.stop() for guard in self.guards))
            self.assertIs(Token.check_token, third_check)
            self.assertIs(Token.update_access_token, third_update)
            await self.token().check_token()
            self.assertEqual(self.transport.calls, 2)
            self.assertIn("check", calls)
            self.assertIn("update", calls)
        finally:
            Token.check_token = self.original_check
            Token.update_access_token = self.original_update

    async def test_weak_state_and_stub_fallback(self):
        token = self.token()
        ref = weakref.ref(token)
        before = self.old.token_snapshot(token)
        self.transport.gate.set()
        await self.new.refresh_access_token(token, before)
        del token
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(len(Token._qqoffice_token_guard["tokens"]), 0)
        self.assertIsNone(self.old.token_snapshot(object()))
        self.assertFalse(await self.new.refresh_access_token(object()))
        self.assertEqual(len(Token._qqoffice_token_guard["owners"]), 2)

    async def test_cancelled_stop_does_not_cancel_sdk_refresh(self):
        token = self.token()
        tasks = await self.pending(token.check_token())
        await self.guards[0].stop()
        stopping = asyncio.create_task(self.guards[1].stop())
        await asyncio.sleep(0)
        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertEqual(self.transport.closed, 0)
        self.transport.gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.transport.closed, 1)
        self.assertIsNone(Token._qqoffice_token_guard["patch"])

    async def test_repeated_cancel_waits_for_resource_cleanup(self):
        self.transport.close_gate = asyncio.Event()
        tasks = await self.pending(self.token().update_access_token())
        caller = tasks[0]
        caller.cancel()
        await self.transport.close_started.wait()
        caller.cancel()
        await asyncio.sleep(0)
        self.assertFalse(caller.done())
        self.transport.close_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(self.transport.closed, 1)
        self.assertEqual(self.transport.active, 0)

    async def test_previous_check_wrapper_and_staggered_expiry_recheck(self):
        for guard in self.guards:
            await guard.stop()
        entered = asyncio.Event()
        proceed = asyncio.Event()
        calls = []

        async def previous_check(token):
            calls.append("check")
            if token.access_token is None or int(time.time()) >= token.expires_in:
                entered.set()
                await proceed.wait()
                await token.update_access_token()

        Token.check_token = previous_check
        try:
            for guard in self.guards:
                guard.install()
            token = self.token()
            checking = asyncio.create_task(token.check_token())
            await entered.wait()
            self.transport.gate.set()
            await token.update_access_token()
            proceed.set()
            await checking
            self.assertEqual(calls, ["check"])
            self.assertEqual(self.transport.calls, 1)
            self.assertIsNone(Token._qqoffice_token_guard["checking"].get())
            for guard in self.guards:
                await guard.stop()
            self.assertIs(Token.check_token, previous_check)
        finally:
            Token.check_token = self.original_check

    async def test_missing_botpy_install_is_noop(self):
        guard = self.old.TokenRefreshGuard()
        with patch.dict(sys.modules, {"botpy.robot": None}):
            guard.install()
        await guard.stop()
        self.assertIsNone(guard._state)


if __name__ == "__main__":
    unittest.main()
