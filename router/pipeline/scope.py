"""Tree-aware signal scope: which model questions a request must answer.

The unified tree engine only ever reads signals that appear as ``when``
conditions or terminal ``weights`` anywhere in the embedded tree.  Answering the
other JEV questions (``fact_check`` / ``modality`` / 28 of the 30 PII subtypes on
the deployed tree) costs a full forward pass and is then thrown away.  This
module extracts exactly what the tree can read so the JEV tagger can skip the
rest (``docs/pipeline-aware-trimming.md``).

Design rules (correctness first):

* **The tree is the only source of truth.**  Every condition of every reachable
  node is a potential read; the union is the safe over-approximation.  Path
  analysis would not save anything on a deployed tree and complicates hot-edit
  correctness.
* **Rule signals are local** (``signals/rules.py``, no model forward): they are
  listed by :func:`required_signal_keys` for signal preparation/validation but
  never added to the model-question scope.
* **Decision-group signals are not regular model questions**: ``capability_route``
  was the old decision bridge and is retired with the unified tree, so
  :func:`compute_tree_scope` never asks JEV for it.
* **A model multi-signal is trimmed per subtype** (currently only ``pii``): only
  referenced PII subtypes are scored, because the tree can only match those.
* **Choice signals keep every configured candidate** even when the tree only
  references some values: dropping candidate options changes the argmax and the
  entropy-confidence of the signal itself, i.e. the matched value and
  ``conf_min``/``min_score`` semantics.  Only whole questions are dropped.
* **When in doubt, compute everything.**  Unknown signal keys, unknown PII
  subtypes or a model multi-signal we do not know how to trim return
  :meth:`SignalScope.full`; over-approximating is always safe, silently dropping
  a read signal never is.  Save/enable validation rejects such trees before they
  can run, so this is only a stale-DB safety net.

The scope is recomputed from the in-memory tree snapshot on every request (one
pass over the nodes, microseconds), so a hot tree edit takes effect on the next
request without any cache invalidation.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.tree import Block, Node, Route, walk_nodes
from router.signals.registry import SIGNALS


@dataclass(frozen=True)
class SignalScope:
    """Model signals (and PII subtypes) a request can actually be matched on.

    ``signals`` holds product-canonical model signal keys (rules and decision
    signals excluded); ``pii_types`` the referenced PII subtypes.  ``full=True``
    means "do not trim": compute every configured question, exactly like before
    this feature existed (``reason`` says why, for logs/debugging).
    """

    signals: frozenset[str] = frozenset()
    pii_types: frozenset[str] = frozenset()
    full: bool = False
    reason: str = ""

    @property
    def trimmed(self) -> bool:
        return not self.full


def full_scope(reason: str = "full pack requested") -> SignalScope:
    """A scope that disables trimming (stale tree, /admin/try, ...)."""
    return SignalScope(signals=frozenset(), pii_types=frozenset(), full=True,
                       reason=reason)


def required_signal_keys(root: Node) -> frozenset[str]:
    """Every signal key the tree may read (rules included), in no order.

    This is the static signal requirement used by startup/preparation checks:
    unlike :func:`compute_tree_scope` it does not classify or trim, it only walks
    all ``when`` conditions and terminal weights.
    """
    keys: set[str] = set()
    for node in walk_nodes(root):
        if node.when is not None:
            keys.add(node.when.signal)
        if isinstance(node, (Block, Route)):
            keys.update(weight.signal for weight in node.weights)
    return frozenset(keys)


def compute_tree_scope(root: Node) -> SignalScope:
    """Collect the model questions the tree may read for any request.

    Pure function over the in-memory tree: no config/database access, no
    caching.  Call it once per snapshot, not per request path.
    """
    try:
        nodes = tuple(walk_nodes(root))
    except TypeError:                   # defensive: stale snapshot root
        return full_scope("tree root is not a routing node")

    signals: set[str] = set()
    pii_types: set[str] = set()
    for node in nodes:
        conditions = []
        if node.when is not None:
            conditions.append(node.when)
        if isinstance(node, (Block, Route)):
            conditions.extend(node.weights)
        for condition in conditions:
            spec = SIGNALS.get(condition.signal)
            if spec is None:
                return full_scope(f"unknown signal {condition.signal!r}")
            if spec.kind != "model":
                continue                    # rule signals: local + cheap
            if spec.group == "decision":
                continue                    # retired decision bridge
            if spec.multi:
                if spec.key != "pii":
                    return full_scope(
                        f"model multi-signal {spec.key!r} is not trimmable")
                allowed = {value[0] for value in spec.values}
                if str(condition.value) not in allowed:
                    return full_scope(
                        f"unknown pii subtype {condition.value!r}")
                pii_types.add(str(condition.value))
            signals.add(spec.key)
    return SignalScope(signals=frozenset(signals),
                       pii_types=frozenset(pii_types), full=False,
                       reason="tree references")
