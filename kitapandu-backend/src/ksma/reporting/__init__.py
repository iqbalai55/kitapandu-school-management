"""KSMA Reporting bounded context.

Reports, completion decisions, and certificates. Schema is fully migrated (see
``alembic/versions/0008_assessment_reporting.py``); this package owns the
domain entities, repositories, application services, and HTTP router.
"""
from __future__ import annotations

# Sub-packages are imported lazily to avoid a circular import with
# ``ksma.assessment.infrastructure.repositories`` which already imports
# ``ksma.reporting.domain``. Callers should import the sub-packages they need
# (e.g., ``from ksma.reporting.domain import CompletionOutcome``).