#!/usr/bin/env python3
"""End-to-end smoke test for a running imapgw against a running AgentMail API (fake or hosted).

Reads IMAP_HOST, IMAP_PORT, AGENTMAIL_INBOX_ID, AGENTMAIL_API_KEY from the environment (or .env).
Prints one PASS/FAIL line per step and the full transcript with the API key masked. Exit 0 on
success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from imapgw.config import load_settings, parse_dotenv  # noqa: E402
from tests.support.imap_client import ImapTestClient  # noqa: E402


class SmokeFailure(Exception):
    pass


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value:
        return value
    dotenv = REPO_ROOT / ".env"
    if dotenv.is_file():
        value = parse_dotenv(dotenv.read_text()).get(name)
        if value:
            return value
    if default is not None:
        return default
    raise SmokeFailure(f"missing environment variable {name}")


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SmokeFailure(what)


async def run() -> int:
    settings = load_settings(dotenv=REPO_ROOT / ".env")
    host = env("IMAP_HOST", settings.imap_host)
    port = int(env("IMAP_PORT", str(settings.imap_port)))
    inbox_id = env("AGENTMAIL_INBOX_ID")
    api_key = env("AGENTMAIL_API_KEY")
    results: list[tuple[str, bool, str]] = []
    client: ImapTestClient | None = None

    async def step(name: str, coro):
        try:
            detail = await coro
            results.append((name, True, detail or ""))
        except Exception as exc:  # noqa: BLE001 - report and stop
            results.append((name, False, str(exc)))
            raise SmokeFailure(name) from exc

    async def s_connect():
        nonlocal client
        client = await ImapTestClient.connect(host, port, timeout=15.0)
        check(client.greeting.startswith(b"* OK"), f"bad greeting {client.greeting!r}")
        return client.greeting.decode()

    async def s_capability():
        resp = await client.cmd("CAPABILITY")
        check(resp.status == "OK", repr(resp))
        check(any(b"IMAP4rev1" in u.line for u in resp.untagged), "IMAP4rev1 not advertised")
        return resp.untagged[0].text

    async def s_login():
        resp = await client.cmd(f'LOGIN "{inbox_id}" "{api_key}"')
        check(resp.status == "OK", repr(resp))
        return resp.text

    async def s_list():
        resp = await client.cmd('LIST "" "*"')
        check(resp.status == "OK", repr(resp))
        names = [u.line.rsplit(b" ", 1)[-1].decode() for u in resp.untagged]
        check("INBOX" in names and "Drafts" in names, f"mailboxes: {names}")
        return ", ".join(names)

    inbox_uids: list[int] = []

    async def s_select_inbox():
        resp = await client.cmd("SELECT INBOX")
        check(resp.status == "OK", repr(resp))
        codes = resp.untagged_codes()
        check("UIDVALIDITY" in codes and "UIDNEXT" in codes, f"missing codes: {codes}")
        exists = [u.line for u in resp.untagged if u.line.endswith(b" EXISTS")]
        return f"{exists[0].decode()} UIDVALIDITY={codes['UIDVALIDITY']} UIDNEXT={codes['UIDNEXT']}"

    async def s_fetch_metadata():
        resp = await client.cmd("UID FETCH 1:* (FLAGS INTERNALDATE RFC822.SIZE)")
        check(resp.status == "OK", repr(resp))
        by_uid = resp.fetch_by_uid()
        inbox_uids.extend(sorted(by_uid))
        return f"{len(by_uid)} messages"

    async def s_fetch_body():
        if not inbox_uids:
            return "no messages to fetch"
        target = inbox_uids[0]
        resp = await client.cmd(f"UID FETCH {target} (RFC822.SIZE BODY.PEEK[])")
        check(resp.status == "OK", repr(resp))
        u = resp.fetch_by_uid()[target]
        check(u.literal is not None, "no literal returned")
        check(f"RFC822.SIZE {len(u.literal)}".encode() in u.line, f"size mismatch: {u.line!r}")
        return f"uid {target}: {len(u.literal)} octets, size matches literal"

    drafts_before = 0

    async def s_select_drafts():
        nonlocal drafts_before
        resp = await client.cmd("SELECT Drafts")
        check(resp.status == "OK", repr(resp))
        exists = [u.line for u in resp.untagged if u.line.endswith(b" EXISTS")][0]
        drafts_before = int(exists.split()[1])
        fetched = await client.cmd("UID FETCH 1:* (FLAGS BODY.PEEK[HEADER.FIELDS (To Subject)])")
        check(fetched.status == "OK", repr(fetched))
        check(all(b"\\Draft" in u.line for u in fetched.untagged), "draft without \\Draft flag")
        return f"{drafts_before} drafts, all flagged \\Draft"

    subject = f"imapgw smoke {int(time.time())}"

    async def s_append():
        raw = (
            f"From: {inbox_id}\r\nTo: smoke@example.com\r\nCc: copy@example.com\r\n"
            f"Subject: {subject}\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "Created by scripts/smoke.py — café \U0001f680\r\n"
        ).encode()
        resp = await client.cmd_literal("APPEND Drafts (\\Draft)", raw)
        check(resp.status == "OK", repr(resp))
        return resp.text

    async def s_verify_append():
        resp = await client.cmd("SELECT Drafts")
        check(resp.status == "OK", repr(resp))
        exists = int([u.line for u in resp.untagged if u.line.endswith(b" EXISTS")][0].split()[1])
        check(exists == drafts_before + 1, f"expected {drafts_before + 1} drafts, got {exists}")
        fetched = await client.cmd("UID FETCH 1:* BODY.PEEK[HEADER.FIELDS (Subject)]")
        found = [u for u in fetched.untagged if subject.encode() in (u.literal or b"")]
        check(len(found) == 1, "appended draft not found by subject")
        return f"{exists} drafts, appended draft visible"

    async def s_noop_logout():
        check((await client.cmd("NOOP")).status == "OK", "NOOP failed")
        resp = await client.cmd("LOGOUT")
        check(
            resp.status == "OK" and any(u.line.startswith(b"* BYE") for u in resp.untagged),
            repr(resp),
        )
        eof = await client.read_eof()
        check(eof == b"", f"server did not close: {eof!r}")
        return "BYE received, connection closed"

    steps = [
        ("connect", s_connect),
        ("CAPABILITY", s_capability),
        ("LOGIN", s_login),
        ("LIST", s_list),
        ("SELECT INBOX", s_select_inbox),
        ("UID FETCH metadata", s_fetch_metadata),
        ("UID FETCH body", s_fetch_body),
        ("SELECT Drafts + FETCH", s_select_drafts),
        ("APPEND Drafts", s_append),
        ("verify APPEND", s_verify_append),
        ("NOOP + LOGOUT", s_noop_logout),
    ]
    ok = True
    try:
        for name, fn in steps:
            await step(name, fn())
    except SmokeFailure:
        ok = False
    finally:
        for name, passed, detail in results:
            print(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
        if client is not None:
            print("--- transcript ---")
            for line in client.transcript:
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                print(text.replace(api_key, "***"))
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
    print("SMOKE", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except SmokeFailure as exc:
        print(f"FAIL  {exc}")
        sys.exit(1)
