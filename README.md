## Alaiy OS Agent — Amazon Listing

The **Amazon listing agent** for Alaiy OS, shipped as a standalone Frappe app. It
takes one product's existing **Amazon Listing** and rewrites the content fields
Amazon actually indexes — title, bullet points, description, and backend search
keywords — plus, optionally, its photos. It never publishes: every run lands in an
`Amazon Enriched Listing` in *Needs Review* status for an admin to edit and approve.

Core (`alaiy_os`) owns the engine — `OS Agent Registry`, `OS Agent Run`, the
LLM ⇄ tool loop, and the "Agents" hub. This app owns one agent's definition, its
tools, its review DocTypes, and its desk surfaces.

It is the Amazon counterpart to `alaiy_os_agent_shopify_listing` and works the same
way; the difference is which registry it reads. This one reads the **Amazon Listing**
DocType from `alaiy_os_connector_amazon_sp_api`, keyed by seller **SKU**.

### What it reads and writes

| | |
|---|---|
| Reads | `Amazon Listing` — title, ASIN, marketplace, listing status, condition, offer data, description, bullet points, keywords, images, and Amazon's own **suppression reasons** (the agent is told to fix the issues that name a field it produces) |
| Writes | `Amazon Enriched Listing` (Needs Review) — title, bullet points, description, keywords, images, plus `needs_review` / `confidence` / `notes` |
| On approval | pushes title, description, bullet points, keywords and produced images back onto the `Amazon Listing`, and sets its `is_enriched` flag. The connector submits to Amazon on its own schedule; this app never calls SP-API. |

Amazon's shape drives the differences from the Shopify agent: there are no variants,
no category or product type and no metafields, so the output is the five content
fields above. Approval never publishes a sixth bullet, and an enrichment that
produced no bullets, keywords or images leaves what the listing already has in place
rather than emptying it.

### What you edit

| File | What goes there |
|------|-----------------|
| `agent_meta.py` | Identity, model, `max_turns`, output format, and the tool list. |
| `prompts/system.md` | The system prompt. |
| `schemas/output.json` | The output JSON Schema. |
| `tools/handlers.py` | `get_product`, `view_image`, `get_reference_values`, `save_listing`. |
| `tools/image_generation.py`, `tools/image_translation.py` | The two optional image steps. |

`setup/install.py` reads `agent_meta.py` and upserts the registry row on install and
every migrate — you should not need to touch it.

### Per-customer overrides

A customer app changes this agent by dropping one markdown file at
`<customer_app>/agents/amazon_listing.md`. Its contents are appended to the vanilla
prompt; optional frontmatter sets `model` and `description`. There is no hook and no
config — the file being there is the whole mechanism. Both image tools are always
registered, so choosing between retouching photos and translating them is a sentence
in that file, not a setting.

This app stores **no API keys**. Model and image access come from Alaiy OS core's
`ai_client` seam.

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench --site $SITE install-app alaiy_os_agent_amazon_listing
```

It can be installed alongside `alaiy_os_agent_shopify_listing`: the two register
different agent ids, own separate DocTypes, use separate desk pages
(`run-amazon-agent` vs `run-agent`), separate realtime events, and separate client
namespaces.

### Running it

From the desk: **Enrich Listing** on an `Amazon Listing` form, **Enrich Listings**
in its list view (bulk, on workers), **Enrich Amazon Listing** on an `Item`, or the
**Amazon Listing** page under Agents in the OS sidebar.

Over REST, through core (queued; poll the run):

```
POST /api/method/alaiy_os.api.agents.run_agent   {"agent": "amazon_listing", "payload": {"sku": "..."}}  -> {"run": "RUN-..."}
GET  /api/method/alaiy_os.api.agents.get_run     {"run": "RUN-..."}                                      -> status/output/error
```

Bulk:

```
POST /api/method/alaiy_os_agent_amazon_listing.api.bulk_enrich     {"skus": ["...", "..."]}
GET  /api/method/alaiy_os_agent_amazon_listing.api.get_bulk_status {"batch": "AMZ-BULK-..."}
```

### Images

Both image steps run in a **second stage**, after the agent's run closes: the tool
returns `url: null` placeholders and queues the rendering, so a run finishes in the
time its LLM turns take rather than the minutes the image API takes. The enriched
listing carries an `Image Status` while that happens. To give stage two its own
worker pool, declare a queue under `workers` in `common_site_config.json` and set:

```json
{ "listing_image_queue": "<queue name>" }
```

It falls back to `long` when that is not configured.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please
[install pre-commit](https://pre-commit.com/#installation) and enable it:

```bash
cd apps/alaiy_os_agent_amazon_listing
pre-commit install
```

Configured tools: ruff, eslint, prettier, pyupgrade.

### License

agpl-3.0
