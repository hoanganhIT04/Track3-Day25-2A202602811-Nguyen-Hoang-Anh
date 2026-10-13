"""Part C — ResilientClient: compose classification, retry, breaker and fallback.

The simulator calls ``start`` for every logical LLM call, then ``on_success``
or ``on_error`` for every attempt. You answer with ``Send`` (try a provider,
maybe after a delay) or ``GiveUp`` (user gets a static degraded answer).

Tests: tests/test_client.py, then `make compare` (6 incident tapes).

Per-call scratch space lives in ``call.state`` (a dict). Suggested keys:
    "ladder": list[str]  providers this call may use, in order
    "idx":    int        position of the provider we are currently on
    "tries":  int        attempts already made on that provider

Decision table for ``on_error`` (implement it!):

| ErrorKind        | breaker              | then                                                    |
|------------------|----------------------|---------------------------------------------------------|
| CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
| QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
| RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
| OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
| SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
| TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |

Cooldowns are per provider and shared by all calls (other calls must avoid a
rate-limited provider too). When every provider in the ladder is unusable,
you may wait for a *short* (<= 5 s) rate-limit cooldown if it fits the deadline.
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker  # noqa: F401
from gameday_lab.errors import ErrorKind, classify, counts_toward_breaker  # noqa: F401
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

RATE_LIMIT_WAIT_MAX_S = 5.0  # only wait for short (rate-limit) cooldowns, never for quota


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """PROVIDED: reads the `client:` block of configs/gameday.yaml."""
        self.providers = providers
        self.order = list(fallback_order)
        self.timeout_s = float(settings.get("attempt_timeout_s", 8.0))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))
        self.backoff = FullJitterBackoff(
            float(settings.get("backoff_base_s", 0.25)),
            float(settings.get("backoff_cap_s", 4.0)),
            random.Random(seed),
        )
        self.budget = RetryBudget(
            float(settings.get("retry_budget_ratio", 0.1)),
            float(settings.get("retry_budget_min_per_s", 0.5)),
            float(settings.get("retry_budget_max_tokens", 20.0)),
        )
        breaker_cfg = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_cfg) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}

    # ------------------------------------------------------------------ provided helper
    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """PROVIDED. Build a Send for ``provider`` (after ``delay``), or None if its breaker refuses.

        Takes a breaker permit (carried in ``tag``), and caps the attempt timeout
        so it never runs past the call's deadline. Returns GiveUp("deadline") if
        there is not enough time left for a meaningful attempt.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        if timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")
        return Send(provider, timeout_s=timeout, delay_s=delay, tag=permit)

    # ------------------------------------------------------------------ TODO
    def ladder(self, task: str) -> list[str]:
        if task == "tool":
            return [p for p in self.order if self.providers[p].supports_tools]
        return list(self.order)

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        ladder = call.state.get("ladder", [])
        idx = call.state.get("idx", 0)

        while idx < len(ladder):
            provider = ladder[idx]
            if self.cooldown_until.get(provider, 0.0) > now + delay:
                idx += 1
                call.state["idx"] = idx
                call.state["tries"] = 0
                delay = 0.0
                continue

            action = self._send(call, provider, now, delay)
            if action is None:
                idx += 1
                call.state["idx"] = idx
                call.state["tries"] = 0
                delay = 0.0
                continue

            if isinstance(action, GiveUp):
                return action

            call.state["idx"] = idx
            call.state["tries"] = 1
            return action

        best_p = None
        best_cd = float("inf")
        for p in ladder:
            cd = self.cooldown_until.get(p, 0.0)
            if cd > now:
                wait_s = cd - now
                if wait_s <= RATE_LIMIT_WAIT_MAX_S:
                    if self.breakers[p].state is not BreakerState.OPEN:
                        if now + wait_s + self.min_time_left_s <= call.deadline:
                            if cd < best_cd:
                                best_cd = cd
                                best_p = p

        if best_p is not None:
            wait_s = best_cd - now
            action = self._send(call, best_p, now, delay=wait_s)
            if action is not None:
                if not isinstance(action, GiveUp):
                    call.state["idx"] = ladder.index(best_p)
                    call.state["tries"] = 1
                    return action
                return action

        return GiveUp("no_provider_available")

    def start(self, call: CallContext, now: float) -> Action:
        self.budget.record_request(now)
        call.state["ladder"] = self.ladder(call.task)
        call.state["idx"] = 0
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        permit = attempt.tag
        if isinstance(permit, Permit):
            self.breakers[attempt.provider].on_success(permit, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        kind = classify(err)
        p = attempt.provider
        permit = attempt.tag
        call = attempt.call

        if kind is ErrorKind.CLIENT_ERROR:
            if isinstance(permit, Permit):
                self.breakers[p].release(permit, now)
            return GiveUp("client_error")

        if kind is ErrorKind.QUOTA_EXHAUSTED:
            if isinstance(permit, Permit):
                self.breakers[p].release(permit, now)
            self.cooldown_until[p] = max(self.cooldown_until.get(p, 0.0), now + self.quota_cooldown_s)
            call.state["idx"] = call.state.get("idx", 0) + 1
            call.state["tries"] = 0
            return self._route(call, now, delay=0.0)

        if kind is ErrorKind.RATE_LIMITED:
            if isinstance(permit, Permit):
                self.breakers[p].release(permit, now)
            retry_after = err.retry_after if err.retry_after is not None else 0.0
            self.cooldown_until[p] = max(self.cooldown_until.get(p, 0.0), now + retry_after)
            call.state["idx"] = call.state.get("idx", 0) + 1
            call.state["tries"] = 0
            return self._route(call, now, delay=0.0)

        # OVERLOADED / SERVER_ERROR / TIMEOUT
        if isinstance(permit, Permit):
            self.breakers[p].on_failure(permit, now)

        tries = call.state.get("tries", 1)
        if tries < self.max_attempts_per_provider and self.breakers[p].state is not BreakerState.OPEN:
            delay = self.backoff.delay(tries, retry_after=err.retry_after)
            if now + delay + self.min_time_left_s <= call.deadline:
                if self.budget.try_acquire_retry(now):
                    action = self._send(call, p, now, delay)
                    if action is not None:
                        if not isinstance(action, GiveUp):
                            call.state["tries"] = tries + 1
                            return action
                        elif action.reason == "deadline":
                            return action

        call.state["idx"] = call.state.get("idx", 0) + 1
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        if isinstance(tag, Permit):
            self.breakers[tag.breaker].release(tag, now)
