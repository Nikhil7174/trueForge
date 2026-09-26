PY ?= .venv/bin/python

.PHONY: seed teardown doctor mcp-cost mcp-iam agent linear

# Create the us-west-2 resources the hygiene agent works on (idempotent).
seed:
	$(PY) infra/seed.py

# Remove exactly the resources recorded in infra/.seed-state.json.
teardown:
	$(PY) infra/teardown.py

# Probe the AWS permissions the MCP servers need (read-only / dry-run).
doctor:
	$(PY) infra/doctor.py

# MCP servers TrueForge connects to (run each in its own terminal).
mcp-cost:
	.venv/bin/tf-cost-mcp

mcp-iam:
	IAM_BACKEND=$${IAM_BACKEND:-aws} .venv/bin/tf-mcp

# Register skills, connectors and the umbrella agent in TrueForge.
agent:
	IAM_BACKEND=$${IAM_BACKEND:-aws} $(PY) -c "import trueforge_hackathon; from trueforge_hackathon.agents.umbrella.plugin import umbrella_agent_name; from trueforge_hackathon.cli.seed import main; main([umbrella_agent_name()])"

# The single Linear entry point: routes labelled tickets (LINEAR_ROUTES) to platform-guardian.
# Poll-only by default; set TRIGGER_MODE=both and LINEAR_WEBHOOK_SECRET to also accept webhooks.
linear:
	TRIGGER_MODE=$${TRIGGER_MODE:-poll} TRIGGER_POLL_SECONDS=$${TRIGGER_POLL_SECONDS:-20} .venv/bin/tf-ticket-trigger
