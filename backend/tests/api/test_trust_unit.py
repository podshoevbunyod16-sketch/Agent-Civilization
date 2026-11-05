"""Юнит-тесты сервиса доверия: формула репутации, валидация, утверждения (этап 5)."""

from __future__ import annotations

import pytest

from app.services import trust as trust_service


class TestReputationFormula:
    """Репутация — детерминированная функция от вкладов."""

    def test_new_agent_starts_neutral(self):
        # Симметричный приор: 100 * 2.5 / (2.5 + 2.5) = 50
        assert trust_service.compute_reputation(0, 0) == pytest.approx(50.0)

    def test_perfect_record_approaches_100(self):
        assert trust_service.compute_reputation(1000, 0) > 99.0

    def test_all_failures_approaches_zero(self):
        assert trust_service.compute_reputation(0, 1000) < 1.0

    def test_mixed_record(self):
        # 10 положительных, 2 отрицательных → 100*12.5/17
        assert trust_service.compute_reputation(10, -2) == pytest.approx(73.53, abs=0.02)

    def test_single_success_above_neutral(self):
        assert trust_service.compute_reputation(1, 0) == pytest.approx(58.33, abs=0.02)

    def test_negative_input_not_double_counted(self):
        assert trust_service.compute_reputation(-3, 2) == pytest.approx(
            trust_service.compute_reputation(0, 2)
        )

    def test_symmetry_monotonic(self):
        scores = [trust_service.compute_reputation(n, 0) for n in (0, 1, 5, 20)]
        assert scores == sorted(scores)

    def test_bounds(self):
        assert 0.0 <= trust_service.compute_reputation(0, 10000) <= 100.0
        assert 0.0 <= trust_service.compute_reputation(10000, 0) <= 100.0

    def test_source_weights_defined_for_all_sources(self):
        from app.db.models.trust import ReputationSource

        for source in ReputationSource:
            assert source in trust_service.SOURCE_WEIGHTS
            assert trust_service.SOURCE_WEIGHTS[source] > 0


class TestSchemaValidation:
    SCHEMA = {
        "type": "object",
        "required": ["summary", "facts"],
        "properties": {
            "summary": {"type": "string", "minLength": 3, "maxLength": 200},
            "facts": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "kind": {"enum": ["short", "long"]},
        },
    }

    def test_valid_payload(self):
        payload = {"summary": "готово", "facts": ["a"], "confidence": 0.5, "kind": "short"}
        assert trust_service.validate_against_schema(payload, self.SCHEMA) == []

    def test_missing_required(self):
        errors = trust_service.validate_against_schema({"summary": "ok"}, self.SCHEMA)
        assert any("facts" in error and "обязательное" in error for error in errors)

    def test_wrong_type(self):
        errors = trust_service.validate_against_schema(
            {"summary": 42, "facts": []}, self.SCHEMA
        )
        assert any("ожидался string" in error for error in errors)

    def test_array_item_type(self):
        errors = trust_service.validate_against_schema(
            {"summary": "ok", "facts": ["a", 1]}, self.SCHEMA
        )
        assert any("$.facts[1]" in error for error in errors), errors

    def test_number_bounds(self):
        errors = trust_service.validate_against_schema(
            {"summary": "ok", "facts": [], "confidence": 5}, self.SCHEMA
        )
        assert any("maximum" in error for error in errors)

    def test_string_length(self):
        errors = trust_service.validate_against_schema(
            {"summary": "ok", "facts": []}, self.SCHEMA
        )
        assert any("minLength" in error or "длина" in error for error in errors)

    def test_enum(self):
        errors = trust_service.validate_against_schema(
            {"summary": "ok", "facts": [], "kind": "huge"}, self.SCHEMA
        )
        assert any("enum" in error for error in errors)

    def test_integer_accepted_for_number(self):
        schema = {"type": "object", "properties": {"n": {"type": "number"}}}
        assert trust_service.validate_against_schema({"n": 1}, schema) == []

    def test_integer_rejects_float(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        assert trust_service.validate_against_schema({"n": 1.5}, schema) != []
        assert trust_service.validate_against_schema({"n": 2}, schema) == []

    def test_boolean_is_not_number(self):
        schema = {"type": "object", "properties": {"n": {"type": "number"}}}
        assert trust_service.validate_against_schema({"n": True}, schema) != []

    def test_payload_not_object(self):
        errors = trust_service.validate_against_schema("строка", {"type": "object"})
        assert errors and "object" in errors[0]


class TestAssertions:
    def test_eq_passes(self):
        outcome = trust_service.evaluate_assertions(
            {"a": {"b": 2}}, [{"path": "a.b", "op": "eq", "value": 2}]
        )
        assert outcome["passed"] is True

    def test_all_must_pass(self):
        outcome = trust_service.evaluate_assertions(
            {"a": 1}, [{"path": "a", "op": "eq", "value": 1}, {"path": "z", "op": "exists"}]
        )
        assert outcome["passed"] is False
        assert [item["ok"] for item in outcome["results"]] == [True, False]

    def test_empty_assertions_do_not_pass(self):
        assert trust_service.evaluate_assertions({"a": 1}, [])["passed"] is False

    def test_comparisons(self):
        payload = {"n": 5}
        cases = [
            ("gt", 4, True),
            ("gt", 5, False),
            ("gte", 5, True),
            ("lt", 6, True),
            ("lte", 4, False),
            ("ne", 4, True),
        ]
        for op, value, expected in cases:
            outcome = trust_service.evaluate_assertions(
                payload, [{"path": "n", "op": op, "value": value}]
            )
            assert outcome["results"][0]["ok"] is expected, op

    def test_contains_and_length(self):
        payload = {"items": [1, 2, 3], "text": "агент"}
        assert trust_service.evaluate_assertions(
            payload, [{"path": "items", "op": "contains", "value": 2}]
        )["passed"]
        assert trust_service.evaluate_assertions(
            payload, [{"path": "items", "op": "len_gte", "value": 3}]
        )["passed"]
        assert trust_service.evaluate_assertions(
            payload, [{"path": "items", "op": "len_lte", "value": 2}]
        )["passed"] is False
        assert trust_service.evaluate_assertions(
            payload, [{"path": "text", "op": "len_gte", "value": 5}]
        )["passed"]

    def test_dunder_path_rejected(self):
        # Атрибутов не существует по определению, но dunder режем явно
        for path in ("a.__class__", "__init__.x", "a.b.__globals__"):
            outcome = trust_service.evaluate_assertions(
                {"a": {"__class__": 1, "b": 2}}, [{"path": path, "op": "exists"}]
            )
            assert outcome["results"][0]["ok"] is False, path

    def test_malformed_path_rejected(self):
        outcome = trust_service.evaluate_assertions(
            {"a": 1}, [{"path": "a b", "op": "exists"}]
        )
        assert outcome["results"][0]["ok"] is False
        assert "путь" in outcome["results"][0]["error"]

    def test_unknown_operator(self):
        outcome = trust_service.evaluate_assertions(
            {"a": 1}, [{"path": "a", "op": "sql", "value": 1}]
        )
        assert outcome["results"][0]["ok"] is False
        assert "оператор" in outcome["results"][0]["error"]

    def test_type_mismatch_does_not_raise(self):
        outcome = trust_service.evaluate_assertions(
            {"a": "текст"}, [{"path": "a", "op": "gt", "value": 1}]
        )
        assert outcome["results"][0]["ok"] is False
