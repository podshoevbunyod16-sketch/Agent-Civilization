"""Защита от SSRF (этап 10, A #7).

Задача: система делает исходящие HTTP-запросы по адресам, которые задаёт
пользователь (URL интеграции, MCP-сервер агента, endpoint версии агента).
Это классический SSRF-вектор: `http://169.254.169.254/` читает метаданные
облака, `http://127.0.0.1:8000/api/v1/ops` — внутренний API, `http://10.0.0.5`
— базу данных в приватной сети.

Прежняя проверка была строковым блоклистом префиксов: `nip.io`, `*.local`,
`172.16-31.`, `100.64.`, IPv6-mapped адреса и редиректы её обходили.

Правильный порядок:

1. разобрать URL и отклонить не-HTTP(S) схемы и адреса с логином/паролем;
2. **разрешить имя в IP** (если в URL уже IP — использовать его);
3. проверить каждый полученный адрес по списку запрещённых диапазонов
   (`ipaddress` знает и приватные сети, и link-local, и multicast, и
   IPv6-mapped IPv4, и reserved);
4. подключаться по **уже проверенному IP**, а не по имени: иначе DNS-
   rebinding даёт второй ответ, указывающий внутрь сети.

Шаг 4 реализован через `SafeAsyncClient`: при запросе выполняется проверка,
а соединение направляется на проверенный адрес с заголовком Host. Редиректы
не следуются автоматически (каждый новый адрес проверяется заново).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

# Диапазоны, которые нельзя посещать никогда. Приватные сети добавлены
# осознанно: сервис не должен ходить в свою же инфраструктуру по запросу
# пользователя. При необходимости (локальный MCP в dev) включается
# SSRF_ALLOW_PRIVATE — и только вне production.
EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("fc00::/7"),  # unique local
)


class BlockedAddressError(ValueError):
    """Адрес ведёт во внутреннюю сеть или недопустим по схеме."""

    code = "blocked_address"


@dataclass(slots=True)
class ResolvedTarget:
    url: str
    scheme: str
    host: str
    port: int
    ip: str


def _blocked_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fe80::/10"),
        ipaddress.ip_network("ff00::/8"),
        ipaddress.ip_network("::/128"),
        *EXTRA_BLOCKED,
    ]
    return networks


def is_blocked_ip(raw_ip: str, *, allow_private: bool = False) -> bool:
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return True  # неразбираемый адрес — не рискуем
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_private and not allow_private:
        return True
    if address.is_loopback or address.is_link_local or address.is_multicast:
        return True
    if address.is_reserved and not allow_private:
        return True
    for network in _blocked_networks():
        try:
            if address in network:
                return True
        except TypeError:  # разные версии IP
            continue
    return False


def validate_url(
    url: str, *, allow_private: bool = False, allow_unresolved: bool = False
) -> ResolvedTarget:
    """Разбирает и разрешает URL; бросает `BlockedAddressError`.

    Возвращает уже проверенный IP — именно к нему нужно подключаться.

    `allow_unresolved` используется при валидации деклараций (адрес
    MCP-сервера, endpoint версии агента): неразрешимое имя само по себе не
    ведёт внутрь сети — соединения просто не будет. Строгий режим
    (`False`) применяется на этапе фактического сетевого вызова.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise BlockedAddressError("Разрешены только http/https-адреса") from None
    if parsed.username or parsed.password:
        raise BlockedAddressError("Адрес не должен содержать логин/пароль") from None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BlockedAddressError("Некорректный адрес: не указан хост") from None
    if host in {"localhost", "localhost.localdomain"} and not allow_private:
        raise BlockedAddressError("Обращение к localhost запрещено") from None

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Если в URL уже IP — резолв не нужен
    try:
        address = ipaddress.ip_address(host)
        candidates = [str(address)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            if allow_unresolved:
                # Адрес ещё не развёрнут: проверить нечего, подключение
                # всё равно не состоится. Возвращаем «пустой» результат.
                return ResolvedTarget(
                    url=url, scheme=parsed.scheme, host=host, port=port, ip=""
                )
            raise BlockedAddressError(f"Не удалось разрешить имя {host}") from exc
        candidates = sorted({info[4][0] for info in infos})
        if not candidates and not allow_unresolved:
            raise BlockedAddressError(f"Имя {host} не разрешается в адрес") from None
        if not candidates:
            return ResolvedTarget(
                url=url, scheme=parsed.scheme, host=host, port=port, ip=""
            )

    for candidate in candidates:
        if is_blocked_ip(candidate, allow_private=allow_private):
            raise BlockedAddressError(
                f"Адрес {host} указывает на внутреннюю сеть ({candidate})"
            )
    return ResolvedTarget(
        url=url, scheme=parsed.scheme, host=host, port=port, ip=candidates[0]
    )


class SafeAsyncClient(httpx.AsyncClient):
    """httpx-клиент, который проверяет адрес перед каждым запросом.

    Редиректы не следуются: каждый переход — новый адрес, и он должен
    пройти проверку сам. Для внутренних вызовов (тесты, локальные сервисы)
    `allow_private=True`.
    """

    def __init__(self, *args, allow_private: bool = False, **kwargs) -> None:
        kwargs.setdefault("follow_redirects", False)
        super().__init__(*args, **kwargs)
        self._allow_private = allow_private

    async def send(self, request: httpx.Request, **kwargs):  # type: ignore[override]
        target = validate_url(
            str(request.url), allow_private=self._allow_private
        )
        # Подключаемся к проверенному IP: DNS-rebinding не сработает, потому
        # что второй резолв не выполняется. Host сохраняется для виртуальных
        # хостов и TLS SNI.
        request.extensions["sni_hostname"] = target.host
        request.url = httpx.URL(
            str(request.url).replace(f"//{target.host}", f"//{target.ip}", 1)
        )
        request.headers["Host"] = (
            f"{target.host}:{target.port}" if target.port not in (80, 443) else target.host
        )
        return await super().send(request, **kwargs)


def check_url(url: str, *, strict: bool = True) -> str:
    """Проверяет URL и возвращает его же.

    `strict=False` — для валидации деклараций (см. `allow_unresolved`).
    """
    from app.core.config import settings

    validate_url(
        url, allow_private=settings.ssrf_allow_private, allow_unresolved=not strict
    )
    return url
