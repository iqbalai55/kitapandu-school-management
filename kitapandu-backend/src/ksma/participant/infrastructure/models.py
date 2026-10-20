"""SQLAlchemy 2.x ORM models for the participant context.

Schema matches ``alembic/versions/0003_participant.py``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    text,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ParticipantRow(Base):
    __tablename__ = "participant"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    participant_no: Mapped[str] = mapped_column(String(40), nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contact: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    identity_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Encrypted at rest per Q37. Stored as bytea from pgp_sym_encrypt().
    support_needs_note_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("tenant_id", "participant_no"),
        # birth_date not in the future — checked via app layer too
    )


class RelatedPartyRow(Base):
    __tablename__ = "related_party"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    party_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    organization_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")


class ParticipantAccountLinkRow(Base):
    __tablename__ = "participant_account_link"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("account.id"), nullable=False)
    participant_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=True)
    related_party_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("related_party.id"), nullable=True)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        CheckConstraint(
            "(participant_id IS NOT NULL AND related_party_id IS NULL) OR "
            "(participant_id IS NULL AND related_party_id IS NOT NULL)",
            name="ck_link_target_xor",
        ),
        # Prevent same account linking twice to same target
        UniqueConstraint(
            "tenant_id", "account_id", "participant_id", "related_party_id",
            name="uq_link_unique_target",
        ),
    )


class ParticipantRelationRow(Base):
    __tablename__ = "participant_relation"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True,
        server_default=text("ksma_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    participant_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("participant.id"), nullable=False)
    related_party_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("related_party.id"), nullable=False)
    relation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    financial_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "participant_id", "related_party_id", "relation_kind",
            name="uq_relation_kind_unique",
        ),
    )