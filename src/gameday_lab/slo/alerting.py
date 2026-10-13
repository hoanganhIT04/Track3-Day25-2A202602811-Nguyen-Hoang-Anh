"""Part E — Multi-window, multi-burn-rate alerting (Google SRE Workbook, ch. 5).

Input: per-minute ``total`` and ``bad`` request counts for a 30-day window.

burn_rate(window) = error_ratio(window) / (1 - SLO)

A rule fires at minute i when BOTH its long and its short window (ending at i,
inclusive) burn at least ``burn_rate``. The long window says "this is
significant"; the short window says "it is still happening" (so the alert
resets quickly after recovery).

Tests: tests/test_alerting.py, then `make slo`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str  # "page" or "ticket"
    long_min: int
    short_min: int
    burn_rate: float


# SRE Workbook defaults for a 30-day SLO window
DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("fast_burn_2pct_1h", "page", 60, 5, 14.4),
    AlertRule("slow_burn_5pct_6h", "page", 360, 30, 6.0),
    AlertRule("budget_leak_10pct_3d", "ticket", 4320, 360, 1.0),
)


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    severity: str
    start_min: int  # first minute the rule fired
    end_min: int  # first minute it stopped firing (exclusive)


class WindowSums:
    """O(1) sum of any window, via prefix sums. Build once, query many times.

    43,200 minutes x 6 windows: recomputing every window with sum() is far too slow.
    """

    def __init__(self, values: list[int]) -> None:
        self.prefix: list[int] = [0] * (len(values) + 1)
        for i, v in enumerate(values):
            self.prefix[i + 1] = self.prefix[i] + v

    def sum(self, end_inclusive: int, length: int) -> int:
        if not self.prefix or length <= 0 or end_inclusive < 0:
            return 0
        n = len(self.prefix) - 1
        end = min(end_inclusive + 1, n)
        start = max(0, end_inclusive - length + 1)
        if start >= end:
            return 0
        return self.prefix[end] - self.prefix[start]


class BurnRateAlerter:
    def __init__(self, slo: float = 0.995, rules: tuple[AlertRule, ...] = DEFAULT_RULES) -> None:
        if not 0 < slo < 1:
            raise ValueError("slo must be in (0, 1)")
        self.slo = slo
        self.rules = rules

    def burn_rate(self, total: WindowSums, bad: WindowSums, end: int, length: int) -> float:
        t = total.sum(end, length)
        if t == 0:
            return 0.0
        b = bad.sum(end, length)
        return (b / t) / (1.0 - self.slo)

    def evaluate(self, total: list[int], bad: list[int]) -> list[AlertEvent]:
        total_sums = WindowSums(total)
        bad_sums = WindowSums(bad)
        events: list[AlertEvent] = []
        n = len(total)

        for rule in self.rules:
            start: int | None = None
            for i in range(n):
                burn_long = self.burn_rate(total_sums, bad_sums, i, rule.long_min)
                burn_short = self.burn_rate(total_sums, bad_sums, i, rule.short_min)
                firing = burn_long >= rule.burn_rate - 1e-9 and burn_short >= rule.burn_rate - 1e-9
                if firing and start is None:
                    start = i
                elif not firing and start is not None:
                    events.append(AlertEvent(rule.name, rule.severity, start, i))
                    start = None
            if start is not None:
                events.append(AlertEvent(rule.name, rule.severity, start, n))

        events.sort(key=lambda e: e.start_min)
        return events


def error_budget_report(total: list[int], bad: list[int], slo: float = 0.995) -> dict[str, float | bool]:
    requests = sum(total)
    bad_count = sum(bad)
    allowed_bad = round((1.0 - slo) * requests, 1)
    availability = round(1.0 - (bad_count / requests), 6) if requests > 0 else 1.0
    budget_consumed = round(bad_count / allowed_bad, 4) if allowed_bad > 0 else 0.0
    budget_remaining = round(1.0 - budget_consumed, 4)
    freeze_releases = bool(budget_remaining <= 0)
    return {
        "requests": requests,
        "allowed_bad": allowed_bad,
        "bad": bad_count,
        "availability": availability,
        "budget_consumed": budget_consumed,
        "budget_remaining": budget_remaining,
        "freeze_releases": freeze_releases,
    }
