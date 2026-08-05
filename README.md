## Alaiy OS Agent — Amazon Listing

The **Amazon listing agent** for Alaiy OS, shipped as a standalone Frappe app. It
takes one product's existing **Amazon Product Listing** and rewrites the content fields
Amazon actually indexes — title, bullet points, description, and backend search
keywords — plus, optionally, its photos. It never publishes: every run lands in an
`Amazon Enriched Listing` in *Needs Review* status for an admin to edit and approve.

Core (`alaiy_os`) owns the engine — `OS Agent Registry`, `OS Agent Run`, the
LLM ⇄ tool loop, and the "Agents" hub. This app owns one agent's definition, its
tools, its review DocTypes, and its desk surfaces.

It is the Amazon counterpart to `alaiy_os_agent_shopify_listing` and works the same
way; the difference is which registry it reads. This one reads the **Amazon Product Listing**
DocType from `alaiy_os_connector_amazon_sp_api`, keyed by seller **SKU**.

### What it reads and writes

| | |
|---|---|
| Reads | `Amazon Product Listing` — title, ASIN, marketplace, listing status, condition, offer data, description, bullet points, keywords, images, its variation family (`is_variation_parent` / `parent_listing` / `variation_theme`), and Amazon's own **suppression reasons** (the agent is told to fix the issues that name a field it produces) |
| Writes | `Amazon Enriched Listing` (Needs Review) — title, bullet points, description, keywords, images, plus `needs_review` / `confidence` / `notes` |
| On approval | pushes title, description, bullet points, keywords and produced images back onto the `Amazon Product Listing` **main image first**, and sets its `is_enriched` flag. The connector submits to Amazon on its own schedule; this app never calls SP-API. |

Amazon's shape drives the differences from the Shopify agent: variations are separate
sibling listings rather than child rows, and there is no category, product type or
metafields, so the output is the five content fields above. Approval never publishes a sixth bullet, and an enrichment that
produced no bullets, keywords or images leaves what the listing already has in place
rather than emptying it.

### What you edit

| File | What goes there |
|------|-----------------|
| `agent_meta.py` | Identity, model, `max_turns`, output format, and the tool list. |
| `prompts/system.md` | The system prompt. |
| `schemas/output.json` | The output JSON Schema. |
| `tools/handlers.py` | `get_product`, `view_image`, `get_reference_values`, `save_listing`. |
| `tools/image_prepare.py` | The image step: white-background main image + translated gallery. |

`setup/install.py` reads `agent_meta.py` and upserts the registry row on install and
every migrate — you should not need to touch it.

### Per-customer overrides

A customer app changes this agent by dropping one markdown file at
`<customer_app>/agents/amazon_listing.md`. Its contents are appended to the vanilla
prompt; optional frontmatter sets `model` and `description`. There is no hook and no
config — the file being there is the whole mechanism. Use it for the brand name, the
house voice, and category rules the vanilla prompt cannot know — not for the image
step, which is fixed (see below).

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

From the desk: **Enrich Listing** on an `Amazon Product Listing` form, **Enrich Listings**
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

### Content rules

The prompt and schema encode the house Amazon rules, so a run either follows them or
lands in review saying why it could not:

- **Title** — `Brand Keyword | Type / Material | Feature | Application | Variant`,
  120–150 characters, brand once at the front, unique per child variant.
- **Bullets** — exactly 5, `UPPERCASE HEADING - description`, ~180–250 characters, in
  the fixed order Material → Design → Applications → Capacity → Everyday Use.
- **Description** — 180–250 words in four paragraphs, then a `Package Includes: /
  Material: / Color: / Size: / Dimensions: / Capacity: / Usage:` block carrying only
  the fields the data actually provides.
- **Keywords** — backend search terms only, never repeating a word already in the
  title or bullets, under ~250 bytes.
- **Restricted words** — medical, absolute, unsupported-quality, environmental,
  safety, promotional and shipping claims, competitor brands, third-party IP, ®/™ and
  emojis are all listed in the prompt and refused unless the data substantiates them.
- **Never invent a specification.** A spec the catalog does not record is expanded
  around using features, functionality, applications and target users, and named in
  `needs_review`. `get_product` shows the agent exactly which specs exist
  (`variant_specifications`, read from the linked Item's specs) so "provided" is a
  fact, not a guess.

### Images

There is **one** image step, and it produces two kinds of image in one call:

| Role | What | How |
|---|---|---|
| `main` | **This variant's own photo** on a plain white background — the search-results tile | AlphaShop `extract_object(transparent=False)` |
| `gallery` | The family's photos, in order | AlphaShop `translate_image` |

The main photo is resolved in code, never by the model: `skuImage` from the linked
Item's variant specs → this listing's own `is_main` row → the family's first photo.
The last of those is a fallback, and it is logged *and* pushed into `needs_review`,
because it means the shopper sees a generic image instead of the variant they picked.

"The family" is the connector's own variation model: for a child listing
(`parent_listing` set, `is_variation_parent` clear) the gallery is the **parent
listing's** images; for a standalone listing or a parent it is the listing's own.
The ordering survives the model: the plan travels with the queued job, and stage two
re-derives the rows and re-sorts them main-first regardless of what the model echoed
back.

> **Not yet available:** the `ai_client` seam in `alaiy_os` implements
> `translate_image` but **not** background extraction. Until `image_support()` reports
> `extract` and the client grows `extract_object(url, transparent=False)`, the main
> image is only translated, and the run says so in `notes` and `needs_review` rather
> than shipping a main image Amazon will suppress. See `tools/image_prepare.py`.

It runs in a **second stage**, after the agent's run closes: the tool returns
`url: null` placeholders and queues the rendering, so a run finishes in the time its
LLM turns take rather than the minutes the image APIs take. The enriched listing
carries an `Image Status` while that happens. To give stage two its own worker pool,
declare a queue under `workers` in `common_site_config.json` and set:

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
