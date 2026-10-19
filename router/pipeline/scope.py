"""Pipeline-aware signal scope: which model questions a request must answer.

The routing decision is made by :func:`router.pipeline.engine.evaluate`, which
only ever reads signals that appear as rows in the pipeline tree.  Answering the
other JEV questions (``fact_check`` / ``modality`` / 28 of the 30 PII subtypes
on the deployed v10 tree) costs a full forward pass and is then thrown away.
This module extracts exactly what the tree can read so the JEV tagger can skip
the rest (``docs/pipeline-aware-trimming.md``).

Design rules (correctness first):

* **The tree is the only source of truth.**  Every row of every branch of every
  node is a potential read; branches/nodes are evaluated in order, so the union
  is the safe over-approximation.  Path analysis would not save anything on the
  deployed tree and complicates hot-edit correctness.
* **Rule signals are local** (``signals/rules.py``, no model forward) and are
  ignored here.
* **A model multi-signal is trimmed per subtype** (currently only ``pii``): only
  referenced PII subtypes are scored, because the pipeline can only match those.
* **Choice signals keep every configured candidate** even when the tree only
  references some values: dropping candidate options changes the argmax and the
  entropy-confidence of the signal itself, i.e. the matched value and
  ``conf_min``/``min_score`` semantics.  Only whole questions are dropped.
* **When in doubt, compute everything.**  Unknown signal keys, unknown PII
  subtypes, an empty pipeline or a model multi-signal we do not know how to
  trim return :meth:`SignalScope.full`; over-approximating is always safe,
  silently dropping a read signal never is.

The scope is recomputed from the in-memory pipeline snapshot on every request
(one pass over the rows, microseconds), so a hot pipeline edit takes effect on
the next request without any cache invalidation.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.model import Station, TreeNode
from router.signals.registry import SIGNALS


@dataclass(frozen=True)
class SignalScope:
    """Model signals (and PII subtypes) a request can actually be matched on.

    ``signals`` holds product-canonical model signal keys (rules excluded);
    ``pii_types`` the referenced PII subtypes.  ``full=True`` means "do not
    trim": compute every configured question, exactly like before this feature
    existed (``reason`` says why, for logs/debugging).
    """

    signals: frozenset[str] = frozenset()
    pii_types: frozenset[str] = frozenset()
    full: bool = False
    reason: str = ""

    @property
    def trimmed(self) -> bool:
        return not self.full


def full_scope(reason: str = "full pack requested") -> SignalScope:
    """A scope that disables trimming (unknown reference, /admin/try, ...)."""
    return SignalScope(signals=frozenset(), pii_types=frozenset(), full=True,
                       reason=reason)


def compute_signal_scope(stations: list[Station] | None) -> SignalScope:
    """Collect the model questions the pipeline tree may read for any request.

    Pure function over the in-memory snapshot: no config/database access, no
    caching.  Call it once per request and pass the result to the tagger via
    ``TagRequest.scope`` (``router/api/routes_v1.py``).
    """
    try:
        stations = list(stations or ())
    except TypeError:                         # defensive: not a station sequence
        return full_scope("pipeline snapshot is not a station list")
    if not stations:
        return full_scope("empty pipeline")

    signals: set[str] = set()
    pii_types: set[str] = set()

    def visit(condition: tuple[str, str] | None) -> str | None:
        """Collect one (signal, value); returns an error reason or None."""
        if condition is None:
            return None
        signal_key, target = condition
        spec = SIGNALS.get(signal_key)
        if spec is None:
            # The evaluator treats an unknown signal as a miss; but a stale or
            # hand-edited tree means our assumptions no longer hold, so pay the
            # full pack.
            return f"unknown signal {signal_key!r}"
        if spec.kind != "model":
            return None                       # rule signals: local + cheap
        if spec.multi:
            if signal_key != "pii":
                return f"model multi-signal {signal_key!r} is not trimmable"
            allowed = {value[0] for value in spec.values}
            if str(target) not in allowed:
                return f"unknown pii subtype {target!r}"
            pii_types.add(str(target))
        signals.add(signal_key)
        return None

    def walk(node: TreeNode) -> str | None:
        reason = visit((node.signal, node.value) if node.signal else None)
        if reason:
            return reason
        for weight in node.weights:
            reason = visit((weight.signal, weight.value))
            if reason:
                return reason
        for child in node.children:
            reason = walk(child)
            if reason:
                return reason
        return None

    for station in stations:
        if station.root is None:
            continue
        reason = walk(station.root)
        if reason:
            return full_scope(reason)
    return SignalScope(signals=frozenset(signals),
                       pii_types=frozenset(pii_types), full=False,
                       reason="pipeline references")
