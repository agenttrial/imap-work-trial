"""Thin typed client for the AgentMail REST API.

* Bearer auth on every API call; the key is never attached to raw download URLs.
* Blocking ``http.client`` runs in a worker thread so the event loop never stalls.
* Pagination iterators follow ``next_page_token`` to the end; they cannot return early.
* 429 and 5xx are retried with ``Retry-After`` or exponential backoff inside a bounded budget.
* Non-2xx responses map to a small exception hierarchy by HTTP status only; response bodies are
  logged at DEBUG and never used for control flow.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import random
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

log = logging.getLogger("imapgw.api")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-cased names
    body: bytes


class TransportError(Exception):
    """Network-level failure: refused, reset, DNS, timeout, malformed HTTP."""


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class ThreadedTransport:
    """``http.client`` executed in a worker thread; one connection per request.

    Uses its own bounded :class:`ThreadPoolExecutor` rather than the event loop's default one, so
    shutting the transport down never affects other users of the loop."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None, *, max_workers: int = 16) -> None:
        self._ssl = ssl_context or ssl.create_default_context()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="imapgw-http"
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._blocking, method, url, dict(headers), body, timeout
        )

    def _blocking(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise TransportError(f"unsupported URL: {url!r}")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, port, timeout=timeout, context=self._ssl
            )
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        except ValueError as exc:
            # http.client raises ValueError for invalid header values and includes the value in
            # the message; never propagate that text.
            raise TransportError(f"invalid request ({type(exc).__name__})") from None
        finally:
            conn.close()


class AgentMailError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AuthError(AgentMailError):
    """401 or 403."""


class NotFoundError(AgentMailError):
    """404."""


class BadRequestError(AgentMailError):
    """400, 409, 413, 422 and other non-retryable 4xx."""


class UnavailableError(AgentMailError):
    """429 after retries, 5xx, timeouts, connection failures."""


@dataclass(frozen=True)
class RawMeta:
    message_id: str
    size: int
    download_url: str
    expires_at: str


SleepFn = Callable[[float], Awaitable[None]]


class AgentMailClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: Transport,
        *,
        timeout: float = 10.0,
        max_attempts: int = 3,
        retry_budget: float = 10.0,
        backoff_base: float = 0.5,
        sleep: SleepFn = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._base = base_url.rstrip("/")
        if not api_key or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in api_key):
            # Never let a malformed credential reach a header, where http.client would echo it
            # in an exception message.
            raise ValueError("API key must be non-empty printable ASCII")
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._retry_budget = retry_budget
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._rng = rng

    # ----- public API -------------------------------------------------------------------

    async def auth_me(self) -> dict:
        return await self._json("GET", "/auth/me")

    async def get_inbox(self, inbox_id: str) -> dict:
        return await self._json("GET", f"/inboxes/{_seg(inbox_id)}")

    def list_messages(
        self,
        inbox_id: str,
        *,
        labels: Sequence[str] = (),
        include_trash: bool = False,
        include_spam: bool = False,
        ascending: bool = True,
    ) -> AsyncIterator[dict]:
        params: dict[str, object] = {"ascending": "true" if ascending else "false"}
        if labels:
            params["labels"] = list(labels)
        if include_trash:
            params["include_trash"] = "true"
        if include_spam:
            params["include_spam"] = "true"
        return self._paginate(f"/inboxes/{_seg(inbox_id)}/messages", params, "messages")

    async def get_message(self, inbox_id: str, message_id: str) -> dict:
        return await self._json("GET", f"/inboxes/{_seg(inbox_id)}/messages/{_seg(message_id)}")

    async def get_raw_meta(self, inbox_id: str, message_id: str) -> RawMeta:
        data = await self._json("GET", f"/inboxes/{_seg(inbox_id)}/messages/{_seg(message_id)}/raw")
        try:
            return RawMeta(
                message_id=str(data["message_id"]),
                size=int(data["size"]),
                download_url=str(data["download_url"]),
                expires_at=str(data.get("expires_at", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UnavailableError(f"malformed raw metadata: {exc}") from exc

    async def download(self, url: str) -> bytes:
        """Fetch a presigned download URL. Sends no application headers; never sees the API key.
        Only a 200 counts: a redirect or partial response must never become message content."""
        resp = await self._with_retries("GET", url, headers={}, body=None)
        if resp.status != 200:
            raise UnavailableError(f"unexpected download status {resp.status}", status=resp.status)
        return resp.body

    async def update_labels(
        self,
        inbox_id: str,
        message_id: str,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> dict:
        body: dict[str, object] = {}
        if add:
            body["add_labels"] = list(add)
        if remove:
            body["remove_labels"] = list(remove)
        return await self._json(
            "PATCH", f"/inboxes/{_seg(inbox_id)}/messages/{_seg(message_id)}", json_body=body
        )

    def list_drafts(self, inbox_id: str) -> AsyncIterator[dict]:
        return self._paginate(f"/inboxes/{_seg(inbox_id)}/drafts", {}, "drafts")

    async def get_draft(self, inbox_id: str, draft_id: str) -> dict:
        return await self._json("GET", f"/inboxes/{_seg(inbox_id)}/drafts/{_seg(draft_id)}")

    async def create_draft(self, inbox_id: str, body: Mapping[str, object]) -> dict:
        return await self._json("POST", f"/inboxes/{_seg(inbox_id)}/drafts", json_body=dict(body))

    async def delete_draft(self, inbox_id: str, draft_id: str) -> None:
        await self._request("DELETE", f"/inboxes/{_seg(inbox_id)}/drafts/{_seg(draft_id)}")

    # ----- internals --------------------------------------------------------------------

    def _url(self, path: str, params: Mapping[str, object] | None) -> str:
        url = self._base + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        return url

    async def _paginate(
        self, path: str, params: Mapping[str, object], items_key: str
    ) -> AsyncIterator[dict]:
        token: str | None = None
        seen: set[str] = set()
        while True:
            q: dict[str, object] = dict(params)
            q["limit"] = "100"
            if token:
                q["page_token"] = token
            page = await self._json("GET", path, params=q)
            if items_key not in page:
                raise UnavailableError(f"malformed page: missing {items_key!r}")
            items = page[items_key]
            if not isinstance(items, list):
                raise UnavailableError(f"malformed page: {items_key!r} is not a list")
            for item in items:
                if not isinstance(item, dict):
                    raise UnavailableError(f"malformed page: {items_key!r} entry is not an object")
                yield item
            token = page.get("next_page_token")
            if not token:
                return
            token = str(token)
            if token in seen:
                raise UnavailableError("pagination loop detected")
            seen.add(token)

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict:
        resp = await self._request(method, path, params=params, json_body=json_body)
        if not resp.body:
            return {}
        try:
            data = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnavailableError(f"malformed JSON from API ({method} {path})") from exc
        if not isinstance(data, dict):
            raise UnavailableError(f"unexpected JSON shape from API ({method} {path})")
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> HttpResponse:
        headers = {"authorization": f"Bearer {self._api_key}", "accept": "application/json"}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["content-type"] = "application/json"
        return await self._with_retries(method, self._url(path, params), headers=headers, body=body)

    async def _with_retries(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._retry_budget
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._transport.request(
                    method, url, headers=headers, body=body, timeout=self._timeout
                )
            except TransportError as exc:
                log.debug("transport failure on %s %s: %s", method, _redact_url(url), exc)
                err: AgentMailError = UnavailableError(
                    f"upstream unreachable: {exc}", retryable=True
                )
                retry_after = None
            else:
                if 200 <= resp.status < 300:
                    return resp
                err = _classify(resp, method, _redact_url(url))
                retry_after = _retry_after(resp.headers)
                if not err.retryable:
                    raise err

            if attempt >= self._max_attempts:
                raise err
            delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
            if loop.time() + delay > deadline:
                raise err
            log.debug(
                "retrying %s %s in %.2fs (attempt %d)", method, _redact_url(url), delay, attempt
            )
            await self._sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        return self._backoff_base * (2 ** (attempt - 1)) * (1.0 + 0.25 * self._rng())


def _seg(value: str) -> str:
    return quote(value, safe="")


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _redact_url(url: str) -> str:
    """Presigned URLs carry signatures in the query; keep only scheme/host/path in logs."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _classify(resp: HttpResponse, method: str, where: str) -> AgentMailError:
    status = resp.status
    snippet = resp.body[:200].decode("utf-8", errors="replace")
    log.debug("API %s %s -> %d %s", method, where, status, snippet)
    if 300 <= status < 400:
        return UnavailableError(f"unexpected redirect ({status})", status=status)
    if status in (401, 403):
        return AuthError(f"upstream rejected credentials ({status})", status=status)
    if status == 404:
        return NotFoundError("upstream resource not found", status=status)
    if status == 429 or status >= 500:
        return UnavailableError(f"upstream unavailable ({status})", status=status, retryable=True)
    return BadRequestError(f"upstream rejected request ({status})", status=status)
