"""An in-memory Transport for unit tests: routes requests to canned responses and records calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from imapgw.apiclient import HttpResponse, TransportError


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


Responder = Callable[[Call], HttpResponse | Exception]


@dataclass
class ScriptedTransport:
    """Each ``request`` consumes the next responder from ``queue``; when the queue is empty the
    ``router`` callable (if any) answers. A responder may return an Exception to raise it."""

    queue: list[HttpResponse | Exception | Responder] = field(default_factory=list)
    router: Responder | None = None
    calls: list[Call] = field(default_factory=list)
    delay: float = 0.0

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        call = Call(method, url, dict(headers), body)
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.queue:
            item = self.queue.pop(0)
        elif self.router is not None:
            item = self.router
        else:
            raise AssertionError(f"unexpected request {method} {url}")
        if callable(item) and not isinstance(item, Exception | HttpResponse):
            item = item(call)
        if isinstance(item, Exception):
            raise item
        return item


def json_response(
    status: int, value: object, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update({k.lower(): v for k, v in headers.items()})
    return HttpResponse(status, hdrs, json.dumps(value).encode("utf-8"))


def bytes_response(status: int, body: bytes, content_type: str = "message/rfc822") -> HttpResponse:
    return HttpResponse(status, {"content-type": content_type}, body)


def transport_error(message: str = "connection refused") -> TransportError:
    return TransportError(message)
