"""KSMA development scripts.

Entry points (run via ``python -m ksma.scripts.<name>``):

- :mod:`ksma.scripts.seed_vaneta_demo` — populate the dev DB with two demo
  tenants, demo users + participants, one published program + batch + sessions,
  finance invoices + payment + allocation, attendance, and a tracking
  ``OperationalRun``. Idempotent — re-running leaves the DB in the same state.

- :mod:`ksma.scripts.smoke_pilot` — exercise the seeded HTTP API end-to-end
  with ``httpx`` against a running dev server and assert the projected state
  matches the expected pilot state.
"""

from __future__ import annotations

# Re-exports for convenience (importing both modules eagerly keeps type-checkers
# happy without affecting runtime).
from ksma.scripts import seed_vaneta_demo as seed_vaneta_demo  # noqa: E402,F401
from ksma.scripts import smoke_pilot as smoke_pilot  # noqa: E402,F401

__all__ = ["seed_vaneta_demo", "smoke_pilot"]
