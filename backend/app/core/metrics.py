"""Наблюдаемость (этап 9): метрики, счётчики отказов, трассировка запросов.

Сознательно **без внешних зависимостей** (prometheus_client не тянем):
формат Prometheus text exposition настолько прост, что писать его самим
дешевле, чем добавлять зависимость и её жизненный цикл. Зато метрики честные:

* гистограмма длительности HTTP по маршрутам с фиксированными корзинами;
* счётчики запросов по методу/маршруту/коду ответа;
* счётчики операций домена (задания, доставка, экономика, проверки);
* gauge'и, которые считаются из базы при сборе (очередь, застрявшие аренды,
  узлы online/offline) — это не «память процесса», поэтому значения
  корректны при любом числе реплик API.

Метрики процесса (память, GC) локальны для реплики и помечены `instance`,
поэтому их нельзя складывать между подами — но именно так их и читает
Prometheus (по одному scrape на под).
"""

from __future__ import annotations

import os
import platform
import resource
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

# Корзины в миллисекундах: от быстрых чтений до тяжёлых отчётов.
LATENCY_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
MAX_ROUTE_KEYS = 500


def _instance_id() -> str:
    return os.environ.get("HOSTNAME") or f"pid-{os.getpid()}"


@dataclass
class _Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * len(LATENCY_BUCKETS))
    total: float = 0.0
    count: int = 0

    def observe(self, milliseconds: float) -> None:
        self.count += 1
        self.total += milliseconds
        for index, bound in enumerate(LATENCY_BUCKETS):
            if milliseconds <= bound:
                self.counts[index] += 1
                break
        else:
            # Значение больше последней корзины: +Inf учитывается отдельно
            pass


class MetricsRegistry:
    """Потокобезопасный реестр метрик одного процесса.

    Используется обычный `threading.Lock`: FastAPI обрабатывает запросы в
    одном event loop, но метрики могут обновлять фоновые потоки (воркеры
    доставки, gc-хуки), и цена блокировки здесь ничтожна.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.instance = _instance_id()
        self.requests: Counter[tuple[str, str, int]] = Counter()
        self.requests_excluded = 0
        self.latency: dict[tuple[str, str], _Histogram] = defaultdict(_Histogram)
        self.events: Counter[tuple[str, str]] = Counter()
        self.durations: dict[str, _Histogram] = defaultdict(_Histogram)
        self.gauges: dict[str, float] = {}
        self.gauges_updated_at: float | None = None
        self.db_collect_errors = 0

    # ── запись ────────────────────────────────────────────────────────
    def observe_request(
        self, *, method: str, route: str, status: int, duration_ms: float
    ) -> None:
        key = (method, route)
        with self._lock:
            if len(self.latency) > MAX_ROUTE_KEYS and key not in self.latency:
                self.requests_excluded += 1
                return
            self.requests[(method, route, status)] += 1
            self.latency[key].observe(duration_ms)

    def incr(self, name: str, value: float = 1.0, *, outcome: str = "ok") -> None:
        with self._lock:
            self.events[(name, outcome)] += int(value)

    def observe_duration(self, name: str, milliseconds: float) -> None:
        with self._lock:
            # `defaultdict` здесь не работает: ключ может быть удалён во время
            # работы (тесты, сброс реестра), и обращение по индексу падало бы
            # с KeyError прямо в пути обработки результата задания.
            histogram = self.durations.get(name)
            if histogram is None:
                histogram = _Histogram()
                self.durations[name] = histogram
            histogram.observe(milliseconds)

    def set_gauges(self, values: dict[str, float]) -> None:
        with self._lock:
            self.gauges = values
            self.gauges_updated_at = time.time()

    # ── чтение ────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "instance": self.instance,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "requests": [
                    {"method": method, "route": route, "status": status, "count": count}
                    for (method, route, status), count in sorted(self.requests.items())
                ],
                "events": [
                    {"name": name, "outcome": outcome, "count": count}
                    for (name, outcome), count in sorted(self.events.items())
                ],
                "gauges": dict(self.gauges),
                "gauges_updated_at": self.gauges_updated_at,
                "routes_tracked": len(self.latency),
                "routes_excluded": self.requests_excluded,
                "db_collect_errors": self.db_collect_errors,
            }

    def render_prometheus(self) -> str:
        """Формат Prometheus text exposition (version 0.0.4)."""
        lines: list[str] = []
        instance = self.instance
        with self._lock:
            lines.append("# HELP ac_uptime_seconds Время с запуска процесса")
            lines.append("# TYPE ac_uptime_seconds gauge")
            lines.append(
                f'ac_uptime_seconds{{instance="{instance}"}} '
                f"{time.time() - self.started_at:.1f}"
            )

            lines.append("# HELP ac_http_requests_total Запросы по методу, маршруту и коду")
            lines.append("# TYPE ac_http_requests_total counter")
            for (method, route, status), count in sorted(self.requests.items()):
                lines.append(
                    f'ac_http_requests_total{{instance="{instance}",method="{method}",'
                    f'route="{route}",status="{status}"}} {count}'
                )

            lines.append("# HELP ac_http_request_duration_ms Гистограмма длительности")
            lines.append("# TYPE ac_http_request_duration_ms histogram")
            for (method, route), histogram in sorted(self.latency.items()):
                labels = f'instance="{instance}",method="{method}",route="{route}"'
                running = 0
                for index, bound in enumerate(LATENCY_BUCKETS):
                    running += histogram.counts[index]
                    lines.append(
                        f"ac_http_request_duration_ms_bucket{{{labels},le=\"{bound}\"}} {running}"
                    )
                inf = f'ac_http_request_duration_ms_bucket{{{labels},le="+Inf"}}'
                lines.append(f"{inf} {histogram.count}")
                lines.append(f'ac_http_request_duration_ms_sum{{{labels}}} {histogram.total:.3f}')
                lines.append(f'ac_http_request_duration_ms_count{{{labels}}} {histogram.count}')

            lines.append("# HELP ac_events_total События домена")
            lines.append("# TYPE ac_events_total counter")
            for (name, outcome), count in sorted(self.events.items()):
                lines.append(
                    f'ac_events_total{{instance="{instance}",event="{name}",'
                    f'outcome="{outcome}"}} {count}'
                )

            lines.append("# HELP ac_operation_duration_ms Длительность операций домена")
            lines.append("# TYPE ac_operation_duration_ms histogram")
            for name, histogram in sorted(self.durations.items()):
                labels = f'instance="{instance}",operation="{name}"'
                running = 0
                for index, bound in enumerate(LATENCY_BUCKETS):
                    running += histogram.counts[index]
                    lines.append(
                        f"ac_operation_duration_ms_bucket{{{labels},le=\"{bound}\"}} {running}"
                    )
                inf = f'ac_operation_duration_ms_bucket{{{labels},le="+Inf"}}'
                lines.append(f"{inf} {histogram.count}")
                lines.append(f'ac_operation_duration_ms_sum{{{labels}}} {histogram.total:.3f}')
                lines.append(f'ac_operation_duration_ms_count{{{labels}}} {histogram.count}')

            for name, value in sorted(self.gauges.items()):
                safe = name.replace(" ", "_")
                lines.append(f"# TYPE ac_{safe} gauge")
                lines.append(f'ac_{safe}{{instance="{instance}"}} {value}')
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()


def process_stats() -> dict[str, Any]:
    """Состояние процесса: память, потоки, версия Python."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "instance": metrics.instance,
        "pid": os.getpid(),
        "python": platform.python_version(),
        "threads": threading.active_count(),
        "max_rss_mb": round(usage.ru_maxrss / 1024, 1),
        "uptime_seconds": round(time.time() - metrics.started_at, 1),
    }
