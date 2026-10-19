"""Unit tests for the pure-Python idempotency helpers.

Seam: ``ksma.shared.idempotency`` for the value-object layer.
We don't exercise the DB-bound ``check_or_register`` here — that needs
a real Postgres session and lives in integration tests.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from ksma.shared.idempotency import (
    IdempotencyContext,
    fingerprint_idempotency_key,
    hash_request_body,
)


class TestFingerprintKey:
    def test_same_key_same_fingerprint(self) -> None:
        a = fingerprint_idempotency_key("abc-123")
        b = fingerprint_idempotency_key("abc-123")
        assert a == b

    def test_different_keys_different_fingerprints(self) -> None:
        a = fingerprint_idempotency_key("abc-123")
        c = fingerprint_idempotency_key("def-456")
        assert a != c

    def test_fingerprint_is_hex_sha256(self) -> None:
        fp = fingerprint_idempotency_key("anything")
        assert len(fp) == 64
        assert all(ch in "0123456789abcdef" for ch in fp)


class TestHashRequestBody:
    def test_dict_canonicalization_is_key_order_independent(self) -> None:
        a = {"a": 1, "b": 2, "c": 3}
        b = {"c": 3, "a": 1, "b": 2}
        assert hash_request_body(a) == hash_request_body(b)

    def test_string_input(self) -> None:
        h = hash_request_body('{"amount": 100}')
        assert len(h) == 64

    def test_bytes_input(self) -> None:
        h = hash_request_body(b'{"amount": 100}')
        assert len(h) == 64

    def test_dict_and_string_of_same_payload_produce_same_hash(self) -> None:
        # The dict is serialised with sort_keys=True before hashing, so it
        # should match the canonical string form.
        payload = {"b": 1, "a": 2}
        string_form = json.dumps(payload, sort_keys=True, default=str)
        assert hash_request_body(payload) == hash_request_body(string_form)

    def test_nested_dict_is_canonicalised(self) -> None:
        a = {"x": {"z": 1, "y": 2}}
        b = {"x": {"y": 2, "z": 1}}
        assert hash_request_body(a) == hash_request_body(b)


class TestIdempotencyContext:
    def test_construction(self) -> None:
        ctx = IdempotencyContext(
            raw_key="key-1",
            fingerprint="fp",
            request_hash="rh",
            action_id="FIN-A05",
            subject_account_id=uuid4(),
            tenant_id=uuid4(),
        )
        assert ctx.raw_key == "key-1"
        assert ctx.action_id == "FIN-A05"
        assert ctx.fingerprint == "fp"
        assert ctx.request_hash == "rh"

    def test_immutable(self) -> None:
        ctx = IdempotencyContext(
            raw_key="k", fingerprint="f", request_hash="r",
            action_id="REG-A02",
            subject_account_id=uuid4(), tenant_id=uuid4(),
        )
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            ctx.action_id = "FIN-A05"  # type: ignore[misc]