"""API-тесты доверия: проверки результата, вердикты, репутация, отзывы, апелляции (этап 5).

Тесты идут по реальному пути: агент → версия → задание → узел забирает
задание → узел отчитывается результатом → проверки результата. Никаких
подставных вердиктов: всё считается сервисом из фактических данных.
"""

from __future__ import annotations

import uuid

from tests.conftest import create_published_agent, register_node_client, version_payload

PY_OK = (
    "def run(payload):\n"
    "    return {'summary': payload.get('topic', 'тема'), 'facts': ['f1', 'f2'],"
    " 'confidence': 0.9}\n"
)


async def _agent(client, user, *, slug: str | None = None, **extra) -> dict:
    slug = slug or f"agent-{uuid.uuid4().hex[:8]}"
    agent = await create_published_agent(client, user, name=slug, slug=slug, **extra)
    return agent


async def _set_capabilities(client, user, agent_id: str, keys: list[str], level: int = 3) -> dict:
    response = await client.put(
        f"/api/v1/agents/{agent_id}/capabilities",
        json={"capabilities": [{"capability_key": key, "declared_level": level} for key in keys]},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _finished_task(client, user, agent: dict, *, output: dict | None = None) -> dict:
    """Полный путь: задание → claim узлом → отчёт с результатом."""
    payload = output if output is not None else {
        "summary": "факт",
        "facts": ["один", "два"],
        "confidence": 0.9,
    }
    submitted = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {"topic": "рынок"}},
        headers=user.headers,
    )
    assert submitted.status_code == 201, submitted.text
    task = submitted.json()

    _node, node_client = await register_node_client(client, user)
    claimed = await node_client.request(
        "POST", "/api/v1/nodes/me/tasks/claim", json_body={"limit": 5}
    )
    assert claimed.status_code == 200, claimed.text
    assignments = claimed.json()["assignments"]
    assert assignments, "узел не получил задание"
    assignment = next(
        (item for item in assignments if item["task_id"] == task["id"]), assignments[0]
    )
    reported = await node_client.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{assignment['task_id']}/result",
        json_body={
            "lease_token": assignment["lease_token"],
            "success": True,
            "output": payload,
            "metrics": {"duration_ms": 12},
        },
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["status"] == "succeeded"
    detail = await client.get(f"/api/v1/tasks/{task['id']}", headers=user.headers)
    assert detail.status_code == 200, detail.text
    return detail.json()


CHECK_CONFIG = {
    "output_schema": {
        "type": "object",
        "required": ["summary", "facts"],
        "properties": {
            "summary": {"type": "string"},
            "facts": {"type": "array", "items": {"type": "string"}},
        },
    },
    "assertions": [{"path": "facts", "op": "len_gte", "value": 2}],
}


class TestAutomaticVerification:
    async def test_schema_and_assertions_pass_verify_result(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)

        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json=CHECK_CONFIG,
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        methods = sorted(check["method"] for check in body["checks"])
        assert methods == ["assertions", "schema"]
        assert all(check["status"] == "passed" for check in body["checks"])
        assert body["verdict"]["verdict"] == "verified"
        assert body["verdict"]["checks_passed"] == 2
        assert body["verdict"]["checks_total"] == 2

    async def test_failed_schema_rejects_result(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent, output={"summary": 123})
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"output_schema": CHECK_CONFIG["output_schema"]},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["checks"][0]["status"] == "failed"
        assert body["checks"][0]["details"]["errors"]
        assert body["verdict"]["verdict"] == "rejected"

    async def test_assertions_contradict_schema_gives_disputed(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent, output={"summary": "ok", "facts": []})
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json=CHECK_CONFIG,
            headers=user.headers,
        )
        body = response.json()
        statuses = {check["method"]: check["status"] for check in body["checks"]}
        assert statuses["schema"] == "passed"
        assert statuses["assertions"] == "failed"
        # Провал веса 1.0 при пройденном весе 1.0 → противоречие, а не отказ
        assert body["verdict"]["verdict"] == "disputed"

    async def test_test_code_runs_in_sandbox(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        test_code = (
            "def run(payload):\n"
            "    output = payload['output']\n"
            "    return {'passed': output.get('confidence', 0) > 0.5}\n"
        )
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"test_code": test_code},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        check = response.json()["checks"][0]
        assert check["method"] == "auto_test"
        assert check["status"] == "passed"
        assert check["details"]["output"]["passed"] is True

    async def test_test_code_can_fail_result(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"test_code": "def run(payload):\n    return {'passed': False}\n"},
            headers=user.headers,
        )
        check = response.json()["checks"][0]
        assert check["status"] == "failed"
        assert check["passed"] is False
        assert response.json()["verdict"]["verdict"] == "rejected"

    async def test_cannot_verify_failed_task(self, client, user):
        agent = await _agent(client, user)
        submitted = await client.post(
            "/api/v1/tasks",
            json={"agent_id": agent["id"], "input_payload": {}},
            headers=user.headers,
        )
        task = submitted.json()
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json=CHECK_CONFIG,
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "task_not_verifiable"

    async def test_foreign_task_forbidden(self, client, user, other_user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json=CHECK_CONFIG,
            headers=other_user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "verification_forbidden"

    async def test_repeat_check_is_idempotent(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        first = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        second = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        assert first.status_code == 201 and second.status_code == 201
        # Повторная проверка не создаёт дублей
        assert len(second.json()["checks"]) == 2

    async def test_list_checks_and_read_verdict(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        listed = await client.get(
            f"/api/v1/tasks/{task['id']}/verifications", headers=user.headers
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 2
        verdict = await client.get(f"/api/v1/tasks/{task['id']}/verdict", headers=user.headers)
        assert verdict.status_code == 200
        assert verdict.json()["verdict"] == "verified"

    async def test_verdict_not_found_before_checks(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.get(f"/api/v1/tasks/{task['id']}/verdict", headers=user.headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "verdict_not_found"


class TestHumanAndPeerVerification:
    async def test_owner_confirms_result(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/human",
            json={"passed": True, "comment": "всё по делу"},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["method"] == "human"
        assert response.json()["weight"] == 3.0
        verdict = await client.get(f"/api/v1/tasks/{task['id']}/verdict", headers=user.headers)
        assert verdict.json()["verdict"] == "verified"
        assert verdict.json()["independent_checks"] == 1

    async def test_foreign_user_cannot_confirm(self, client, user, other_user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/human",
            json={"passed": True},
            headers=other_user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "verification_forbidden"

    async def test_duplicate_human_check_conflicts(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/human",
            json={"passed": True},
            headers=user.headers,
        )
        again = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/human",
            json={"passed": False},
            headers=user.headers,
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "verification_exists"

    async def test_admin_can_confirm_foreign_task(self, client, user, admin_user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/human",
            json={"passed": False, "comment": "не подтверждено"},
            headers=admin_user.headers,
        )
        assert response.status_code == 201
        assert response.json()["passed"] is False

    async def test_peer_verification_by_independent_agent(self, client, user, other_user):
        worker = await _agent(client, user)
        task = await _finished_task(client, user, worker)

        verifier = await _agent(client, other_user)
        version = await client.post(
            f"/api/v1/agents/{verifier['id']}/versions",
            json=version_payload(
                publish=True,
                entrypoint_type="python",
                entrypoint_config={
                    "code": (
                        "def run(payload):\n"
                        "    output = payload.get('output') or {}\n"
                        "    ok = bool(output.get('facts'))\n"
                        "    return {'verdict': ok, 'score': 0.8 if ok else 0.0,"
                        " 'comment': 'проверено'}\n"
                    )
                },
                resource_limits={"max_wall_seconds": 10, "max_memory_mb": 128},
            ),
            headers=other_user.headers,
        )
        assert version.status_code == 201, version.text

        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/peer",
            json={"verifier_agent_id": verifier["id"]},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["method"] == "peer_agent"
        assert body["status"] == "passed"
        assert body["weight"] == 2.5
        assert body["details"]["answer"]["verdict"] is True

        verdict = await client.get(f"/api/v1/tasks/{task['id']}/verdict", headers=user.headers)
        assert verdict.json()["verdict"] == "verified"

    async def test_agent_cannot_verify_itself(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/peer",
            json={"verifier_agent_id": agent["id"]},
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "self_verification_forbidden"

    async def test_same_owner_peer_is_not_independent(self, client, user):
        worker = await _agent(client, user)
        task = await _finished_task(client, user, worker)
        sibling = await _agent(client, user)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/peer",
            json={"verifier_agent_id": sibling["id"]},
            headers=user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "verifier_not_independent"

    async def test_peer_duplicate_conflicts(self, client, user, other_user):
        worker = await _agent(client, user)
        task = await _finished_task(client, user, worker)
        verifier = await _agent(client, other_user)
        await client.post(
            f"/api/v1/agents/{verifier['id']}/versions",
            json=version_payload(
                publish=True,
                entrypoint_type="python",
                entrypoint_config={"code": "def run(payload):\n    return {'verdict': True}\n"},
                resource_limits={"max_wall_seconds": 10, "max_memory_mb": 128},
            ),
            headers=other_user.headers,
        )
        first = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/peer",
            json={"verifier_agent_id": verifier["id"]},
            headers=user.headers,
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/peer",
            json={"verifier_agent_id": verifier["id"]},
            headers=user.headers,
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "verification_exists"


class TestReputation:
    async def test_successful_task_adds_reputation_event(self, client, user):
        agent = await _agent(client, user)
        await _finished_task(client, user, agent)
        breakdown = await client.get(
            f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers
        )
        assert breakdown.status_code == 200, breakdown.text
        body = breakdown.json()
        assert body["by_source"]["task_success"]["count"] == 1
        assert body["positive"] > 0
        assert body["formula_score"] == body["reputation_score"]

    async def test_verified_result_adds_stronger_event(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        before = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()["reputation_score"]
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        after = await client.get(
            f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers
        )
        body = after.json()
        assert body["by_source"]["verification_passed"]["count"] == 1
        assert body["reputation_score"] > before
        assert body["verified_results"] == 1

    async def test_rejected_result_destroy_reputation(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent, output={"summary": 1})
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"output_schema": CHECK_CONFIG["output_schema"]},
            headers=user.headers,
        )
        body = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert body["by_source"]["verification_failed"]["count"] == 1
        assert body["negative"] > 0
        # Провал весит больше успеха того же задания
        assert body["reputation_score"] < 50

    async def test_reputation_is_idempotent_per_task(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        for _ in range(3):
            await client.post(
                f"/api/v1/tasks/{task['id']}/verifications",
                json=CHECK_CONFIG,
                headers=user.headers,
            )
        body = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        # Одна проверка + один вердикт, повторы не накручивают рейтинг
        assert body["by_source"]["verification_passed"]["count"] == 1

    async def test_reputation_unknown_agent(self, client, user):
        response = await client.get(
            f"/api/v1/agents/{uuid.uuid4()}/reputation", headers=user.headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "agent_not_found"

    async def test_reputation_requires_auth(self, client, user):
        agent = await _agent(client, user)
        response = await client.get(f"/api/v1/agents/{agent['id']}/reputation")
        assert response.status_code == 401


class TestReviews:
    async def test_owner_review_affects_reputation(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        before = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()["reputation_score"]
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/review",
            json={"rating": 5, "comment": "быстро и точно", "confirm_result": True},
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["rating"] == 5
        assert body["confirmed_result"] is True

        rating = (
            await client.get(f"/api/v1/agents/{agent['id']}/rating", headers=user.headers)
        ).json()
        assert rating["reviews"] == 1
        assert rating["average_rating"] == 5.0
        assert rating["confirmed_results"] == 1
        assert rating["distribution"]["5"] == 1

        reputation = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert reputation["by_source"]["review"]["count"] == 1
        assert reputation["reputation_score"] > before

    async def test_low_rating_lowers_reputation(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        before = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()["reputation_score"]
        await client.post(
            f"/api/v1/tasks/{task['id']}/review", json={"rating": 1}, headers=user.headers
        )
        after = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()["reputation_score"]
        assert after < before

    async def test_neutral_rating_no_event(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/review", json={"rating": 3}, headers=user.headers
        )
        body = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert "review" not in body["by_source"]

    async def test_foreign_user_cannot_review(self, client, user, other_user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        response = await client.post(
            f"/api/v1/tasks/{task['id']}/review",
            json={"rating": 5},
            headers=other_user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "review_forbidden"

    async def test_single_review_per_task(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/review", json={"rating": 5}, headers=user.headers
        )
        again = await client.post(
            f"/api/v1/tasks/{task['id']}/review", json={"rating": 1}, headers=user.headers
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "review_exists"

    async def test_review_validation(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        for rating in (0, 6):
            response = await client.post(
                f"/api/v1/tasks/{task['id']}/review",
                json={"rating": rating},
                headers=user.headers,
            )
            assert response.status_code == 422


class TestCapabilities:
    async def test_declared_vs_verified_levels(self, client, user):
        agent = await _agent(client, user)
        await _set_capabilities(client, user, agent["id"], ["research.web_search"])
        trust = await client.get(f"/api/v1/agents/{agent['id']}/trust", headers=user.headers)
        assert trust.status_code == 200, trust.text
        capability = trust.json()["capabilities"][0]
        assert capability["declared_level"] == 3
        assert capability["verified_level"] is None
        assert capability["trust_level"] == "declared"

    async def test_capabilities_verified_only_after_checks(self, client, user):
        agent = await _agent(client, user)
        await _set_capabilities(client, user, agent["id"], ["research.web_search"])
        for _ in range(2):
            task = await _finished_task(client, user, agent)
            response = await client.post(
                f"/api/v1/tasks/{task['id']}/verifications",
                json=CHECK_CONFIG,
                headers=user.headers,
            )
            assert response.status_code == 201, response.text
        trust = (
            await client.get(f"/api/v1/agents/{agent['id']}/trust", headers=user.headers)
        ).json()
        capability = trust["capabilities"][0]
        assert capability["verified_level"] == 3
        assert capability["trust_level"] == "verified"
        assert capability["verified_at"] is not None
        assert trust["verdicts"]["verified"] == 2

    async def test_single_verification_not_enough(self, client, user):
        agent = await _agent(client, user)
        await _set_capabilities(client, user, agent["id"], ["research.web_search"])
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        trust = (
            await client.get(f"/api/v1/agents/{agent['id']}/trust", headers=user.headers)
        ).json()
        assert trust["capabilities"][0]["verified_level"] is None

    async def test_low_verification_ratio_unverifies(self, client, user):
        agent = await _agent(client, user)
        await _set_capabilities(client, user, agent["id"], ["research.web_search"])
        # 2 подтверждённых
        for _ in range(2):
            good = await _finished_task(client, user, agent)
            await client.post(
                f"/api/v1/tasks/{good['id']}/verifications",
                json=CHECK_CONFIG,
                headers=user.headers,
            )
        # 2 опровергнутых
        for _ in range(2):
            bad = await _finished_task(client, user, agent, output={"summary": 1})
            await client.post(
                f"/api/v1/tasks/{bad['id']}/verifications",
                json={"output_schema": CHECK_CONFIG["output_schema"]},
                headers=user.headers,
            )
        trust = (
            await client.get(f"/api/v1/agents/{agent['id']}/trust", headers=user.headers)
        ).json()
        assert trust["verdicts"]["verified"] == 2
        assert trust["verdicts"]["rejected"] == 2
        # Доля подтверждений 50% < 80% → возможность не подтверждена
        assert trust["capabilities"][0]["verified_level"] is None

    async def test_refresh_capabilities_endpoint(self, client, user):
        agent = await _agent(client, user)
        await _set_capabilities(client, user, agent["id"], ["research.web_search"])
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/verification/refresh", headers=user.headers
        )
        assert response.status_code == 200
        assert response.json()[0]["verified_level"] is None

    async def test_foreign_user_cannot_refresh(self, client, user, other_user):
        agent = await _agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/verification/refresh", headers=other_user.headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "capability_forbidden"


class TestAnomalies:
    async def test_result_reuse_detected(self, client, user):
        agent = await _agent(client, user)
        same = {"summary": "одинаково", "facts": ["a", "b"]}
        for _ in range(2):
            await _finished_task(client, user, agent, output=same)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=user.headers
        )
        assert response.status_code == 200, response.text
        found = response.json()["found"]
        assert any(item["anomaly_type"] == "result_reuse" for item in found)

    async def test_clean_agent_has_no_anomalies(self, client, user):
        agent = await _agent(client, user)
        await _finished_task(client, user, agent, output={"summary": "a"})
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=user.headers
        )
        assert response.status_code == 200
        assert response.json()["found"] == []

    async def test_scan_is_idempotent(self, client, user):
        agent = await _agent(client, user)
        same = {"summary": "одинаково"}
        for _ in range(2):
            await _finished_task(client, user, agent, output=same)
        await client.post(f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=user.headers)
        second = await client.post(
            f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=user.headers
        )
        assert second.json()["found"] == []

    async def test_anomaly_penalty_and_dismissal(self, client, user, admin_user):
        agent = await _agent(client, user)
        same = {"summary": "одинаково"}
        for _ in range(2):
            await _finished_task(client, user, agent, output=same)
        scan = await client.post(
            f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=user.headers
        )
        anomaly = scan.json()["found"][0]
        assert anomaly["status"] == "open"

        penalized = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert "anomaly_penalty" in penalized["by_source"]

        owner_list = await client.get(
            f"/api/v1/anomalies?agent_id={agent['id']}", headers=user.headers
        )
        assert owner_list.status_code == 200
        assert len(owner_list.json()) == 1

        dismissed = await client.post(
            f"/api/v1/anomalies/{anomaly['id']}/resolve",
            json={"confirm": False},
            headers=admin_user.headers,
        )
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["status"] == "dismissed"

        cleared = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert "anomaly_penalty" not in cleared["by_source"]
        assert cleared["reputation_score"] > penalized["reputation_score"]

    async def test_only_admin_resolves_anomaly(self, client, user):
        agent = await _agent(client, user)
        same = {"summary": "одинаково"}
        for _ in range(2):
            await _finished_task(client, user, agent, output=same)
        scan = await client.post(
            f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=user.headers
        )
        anomaly_id = scan.json()["found"][0]["id"]
        response = await client.post(
            f"/api/v1/anomalies/{anomaly_id}/resolve",
            json={"confirm": True},
            headers=user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "anomaly_forbidden"

    async def test_foreign_scan_forbidden(self, client, user, other_user):
        agent = await _agent(client, user)
        response = await client.post(
            f"/api/v1/agents/{agent['id']}/anomalies/scan", headers=other_user.headers
        )
        assert response.status_code == 403

    async def test_global_anomaly_list_admin_only(self, client, user, admin_user):
        response = await client.get("/api/v1/anomalies", headers=user.headers)
        assert response.status_code == 403
        allowed = await client.get("/api/v1/anomalies", headers=admin_user.headers)
        assert allowed.status_code == 200


class TestAppeals:
    async def _rejected_setup(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent, output={"summary": 1})
        verification = await client.post(
            f"/api/v1/tasks/{task['id']}/verifications",
            json={"output_schema": CHECK_CONFIG["output_schema"]},
            headers=user.headers,
        )
        body = verification.json()
        return agent, task, body["checks"][0], body

    async def test_owner_opens_appeal(self, client, user):
        agent, task, check, _ = await self._rejected_setup(client, user)
        response = await client.post(
            "/api/v1/appeals",
            json={
                "agent_id": agent["id"],
                "task_id": task["id"],
                "verification_id": check["id"],
                "reason": "Схема проверялась неверно",
                "evidence": {"note": "поле summary — число по контракту"},
            },
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "open"

    async def test_appeal_requires_target_and_reason(self, client, user):
        agent = await _agent(client, user)
        no_target = await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "reason": "несправедливо"},
            headers=user.headers,
        )
        assert no_target.status_code == 422
        assert no_target.json()["error"]["code"] == "appeal_target_required"
        short_reason = await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "task_id": str(uuid.uuid4()), "reason": "x"},
            headers=user.headers,
        )
        assert short_reason.status_code == 422

    async def test_foreign_user_cannot_appeal(self, client, user, other_user):
        agent, task, _, _ = await self._rejected_setup(client, user)
        response = await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "task_id": task["id"], "reason": "хочу отменить"},
            headers=other_user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "appeal_forbidden"

    async def test_accepted_appeal_reverses_reputation(self, client, user, admin_user):
        agent, task, check, _ = await self._rejected_setup(client, user)
        before = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert before["by_source"]["verification_failed"]["count"] == 1

        appeal = await client.post(
            "/api/v1/appeals",
            json={
                "agent_id": agent["id"],
                "task_id": task["id"],
                "verification_id": check["id"],
                "reason": "Проверка ошибочна: контракт допускает число",
            },
            headers=user.headers,
        )
        resolved = await client.post(
            f"/api/v1/appeals/{appeal.json()['id']}/resolve",
            json={"accept": True, "resolution": "Проверка отозвана"},
            headers=admin_user.headers,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "accepted"

        after = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert "verification_failed" not in after["by_source"]
        assert after["by_source"]["appeal"]["count"] == 1
        assert after["reputation_score"] > before["reputation_score"]

        # Проверка отозвана, вердикт пересчитан
        checks = await client.get(
            f"/api/v1/tasks/{task['id']}/verifications", headers=user.headers
        )
        assert checks.json()[0]["status"] == "skipped"
        verdict = await client.get(f"/api/v1/tasks/{task['id']}/verdict", headers=user.headers)
        assert verdict.json()["verdict"] == "unverified"

    async def test_rejected_appeal_penalizes(self, client, user, admin_user):
        agent, task, _, _ = await self._rejected_setup(client, user)
        appeal = await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "task_id": task["id"], "reason": "не согласен"},
            headers=user.headers,
        )
        resolved = await client.post(
            f"/api/v1/appeals/{appeal.json()['id']}/resolve",
            json={"accept": False, "resolution": "Проверка корректна"},
            headers=admin_user.headers,
        )
        assert resolved.json()["status"] == "rejected"
        reputation = (
            await client.get(f"/api/v1/agents/{agent['id']}/reputation", headers=user.headers)
        ).json()
        assert reputation["by_source"]["appeal"]["delta"] < 0

    async def test_only_admin_resolves_appeal(self, client, user, other_user):
        agent, task, _, _ = await self._rejected_setup(client, user)
        appeal = await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "task_id": task["id"], "reason": "не согласен"},
            headers=user.headers,
        )
        response = await client.post(
            f"/api/v1/appeals/{appeal.json()['id']}/resolve",
            json={"accept": True},
            headers=other_user.headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "appeal_forbidden"

    async def test_double_resolution_conflicts(self, client, user, admin_user):
        agent, task, _, _ = await self._rejected_setup(client, user)
        appeal = await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "task_id": task["id"], "reason": "не согласен"},
            headers=user.headers,
        )
        url = f"/api/v1/appeals/{appeal.json()['id']}/resolve"
        await client.post(url, json={"accept": True}, headers=admin_user.headers)
        second = await client.post(url, json={"accept": False}, headers=admin_user.headers)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "appeal_closed"

    async def test_duplicate_open_appeal_conflicts(self, client, user):
        agent, task, _, _ = await self._rejected_setup(client, user)
        payload = {
            "agent_id": agent["id"],
            "task_id": task["id"],
            "reason": "первая апелляция",
        }
        first = await client.post("/api/v1/appeals", json=payload, headers=user.headers)
        assert first.status_code == 201
        second = await client.post("/api/v1/appeals", json=payload, headers=user.headers)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "appeal_exists"

    async def test_owner_sees_only_own_appeals(self, client, user, other_user, admin_user):
        agent, task, _, _ = await self._rejected_setup(client, user)
        await client.post(
            "/api/v1/appeals",
            json={"agent_id": agent["id"], "task_id": task["id"], "reason": "моя апелляция"},
            headers=user.headers,
        )
        own = await client.get("/api/v1/appeals", headers=user.headers)
        assert own.status_code == 200 and len(own.json()) == 1
        foreign = await client.get("/api/v1/appeals", headers=other_user.headers)
        assert foreign.json() == []
        admin_view = await client.get("/api/v1/appeals", headers=admin_user.headers)
        assert len(admin_view.json()) == 1
        filtered = await client.get(
            "/api/v1/appeals?status=open", headers=admin_user.headers
        )
        assert len(filtered.json()) == 1

    async def test_resolve_unknown_appeal(self, client, admin_user):
        response = await client.post(
            f"/api/v1/appeals/{uuid.uuid4()}/resolve",
            json={"accept": True},
            headers=admin_user.headers,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "appeal_not_found"


class TestTrustOverview:
    async def test_overview_counts(self, client, user):
        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        response = await client.get("/api/v1/trust/overview", headers=user.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["verdicts"]["verified"] == 1
        assert body["checks"]["schema"]["passed"] == 1
        assert body["checks"]["assertions"]["passed"] == 1
        assert body["total_agents"] >= 1

    async def test_sources_catalog(self, client):
        response = await client.get("/api/v1/trust/sources")
        assert response.status_code == 200
        sources = {item["source"] for item in response.json()}
        assert "task_success" in sources
        assert "appeal" in sources


class TestAuditTrail:
    async def test_critical_actions_are_audited(self, client, user, session):
        from sqlalchemy import select

        from app.db.models.audit import AuditLog

        agent = await _agent(client, user)
        task = await _finished_task(client, user, agent)
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications", json=CHECK_CONFIG, headers=user.headers
        )
        await client.post(
            f"/api/v1/tasks/{task['id']}/verifications/human",
            json={"passed": True},
            headers=user.headers,
        )
        await client.post(
            f"/api/v1/tasks/{task['id']}/review", json={"rating": 4}, headers=user.headers
        )
        actions = set(
            (await session.execute(select(AuditLog.action))).scalars().all()
        )
        assert {"trust.verify.auto", "trust.verify.human", "trust.review.create"} <= actions
