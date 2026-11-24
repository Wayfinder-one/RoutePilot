from __future__ import annotations

import random
import threading

from router.routing.types import NoAvailableBackend
from router.upstream.runtime_keys import runtime_model_key


class Lease:
    def __init__(self, balancer: "LoadBalancer", model: str, *, runtime_key: str,
                 dry_run: bool = False, selection: dict | None = None):
        self.balancer = balancer
        # ``model`` keeps the logical model id (headers / ledger / lease); only
        # the pre-built runtime key is used to release inflight bookkeeping.
        self.model = model
        self.runtime_key = runtime_key
        self.dry_run = dry_run
        self.selection = selection or {}
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.dry_run:
            return
        self.balancer._release(self.runtime_key)


class LoadBalancer:
    """Pick a model for a request: sticky/preferred when given, otherwise random.

    In-flight counters are kept for trace/observability only; selection among a
    multi-model target is uniform random, not least-in-flight.

    Breaker checks and inflight bookkeeping are keyed by
    ``runtime_model_key(scope, model)`` so the same logical model in two graphs
    (or a fixed-model direct call) has independent runtime state.  Everything
    this class returns still projects back to logical model ids.
    """

    def __init__(self, breaker=None, rng=None):
        self.breaker = breaker
        self._inflight: dict[str, int] = {}
        self._lock = threading.Lock()
        self._rng = rng or random.Random()

    def available(self, models: list[str], *, scope: str = '') -> list[str]:
        available = []
        for model in models:
            if model in available:
                continue
            if self.breaker is not None and self.breaker.is_open(
                    runtime_model_key(scope, model)):
                continue
            available.append(model)
        return available

    def _selection_locked(self, models: list[str], scope: str) -> dict:
        available = self.available(models, scope=scope)
        return {
            "available_models": list(available),
            "unavailable": {
                model: "breaker_open" for model in models if model not in available
            },
            "inflight": {
                model: self._inflight.get(runtime_model_key(scope, model), 0)
                for model in models
            },
        }

    def selection(self, models: list[str], *, scope: str = '') -> dict:
        with self._lock:
            return self._selection_locked(models, scope)

    def acquire(self, models: list[str], *, preferred: str | None = None,
                dry_run: bool = False, scope: str = '') -> Lease:
        with self._lock:
            selection = self._selection_locked(models, scope)
            available = selection["available_models"]
            if preferred is not None:
                if preferred not in available:
                    raise NoAvailableBackend(
                        f"preferred model {preferred} is unavailable", selection=selection,
                    )
                model = preferred
            elif available:
                # 多模型目标不做负载比较：在可用模型里等概率随机选一个。
                model = self._rng.choice(available)
            else:
                raise NoAvailableBackend("no available backend", selection=selection)
            runtime_key = runtime_model_key(scope, model)
            if not dry_run:
                self._inflight[runtime_key] = self._inflight.get(runtime_key, 0) + 1
            return Lease(self, model, runtime_key=runtime_key, dry_run=dry_run,
                         selection=selection)

    def inflight(self, model: str, *, scope: str = '') -> int:
        with self._lock:
            return self._inflight.get(runtime_model_key(scope, model), 0)

    def _release(self, runtime_key: str) -> None:
        with self._lock:
            count = self._inflight.get(runtime_key, 0)
            if count <= 1:
                self._inflight.pop(runtime_key, None)
            else:
                self._inflight[runtime_key] = count - 1
