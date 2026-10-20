"""Allow ``python -m ksma.scripts.seed_vaneta_demo`` to invoke ``main()``."""
from __future__ import annotations

import asyncio

from ksma.scripts.seed_vaneta_demo import main


if __name__ == "__main__":
    asyncio.run(main())
