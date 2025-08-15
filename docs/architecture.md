# RouteForge architecture

`models.py` contains immutable domain records. `instance.py` provides deterministic generators and the shared distance, travel-time, energy, and battery-bucket functions. `evaluation.py` is the source of truth for simulation and cost accounting; it reports hard violations instead of hiding them in a penalty number.

`solver.py` has a small greedy builder and `StateAwareACO`. Both use the same candidate feasibility checks and dynamic charge policy. The ACO keeps a three-dimensional pheromone tensor, where the third axis is the battery bucket before an edge. Pheromone deposits are tied to the actual bucket observed while constructing an ant's route.

The CLI only wires these components together. `report.py` renders the immutable result to JSON, CSV, and static HTML/SVG so a reviewer can inspect a run without Python. `benchmark.py` reuses the same solver/evaluator path for one compact and one larger stress instance, while `sweep.py` repeats both cases over explicit seeds and records aggregate means/population standard deviations. Neither module silently changes the cost model or treats an incomplete route as a win; feasibility, served-customer counts, violations, charging, lateness, convergence, and the greedy comparison remain visible in every artifact.

## State transition

1. Start a vehicle at the depot with full battery and empty compartments.
2. Filter unvisited customers by battery safety, two-compartment capacity, and service-window feasibility.
3. Select a customer by pheromone × distance/urgency heuristic, or move to a reachable charger when no customer is feasible.
4. At a charger, estimate remaining energy and add a bounded charge target; record charge time and kWh.
5. Return to the depot, evaluate the complete plan, update the state-aware pheromone tensor, and retain the best plan.

The evaluator repeats these transitions independently. This intentional duplication is a guard against a solver reporting a plan that cannot be replayed.
