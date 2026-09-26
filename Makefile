PY ?= .venv/bin/python

.PHONY: seed teardown doctor

# Create the us-west-2 resources the hygiene agent works on (idempotent).
seed:
	$(PY) infra/seed.py

# Remove exactly the resources recorded in infra/.seed-state.json.
teardown:
	$(PY) infra/teardown.py

# Probe the AWS permissions the MCP servers need (read-only / dry-run).
doctor:
	$(PY) infra/doctor.py
