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

from router.pipeline.model import Node
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


def compute_signal_scope(nodes: list[Node] | None) -> SignalScope:
    """Collect the model questions the pipeline tree may read for any request.

    Pure function over the in-memory snapshot: no config/database access, no
    caching.  Call it once per request and pass the result to the tagger via
    ``TagRequest.scope`` (``router/api/routes_v1.py``).
    """
    try:
        nodes = list(nodes or ())
    except TypeError:                         # defensive: not a node sequence
        return full_scope("pipeline snapshot is not a node list")
    if not nodes:
        return full_scope("empty pipeline")

    signals: set[str] = set()
    pii_types: set[str] = set()
    for node in nodes:
        for branch in node.branches:
            for row in branch.rows:
                spec = SIGNALS.get(row.signal)
                if spec is None:
                    # The evaluator treats an unknown signal as a miss; but a
                    # stale/hand-edited tree is a signal that our assumptions
                    # no longer hold, so pay the full pack.
                    return full_scope(f"unknown signal {row.signal!r}")
                if spec.kind != "model":
                    continue                  # rule signals: local + cheap
                if spec.multi:
                    if row.signal != "pii":
                        return full_scope(
                            f"model multi-signal {row.signal!r} is not trimmable")
                    allowed = {value[0] for value in spec.values}
                    if str(row.value) not in allowed:
                        return full_scope(f"unknown pii subtype {row.value!r}")
                    pii_types.add(str(row.value))
                signals.add(row.signal)
    return SignalScope(signals=frozenset(signals),
                       pii_types=frozenset(pii_types), full=False,
                       reason="pipeline references")
