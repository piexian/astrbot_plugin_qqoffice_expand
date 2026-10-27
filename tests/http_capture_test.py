"""HTTP 观察器跨重载、并发和补丁所有权回归。"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def generation(name):
    spec = importlib.util.spec_from_file_location(
        f"core.{name}", ROOT / "core/http_capture.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status, body="", gate=None):
        self.status = status
        self.body = body
        self.headers = {}
        self.gate = gate
        self.reads = 0

    async def text(self):
        self.reads += 1
        return self.body


SDKError = type("SDKError", (Exception,), {"__module__": "botpy.errors"})


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old = generation("capture_old")
        self.new = generation("capture_new")

        async def original(response):
            if response.gate is not None:
                await response.gate.wait()
            if response.status >= 400:
                raise SDKError("SDK error")
            return None

        self.original = original
        self.http = types.SimpleNamespace(_handle_response=original)

    async def test_generations_both_exit_orders(self):
        for old_first in (True, False):
            with self.subTest(old_first=old_first):
                entered = [asyncio.Event(), asyncio.Event()]
                release = [asyncio.Event(), asyncio.Event()]
                captures = []

                async def request(index, module):
                    with module.capture_response_status(self.http) as captured:
                        entered[index].set()
                        await release[index].wait()
                        await self.http._handle_response(Response(201 + index))
                        captures.append((index, captured))

                tasks = [
                    asyncio.create_task(request(0, self.old)),
                    asyncio.create_task(request(1, self.new)),
                ]
                await asyncio.gather(*(event.wait() for event in entered))
                first = 0 if old_first else 1
                release[first].set()
                await tasks[first]
                self.assertIsNot(self.http._handle_response, self.original)
                release[1 - first].set()
                await tasks[1 - first]
                self.assertEqual(
                    dict(captures), {0: {"status": 201}, 1: {"status": 202}}
                )
                self.assertIs(self.http._handle_response, self.original)
                state = self.http._qqoffice_response_capture
                self.assertEqual(state["users"], 0)
                self.assertIsNone(state["installed"])
                self.assertIsNone(state["previous"])
                self.assertIsNone(state["current"].get())

    async def test_independent_status_error_and_uncaptured_requests(self):
        barrier = asyncio.Event()
        entered = [asyncio.Event() for _ in range(3)]
        error_response = Response(401, '{"code":11244,"message":"expired"}', barrier)

        async def request(index, status):
            with (self.old if index == 0 else self.new).capture_response_status(
                self.http
            ) as captured:
                entered[index].set()
                response = (
                    error_response if status == 401 else Response(status, gate=barrier)
                )
                try:
                    await self.http._handle_response(response)
                except SDKError:
                    pass
                return captured

        async def unrelated():
            entered[2].set()
            await barrier.wait()
            await self.http._handle_response(Response(204))

        tasks = [
            asyncio.create_task(request(0, 200)),
            asyncio.create_task(request(1, 401)),
        ]
        other = asyncio.create_task(unrelated())
        await asyncio.gather(*(event.wait() for event in entered))
        barrier.set()
        good, bad = await asyncio.gather(*tasks)
        await other
        self.assertEqual(good, {"status": 200})
        self.assertEqual(bad["error"].code, 11244)
        self.assertEqual(bad["status"], 401)
        self.assertEqual(error_response.reads, 1)
        self.assertIs(self.http._handle_response, self.original)

    async def test_third_party_wrapper_survives_and_reentry(self):
        calls = []
        with self.old.capture_response_status(self.http):
            inner = self.http._handle_response

            async def third_party(response):
                calls.append(response.status)
                return await inner(response)

            self.http._handle_response = third_party
        self.assertIs(self.http._handle_response, third_party)
        for module in (self.new, self.old):
            with module.capture_response_status(self.http) as captured:
                await self.http._handle_response(Response(202))
            self.assertEqual(captured, {"status": 202})
            self.assertIs(self.http._handle_response, third_party)
        self.assertEqual(calls, [202, 202])

    async def test_reloaded_dispatch_uses_current_error_class(self):
        from core import client
        from core import errors

        baseline = client.capture_response_status
        entered, release = asyncio.Event(), asyncio.Event()

        async def old_request():
            with self.old.capture_response_status(self.http):
                entered.set()
                await release.wait()

        class HTTP:
            async def request(inner, route, **kwargs):
                await self.http._handle_response(
                    Response(401, '{"code":11244,"message":"expired"}')
                )

        old = asyncio.create_task(old_request())
        await entered.wait()
        old_error = self.old.from_http_response
        foreign = importlib.util.spec_from_file_location(
            "foreign_errors", ROOT / "core/errors.py"
        )
        foreign_module = importlib.util.module_from_spec(foreign)
        foreign.loader.exec_module(foreign_module)
        self.old.from_http_response = foreign_module.from_http_response
        try:
            with patch.object(
                client,
                "capture_response_status",
                lambda _: self.new.capture_response_status(self.http),
            ):
                with self.assertRaises(errors.QQOfficeAPIError) as raised:
                    await client._dispatch(HTTP(), "GET", "/users/@me", None, None)
                self.assertEqual(raised.exception.code, 11244)
        finally:
            self.old.from_http_response = old_error
            release.set()
            await old
        self.assertIs(client.capture_response_status, baseline)

    async def test_nested_and_missing_handler(self):
        with self.old.capture_response_status(self.http) as outer:
            with self.new.capture_response_status(self.http) as inner:
                await self.http._handle_response(Response(202))
            await self.http._handle_response(Response(204))
        self.assertEqual(inner, {"status": 202})
        self.assertEqual(outer, {"status": 204})
        with self.new.capture_response_status(object()) as captured:
            self.assertEqual(captured, {})


if __name__ == "__main__":
    unittest.main()
