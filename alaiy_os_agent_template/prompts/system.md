You are <AGENT NAME>, an agent running inside Alaiy OS.

Replace this file with your agent's system prompt. Describe:

- ROLE: who the agent is and what single job it does.
- INPUT: what the user message will contain (e.g. a JSON payload, a doc name).
- WORKFLOW: the step-by-step process, including when to call each tool.
- RULES: hard constraints - what it must never invent, formatting, tone.
- OUTPUT: what the final reply must be. When output_format is "JSON", core
  appends the JSON Schema and requires the final message to be exactly one
  JSON object with no prose or code fences, so end by telling the agent to
  "reply with the final JSON object only".

Keep it specific and testable - this prompt is the agent's whole behaviour.
