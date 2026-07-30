# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Tool handlers for this agent.

Each callable here is referenced by dotted path in agent_meta.py and invoked by
the Alaiy OS executor's tool loop. A handler either:

  • returns JSON-serializable data (dict/list/str/…), which is sent back to the
    model as the tool_result, or
  • returns a dict with a "_content_blocks" key holding ready-made Anthropic
    content blocks — use this for vision (e.g. an {"type": "image", ...} block).

Raising is fine: the executor catches the exception and feeds the traceback
back to the model as an errored tool_result, so the agent can recover or fall
back. Prefer a clear frappe.throw() message telling the model what to do next.
"""

import frappe  # noqa: F401  (available to handlers; used by most real tools)


def example_tool(query):
	"""Replace with your real tool. Must return JSON-serializable data."""
	return {"echo": query}
