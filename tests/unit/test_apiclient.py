import unittest

from imapgw.apiclient import (
    AgentMailClient,
    AuthError,
    BadRequestError,
    NotFoundError,
    UnavailableError,
)
from tests.support.scripted_transport import (
    ScriptedTransport,
    bytes_response,
    json_response,
    transport_error,
)

BASE = "http://api.test/v0"
KEY = "secret-key-123"


def make_client(transport: ScriptedTransport, **kw) -> AgentMailClient:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    client = AgentMailClient(
        BASE,
        KEY,
        transport,
        timeout=1.0,
        max_attempts=kw.pop("max_attempts", 3),
        retry_budget=kw.pop("retry_budget", 10.0),
        backoff_base=kw.pop("backoff_base", 0.01),
        sleep=fake_sleep,
        rng=lambda: 0.0,
    )
    client._slept = slept  # type: ignore[attr-defined]
    return client


class RequestShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bearer_header_and_url_encoding(self):
        t = ScriptedTransport([json_response(200, {"inbox_id": "a@b"})])
        client = make_client(t)
        await client.get_inbox("a@b")
        call = t.calls[0]
        self.assertEqual(call.headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(call.url, f"{BASE}/inboxes/a%40b")

    async def test_download_sends_no_headers_and_not_the_key(self):
        t = ScriptedTransport([bytes_response(200, b"raw bytes")])
        client = make_client(t)
        data = await client.download("http://files.test/raw/x?sig=abc")
        self.assertEqual(data, b"raw bytes")
        self.assertEqual(t.calls[0].headers, {})
        self.assertNotIn(KEY, repr(t.calls[0]))

    async def test_update_labels_body(self):
        t = ScriptedTransport([json_response(200, {"message_id": "m", "labels": ["read"]})])
        client = make_client(t)
        await client.update_labels("a@b", "m", add=["read"], remove=["unread"])
        call = t.calls[0]
        self.assertEqual(call.method, "PATCH")
        self.assertEqual(call.headers["content-type"], "application/json")
        self.assertIn(b'"add_labels": ["read"]', call.body)
        self.assertIn(b'"remove_labels": ["unread"]', call.body)

    async def test_list_messages_params(self):
        t = ScriptedTransport([json_response(200, {"count": 0, "messages": []})])
        client = make_client(t)
        items = [m async for m in client.list_messages("a@b", labels=["received"])]
        self.assertEqual(items, [])
        url = t.calls[0].url
        self.assertIn("labels=received", url)
        self.assertIn("ascending=true", url)
        self.assertIn("limit=100", url)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_all_pages(self):
        t = ScriptedTransport(
            [
                json_response(200, {"messages": [{"id": 1}, {"id": 2}], "next_page_token": "t2"}),
                json_response(200, {"messages": [{"id": 3}], "next_page_token": "t3"}),
                json_response(200, {"messages": [{"id": 4}]}),
            ]
        )
        client = make_client(t)
        items = [m["id"] async for m in client.list_messages("a@b")]
        self.assertEqual(items, [1, 2, 3, 4])
        self.assertEqual(len(t.calls), 3)
        self.assertNotIn("page_token", t.calls[0].url)
        self.assertIn("page_token=t2", t.calls[1].url)
        self.assertIn("page_token=t3", t.calls[2].url)

    async def test_failure_mid_pagination_propagates(self):
        t = ScriptedTransport(
            [
                json_response(200, {"drafts": [{"id": 1}], "next_page_token": "t2"}),
                json_response(503, {"error": "down"}),
                json_response(503, {"error": "down"}),
                json_response(503, {"error": "down"}),
            ]
        )
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            _ = [d async for d in client.list_drafts("a@b")]

    async def test_pagination_loop_guard(self):
        t = ScriptedTransport(
            router=lambda call: json_response(200, {"messages": [], "next_page_token": "same"})
        )
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            _ = [m async for m in client.list_messages("a@b")]


class RetryAndErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_401_is_auth_error_without_retry(self):
        t = ScriptedTransport([json_response(401, {"error": {"message": "bad key"}})])
        client = make_client(t)
        with self.assertRaises(AuthError):
            await client.auth_me()
        self.assertEqual(len(t.calls), 1)

    async def test_404_and_400(self):
        t = ScriptedTransport([json_response(404, {}), json_response(400, {})])
        client = make_client(t)
        with self.assertRaises(NotFoundError):
            await client.get_draft("a@b", "nope")
        with self.assertRaises(BadRequestError):
            await client.create_draft("a@b", {"html": "x"})

    async def test_429_retries_and_honours_retry_after(self):
        t = ScriptedTransport(
            [
                json_response(429, {}, headers={"Retry-After": "2"}),
                json_response(200, {"ok": True}),
            ]
        )
        client = make_client(t)
        data = await client.auth_me()
        self.assertEqual(data, {"ok": True})
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(client._slept, [2.0])  # type: ignore[attr-defined]

    async def test_503_exhausts_attempts(self):
        t = ScriptedTransport(router=lambda call: json_response(503, {}))
        client = make_client(t, max_attempts=3)
        with self.assertRaises(UnavailableError) as cm:
            await client.auth_me()
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(cm.exception.status, 503)

    async def test_transport_error_retried_then_raised(self):
        t = ScriptedTransport([transport_error(), transport_error(), json_response(200, {"a": 1})])
        client = make_client(t)
        self.assertEqual(await client.auth_me(), {"a": 1})

    async def test_retry_budget_caps_delay(self):
        t = ScriptedTransport(
            [json_response(429, {}, headers={"Retry-After": "60"}), json_response(200, {})]
        )
        client = make_client(t, retry_budget=5.0)
        with self.assertRaises(UnavailableError):
            await client.auth_me()
        self.assertEqual(len(t.calls), 1)

    async def test_malformed_json_is_unavailable(self):
        t = ScriptedTransport([bytes_response(200, b"<html>", "text/html")])
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            await client.auth_me()

    async def test_raw_meta_parsing(self):
        t = ScriptedTransport(
            [
                json_response(
                    200,
                    {
                        "message_id": "m1",
                        "size": 375,
                        "download_url": "http://files.test/raw/m1",
                        "expires_at": "2026-09-19T00:00:00Z",
                    },
                )
            ]
        )
        client = make_client(t)
        meta = await client.get_raw_meta("a@b", "m1")
        self.assertEqual((meta.size, meta.download_url), (375, "http://files.test/raw/m1"))


if __name__ == "__main__":
    unittest.main()


class TrashQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_trash_view_requests_include_trash(self):
        t = ScriptedTransport([json_response(200, {"count": 0, "messages": []})])
        client = make_client(t)
        _ = [m async for m in client.list_messages("a@b", labels=["trash"], include_trash=True)]
        url = t.calls[0].url
        self.assertIn("labels=trash", url)
        self.assertIn("include_trash=true", url)


class ShapeAndStatusTests(unittest.IsolatedAsyncioTestCase):
    """Review findings F10, F11, F3: malformed successes and unexpected statuses must fail."""

    async def test_missing_items_key_is_an_error_not_an_empty_page(self):
        t = ScriptedTransport([json_response(200, {"error": {"message": "wrong shape"}})])
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            _ = [m async for m in client.list_messages("a@b")]

    async def test_non_object_items_rejected(self):
        t = ScriptedTransport([json_response(200, {"messages": ["not-an-object"]})])
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            _ = [m async for m in client.list_messages("a@b")]

    async def test_redirect_on_download_is_not_content(self):
        t = ScriptedTransport(
            [json_response(302, {"error": "moved"}, headers={"Location": "http://x"})]
        )
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            await client.download("http://files.test/raw/x")

    async def test_redirect_on_api_call_is_an_error(self):
        t = ScriptedTransport([json_response(301, {})])
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            await client.auth_me()

    async def test_download_requires_exactly_200(self):
        t = ScriptedTransport([bytes_response(206, b"partial")])
        client = make_client(t)
        with self.assertRaises(UnavailableError):
            await client.download("http://files.test/raw/x")

    async def test_malformed_key_never_reaches_a_header(self):
        with self.assertRaises(ValueError) as cm:
            AgentMailClient(BASE, "secret\r\nX: 1", ScriptedTransport())
        self.assertNotIn("secret", str(cm.exception))
        with self.assertRaises(ValueError):
            AgentMailClient(BASE, "", ScriptedTransport())

    async def test_transport_value_error_does_not_leak(self):
        from imapgw.apiclient import ThreadedTransport, TransportError

        transport = ThreadedTransport()
        try:
            with self.assertRaises(TransportError) as cm:
                await transport.request(
                    "GET",
                    "http://127.0.0.1:9/x",
                    headers={"authorization": "Bearer s3cret\r\n"},
                    body=None,
                    timeout=1.0,
                )
            self.assertNotIn("s3cret", str(cm.exception))
        finally:
            transport.close()
