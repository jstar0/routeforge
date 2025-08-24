# Design decisions

This document exists to be interrogated. Each entry names a decision that is
visible in the code, the alternative it rejected, and the cost it accepted.

Read section 2 first. It reports a measured result that goes against the idea
the project is named for.

## 1. What the metaheuristic actually is

`StateAwareACO` in [`src/routeforge/solver.py`](../src/routeforge/solver.py) is
not a textbook variant. It borrows pieces from several:

| Component | Implementation | Closest textbook analogue |
| --- | --- | --- |
| Transition rule | `p ∝ τ^α · η^β · jitter`, roulette-wheel selection over feasible candidates, `α = 1`, `β = 3`, jitter uniform in `[1, 1.02]` | Ant System |
| Pheromone structure | `τ[from][to][battery_bucket]`, four buckets by default | no standard analogue; a state-augmented tensor |
| Heuristic term | `η = 1/(km + 0.25) · (1 + 30 · urgency) · demand`, where `urgency = 1/max(1, latest_minute − now)` | a composite of distance, time-window slack, and load |
| Evaporation | `τ ← max(0.01, τ · (1 − 0.18))` over the whole tensor | Ant System, with an MMAS-style `τ_min` floor |
| Deposit | top 20% of ants by cost deposit `80/(1 + cost)`, multiplied by `elite_weight = 2.0` when the ant is feasible and at least as good as the incumbent; capped at `τ_max = 100` | rank-based AS plus elitist AS, with MMAS-style bounds |
| Local search | intra-route pairwise customer swap, first-improvement, 2 passes | a restricted neighbourhood; not 2-opt (no segment reversal) and not inter-route |

Rejected alternative: implement MMAS or ACS exactly. That would be easier to
cite but would not carry the battery-state idea, which was the reason for
building it.

Cost accepted: because it is a hybrid, "which ACO is this?" has no one-word
answer, and the parameter set is inherited from convention rather than tuned.

## 2. The battery-state pheromone tensor does not pay off, and the repository says so

The project's distinguishing idea is indexing pheromone by discretised battery
state, so the same arc can be attractive with a full battery and unattractive
with a low one. `SolverConfig.state_aware = False` collapses the tensor to a
plain `(from, to)` matrix, which makes the idea testable instead of asserted.

`routeforge ablation` runs both variants over eight fixed seeds at two scales,
holding the instance, cost model, fleet, ant and iteration budget, and the
per-ant random stream identical. The measured result:

| scale | state-aware cheaper | flat cheaper | ties | mean cost delta |
| --- | ---: | ---: | ---: | ---: |
| compact (12 customers) | 2 | 2 | 4 | +0.052807 |
| stress (30 customers) | 2 | 6 | 0 | -19.963170 |

A positive delta favours the state-aware tensor. State-aware wins 4 of 16 seeded
instances, ties 4, and loses 8, so the verdict recorded in the artifact is
**not supported**.

The mechanism is visible in the magnitudes. After a 30-customer solve, 4978 of
5184 tensor cells sit at the `0.01` floor: only 206 carry signal. Reinforced
`tau` spans 0.0106 to 4.5586, a 456x range at `alpha = 1`. The heuristic term
`1/(km + 0.25)` spans 0.0264 to 1.2041, which at `beta = 3` is a range of about
95,000x before the urgency and demand multipliers widen it further. The
heuristic outweighs the pheromone by roughly two orders of magnitude, so the
search behaves close to a randomised greedy with many restarts. The battery axis
additionally splits reinforcement for one `(from, to)` pair across up to four
cells, so per-cell learning is four times slower.

What follows from that:

- The project's technical claim is the **constraint model, the diagnostics, and
  the reproducible experiment harness**, not that battery-state pheromone is an
  improvement.
- The state-aware tensor stays the default because it is the idea under test and
  the ablation is the evidence about it. It is not presented as a win.
- `test_verdict_follows_the_counts_rather_than_the_hypothesis` in
  [`tests/test_ablation.py`](../tests/test_ablation.py) asserts the verdict
  string tracks the counts, so the conclusion cannot drift away from the data.

Rejected alternative: tune parameters until the state-aware variant wins, or
report the one seed where it wins by the largest margin. Both would produce a
better-looking README and a result that does not survive a reviewer re-running
the command.

## 3. A customer is only admissible if the vehicle can also get home

`_customer_feasible` checks that the vehicle can reach the candidate while
holding its 20% safety reserve, that both compartments have room, and that the
service window can be met. It then checks the leg the original implementation
forgot: whether the vehicle can still reach the depot afterwards, carrying the
load it will have collected. Because this is a *collection* problem, the vehicle
is heaviest on that final leg, which is exactly when energy per kilometre is
highest.

A customer whose depot return is out of range is still admissible if a charger
is reachable from it, and `_close_route` honours that promise by diverting
through a charger before appending the depot.

This was not a cosmetic fix. Without it the greedy baseline stranded a vehicle
at its last stop on 4 of 10 sampled instances, the evaluator charged a 10,000
infeasibility penalty, and the README's headline comparison (503.86 against
10734.22) was 93% penalty and 7% routing. With the check in place the greedy
baseline is feasible on all sixteen benchmark instances and the honest
improvement range is 0.00% to 43.77%.

Proof: [`tests/test_return_leg.py`](../tests/test_return_leg.py) asserts
feasibility for both builders across every benchmark seed and scale, and
`test_improvement_over_greedy_is_a_routing_result_not_a_penalty` fails if the
headline gap ever returns to penalty territory.

## 4. Feasibility is evaluated, not enforced by throwing away a plan

`evaluate_plan` in [`src/routeforge/evaluation.py`](../src/routeforge/evaluation.py)
never throws away a plan. It returns an `Evaluation` carrying `total_cost`,
`feasible`, `on_time`, `violations`, `missing_customers`, and per-route diagnostics.
Constraint breaches enter the cost as penalties:

```
total_cost = fixed + distance·rate + charging·rate
           + lateness·rate
           + (missing + duplicates)·missed_penalty
           + route_violations·missed_penalty
```

`feasible` is false for any hard violation, missing or duplicate customer, or
nonzero lateness. Lateness is still retained in `total_cost`, so competing late
plans can be diagnosed and ranked without being reported as operationally
acceptable.

Rejected alternative: reject infeasible plans during construction. Then a
partially solvable instance yields no answer at all and no explanation.

Coverage summaries remain in `violations`, but they are not charged a second
time: a missing or duplicated customer incurs one customer-level penalty. Every
independent route violation still incurs the same fixed penalty, a deliberately
simple ranking rule rather than a calibrated operating-cost model.

## 5. Unserved customers are surfaced, not dropped

When an ant cannot serve everyone, the builder appends a pseudo-route
`[depot, *remaining, depot]` so the evaluator reports missing coverage rather
than returning a tidy plan that quietly omits work.

## 6. Charging is a decision inside construction, with an explicit cap

When no customer is feasible, the ant looks for a reachable charger, excluding
the one it just used, and stops after `len(chargers) * 2` charger moves or when
a charge adds no energy. Without those guards an ant can oscillate between two
chargers forever.

Cost accepted: the cap is a loop guard, not an optimality argument.

## 7. Two random-number regimes, seeded per ant

Each ant gets `Random(seed + iteration * 1_000_003 + ant * 9_973)`, so a run is
reproducible and an individual ant is reproducible independently of iteration
order. This is what lets the ablation compare two variants on the same random
stream: the bucket index affects only the `τ` lookup, never the number of draws.

## 8. Standard library only

The runtime imports nothing outside the standard library, so a reviewer can
install and reproduce the demo without a solver service, a licence, or a
platform-specific wheel.

Cost accepted: no vectorised distance matrix, no NumPy, and no access to a
mature solver for comparison. See weakness 1.

## Known weaknesses

Measured, not inferred.

1. **The only baseline is a greedy constructor.** No exact solver, no OR-Tools
   comparison, no lower bound, and no published benchmark instance (no Solomon,
   Gehring-Homberger, or Schneider EVRPTW set). A repository-wide search for
   `ortools|solomon|gehring|cplex|gurobi|lower bound|optimal|exact|mip` returns
   nothing. There is **no optimality-gap evidence at all**. The only claim
   available is "better than my own greedy on my own instances", and across the
   sixteen benchmark instances that ranges from 0.00% to 43.77%.
2. **Time-window trade-offs remain under-modelled.** A late plan is now reported
   as infeasible, but lateness is still a single fixed cost of 8 per minute.
   There is no service-level target, customer-specific tardiness cost, or
   sensitivity analysis. Changing that one coefficient can reorder candidate
   plans, and the generated fixtures almost never exercise the question.
3. **Driving energy is free; only purchased charging is priced.** `total_energy`
   is computed and reported but never enters `total_cost`, and every vehicle
   starts full at no cost. That creates a mild incentive to avoid chargers,
   which is the opposite of what an operator would want.
4. **Infeasibility penalties are still coarse.** Missing and duplicate customers
   are now charged once each, but every independent route violation has the same
   fixed penalty regardless of its severity. A reserve breach, an unknown node,
   and a malformed route are not economically comparable events, so the
   absolute cost of an infeasible plan remains a ranking device rather than an
   operating-cost estimate.
5. **Local search is nearly inert.** It is intra-route swap only, and the break
   chain means at most **one improving swap per pass**, so the default of two
   passes caps the whole procedure at two swaps. Measured on an 80-customer
   instance: 4187.58 to 4187.39, a 0.005% gain for 1.7 seconds. There is no
   segment reversal, no relocate, and no inter-route move — the operators that
   usually matter most for vehicle routing.
6. **Instances are self-generated and structurally uniform.** Customers sit on a
   random annulus between 18% and 48% of the scale radius, chargers on a
   *perfect circle* at exactly 30%, every customer gets a 300-400 minute window
   in a 40 km world at 40 km/h, and the fleet is always homogeneous. Time
   windows are therefore almost never binding, which is why lateness is 0.0 in
   every benchmark row.
7. **Scaling is poor and the driver is not the ant loop.** Measured wall-clock
   at 12 ants and 24 iterations: 12 customers 0.04 s, 30 customers 0.70 s, 50
   customers 3.27 s, 80 customers 22.9 s — empirically around O(n^3.5). The cost
   centre is `_required_energy_to_finish`, an O(m^2) nearest-neighbour scan run
   at *every* charger visit in both construction and evaluation. Classic
   benchmark scale (100 to 1000 customers) is out of reach in pure Python here.
8. **Multi-objective handling is a fixed weighted sum** with hard-coded weights.
   No Pareto front, no epsilon-constraint, no normalisation, no weight
   sensitivity analysis. Change the penalty magnitudes and the ranking can
   change.
9. **Parameters are conventional, not tuned.** `alpha=1`, `beta=3`,
   `rho=0.18`, elite weight 2, deposit `80/(1+cost)`, `tau` bounds 0.01 and
   100, 2% jitter, top-20% depositing. Only the battery-state dimension has
   been ablated.
10. **The energy model is a linear load term.** Speed, gradient, temperature,
   regenerative braking and auxiliary load are all absent.
11. **`battery_bucket` uses four uniform slices of capacity.** An arbitrary
   discretisation, never compared against non-uniform buckets or a different
   bucket count.
12. **The solver and evaluator share a charging policy through a private
   import.** `solver.py` imports `_State` from `evaluation.py` inside a method
   to reuse `dynamic_charge_amount`. It achieves the intended guarantee — the
   report describes the run that produced it — through a coupling that would
   not survive a package split.
13. **Convergence budget is uneven.** On 30-customer cases the best cost is
   found around iteration 25-32 of 35, which is reasonable. On 12-customer
   cases it converges by iteration 3-6, so the compact benchmark spends about
   80% of its budget confirming a result it already had.
14. **Failure-mode coverage remains synthetic and local.** Battery-safety,
   lateness, invalid fleet assignment, dynamic charge targets and a feasible
   improving local-search swap now have focused tests. There is still no
   independent route validator, no randomized property test over malformed
   routes, no test of charger oscillation at the hop cap, and no real fleet or
   road-network fixture.

## Fixed after the review

The positional vehicle-mapping defect described in the first review has been
fixed. `RoutePlan` now stores `vehicle_indices`; both builders assign the index
while constructing a route, and `evaluate_plan` validates that assignment. The
new heterogeneous-fleet regression test covers an idle first vehicle followed
by a route from the second vehicle.

The remaining evaluator/solver diagnostic branches are now covered by focused
synthetic tests: a battery safety breach and depletion are both surfaced, an
out-of-range vehicle assignment preserves its fleet-limit diagnostic, dynamic
charging fills only when critical and otherwise targets remaining work, and
local search accepts a cost-improving feasible swap.

## Scope

Synthetic instances only. No client routes, customer addresses, depot
locations, fleet data, or delivery records are in this repository, and no result
here is a claim about a real logistics operation.
