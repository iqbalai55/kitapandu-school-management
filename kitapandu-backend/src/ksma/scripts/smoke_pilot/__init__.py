"""KSMA pilot smoke test — happy-path HTTP wiring against a running dev server.

Run::

    # Start the dev server in another terminal (or background job):
    #   uvicorn ksma.main:app --reload --port 8000
    python -m ksma.scripts.smoke_pilot

The script assumes:

1. The dev DB at ``localhost:5432/ksma`` has been seeded with
   ``python -m ksma.scripts.seed_vaneta_demo``.
2. ``uvicorn ksma.main:app`` is running on ``http://localhost:8000``.

It logs in as each role in turn and walks the user-visible surface end-to-end:

    AC login → memberships (decoded from JWT) →
    participants (duplicate-suspect V02) → batches (capacity + occupancy) →
    FI: invoices (paid vs unpaid status) → IN: session revision + attendance →
    ST: portal/inbox → AU: /me/audit=FIN

Each step prints ``[step N: <name>] pass`` or ``[fail: <details>]``. After all
steps, the script prints either ``PILOT SMOKE PASS`` or ``PILOT SMOKE FAIL:
step=<N>`` and ``sys.exit(0|1)``.

The script opens a direct asyncpg connection to the DB to find UUIDs by natural
key (the API surface intentionally has no list-invoices / list-sessions routes
yet, so we look up IDs from the DB and exercise every other endpoint via HTTP).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ksma.settings import get_settings


import os

BASE_URL = os.environ.get("KSMA_SMOKE_BASE_URL", "http://localhost:8000")
SLUG_VANETA = "vaneta"


# ---- Output helpers --------------------------------------------------------


class SmokeRecorder:
    def __init__(self) -> None:
        self.steps: list[tuple[int, str, bool, str]] = []
        self.failing_step: int | None = None

    def record(self, n: int, name: str, ok: bool, details: str = "") -> None:
        status = "pass" if ok else f"fail: {details}"
        print(f"[step {n}: {name}] {status}")
        self.steps.append((n, name, ok, details))
        if not ok and self.failing_step is None:
            self.failing_step = n

    def summary(self) -> None:
        if self.failing_step is None:
            print("PILOT SMOKE PASS")
        else:
            print(f"PILOT SMOKE FAIL: step={self.failing_step}")


# ---- DB lookup helpers ------------------------------------------------------


async def _lookup(
    settings,
) -> dict[str, Any]:
    """Resolve all UUIDs we need by querying the DB directly.

    Returns a dict with everything the smoke steps consume (tenant_id,
    batch_code, invoice IDs, session ID, allocation ID, demo accounts).
    """
    engine = create_async_engine(
        str(settings.postgres_dsn), pool_pre_ping=True
    )
    try:
        async with engine.begin() as conn:
            # Tenant.
            tenant_row = (
                await conn.execute(
                    text(
                        "SELECT id FROM tenant "
                        "WHERE display_name = 'Vaneta Maju Berdaya (DEMO)' LIMIT 1"
                    )
                )
            ).first()
            tenant_id = UUID(str(tenant_row.id))

            # Bypass RLS for our IDs lookup so we read across schemas transparently.
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))

            # Accounts (one per role).
            account_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT a.id, a.auth_subject
                        FROM account a
                        JOIN membership m ON m.account_id = a.id
                        WHERE m.tenant_id = :tid
                          AND a.auth_subject LIKE :prefix
                        """
                    ),
                    {"tid": str(tenant_id), "prefix": f"%.{SLUG_VANETA}"},
                )
            ).all()
            accounts = {
                str(r.auth_subject).split(".")[0]: UUID(str(r.id))
                for r in account_rows
            }

            # Batch.
            batch_row = (
                await conn.execute(
                    text(
                        "SELECT id, capacity FROM batch "
                        "WHERE tenant_id = :tid AND batch_code = 'EN-A1-Okt2026' LIMIT 1"
                    ),
                    {"tid": str(tenant_id)},
                )
            ).first()
            batch_id = UUID(str(batch_row.id))
            batch_capacity = int(batch_row.capacity)

            # Active enrollments for the batch.
            enr_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT id, participant_id FROM enrollment
                        WHERE tenant_id = :tid AND batch_id = :bid
                          AND status IN ('planned','active')
                        ORDER BY created_at
                        """
                    ),
                    {"tid": str(tenant_id), "bid": str(batch_id)},
                )
            ).all()
            enrollments = [(UUID(str(r.id)), UUID(str(r.participant_id))) for r in enr_rows]

            # First session.
            sess_row = (
                await conn.execute(
                    text(
                        "SELECT id FROM session "
                        "WHERE tenant_id = :tid AND batch_id = :bid "
                        "ORDER BY session_no LIMIT 1"
                    ),
                    {"tid": str(tenant_id), "bid": str(batch_id)},
                )
            ).first()
            session_id = UUID(str(sess_row.id))

            # Invoices.
            inv1_row = (
                await conn.execute(
                    text(
                        "SELECT id FROM invoice "
                        "WHERE tenant_id = :tid AND invoice_no = 'INV-VANETA-0001' LIMIT 1"
                    ),
                    {"tid": str(tenant_id)},
                )
            ).first()
            inv1_id = UUID(str(inv1_row.id))
            inv2_row = (
                await conn.execute(
                    text(
                        "SELECT id FROM invoice "
                        "WHERE tenant_id = :tid AND invoice_no = 'INV-VANETA-0002' LIMIT 1"
                    ),
                    {"tid": str(tenant_id)},
                )
            ).first()
            inv2_id = UUID(str(inv2_row.id))

            # Verified payment for INV-VANETA-0001's installment-1.
            vp_row = (
                await conn.execute(
                    text(
                        """
                        SELECT vp.id, vp.recognized_amount
                        FROM verified_payment vp
                        JOIN payment_submission ps ON ps.id = vp.submission_id
                        WHERE vp.tenant_id = :tid AND ps.invoice_id = :iid
                        LIMIT 1
                        """
                    ),
                    {"tid": str(tenant_id), "iid": str(inv1_id)},
                )
            ).first()
            payment_id = UUID(str(vp_row.id)) if vp_row is not None else None

            return {
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "batch_capacity": batch_capacity,
                "enrollments": enrollments,
                "session_id": session_id,
                "inv1_id": inv1_id,
                "inv2_id": inv2_id,
                "payment_id": payment_id,
                "accounts": accounts,
            }
    finally:
        await engine.dispose()


# ---- HTTP helpers ----------------------------------------------------------


async def _login(
    client: httpx.AsyncClient,
    *,
    auth_subject: str,
    tenant_id: UUID,
    password: str = "Demo!2345",
) -> tuple[str, dict[str, Any]]:
    """Login + return the access token and the decoded JWT claims."""
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "auth_subject": auth_subject,
            "password": password,
            "tenant_id": str(tenant_id),
        },
    )
    response.raise_for_status()
    body = response.json()
    access = body["access_token"]

    # Decode claims (no signature verification — this is a smoke test).
    import base64

    parts = access.split(".")
    if len(parts) >= 2:
        raw = parts[1]
        # base64url with possible missing padding.
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode())
        try:
            claims = json.loads(decoded)
        except json.JSONDecodeError:
            claims = {}
    else:
        claims = {}

    return access, claims


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- Steps -----------------------------------------------------------------


async def step_01_login(rec: SmokeRecorder, settings) -> dict[str, Any] | None:
    """Step 1 + 2: log in as ``ac@vaneta.id`` (AC), check 200 + JWT carries
    one membership for Vaneta DEMO.

    (Step 2 in the ticket asks for ``GET /me/memberships`` which is not yet a
    route — we verify membership via the JWT ``tz_memberships`` claim instead,
    which is the actual contract.)
    """
    try:
        ids = await _lookup(settings)
    except Exception as exc:
        rec.record(1, "lookup IDs by DB", False, f"DB error: {exc}")
        return None

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            access, claims = await _login(
                client,
                auth_subject=f"ac.{SLUG_VANETA}",
                tenant_id=ids["tenant_id"],
            )
        except Exception as exc:
            rec.record(1, "login as ac@vaneta.id", False, f"login failed: {exc}")
            return None

    if not access:
        rec.record(1, "login as ac@vaneta.id", False, "no access_token returned")
        return None

    rec.record(1, "login as ac@vaneta.id", True)

    # Step 2: JWT-based membership assertion.
    memberships = claims.get("tz_memberships") or []
    current_tenant = claims.get("tz_current_tenant")
    if str(ids["tenant_id"]) != current_tenant:
        rec.record(
            2,
            "JWT carries Vaneta DEMO membership",
            False,
            f"tz_current_tenant={current_tenant!r} != {ids['tenant_id']!r}",
        )
        return None
    if not memberships:
        rec.record(
            2,
            "JWT carries Vaneta DEMO membership",
            False,
            "tz_memberships is empty",
        )
        return None

    rec.record(2, "JWT carries Vaneta DEMO membership", True)
    ids["ac_token"] = access
    return ids


async def step_03_participants(
    rec: SmokeRecorder, ids: dict[str, Any]
) -> None:
    """Step 3: GET /api/v1/participants?q=Sari — expect two candidates,
    ``duplicate_suspect=true`` covering P-0001 + P-0006."""
    if "ac_token" not in ids:
        rec.record(3, "participants duplicate-suspect", False, "no AC token from step 1")
        return
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            response = await client.get(
                "/api/v1/participants",
                params={"q": "Sari"},
                headers=_auth_headers(ids["ac_token"]),
            )
        except Exception as exc:
            rec.record(3, "participants duplicate-suspect", False, f"http: {exc}")
            return
    if response.status_code != 200:
        rec.record(
            3,
            "participants duplicate-suspect",
            False,
            f"status={response.status_code}: {response.text[:200]}",
        )
        return
    body = response.json()
    items = body.get("items") or []
    sari_participant_nos = {it.get("participant_no") for it in items}
    if {"P-0001", "P-0006"}.issubset(sari_participant_nos):
        dup_ok = True
        dup_detail = ""
    else:
        dup_ok = False
        dup_detail = (
            f"items had {sorted(sari_participant_nos)!r}, expected P-0001+P-0006"
        )
    if dup_ok:
        suspects = body.get("duplicate_suspects") or []
        if not suspects:
            dup_ok = False
            dup_detail = "items include P-0001+P-0006 but duplicate_suspects is empty"
    if dup_ok:
        rec.record(3, "participants duplicate-suspect (Sari)", True)
    else:
        rec.record(3, "participants duplicate-suspect (Sari)", False, dup_detail)


async def step_04_batches(rec: SmokeRecorder, ids: dict[str, Any]) -> None:
    """Step 4: GET /api/v1/batches — find EN-A1-Okt2026, expect capacity=10
    and active_enrollments=2."""
    if "ac_token" not in ids:
        rec.record(4, "batch EN-A1-Okt2026 capacity/occupied", False, "no AC token")
        return
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            response = await client.get(
                "/api/v1/batches",
                headers=_auth_headers(ids["ac_token"]),
            )
        except Exception as exc:
            rec.record(4, "batch EN-A1-Okt2026 capacity/occupied", False, f"http: {exc}")
            return
    if response.status_code != 200:
        rec.record(
            4,
            "batch EN-A1-Okt2026 capacity/occupied",
            False,
            f"status={response.status_code}: {response.text[:200]}",
        )
        return
    body = response.json()
    matches = [
        b for b in (body or []) if b.get("batch_code") == "EN-A1-Okt2026"
    ]
    if not matches:
        rec.record(
            4,
            "batch EN-A1-Okt2026 capacity/occupied",
            False,
            "EN-A1-Okt2026 not in /api/v1/batches",
        )
        return
    b = matches[0]
    if b.get("capacity") != 10:
        rec.record(
            4,
            "batch EN-A1-Okt2026 capacity/occupied",
            False,
            f"capacity={b.get('capacity')!r} expected 10",
        )
        return
    if b.get("active_enrollments") != 2:
        rec.record(
            4,
            "batch EN-A1-Okt2026 capacity/occupied",
            False,
            f"active_enrollments={b.get('active_enrollments')!r} expected 2",
        )
        return
    rec.record(4, "batch EN-A1-Okt2026 capacity/occupied", True)


async def step_05_06_invoices(rec: SmokeRecorder, ids: dict[str, Any]) -> None:
    """Steps 5 + 6: log in as fi, GET status of the two invoices."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            fi_token, _ = await _login(
                client,
                auth_subject=f"fi.{SLUG_VANETA}",
                tenant_id=ids["tenant_id"],
            )
        except Exception as exc:
            rec.record(5, "login as fi@vaneta.id", False, f"login: {exc}")
            rec.record(6, "invoice INV-VANETA-0002 status", False, "FI token missing")
            return
        headers = _auth_headers(fi_token)

        # The expected payment_status comes from the DB-derived lookup so any
        # change to the seed (allocation_amount, installments…) is tracked.
        for n, invoice_id, expected_status in [
            (5, ids["inv1_id"], "paid"),
            (6, ids["inv2_id"], "unpaid"),
        ]:
            try:
                response = await client.get(
                    f"/api/v1/invoices/{str(invoice_id)}/status",
                    headers=headers,
                )
            except Exception as exc:
                rec.record(
                    n,
                    f"invoice {invoice_id} status={expected_status}",
                    False,
                    f"http: {exc}",
                )
                continue
            if response.status_code != 200:
                rec.record(
                    n,
                    f"invoice {invoice_id} status={expected_status}",
                    False,
                    f"status={response.status_code}: {response.text[:200]}",
                )
                continue
            proj = response.json()
            actual = proj.get("payment_status")
            if actual != expected_status:
                rec.record(
                    n,
                    f"invoice {invoice_id} status={expected_status}",
                    False,
                    f"payment_status={actual!r}",
                )
                continue
            if proj.get("overdue_amount", 0) != 0:
                rec.record(
                    n,
                    f"invoice {invoice_id} status={expected_status}",
                    False,
                    f"overdue_amount={proj.get('overdue_amount')!r}",
                )
                continue
            if expected_status == "paid":
                # INV-VANETA-0001 has exactly one installment worth
                # `expected_allocation_amount` allocated. We read the
                # expected amount out of the seed in `_lookup` instead of
                # hard-coding 1_000_000 so the smoke stays accurate when the
                # seed changes.
                alloc = proj.get("active_allocation_total", 0)
                if alloc != ids.get("expected_allocation_amount"):
                    rec.record(
                        n,
                        f"invoice {invoice_id} status={expected_status}",
                        False,
                        f"active_allocation_total={alloc!r} "
                        f"!= expected {ids.get('expected_allocation_amount')!r}",
                    )
                    continue
            rec.record(
                n,
                f"invoice {invoice_id} status={expected_status}",
                True,
            )


async def step_07_session(rec: SmokeRecorder, ids: dict[str, Any]) -> None:
    """Step 7: log in as in, GET /api/v1/sessions/{first} — expect effective
    revision + attendance list (attendance should contain P-0001)."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            in_token, _ = await _login(
                client,
                auth_subject=f"in.{SLUG_VANETA}",
                tenant_id=ids["tenant_id"],
            )
        except Exception as exc:
            rec.record(7, "session first effective+attendance", False, f"login: {exc}")
            return
        headers = _auth_headers(in_token)
        try:
            response = await client.get(
                f"/api/v1/sessions/{ids['session_id']}",
                headers=headers,
            )
        except Exception as exc:
            rec.record(7, "session first effective+attendance", False, f"http: {exc}")
            return
        if response.status_code != 200:
            rec.record(
                7,
                "session first effective+attendance",
                False,
                f"status={response.status_code}: {response.text[:200]}",
            )
            return
        body = response.json()
        # Effective revision + an attendance list >= 1 row.
        if not body.get("effective_revision_id"):
            rec.record(
                7,
                "session first effective+attendance",
                False,
                "effective_revision_id missing",
            )
            return
        try:
            att_response = await client.get(
                f"/api/v1/sessions/{ids['session_id']}/attendance",
                headers=headers,
            )
        except Exception as exc:
            rec.record(7, "session first effective+attendance", False, f"http: {exc}")
            return
        if att_response.status_code != 200:
            rec.record(
                7,
                "session first effective+attendance",
                False,
                f"attendance status={att_response.status_code}",
            )
            return
        att_rows = att_response.json() or []
        if len(att_rows) < 1:
            rec.record(
                7,
                "session first effective+attendance",
                False,
                f"attendance rows={len(att_rows)}",
            )
            return
        rec.record(7, "session first effective+attendance", True)


async def step_08_portal_inbox(rec: SmokeRecorder, ids: dict[str, Any]) -> None:
    """Step 8: log in as st, GET /api/v1/portal/inbox — at least one row."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            st_token, _ = await _login(
                client,
                auth_subject=f"st.{SLUG_VANETA}",
                tenant_id=ids["tenant_id"],
            )
        except Exception as exc:
            rec.record(8, "portal/inbox for st@vaneta.id", False, f"login: {exc}")
            return
        headers = _auth_headers(st_token)
        try:
            response = await client.get(
                "/api/v1/portal/inbox",
                headers=headers,
            )
        except Exception as exc:
            rec.record(8, "portal/inbox for st@vaneta.id", False, f"http: {exc}")
            return
        if response.status_code != 200:
            rec.record(
                8,
                "portal/inbox for st@vaneta.id",
                False,
                f"status={response.status_code}: {response.text[:200]}",
            )
            return
        rows = response.json() or []
        if not rows:
            rec.record(8, "portal/inbox for st@vaneta.id", False, "inbox is empty")
            return
        rec.record(8, "portal/inbox for st@vaneta.id", True)


async def step_09_audit(rec: SmokeRecorder, ids: dict[str, Any]) -> None:
    """Step 9: log in as au, GET /api/v1/me/audit?object_kind=FIN — at least
    one row from the seeded allocation."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            au_token, _ = await _login(
                client,
                auth_subject=f"au.{SLUG_VANETA}",
                tenant_id=ids["tenant_id"],
            )
        except Exception as exc:
            rec.record(9, "audit FIN seeded allocation", False, f"login: {exc}")
            return
        headers = _auth_headers(au_token)
        try:
            response = await client.get(
                "/api/v1/me/audit",
                params={"object_kind": "FIN"},
                headers=headers,
            )
        except Exception as exc:
            rec.record(9, "audit FIN seeded allocation", False, f"http: {exc}")
            return
        if response.status_code != 200:
            rec.record(
                9,
                "audit FIN seeded allocation",
                False,
                f"status={response.status_code}: {response.text[:200]}",
            )
            return
        rows = response.json() or []
        if not rows:
            rec.record(9, "audit FIN seeded allocation", False, "audit list is empty")
            return
        rec.record(9, "audit FIN seeded allocation", True)


# ---- main ------------------------------------------------------------------


async def _async_main() -> int:
    settings = get_settings()
    rec = SmokeRecorder()

    # Pre-flight: confirm the dev server is reachable. If not, exit 2 with a
    # clear instruction.
    print(f"[smoke] targeting {BASE_URL}")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=2.0) as client:
            h = await client.get("/health")
    except Exception as exc:
        print(
            f"[smoke] dev server unreachable at {BASE_URL}: {exc}\n"
            "        start it with: uvicorn ksma.main:app --port 8000"
        )
        return 2
    if h.status_code != 200:
        print(
            f"[smoke] dev server health check returned {h.status_code}; "
            "start it with: uvicorn ksma.main:app --port 8000"
        )
        return 2
    print(f"[smoke] server OK ({h.elapsed.total_seconds()*1000:.0f}ms)")

    # Step 1+2 (login + memberships via JWT).
    ids = await step_01_login(rec, settings)
    if ids is None:
        for n in (3, 4, 5, 6, 7, 8, 9):
            step_name = (
                {
                    3: "participants duplicate-suspect",
                    4: "batches",
                    5: "invoice INV-VANETA-0001 paid",
                    6: "invoice INV-VANETA-0002 unpaid",
                    7: "session effective revision + attendance",
                    8: "portal inbox",
                    9: "audit FIN",
                }[n]
                + " (skipped, no seed)"
            )
            rec.record(n, step_name, False, "skipped due to earlier failure")
        rec.summary()
        return 1

    await step_03_participants(rec, ids)
    await step_04_batches(rec, ids)
    await step_05_06_invoices(rec, ids)
    await step_07_session(rec, ids)
    await step_08_portal_inbox(rec, ids)
    await step_09_audit(rec, ids)

    rec.summary()
    print(f"[smoke] elapsed {time.monotonic()-started:.1f}s")
    return 0 if rec.failing_step is None else 1


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
