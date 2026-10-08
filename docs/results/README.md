# Measured results

Raw output of simulator runs on one machine, with `HANDOFF_LLM=off` (rule-based explainer).
All servers, branches and files are synthetic.

| File | Command |
|---|---|
| `simulated-morning.json` | `python -m handoff simulate --out docs/results/simulated-morning.json` |
| `batch-30-mornings.json` | `python -m handoff batch --mornings 30 --seed 100 --out docs/results/batch-30-mornings.json` |
| `batch-30-mornings-before-watch-fix.json` | the same batch before the engine re-checked delivered files (86 of 88; two late, different duplicates went unnoticed) |

Wall-clock times vary from run to run; counts and classifications are deterministic for a given
scenario and seed.
