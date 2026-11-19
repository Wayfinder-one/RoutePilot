# JEV batch spike (experiment)

Experiment-only code that informed the unified JEV decision path. It lives
outside the product tree on purpose: nothing here ships with RoutePilot, and the
default test run (`testpaths = ["tests"]` in `pyproject.toml`) never collects it.

## Files

| File | Purpose |
|---|---|
| `spike_jev_batch.py` | H1 equivalence / H2 capacity / H3 budget probe for packing every question of a canvas into one `/v1/systemone` request. `--dry-run` prints the request plan without touching the network. |
| `test_spike_jev_batch.py` | Offline tests for the spike. Every case uses scripted/fake transports; no network access. |
| `spike_systemone_graph.py` | Self-contained probe for the live `/v1/systemone` wire contract (needs network; never prints the API key). |

## Offline test command

Run from the repository root:

```bash
.venv/bin/python -m pytest -q experiments/jev-batch/test_spike_jev_batch.py
```

Observed output (the duration varies with the machine):

```
.........................                                                [100%]
25 passed
```

The same suite also runs from this directory:

```bash
cd experiments/jev-batch && ../../.venv/bin/python -m pytest -q test_spike_jev_batch.py
```

## Dry run (no network)

```bash
.venv/bin/python experiments/jev-batch/spike_jev_batch.py \
  --samples /path/to/samples.jsonl --scenario all --dry-run
```

`--dry-run` prints the planned request budget and writes nothing.

## Data dependencies

The spike builds its question groups from `configs/default_routing_tree.json`
and `configs/cases/case-logic.json` / `configs/cases/case-industry.json`, so it
expects those working-tree files to exist. `configs/cases/` is local demo data
that is intentionally not tracked by Git; without it the suite fails during
setup, exactly as it did before the move.
