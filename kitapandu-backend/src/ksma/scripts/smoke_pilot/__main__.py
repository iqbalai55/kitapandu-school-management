"""Allow ``python -m ksma.scripts.smoke_pilot`` to invoke ``main()``."""
from __future__ import annotations

import sys

from ksma.scripts.smoke_pilot import main


if __name__ == "__main__":
    sys.exit(main())
