"""Canary releases and rollback.

Two things make this different from a feature flag with a percentage on it.

**Rollback is automatic and fast.** A canary you have to watch is a canary
nobody watches at 2am. Every recorded outcome re-checks the guard rails, and
breaching one rolls back immediately — no human in the path.

**A canary that has not seen enough traffic is not passing, it is unknown.**
Promoting on three good requests is how a 30%-failure release reaches
production. `min_samples` gates promotion, and the status says `pending` rather
than `healthy` until it is met. The distinction matters: "we do not know yet"
and "it is fine" look identical on a dashboard that only tracks failures.

Guard rails compare against the *baseline's measured behaviour*, not against
absolute numbers. A canary at a 4% error rate is fine if the baseline is 4%,
and an emergency if the baseline is 0.1%.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum

from agentcore import store

log = logging.getLogger("p11.canary")

NAMESPACE = "p11_canaries"


class State(str, Enum):
    pending = "pending"        # live, not yet enough traffic to judge
    healthy = "healthy"        # meeting the guard rails
    rolled_back = "rolled_back"
    promoted = "promoted"


@dataclass
class GuardRails:
    #: Multiple of the baseline error rate that trips a rollback.
    max_error_rate_multiple: float = 2.0
    #: Absolute floor, so a baseline of 0.0 does not make every error a breach.
    error_rate_floor: float = 0.02
    max_latency_multiple: float = 1.5
    max_cost_multiple: float = 1.3
    #: Outcomes needed before the canary can be judged at all.
    min_samples: int = 20

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Observation:
    ok: bool
    latency_ms: float
    cost_usd: float


@dataclass
class Canary:
    name: str
    baseline_version: str
    canary_version: str
    traffic_pct: float
    rails: GuardRails = field(default_factory=GuardRails)
    state: State = State.pending
    baseline: list[Observation] = field(default_factory=list)
    canary: list[Observation] = field(default_factory=list)
    rollback_reason: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    # ---------- measurement ----------

    @staticmethod
    def _rate(observations: list[Observation]) -> float:
        if not observations:
            return 0.0
        return sum(1 for o in observations if not o.ok) / len(observations)

    @staticmethod
    def _mean(values: list[float]) -> float:
        return statistics.fmean(values) if values else 0.0

    def metrics(self) -> dict:
        return {
            "baseline": {
                "samples": len(self.baseline),
                "error_rate": round(self._rate(self.baseline), 4),
                "mean_latency_ms": round(
                    self._mean([o.latency_ms for o in self.baseline]), 2
                ),
                "mean_cost_usd": round(
                    self._mean([o.cost_usd for o in self.baseline]), 6
                ),
            },
            "canary": {
                "samples": len(self.canary),
                "error_rate": round(self._rate(self.canary), 4),
                "mean_latency_ms": round(
                    self._mean([o.latency_ms for o in self.canary]), 2
                ),
                "mean_cost_usd": round(self._mean([o.cost_usd for o in self.canary]), 6),
            },
        }

    # ---------- the guard rails ----------

    def check(self) -> str | None:
        """Return a breach reason, or None. Compared against the baseline's
        actual behaviour rather than absolute numbers."""
        if len(self.canary) < self.rails.min_samples:
            return None

        m = self.metrics()
        base, can = m["baseline"], m["canary"]

        allowed_errors = max(
            self.rails.error_rate_floor,
            base["error_rate"] * self.rails.max_error_rate_multiple,
        )
        if can["error_rate"] > allowed_errors:
            return (
                f"error rate {can['error_rate']:.1%} exceeds the allowed "
                f"{allowed_errors:.1%} (baseline {base['error_rate']:.1%})"
            )

        if base["mean_latency_ms"] > 0:
            allowed = base["mean_latency_ms"] * self.rails.max_latency_multiple
            if can["mean_latency_ms"] > allowed:
                return (
                    f"latency {can['mean_latency_ms']:.0f}ms exceeds the allowed "
                    f"{allowed:.0f}ms (baseline {base['mean_latency_ms']:.0f}ms)"
                )

        if base["mean_cost_usd"] > 0:
            allowed = base["mean_cost_usd"] * self.rails.max_cost_multiple
            if can["mean_cost_usd"] > allowed:
                return (
                    f"cost ${can['mean_cost_usd']:.5f}/call exceeds the allowed "
                    f"${allowed:.5f} (baseline ${base['mean_cost_usd']:.5f})"
                )
        return None

    # ---------- lifecycle ----------

    def record(self, arm: str, ok: bool, latency_ms: float, cost_usd: float) -> State:
        """Record one outcome and re-check the rails immediately.

        Checking on every observation rather than on a timer is what makes the
        rollback fast enough to matter — a bad release should be measured in
        requests, not minutes.
        """
        if self.state in (State.rolled_back, State.promoted):
            return self.state

        observation = Observation(ok=ok, latency_ms=latency_ms, cost_usd=cost_usd)
        (self.canary if arm == "canary" else self.baseline).append(observation)

        if (breach := self.check()) is not None:
            self.rollback(breach)
        elif len(self.canary) >= self.rails.min_samples:
            self.state = State.healthy
        save(self)
        return self.state

    def rollback(self, reason: str) -> None:
        self.state = State.rolled_back
        self.rollback_reason = reason
        self.traffic_pct = 0.0
        self.ended_at = time.time()
        log.error("rolled back canary %s: %s", self.name, reason)
        save(self)

    def promote(self) -> tuple[bool, str]:
        """Promote only on enough evidence.

        Refusing to promote an under-observed canary is the point: 'we do not
        know yet' and 'it is fine' look identical on a dashboard that only
        tracks failures.
        """
        if self.state is State.rolled_back:
            return False, f"cannot promote a rolled-back canary: {self.rollback_reason}"
        if len(self.canary) < self.rails.min_samples:
            return False, (
                f"only {len(self.canary)} canary observations, need "
                f"{self.rails.min_samples} — this is unknown, not healthy"
            )
        if (breach := self.check()) is not None:
            return False, f"guard rail breached: {breach}"

        self.state = State.promoted
        self.traffic_pct = 100.0
        self.ended_at = time.time()
        save(self)
        return True, f"promoted {self.canary_version} on {len(self.canary)} observations"

    def route(self, request_id: str) -> str:
        """Which arm serves this request. Stable per request id, so a retry
        lands in the same arm and does not smear the comparison."""
        if self.state in (State.rolled_back,):
            return "baseline"
        if self.state is State.promoted:
            return "canary"
        bucket = int(hash(request_id) % 100)
        return "canary" if bucket < self.traffic_pct else "baseline"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "baseline_version": self.baseline_version,
            "canary_version": self.canary_version,
            "traffic_pct": self.traffic_pct,
            "state": self.state.value,
            "rollback_reason": self.rollback_reason,
            "rails": self.rails.as_dict(),
            "metrics": self.metrics(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "baseline_obs": [o.__dict__ for o in self.baseline],
            "canary_obs": [o.__dict__ for o in self.canary],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Canary:
        canary = cls(
            name=d["name"],
            baseline_version=d["baseline_version"],
            canary_version=d["canary_version"],
            traffic_pct=d["traffic_pct"],
            rails=GuardRails(**d["rails"]),
            state=State(d["state"]),
            rollback_reason=d.get("rollback_reason"),
            started_at=d.get("started_at", 0.0),
            ended_at=d.get("ended_at"),
        )
        canary.baseline = [Observation(**o) for o in d.get("baseline_obs", [])]
        canary.canary = [Observation(**o) for o in d.get("canary_obs", [])]
        return canary


# ---------- persistence ----------


def save(canary: Canary) -> None:
    store.kv_set(NAMESPACE, canary.name, canary.as_dict())


def load(name: str) -> Canary | None:
    row = store.kv_get(NAMESPACE, name)
    return Canary.from_dict(row) if row else None


def all_canaries() -> list[Canary]:
    return [Canary.from_dict(v) for _, v in store.kv_list(NAMESPACE)]


def start(
    name: str,
    baseline_version: str,
    canary_version: str,
    traffic_pct: float = 10.0,
    rails: GuardRails | None = None,
) -> Canary:
    canary = Canary(
        name=name,
        baseline_version=baseline_version,
        canary_version=canary_version,
        traffic_pct=traffic_pct,
        rails=rails or GuardRails(),
    )
    save(canary)
    log.info("started canary %s at %.0f%% traffic", name, traffic_pct)
    return canary
