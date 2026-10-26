"""Pure ``choice`` records for the JEV decision model (no model dependency).

The tagger (:class:`router.signals.jev.JevTagger`) owns the tokenizer-exact
input window and the forward pass; the two pure steps around them live here so
a ``DecisionRunner`` adapter or the routing-tree evaluator can build/decode a
``choice`` question without importing torch/transformers:

* :func:`build_choice_record` — the canonical JEV ``choice`` record: one prompt
  per candidate, ``<key>: <description>`` as the option label, candidate keys in
  ``answer_keys``. Candidate order is behaviour: it is never sorted or
  de-duplicated.
* :func:`decode_choice` — candidate probabilities → the
  ``{"probabilities", "ranking", "input", "temperature"}`` result the callers
  consume (the ranking is descending; equal probabilities keep candidate
  order, i.e. the sort is stable).

Pure module: no torch, no ``router.signals`` import (2026-10-26 unified
routing tree, Task 1 extracted this boundary from the JEV tagger).
"""

from __future__ import annotations


class ChoiceError(ValueError):
    """The choice question/candidate set or the scored probabilities are invalid."""


def build_choice_record(state: str, instructions: str,
                        candidates: dict[str, str]) -> dict:
    """Compile one request into a single JEV ``choice`` record.

    ``state`` is already-windowed request text; the rendering matches
    ``router.signals.jev.build_signal_records`` (and Open-Jev): one prompt per
    candidate, ``<key>: <description>`` as the option label, candidate keys in
    ``answer_keys`` in the operator's order.
    """
    if not isinstance(instructions, str) or not instructions.strip():
        raise ChoiceError("choice instructions must be a nonempty string")
    if not isinstance(candidates, dict) or not 2 <= len(candidates) <= 255:
        raise ChoiceError("choice needs 2..255 candidates")
    if any(not isinstance(key, str) or not isinstance(value, str)
           for key, value in candidates.items()):
        raise ChoiceError("choice candidates must map keys to descriptions")
    return {
        "state": state,
        "kind": "choice",
        "question": instructions,
        "options": [f"{key}: {value}" for key, value in candidates.items()],
        "answer_keys": list(candidates),
    }


def decode_choice(record: dict, probs: list[float], window: dict,
                  temperature: float) -> dict:
    """Turn one record's candidate probabilities into the public result shape.

    ``probabilities`` keeps the candidate order of ``record["answer_keys"]``;
    ``ranking`` is descending by probability and stable, so equal
    probabilities keep candidate order. ``window`` (the tagger's input-window
    metadata) and ``temperature`` are passed through unchanged for the request
    ledger.
    """
    keys = record["answer_keys"]
    if len(keys) != len(probs):
        raise ChoiceError("choice probability count does not match candidates")
    ranking = sorted(
        ({"model": key, "p": float(prob)} for key, prob in zip(keys, probs)),
        key=lambda row: row["p"], reverse=True)
    return {
        "probabilities": dict(zip(keys, probs)),
        "ranking": ranking,
        "input": window,
        "temperature": temperature,
    }
