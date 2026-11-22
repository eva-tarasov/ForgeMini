"""Mini-evals for ForgeMini: run a fixed set of tasks N times against a running server and measure the outcomes.

The headline metric is the FALSE SUCCESS: the system said "verified" but the result is wrong by an independent
oracle (hidden tests the agent never saw), or it said "verified" on a task that must not be solvable.
"""
