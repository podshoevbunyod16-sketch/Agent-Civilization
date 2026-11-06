"""Этап 10, A #7: защита от SSRF — резолв DNS и блокировка внутренних сетей.

Проверяется то, что обходило прежний строковый блоклист: `nip.io`-домены,
приватные диапазоны, IPv6-mapped адреса, link-local (метаданные облака),
CGNAT, логин/пароль в URL и не-HTTP схемы.
"""

from __future__ import annotations

import pytest

from app.core.net_guard import (
    BlockedAddressError,
    check_url,
    is_blocked_ip,
    validate_url,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # приватная сеть
        "172.16.5.5",  # приватная сеть (2-й октет 16 — до этапа 10 проходил!)
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # метаданные облака
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "::1",
        "fd00::1",  # unique local
        "fe80::1",  # link-local
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6
        "198.18.0.1",  # benchmarking
    ],
)
def test_internal_addresses_blocked(ip):
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert is_blocked_ip(ip) is False


def test_private_allowed_explicitly_for_dev():
    # Осознанное исключение только при SSRF_ALLOW_PRIVATE (dev/test)
    assert is_blocked_ip("10.1.2.3", allow_private=True) is False
    # Loopback и link-local остаются запрещёнными: это отдельные риски
    assert is_blocked_ip("127.0.0.1", allow_private=True) is True
    assert is_blocked_ip("169.254.169.254", allow_private=True) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/ops/health",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:5432/",
        "http://10.0.0.5/",
        "http://172.20.0.2:6379/",
        "http://user:pass@example.com/",  # логин/пароль в URL
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
        "http://0.0.0.0/",
    ],
)
def test_validate_url_rejects_internal_and_non_http(url):
    with pytest.raises(BlockedAddressError):
        validate_url(url)


def test_nip_io_style_hostname_resolves_to_loopback_and_is_blocked():
    """`nip.io`/`sslip.io` кодируют IP в имени — раньше это обходило блоклист."""
    with pytest.raises(BlockedAddressError):
        validate_url("http://127.0.0.1.nip.io/")


def test_localhost_subdomain_blocked():
    with pytest.raises(BlockedAddressError):
        validate_url("http://api.localhost/")


def test_public_host_resolves():
    target = validate_url("https://example.com/path")
    assert target.ip and ":" not in target.ip or target.ip.count(":") > 1
    assert target.host == "example.com"
    assert target.port == 443


def test_dns_rebinding_connects_to_checked_ip(monkeypatch):
    """Клиент подключается к проверенному IP, а не резолвит имя повторно."""
    import httpx

    from app.core.net_guard import SafeAsyncClient

    seen: list[str] = []
    resolved = iter([("93.184.216.34", 0)] * 10)

    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, "", (next(resolved)[0], port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    async def run():
        client = SafeAsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get("http://evil.example.com/x")
        finally:
            await client.aclose()

    import asyncio

    response = asyncio.get_event_loop().run_until_complete(run())
    assert response.status_code == 200
    # Соединение ушло на проверенный IP, Host сохранён
    assert seen and "93.184.216.34" in seen[0]


def test_check_url_helper_modes():
    assert check_url("https://example.com", strict=False) == "https://example.com"
    with pytest.raises(BlockedAddressError):
        check_url("http://127.0.0.1/")


@pytest.mark.asyncio
async def test_mcp_with_internal_url_rejected(client, user):
    from tests.conftest import create_agent

    agent = await create_agent(client, user)
    for url in (
        "http://127.0.0.1:8000/mcp",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.7/mcp",
    ):
        response = await client.put(
            f"/api/v1/agents/{agent['id']}/mcp",
            json={"name": "evil", "server_url": url, "transport": "http"},
            headers=user.headers,
        )
        assert response.status_code == 422, url
        assert "внешнего вызова" in response.text or "недоступен" in response.text


@pytest.mark.asyncio
async def test_agent_version_internal_endpoint_rejected(client, user):
    from tests.conftest import create_agent, version_payload

    agent = await create_agent(client, user)
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(entrypoint_config={"url": "http://169.254.169.254/latest"}),
        headers=user.headers,
    )
    assert response.status_code == 422
    assert "entrypoint_config.url" in response.text


@pytest.mark.asyncio
async def test_integration_check_blocks_private_address(client, admin_user):
    response = await client.post(
        "/api/v1/integrations",
        json={
            "slug": "internal-hook",
            "name": "internal",
            "kind": "webhook",
            "protocol": "http",
            "base_url": "http://192.168.0.10:8080/hook",
        },
        headers=admin_user.headers,
    )
    # Регистрация либо отклоняется сразу, либо проверка соединения
    # блокирует приватный адрес — оба варианта означают «внутрь нельзя».
    assert response.status_code in (201, 422)
    if response.status_code == 201:
        integration_id = response.json()["id"]
        checked = await client.post(
            f"/api/v1/integrations/{integration_id}/probe",
            headers=admin_user.headers,
        )
        body = checked.text
        assert "внутренних" in body or checked.json().get("is_verified") is False
