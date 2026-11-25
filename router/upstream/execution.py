"""Peripheral execution catalog: turn one kernel decision into one upstream call.

The kernel decides *which* logical model runs and reports the non-secret provider
facts (name/base_url/reasoning_format); the peripheral owns the Provider Key and
the SQLite execution catalog.  This module is the single place that joins the two
without ever sending a credential to the kernel:

* the **logical model** is looked up in the peripheral catalog
  (``load_backends(conn)``), so a decision naming a model the peripheral cannot
  execute is rejected instead of being silently substituted;
* the catalog provider name and the fully-resolved ``base_url/chat/completions``
  URL must match the kernel decision exactly: a catalog that drifted from the
  graph the kernel decided on would otherwise send the request with the wrong
  credential (design §6.1 ``execution_catalog_mismatch``);
* the returned backend is the peripheral template (its stored/environment key
  stays in ``backend.headers``) with only ``upstream_name`` replaced by the real
  model id from the decision.

No retries and no fallbacks: a mismatch is a 503 the route maps without calling
any upstream.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from router.upstream.catalog import Backend, load_backends


class ExecutionCatalogMismatch(RuntimeError):
    """The peripheral execution catalog cannot execute the kernel decision."""


def backend_for_decision(payload: dict, conn: sqlite3.Connection) -> Backend:
    """Resolve the kernel decision's logical model to a peripheral ``Backend``.

    Raises ``ExecutionCatalogMismatch("execution_catalog_mismatch")`` when the
    logical model is missing, the catalog provider name differs from the kernel
    decision, or the catalog URL is not the decision's ``base_url`` +
    ``/chat/completions``.  The returned backend keeps the peripheral credential
    headers and carries the kernel's real ``model`` as ``upstream_name``.
    """
    template = load_backends(conn).get(payload["logical_model"])
    provider = payload["provider"]
    if template is None or template.provider != provider["name"]:
        raise ExecutionCatalogMismatch("execution_catalog_mismatch")
    expected_url = provider["base_url"].rstrip("/") + "/chat/completions"
    if template.url != expected_url:
        raise ExecutionCatalogMismatch("execution_catalog_mismatch")
    return replace(template, upstream_name=payload["model"])
