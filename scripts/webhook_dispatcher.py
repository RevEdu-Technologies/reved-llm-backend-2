"""Outbound webhook dispatcher — drains the ``webhook_deliveries`` outbox.

Polls for due deliveries and POSTs each to its subscriber URL with an
HMAC-signed body, rescheduling failures with exponential backoff. Run as a
long-lived sidecar process or invoke ``--once`` from a scheduler tick.

Usage
-----
    python -m scripts.webhook_dispatcher              # loop forever
    python -m scripts.webhook_dispatcher --once       # one pass, then exit
    python -m scripts.webhook_dispatcher --interval 5 # poll every 5s
    python -m scripts.webhook_dispatcher --batch 50   # claim up to 50/pass

Exits cleanly on SIGINT/SIGTERM. Each pass is independent; a crash loses no
events (they stay ``pending`` in the outbox and are re-claimed next pass).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.services.webhook_service import WebhookService

logger = logging.getLogger("reved.webhook_dispatcher")


async def _run(interval: float, batch: int, once: bool) -> None:
    service = WebhookService()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows event loops;
            # KeyboardInterrupt still breaks the loop there.
            pass

    try:
        while not stop.is_set():
            try:
                result = await service.deliver_due(limit=batch)
                if result.claimed:
                    logger.info(
                        "dispatch pass claimed=%d delivered=%d retried=%d failed=%d",
                        result.claimed,
                        result.delivered,
                        result.retried,
                        result.failed,
                    )
            except Exception:  # noqa: BLE001 — never let one bad pass kill the loop
                logger.exception("dispatch pass failed")

            if once:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="RevEd webhook dispatcher")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    parser.add_argument("--batch", type=int, default=20, help="max deliveries claimed per pass")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = parser.parse_args()

    configure_logging(level=logging.INFO)
    asyncio.run(_run(interval=args.interval, batch=args.batch, once=args.once))


if __name__ == "__main__":
    main()
