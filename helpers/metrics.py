"""
Prometheus-style metrics helper for hedge bots.

This module wraps `prometheus_client` primitives behind a light abstraction so the
trading bots can emit structured metrics without littering the core logic with
Prometheus-specific details. If `prometheus_client` is unavailable, all helpers
degrade to no-ops so metrics can be toggled on later without code changes.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Optional

try:  # pragma: no cover - optional dependency
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
except ImportError:  # pragma: no cover - metrics disabled when dependency missing
    Counter = Gauge = Histogram = None  # type: ignore
    start_http_server = None

LOGGER = logging.getLogger("metrics")
_SERVER_LOCK = threading.Lock()
_SERVER_STARTED = False

if Gauge is not None:  # pragma: no cover - trivial getters
    _EXTENDED_POSITION = Gauge(
        "hedge_extended_position",
        "Current Extended base position (signed)",
        ["bot", "market"],
    )
    _LIGHTER_POSITION = Gauge(
        "hedge_lighter_position",
        "Current Lighter base position (signed)",
        ["bot", "market"],
    )
    _LAST_ITERATION = Gauge(
        "hedge_iteration_index",
        "Last completed iteration index",
        ["bot", "market"],
    )
    _LAST_HOLD_SECONDS = Gauge(
        "hedge_last_hold_seconds",
        "Hold duration used for the most recent cycle",
        ["bot", "market"],
    )
else:
    _EXTENDED_POSITION = None
    _LIGHTER_POSITION = None
    _LAST_ITERATION = None
    _LAST_HOLD_SECONDS = None

if Counter is not None:  # pragma: no cover - trivial getters
    _ITERATIONS_COMPLETED = Counter(
        "hedge_iterations_completed_total",
        "Number of hedge iterations completed",
        ["bot", "market"],
    )
    _HEDGE_FAILURES = Counter(
        "hedge_failure_events_total",
        "Count of hedge failure recoveries",
        ["bot", "market", "reason"],
    )
else:
    _ITERATIONS_COMPLETED = None
    _HEDGE_FAILURES = None

if Histogram is not None:  # pragma: no cover - trivial getters
    _HEDGE_LATENCY_MS = Histogram(
        "hedge_latency_ms",
        "Latency measurements for different hedge phases (milliseconds)",
        ["bot", "market", "phase"],
        buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    )
else:
    _HEDGE_LATENCY_MS = None


class HedgeMetrics:
    """Prometheus metrics facade with graceful degradation when disabled."""

    def __init__(self, bot_name: str, market: str, port: Optional[int] = None) -> None:
        self._enabled = Gauge is not None and Counter is not None and Histogram is not None
        self._bot = bot_name
        self._market = market
        if not self._enabled:
            if port:
                LOGGER.warning(
                    "prometheus_client not installed; metrics disabled for bot %s on port %s",
                    bot_name,
                    port,
                )
            return

        if port and start_http_server is not None:
            global _SERVER_STARTED
            with _SERVER_LOCK:
                if not _SERVER_STARTED:
                    LOGGER.info("Starting Prometheus metrics HTTP server on port %s", port)
                    start_http_server(port)
                    _SERVER_STARTED = True
                elif port:
                    LOGGER.debug(
                        "Metrics server already running; ignoring additional port %s", port
                    )

        self._extended_position = (
            _EXTENDED_POSITION.labels(bot=bot_name, market=market)
            if _EXTENDED_POSITION
            else None
        )
        self._lighter_position = (
            _LIGHTER_POSITION.labels(bot=bot_name, market=market)
            if _LIGHTER_POSITION
            else None
        )
        self._last_iteration = (
            _LAST_ITERATION.labels(bot=bot_name, market=market)
            if _LAST_ITERATION
            else None
        )
        self._last_hold = (
            _LAST_HOLD_SECONDS.labels(bot=bot_name, market=market)
            if _LAST_HOLD_SECONDS
            else None
        )
        self._iterations_completed = (
            _ITERATIONS_COMPLETED.labels(bot=bot_name, market=market)
            if _ITERATIONS_COMPLETED
            else None
        )
        self._failures = (
            _HEDGE_FAILURES.labels  # type: ignore[assignment]
            if _HEDGE_FAILURES
            else None
        )
        self._latency = (
            _HEDGE_LATENCY_MS.labels  # type: ignore[assignment]
            if _HEDGE_LATENCY_MS
            else None
        )

    def _decimal_to_float(self, value: Decimal) -> float:
        return float(value)

    def set_positions(self, extended: Decimal, lighter: Decimal) -> None:
        """Set gauges for current positions."""
        if not self._enabled:
            return
        if self._extended_position is not None:
            self._extended_position.set(self._decimal_to_float(extended))
        if self._lighter_position is not None:
            self._lighter_position.set(self._decimal_to_float(lighter))

    def record_iteration(self, iteration_index: int) -> None:
        """Track iteration counts."""
        if not self._enabled:
            return
        if self._iterations_completed is not None:
            self._iterations_completed.inc()
        if self._last_iteration is not None:
            self._last_iteration.set(iteration_index)

    def record_hold_duration(self, seconds: float) -> None:
        if not self._enabled:
            return
        if self._last_hold is not None:
            self._last_hold.set(seconds)

    def observe_latency(self, phase: str, duration_ms: float) -> None:
        if not self._enabled or self._latency is None:
            return
        try:
            self._latency(bot=self._bot, market=self._market, phase=phase).observe(duration_ms)
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("Failed to record latency metric (phase=%s)", phase)

    def increment_failure(self, reason: str) -> None:
        if not self._enabled or self._failures is None:
            return
        try:
            self._failures(bot=self._bot, market=self._market, reason=reason).inc()
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("Failed to increment failure metric (reason=%s)", reason)
