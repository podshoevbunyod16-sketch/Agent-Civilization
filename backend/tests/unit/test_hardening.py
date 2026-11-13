"""Регрессионные тесты production-hardening.

* X-Forwarded-For не доверяется без доверенного прокси
* anti-replay fail-closed в production (и при ANTI_REPLAY_FAIL_CLOSED=true)
* локальный резервный rate limiter при недоступности Redis
* WS-тикет — отдельный тип токена с коротким TTL
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import deps
from app.core import redis_client
from app.core.config import settings
from app.core.errors import RateLimitError
from app.core.security import TokenError, create_token, decode_token
from app.services import ratelimit


def _request(peer: str, forwarded: str | None = None):
    headers = {}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer))


class TestTrustedProxies:
    def test_xff_ignored_without_trusted_proxies(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", [])
        req = _request("192.0.2.1", "203.0.113.9, 10.0.0.1")
        assert deps.client_ip(req) == "192.0.2.1"

    def test_xff_ignored_from_untrusted_peer(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.0/8"])
        req = _request("192.0.2.1", "203.0.113.9")
        assert deps.client_ip(req) == "192.0.2.1"

    def test_xff_honored_from_trusted_peer(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.0/8"])
        req = _request("10.1.2.3", "203.0.113.9, 10.1.2.3")
        assert deps.client_ip(req) == "203.0.113.9"

    def test_xff_honored_exact_proxy_ip(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", ["10.1.2.3"])
        req = _request("10.1.2.3", "198.51.100.7")
        assert deps.client_ip(req) == "198.51.100.7"

    def test_invalid_proxy_entries_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", ["not-an-ip", ""])
        req = _request("10.1.2.3", "203.0.113.9")
        assert deps.client_ip(req) == "10.1.2.3"


class TestAntiReplayFailClosed:
    async def test_fail_closed_when_configured(self, monkeypatch):
        async def _unavailable(_operation):
            return redis_client.UNAVAILABLE

        monkeypatch.setattr(redis_client, "_exec", _unavailable)
        monkeypatch.setattr(settings, "anti_replay_fail_closed", True)
        assert await redis_client.consume_once("test-key-closed", 60) is False

    async def test_fail_open_when_configured(self, monkeypatch):
        async def _unavailable(_operation):
            return redis_client.UNAVAILABLE

        monkeypatch.setattr(redis_client, "_exec", _unavailable)
        monkeypatch.setattr(settings, "anti_replay_fail_closed", False)
        assert await redis_client.consume_once("test-key-open", 60) is True

    def test_default_closed_only_in_prod_like_envs(self, monkeypatch):
        monkeypatch.setattr(settings, "anti_replay_fail_closed", None)
        monkeypatch.setattr(settings, "environment", "production")
        assert settings.anti_replay_closed() is True
        monkeypatch.setattr(settings, "environment", "staging")
        assert settings.anti_replay_closed() is True
        monkeypatch.setattr(settings, "environment", "development")
        assert settings.anti_replay_closed() is False


class TestLocalRateLimitFallback:
    async def test_local_limiter_enforced_when_redis_down(self, monkeypatch):
        async def _none(_key, _ttl):
            return None

        monkeypatch.setattr(redis_client, "incr_window", _none)
        monkeypatch.setattr(settings, "rate_limit_enabled", True)
        ratelimit._local_limiter._counters.clear()

        key_id = "hardening-test-ip"
        for _ in range(3):
            await ratelimit.check_rate_limit("hardening-test", key_id, limit=3, window=60)
        with pytest.raises(RateLimitError):
            await ratelimit.check_rate_limit("hardening-test", key_id, limit=3, window=60)


class TestWsTicket:
    def test_ws_ticket_roundtrip(self):
        ticket, _exp, _jti = create_token("user-1", token_type="ws", ttl_seconds=60)
        payload = decode_token(ticket, expected_type="ws")
        assert payload["sub"] == "user-1"
        assert payload["type"] == "ws"

    def test_access_token_not_valid_as_ws_ticket(self):
        token, _exp, _jti = create_token("user-1", token_type="access", ttl_seconds=60)
        with pytest.raises(TokenError):
            decode_token(token, expected_type="ws")

    def test_ws_ticket_not_valid_as_access_token(self):
        ticket, _exp, _jti = create_token("user-1", token_type="ws", ttl_seconds=60)
        with pytest.raises(TokenError):
            decode_token(ticket, expected_type="access")

    def test_ws_ticket_not_valid_as_refresh_token(self):
        ticket, _exp, _jti = create_token("user-1", token_type="ws", ttl_seconds=60)
        with pytest.raises(TokenError):
            decode_token(ticket, expected_type="refresh")
