"""KSMA Portal bounded context.

Owns ``ServiceRequest``, ``InternalNotification``, ``CommunicationPreference``.
The schema is fully migrated (see ``alembic/versions/0009_portal_operations.py``);
this package owns the domain entities, repositories, application services, and
HTTP router. ServiceRequest is reused as the WorkItem primitive for
cross-BC handoffs (ADR-0003) — see ``ksma.portal.application.work_items``.
"""
from __future__ import annotations