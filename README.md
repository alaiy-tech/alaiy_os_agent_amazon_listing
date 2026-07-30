## Alaiy OS Agent Template

Template for building an **Alaiy OS agent** as a standalone Frappe app. Each
agent lives in its own repo: its prompt, tools, output schema, and any UI ship
together and are installed onto an Alaiy OS site as an app. On install the
agent self-registers with Alaiy OS core (the `OS Agent Registry`), the same way
a connector app registers itself with the `OS Connector Registry`.

Core (`alaiy_os`) owns the engine — `OS Agent Registry`, `OS Agent Run`, the
LLM ⇄ tool loop, and the "Agents" hub in the workspace. This app owns one
agent's definition.

### What you edit

Building a new agent means editing four things; the rest is generic plumbing:

| File | What goes there |
|------|-----------------|
| `agent_meta.py` | Identity, model, `max_turns`, output format, and the tool list (`tool_id`, description, handler path, parameter schema). |
| `prompts/system.md` | The system prompt. |
| `schemas/output.json` | The output JSON Schema (only when `output_format` is `"JSON"`). |
| `tools/handlers.py` | The Python callables your tools point at. |

`setup/install.py` reads `agent_meta.py` and upserts the registry row on
install and every migrate — you should not need to touch it.

### Create a new agent from this template

1. Copy this repo to `alaiy_os_agent_<name>` and rename the inner app package
   dir to match. Replace every `alaiy_os_agent_template` /
   `Alaiy Os Agent Template` identifier (in `pyproject.toml`, `hooks.py`,
   `modules.txt`, and the `setup/install.py` import) with your app name.
2. Fill in `agent_meta.py`, `prompts/system.md`, `schemas/output.json`, and
   `tools/handlers.py`.
3. For a custom UI, add a desk Page and set `page` in `agent_meta.py`;
   otherwise it stays `None` and the agent is reached through the core Agents
   hub.

This app stores **no API keys**. Model access comes from Alaiy OS core, and any
third-party keys, usage, and billing are handled by a separate Alaiy service —
do not add a credentials DocType here.

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench --site $SITE install-app alaiy_os_agent_<name>
```

The engine's `anthropic_api_key` is managed by Alaiy OS core, not by this app.

### Running an agent

Agents run through core's REST surface (queued; poll the run):

```
POST /api/method/alaiy_os.api.agents.run_agent   {"agent": "<agent_id>", "payload": {...}}  -> {"run": "RUN-..."}
GET  /api/method/alaiy_os.api.agents.get_run      {"run": "RUN-..."}                          -> status/output/error
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please
[install pre-commit](https://pre-commit.com/#installation) and enable it:

```bash
cd apps/alaiy_os_agent_template
pre-commit install
```

Configured tools: ruff, eslint, prettier, pyupgrade.

### License

agpl-3.0
