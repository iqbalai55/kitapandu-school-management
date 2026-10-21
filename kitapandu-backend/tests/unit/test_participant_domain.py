"""Unit tests for ``ksma.participant.domain`` — the value-object layer.

Seam
----
``ksma.participant.domain`` (``src/ksma/participant/domain/__init__.py``).
Public surface pinned here:

- ``FinancialScope`` — the JSONB-shaped payer scope from **Q33(a)** with two
  wire forms: the shorthand ``{"all_approved": true}`` (default for new payer
  relations, deliberately conservative) and an explicit
  ``{"invoices": [uuid, ...]}`` allow-list.
- ``FinancialScope.to_json`` (:107) — the shorthand form must emit the literal
  ``True`` flag, never a falsy stand-in.
- ``FinancialScope.from_json`` (:112, :115, :116) — absent payload yields the
  conservative default, the ``all_approved`` flag is honoured, and the invoice
  allow-list is parsed into real ``UUID`` values.
- ``FinancialScope.covers_invoice`` (:120) — the shorthand scope covers *every*
  approved invoice; the allow-list covers only the listed ones.
- ``DuplicateSuspect`` — the search-time (never stored, **V02**) duplicate
  marker.

Tests are pure-Python: no DB, no Docker, no network. Every assertion is on the
typed round-trip shape, not on ``repr`` or truthiness.

What is NOT asserted here (lives in the integration tier):
- RLS on the JSONB column, Alembic round-trips, PostgreSQL JSONB coercion.
- The duplicate-detection heuristic itself (application/infrastructure tier).
"""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from ksma.participant.domain import DuplicateSuspect, FinancialScope

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
INVOICE_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INVOICE_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INVOICE_C = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

# The non-trivial JSONB shape from docs/specs/002-participant.md — unit IDs
# are not part of the wire form, but participant-scoped invoice UUIDs and the
# inheritance flag are.
ALLOW_LIST_JSON: dict[str, object] = {"invoices": [str(INVOICE_A), str(INVOICE_B)]}
SHORTHAND_JSON: dict[str, object] = {"all_approved": True}


# ===========================================================================
# to_json — :107
# ===========================================================================


class TestFinancialScopeToJson:
    """``FinancialScope.to_json`` — two wire forms, Q33(a)."""

    def test_shorthand_form_emits_the_literal_true_flag(self) -> None:
        """Kills :107 ``True -> False``.

        The shortened wire form is only useful if the consumer can branch on
        the flag; emitting ``False`` would silently downgrade a blanket payer
        scope into an empty allow-list.
        """
        assert FinancialScope(all_approved=True).to_json() == {"all_approved": True}

    def test_shorthand_form_carries_no_invoice_keys(self) -> None:
        payload = FinancialScope(all_approved=True).to_json()
        assert "invoices" not in payload
        assert payload["all_approved"] is True

    def test_allow_list_form_stringifies_every_invoice_uuid(self) -> None:
        payload = FinancialScope(invoices=(INVOICE_A, INVOICE_B)).to_json()
        assert payload == {"invoices": [str(INVOICE_A), str(INVOICE_B)]}
        assert all(isinstance(x, str) for x in payload["invoices"])

    def test_empty_allow_list_form_is_still_the_invoice_key(self) -> None:
        assert FinancialScope().to_json() == {"invoices": []}

    def test_allow_list_form_is_json_serialisable(self) -> None:
        payload = FinancialScope(invoices=(INVOICE_A,)).to_json()
        assert json.loads(json.dumps(payload)) == payload


# ===========================================================================
# from_json — :112, :115, :116
# ===========================================================================


class TestFinancialScopeFromJson:
    """``FinancialScope.from_json`` — the JSONB decode side of Q33(a)."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(None, id="null_jsonb_column"),
            pytest.param({}, id="empty_jsonb_object"),
        ],
    )
    def test_absent_payload_yields_the_conservative_default(
        self, payload: dict[str, object] | None
    ) -> None:
        """Kills :112 ``delete not`` (``if data:`` inverted).

        A NULL / empty JSONB column must decode to the *conservative* default
        (no blanket approval) rather than raising ``AttributeError`` on
        ``.get()``.
        """
        scope = FinancialScope.from_json(payload)
        assert scope.all_approved is False
        assert scope.invoices == ()

    def test_all_approved_flag_is_honoured(self) -> None:
        """Kills :115 ``True -> False``.

        A blanket payer scope must decode back as blanket — otherwise every
        inherited payer link would lose its finance access on reload.
        """
        scope = FinancialScope.from_json(dict(SHORTHAND_JSON))
        assert scope.all_approved is True
        assert scope.invoices == ()

    def test_invoice_allow_list_is_parsed_into_uuids(self) -> None:
        """Kills :116 ``or -> and``.

        When ``invoices`` is present it is the source of truth; the ``or ()``
        fallback only applies when the key is missing/None. With the mutant,
        a populated allow-list collapses to the empty tuple and a payer
        silently loses every scoped invoice.
        """
        scope = FinancialScope.from_json(dict(ALLOW_LIST_JSON))
        assert scope.invoices == (INVOICE_A, INVOICE_B)
        assert scope.all_approved is False

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"invoices": []}, id="empty_list"),
            pytest.param({"invoices": None}, id="null_list"),
            pytest.param({"invoices": ()}, id="tuple_list"),
            pytest.param({"participant_id": str(TENANT_ID)}, id="list_key_absent"),
        ],
    )
    def test_missing_or_empty_invoice_key_decodes_to_no_invoices(
        self, payload: dict[str, object]
    ) -> None:
        """Boundary pair for :116 — the ``or ()`` fallback side."""
        scope = FinancialScope.from_json(payload)
        assert scope.invoices == ()
        assert scope.all_approved is False

    def test_all_approved_false_falls_through_to_the_allow_list(self) -> None:
        """``all_approved`` is only truthy-gated; an explicit ``False`` keeps
        the sibling invoice allow-list reachable (Q33(a) two-form shape)."""
        payload: dict[str, object] = {"all_approved": False, **ALLOW_LIST_JSON}
        scope = FinancialScope.from_json(payload)
        assert scope.all_approved is False
        assert scope.invoices == (INVOICE_A, INVOICE_B)


# ===========================================================================
# Round-trip — ticket acceptance criterion
# ===========================================================================


class TestFinancialScopeRoundTrip:
    """``to_json`` -> ``from_json`` must be lossless on the typed fields."""

    @pytest.mark.parametrize(
        ("original", "expected_json"),
        [
            pytest.param(
                FinancialScope(all_approved=True),
                {"all_approved": True},
                id="shorthand",
            ),
            pytest.param(
                FinancialScope(invoices=(INVOICE_A, INVOICE_B, INVOICE_C)),
                {"invoices": [str(INVOICE_A), str(INVOICE_B), str(INVOICE_C)]},
                id="allow_list",
            ),
            pytest.param(
                FinancialScope(),
                {"invoices": []},
                id="default_conservative",
            ),
        ],
    )
    def test_round_trip_preserves_each_typed_field(
        self, original: FinancialScope, expected_json: dict[str, object]
    ) -> None:
        wire = json.loads(json.dumps(original.to_json()))
        assert wire == expected_json
        restored = FinancialScope.from_json(wire)
        assert restored.all_approved is original.all_approved
        assert restored.invoices == original.invoices
        assert restored.to_json() == expected_json


# ===========================================================================
# covers_invoice — :120
# ===========================================================================


class TestFinancialScopeCoversInvoice:
    """``FinancialScope.covers_invoice`` — the finance gate (Q33(a))."""

    def test_blanket_scope_covers_any_invoice(self) -> None:
        """Kills :120 ``True -> False``.

        ``all_approved=True`` is the blanket payer grant; denying an invoice
        under it would revoke inherited finance access for every guardian.
        """
        assert FinancialScope(all_approved=True).covers_invoice(INVOICE_C) is True
        assert FinancialScope(all_approved=True).covers_invoice(uuid4()) is True

    @pytest.mark.parametrize(
        ("invoice_id", "covered"),
        [
            pytest.param(INVOICE_A, True, id="listed_invoice"),
            pytest.param(INVOICE_B, True, id="listed_invoice_2"),
            pytest.param(INVOICE_C, False, id="unlisted_invoice"),
        ],
    )
    def test_allow_list_scope_covers_only_listed_invoices(
        self, invoice_id: UUID, covered: bool
    ) -> None:
        """Boundary pair for :120 — the membership side of the guard."""
        scope = FinancialScope(invoices=(INVOICE_A, INVOICE_B))
        assert scope.covers_invoice(invoice_id) is covered

    def test_default_scope_covers_nothing(self) -> None:
        assert FinancialScope().covers_invoice(INVOICE_A) is False


# ===========================================================================
# DuplicateSuspect — search-time marker (V02)
# ===========================================================================


class TestDuplicateSuspect:
    """``DuplicateSuspect`` is computed at search time, never stored (V02)."""

    def test_matched_ids_are_a_tuple_of_uuids(self) -> None:
        suspect = DuplicateSuspect(
            participant_id=INVOICE_A,
            matched_participant_ids=(INVOICE_B, INVOICE_C),
            reason="same_name",
        )
        assert suspect.participant_id == INVOICE_A
        assert suspect.matched_participant_ids == (INVOICE_B, INVOICE_C)
        assert suspect.reason == "same_name"

    def test_empty_match_set_is_representable(self) -> None:
        suspect = DuplicateSuspect(
            participant_id=INVOICE_A,
            matched_participant_ids=(),
            reason="same_contact_email",
        )
        assert suspect.matched_participant_ids == ()