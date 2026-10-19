"""Outbox worker CLI entry point.

Usage::

    python -m ksma.shared.outbox.run
"""
from __future__ import annotations

import asyncio

from ksma.settings import get_settings
from ksma.shared.outbox import run_forever


def main() -> None:
    settings = get_settings()
    asyncio.run(
        run_forever(
            poll_interval=settings.outbox_poll_interval_seconds,
            batch_size=settings.outbox_batch_size,
        )
    )


if __name__ == "__main__":
    main()