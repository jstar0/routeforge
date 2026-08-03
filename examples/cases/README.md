# RouteForge fixed cases

These portable scene JSON files are RouteForge v1 inputs. They are not road-network or production-routing data; coordinates use the documented flat km model.

Run all three from this directory's parent:

```bash
for case_file in examples/cases/*.json; do
  name=${case_file:t:r}
  uv run routeforge solve --instance "$case_file" --seed 42 --iterations 30 --ants 8 --output-dir "outputs/cases/$name"
done
```

| Case | Expected real engine behavior | What it demonstrates |
|---|---|---|
| `normal-feasible.json` | 2/2 customers served, feasible, no charging | Basic two-compartment collection and time windows |
| `requires-charging.json` | Feasible and visits charger; the current deterministic run charges 22.74 kWh | Charging is driven by route energy state, not a chart interpolation |
| `tight-search-infeasible.json` | `feasible=false` with evaluator diagnostics | A narrow time-window search result must be presented as constraint/search failure, never as a proof that all routes are impossible |

The test suite checks these engine properties under seed 42, 30 iterations and 8 ants. It does not treat an ACO failure as a mathematical infeasibility proof.
