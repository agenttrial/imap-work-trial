"""``python -m imapgw``: start the gateway and run until SIGINT/SIGTERM."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from imapgw.config import configure_logging, load_settings
from imapgw.server import Server
from imapgw.uidstore import StoreUnavailable

log = logging.getLogger("imapgw")


async def _run() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        server = Server(settings)
    except StoreUnavailable as exc:
        log.error("%s", exc)
        log.error("refusing to start: fix the UID store problem rather than losing UID state")
        return 2
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await server.start()
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", settings.imap_host, settings.imap_port, exc)
        return 1
    print(f"imapgw ready on {server.host}:{server.port} (API {settings.api_url})", flush=True)
    await stop.wait()
    log.info("shutting down")
    await server.stop()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
