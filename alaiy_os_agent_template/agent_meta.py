# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Single source of truth for this agent's registration metadata — the agent
equivalent of a connector's connector_meta.py. Consumed by setup/install.py →
upserted into alaiy_os's OS Agent Registry (and its OS Agent Tool child rows).

To spin up a new agent from this template, rename "template"/"Template"
throughout, edit the values below, and fill in prompts/system.md,
schemas/output.json, and tools/handlers.py.

Credentials are NOT stored here. Model access is provided by Alaiy OS core
(the engine's anthropic_api_key) and any third-party keys/usage/billing are
handled by a separate Alaiy service, not by this app.
"""

import json
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent


def _read(relpath):
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


agent_meta = {
	# ── Identity (OS Agent Registry) ──────────────────────────────────────────
	# agent_id is the primary key. Keep it stable across releases — changing it
	# orphans run history and creates a second agent.
	"agent_id": "template",
	"agent_name": "Template Agent",
	"description": "Template agent — rename me.",
	"icon": "cpu",  # Lucide/Feather icon name, shown in the Agents hub
	# Optional custom desk Page this app ships for the agent's UI; None reaches
	# the agent through the core Agents hub (the OS Agent Registry list).
	"page": None,
	# No settings DocType: this app stores no credentials (see module docstring).
	"settings_doctype": None,

	# ── Engine config ─────────────────────────────────────────────────────────
	"model": "claude-sonnet-5",
	"max_turns": 8,
	"system_prompt": _read("prompts/system.md"),
	# "Text" for freeform output, or "JSON" for a schema-validated object.
	"output_format": "JSON",
	"output_schema": json.loads(_read("schemas/output.json")),

	# ── Tools (OS Agent Tool child rows) ──────────────────────────────────────
	# handler: importable dotted path to a callable in this app.
	# parameters_schema: JSON Schema (type: object) for the tool's arguments.
	# connector: optional OS Connector Registry id this tool depends on; the
	#            engine refuses to run the agent if that connector is missing.
	"tools": [
		{
			"tool_id": "example_tool",
			"description": (
				"Tell the LLM what this tool does and when to call it. This text "
				"is sent to the model verbatim."
			),
			"handler": "alaiy_os_agent_template.tools.handlers.example_tool",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"query": {"type": "string", "description": "What to look up."},
				},
				"required": ["query"],
			},
			"connector": None,
		},
	],
}
