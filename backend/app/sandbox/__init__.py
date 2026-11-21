"""Execution layer of ForgeMini: Docker sandbox, agent tools, agent loop.

Layering (each module knows only about the ones above it in this list):
  changeset.py     pure data: Snapshot (files of a repo) and Changeset (what the agent changed)
  docker_sandbox.py one throw-away container, no network, resource limits
  tools.py          the 4 tools the model may call (bash/read_file/edit_file/finish)
  agent_loop.py     the model <-> tools loop with budgets; returns a *claim*, never a verdict
"""
