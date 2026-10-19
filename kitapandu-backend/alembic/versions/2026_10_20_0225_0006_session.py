"""Session context — Session, SessionRevision, Attendance, TeachingJournal, SessionCompletion

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 02:25

R09 invariants:
- Unique attendance per (session, enrollment) via composite UNIQUE
- ``attendance_code`` CHECK enforces the closed enum; NULL = ``belum_diisi``
- SessionCompletion carries ``supersedes_id`` FK to itself for post-close
  correction chains (V09)
- Closure requires completeness — enforced at the application layer in
  ``close_session`` (also documented in §3.25)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # session (U scope)
    op.execute(
        """
        CREATE TABLE session (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            unit_id                 uuid NOT NULL REFERENCES education_unit(id) ON DELETE RESTRICT,
            batch_id                uuid NOT NULL REFERENCES batch(id) ON DELETE RESTRICT,
            session_no              integer NOT NULL CHECK (session_no > 0),
            effective_revision_id   uuid,
            lifecycle_status        text NOT NULL CHECK (lifecycle_status IN
                                    ('planned','scheduled','cancelled','closed')),
            closure_id              uuid,
            reschedule_reason       text,
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL,
            UNIQUE (batch_id, session_no)
        )
        """
    )
    op.execute("CREATE INDEX ix_session_tenant ON session(tenant_id)")
    op.execute("CREATE INDEX ix_session_batch ON session(batch_id)")

    # session_revision (U scope)
    op.execute(
        """
        CREATE TABLE session_revision (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            session_id          uuid NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
            revision_no         integer NOT NULL CHECK (revision_no > 0),
            starts_at           timestamptz NOT NULL,
            ends_at             timestamptz NOT NULL,
            timezone            text NOT NULL,
            location            jsonb,
            change_reason       text NOT NULL,
            published_at        timestamptz,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (session_id, revision_no),
            CHECK (ends_at > starts_at)
        )
        """
    )
    op.execute("CREATE INDEX ix_session_revision_session ON session_revision(session_id)")

    # Wire the Session FKs now that session_revision exists.
    op.execute(
        "ALTER TABLE session ADD CONSTRAINT session_effective_revision_fk "
        "FOREIGN KEY (effective_revision_id) REFERENCES session_revision(id)"
    )

    # attendance (U scope) — composite UNIQUE per R09
    op.execute(
        """
        CREATE TABLE attendance (
            id                  uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            session_id          uuid NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
            enrollment_id       uuid NOT NULL REFERENCES enrollment(id) ON DELETE RESTRICT,
            attendance_code     text CHECK (attendance_code IS NULL OR attendance_code IN
                                  ('hadir','izin','sakit','tidak_hadir','terlambat')),
            note                text,
            recorded_at         timestamptz,
            recorded_by         text,
            correction_reason   text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text NOT NULL,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            updated_by          text NOT NULL,
            UNIQUE (session_id, enrollment_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_attendance_tenant ON attendance(tenant_id)")
    op.execute(
        "CREATE INDEX ix_attendance_enrollment ON attendance(enrollment_id)"
    )

    # teaching_journal (U scope) — one logical journal per session
    op.execute(
        """
        CREATE TABLE teaching_journal (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            session_id              uuid NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
            actual_instructor_id    uuid NOT NULL REFERENCES membership(id),
            topic                   text NOT NULL,
            activities              text,
            internal_note           text,
            portal_summary          text,
            follow_up               text,
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL,
            UNIQUE (session_id)
        )
        """
    )

    # session_completion (U scope)
    op.execute(
        """
        CREATE TABLE session_completion (
            id                      uuid PRIMARY KEY DEFAULT ksma_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
            session_id              uuid NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
            completion_no           integer NOT NULL CHECK (completion_no > 0),
            decision                jsonb NOT NULL,
            checklist_snapshot      jsonb NOT NULL,
            policy_id               uuid,
            supersedes_id           uuid REFERENCES session_completion(id),
            correction_reason       text,
            outcome                 text NOT NULL CHECK (outcome IN ('completed','incomplete','reopened')),
            created_at              timestamptz NOT NULL DEFAULT now(),
            created_by              text NOT NULL,
            updated_at              timestamptz NOT NULL DEFAULT now(),
            updated_by              text NOT NULL,
            UNIQUE (session_id, completion_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_session_completion_session ON session_completion(session_id)"
    )

    # Wire Session.closure_id now that session_completion exists.
    op.execute(
        "ALTER TABLE session ADD CONSTRAINT session_closure_fk "
        "FOREIGN KEY (closure_id) REFERENCES session_completion(id)"
    )

    # RLS for every U table.
    rls_tables = ["session", "session_revision", "attendance", "teaching_journal", "session_completion"]
    for tbl in rls_tables:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (
              ksma_bypass_rls()
              OR tenant_id = ksma_current_tenant()
            )
            WITH CHECK (
              ksma_bypass_rls()
              OR tenant_id = ksma_current_tenant()
            )
            """
        )


def downgrade() -> None:
    rls_tables = [
        "session_completion",
        "teaching_journal",
        "attendance",
        "session_revision",
        "session",
    ]
    for tbl in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session DROP CONSTRAINT IF EXISTS session_closure_fk")
    op.execute("ALTER TABLE session DROP CONSTRAINT IF EXISTS session_effective_revision_fk")
    op.execute("DROP TABLE IF EXISTS session_completion CASCADE")
    op.execute("DROP TABLE IF EXISTS teaching_journal CASCADE")
    op.execute("DROP TABLE IF EXISTS attendance CASCADE")
    op.execute("DROP TABLE IF EXISTS session_revision CASCADE")
    op.execute("DROP TABLE IF EXISTS session CASCADE")