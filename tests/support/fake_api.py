"""Spawns the repo's fake AgentMail API (Node) on an ephemeral port and exposes its test-control
endpoints. Used only by tests; the server under test never touches ``/_test/*``."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_SCRIPT = REPO_ROOT / "test-harness" / "fake-agentmail-api.mjs"
READY_PREFIX = "Fake AgentMail API ready at "

INBOX_ID = "candidate@imap.test"
API_KEY = "test_agentmail_key"


class FakeApiProcess:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self.base_url = ""
        self.control_root = ""

    # ----- lifecycle ----------------------------------------------------------------------

    def start(self, timeout: float = 15.0) -> None:
        env = {**os.environ, "HARNESS_API_PORT": "0", "HARNESS_QUIET": "1"}
        self._proc = subprocess.Popen(
            ["node", str(FAKE_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self._proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("fake API did not report readiness in time")
            line = self._proc.stdout.readline()
            if line == "" and self._proc.poll() is not None:
                raise RuntimeError("fake API exited before becoming ready")
            if line.startswith(READY_PREFIX):
                self.base_url = line[len(READY_PREFIX) :].strip()
                break
        parts = urlsplit(self.base_url)
        self.control_root = f"{parts.scheme}://{parts.netloc}"
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for _ in self._proc.stdout:
            pass

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        self._proc = None

    # ----- control endpoints (synchronous; fine for tests) ---------------------------------

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        parts = urlsplit(self.control_root)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"content-type": "application/json"} if data else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"{method} {path} -> {resp.status}: {payload!r}")
            return json.loads(payload) if payload else {}
        finally:
            conn.close()

    def reset(self) -> dict:
        return self._call("POST", "/_test/reset")

    def add_message(self) -> dict:
        return self._call("POST", "/_test/add-message")

    def fail_next(
        self,
        *,
        method: str = "GET",
        path_prefix: str = "/",
        status: int = 503,
        delay_ms: int = 0,
        times: int = 1,
    ) -> dict:
        return self._call(
            "POST",
            "/_test/fail-next",
            {
                "method": method,
                "path_prefix": path_prefix,
                "status": status,
                "delay_ms": delay_ms,
                "times": times,
            },
        )

    def state(self) -> dict:
        return self._call("GET", "/_test/state")

    def requests(self) -> list[dict]:
        return self._call("GET", "/_test/requests")["requests"]
