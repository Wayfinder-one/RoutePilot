from __future__ import annotations

import threading

from router.routing.types import NoAvailableBackend


class Lease:
    def __init__(self, balancer: "LoadBalancer", model: str, *, dry_run: bool = False,
                 selection: dict | None = None):
        self.balancer = balancer
        self.model = model
        self.dry_run = dry_run
        self.selection = selection or {}
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.dry_run:
            return
        self.balancer._release(self.model)


class LoadBalancer:
    def __init__(self, breaker=None):
        self.breaker = breaker
        self._inflight: dict[str, int] = {}
        self._lock = threading.Lock()

    def available(self, models: list[str]) -> list[str]:
        available = []
        for model in models:
            if model in available:
                continue
            if self.breaker is not None and self.breaker.is_open(model):
                continue
            available.append(model)
        return available

    def _selection_locked(self, models: list[str]) -> dict:
        available = self.available(models)
        return {
            "available_models": list(available),
            "unavailable": {
                model: "breaker_open" for model in models if model not in available
            },
            "inflight": {model: self._inflight.get(model, 0) for model in models},
        }

    def selection(self, models: list[str]) -> dict:
        with self._lock:
            return self._selection_locked(models)

    def acquire(self, models: list[str], *, preferred: str | None = None,
                dry_run: bool = False) -> Lease:
        with self._lock:
            selection = self._selection_locked(models)
            available = selection["available_models"]
            if preferred is not None:
                if preferred not in available:
                    raise NoAvailableBackend(
                        f"preferred model {preferred} is unavailable", selection=selection,
                    )
                model = preferred
            elif available:
                model = min(available, key=lambda candidate: self._inflight.get(candidate, 0))
            else:
                raise NoAvailableBackend("no available backend", selection=selection)
            if not dry_run:
                self._inflight[model] = self._inflight.get(model, 0) + 1
            return Lease(self, model, dry_run=dry_run, selection=selection)

    def inflight(self, model: str) -> int:
        with self._lock:
            return self._inflight.get(model, 0)

    def _release(self, model: str) -> None:
        with self._lock:
            count = self._inflight.get(model, 0)
            if count <= 1:
                self._inflight.pop(model, None)
            else:
                self._inflight[model] = count - 1
