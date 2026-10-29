from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

import httpx

from router.jev.config import JevSettings


class JevError(RuntimeError):
    pass


@dataclass(frozen=True)
class JevCandidate:
    key: str
    label: str
    capability: str


@dataclass(frozen=True)
class JevAnswer:
    probabilities: dict[str, float]
    ranking: list[str]
    p_top: float
    margin: float
    usage: dict
    duration_ms: float

    @property
    def selected_key(self) -> str:
        return self.ranking[0]


class JevClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def choose(
        self,
        settings: JevSettings,
        text: str,
        candidates: tuple[JevCandidate, ...],
        *,
        instructions: str | None = None,
    ) -> JevAnswer:
        if not 2 <= len(candidates) <= 255:
            raise JevError("JEV choice needs 2–255 candidates")
        keys = [candidate.key for candidate in candidates]
        labels = [candidate.label for candidate in candidates]
        if len(set(keys)) != len(keys) or len(set(labels)) != len(labels):
            raise JevError("JEV candidate keys and labels must be unique")
        if any(not candidate.label.strip() or not candidate.capability.strip() for candidate in candidates):
            raise JevError("JEV candidates need labels and capabilities")
        payload = {
            "model": settings.model,
            "state": settings.state_template.format(request=text),
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": instructions or settings.instructions,
                    "criteria": {candidate.label: candidate.capability for candidate in candidates},
                }
            },
        }
        api_key = settings.api_key or os.environ.get("ROUTER_JEV_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        started = time.monotonic()
        try:
            response = await self.http.post(
                f"{settings.base_url}/v1/systemone",
                json=payload,
                headers=headers,
                timeout=settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise JevError(f"JEV transport failed: {type(exc).__name__}") from exc
        duration_ms = (time.monotonic() - started) * 1000
        if response.status_code != 200:
            raise JevError(f"JEV returned HTTP {response.status_code}")
        try:
            body = response.json()
            raw = body["answers"]["route"]["probabilities"]
        except (ValueError, KeyError, TypeError) as exc:
            raise JevError("JEV response is missing answers.route.probabilities") from exc
        probabilities = self._probabilities(raw, candidates)
        ranking = [
            key for _, key in sorted(
                enumerate(keys),
                key=lambda item: (-probabilities[item[1]], item[0]),
            )
        ]
        p_top = probabilities[ranking[0]]
        margin = p_top - probabilities[ranking[1]]
        usage = body.get("usage") if isinstance(body, dict) else {}
        return JevAnswer(
            probabilities=probabilities,
            ranking=ranking,
            p_top=p_top,
            margin=margin,
            usage=usage if isinstance(usage, dict) else {},
            duration_ms=duration_ms,
        )

    @staticmethod
    def _probabilities(raw, candidates: tuple[JevCandidate, ...]) -> dict[str, float]:
        if not isinstance(raw, dict):
            raise JevError("JEV probabilities must be an object")
        labels = [candidate.label for candidate in candidates]
        if set(raw) != set(labels) or len(raw) != len(labels):
            raise JevError("JEV probabilities do not cover exactly the configured candidates")
        values = []
        for label in labels:
            value = raw[label]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JevError("JEV probabilities must be numbers")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise JevError("JEV probabilities must be finite and nonnegative")
            values.append(number)
        total = sum(values)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-3) or total <= 0:
            raise JevError(f"JEV probabilities must sum to 1, got {total}")
        return {
            candidate.key: value / total
            for candidate, value in zip(candidates, values)
        }
