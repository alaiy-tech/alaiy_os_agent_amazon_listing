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
| Reads | `Amazon Product Listing` — title, ASIN, marketplace, listing status, condition, offer data, description, bullet points, keywords, images, **product type**, its variation family (`is_variation_parent` / `parent_listing` / `variation_theme`), and Amazon's own **suppression reasons** (the agent is told to fix the issues that name a field it produces) |
| Writes | `Amazon Enriched Listing` (Needs Review) — title, bullet points, description, keywords, images, product type, plus `needs_review` / `confidence` / `notes` |
| On approval | pushes title, description, bullet points, keywords and produced images back onto the `Amazon Product Listing` **main image first**, writes its reviewed product type, and sets its `is_enriched` flag. The connector submits to Amazon on its own schedule; this app never calls SP-API directly. |

Amazon's shape drives the differences from the Shopify agent: variations are separate
sibling listings rather than child rows, and there are no metafields, so the output is
the content fields above plus Amazon's product type. Approval never publishes a sixth
bullet, and an enrichment that produced no bullets, keywords or images leaves what the
listing already has in place rather than emptying it.

### Product type

Amazon's product type (`TOWEL`, `SHIRT`, `LUGGAGE`) is a category key from Amazon's own
register, not free text. Every update Amazon accepts has to declare one, so a listing
without it cannot be changed at all — and the type also decides which conventions the
copy should follow.

**The agent does not produce a product type.** It is derived, and the ordering is the
point:

> enrich the title first → **then** ask Amazon to classify it

The lookup runs inside `save_listing`, against the **enriched** title, never the raw one
the listing arrived with. A product type and the title it is published beside have to
describe the same product; raw Amazon titles are routinely keyword-stuffed,
mistranslated, or about something other than what the enrichment turns out to be, so a
type classified from the old title can contradict the new one — and Amazon rejects that
combination. `product_type.resolve()` takes the enriched title as a required argument
precisely so the ordering is enforced by the signature rather than by convention.

**Amazon is asked on every run, whether or not the listing already has a type.** A
stored product type classifies the copy the listing had when it was synced, and
enrichment rewrites that copy — so the listings most likely to be misclassified are
exactly the ones a "skip if already set" rule would never re-check.

| Situation | What happens |
|---|---|
| Amazon classifies the enriched title | That becomes the enrichment's `product_type`, with the full shortlist stored beside it. `source` = `suggested`. |
| …and it differs from what the listing sells as today | The conflict goes to `needs_review` naming both types. A recategorisation is a decision about a live listing, so a human makes it. |
| Amazon has nothing to say (no answer, lookup failed, auto-accept off) | The type the listing already published with is kept — dropping it would leave the listing unwritable on Amazon. `source` = `listing`. |
| …and there was no stored type either | `product_type` stays empty and "Product type" goes to `needs_review`. Never a guess, and never a fallback to the raw title. |

On approval the reviewed value is written onto the listing, including when it replaces
an existing one — the disagreement was put in front of the reviewer at save time, and
approving is the decision. Correct the field before approving if it is wrong. This
writes the local record only; whether a change of classification can be submitted to
Amazon is the connector's call.


Set `amazon_listing_product_type_auto_accept: 0` in `site_config.json` to stop the top
suggestion being pre-selected, so every product type is chosen by a human. Suggestions
are still gathered and shown either way.

### What you edit

| File | What goes there |
|------|-----------------|
| `agent_meta.py` | Identity, model, `max_turns`, output format, and the tool list. |
| `prompts/system.md` | The system prompt. |
| `schemas/output.json` | The output JSON Schema. |
| `tools/handlers.py` | `get_product`, `view_image`, `get_reference_values`, `save_listing`. |
| `product_type.py` | Which Amazon product type a listing belongs to, and what may be stored as one. |
| `tools/image_prepare.py` | The image step: white-background main image + translated gallery. |
| `image_store.py` | Where a produced image is stored (S3), and how it is read back. |

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

### Where produced images are stored

A processed photo goes to **S3**, and the listing row stores the object's URL. Local
disk was the wrong home for it: the image is produced on whichever worker took the
job, read back minutes later by a reviewer and again at approval time, and expected to
still be there after a redeploy — none of which a single instance's disk gives you,
and none of which a CDN can sit in front of.

Set `S3_BUCKET` and images go to S3. Leave it unset and the app writes local public
Frappe Files exactly as it did before, which is what a dev site and CI want. Nothing
else changes: `tools/images.py` is the only place that knows which backend answered.

| Variable | Default | What it is |
|---|---|---|
| `S3_BUCKET` | *(unset)* | The bucket. **Its presence is the switch** — unset means local Files. Use `white-background`. |
| `IMAGE_S3_REGION` | `ap-south-1` | The bucket's region. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | *(unset)* | Resolved by boto3's own chain, so an instance role or a profile works instead. This app never reads them and holds no credential. |
| `IMAGE_S3_PREFIX` | `images/` | Key prefix. Objects land under `<prefix><category>/<YYYY>/<MM>/`. |
| `IMAGE_S3_ACL` | `private` | The object ACL. `none` sends no ACL header — needed only on a bucket that still uses ACLs but rejects this one. |
| `IMAGE_S3_URL_EXPIRY` | `604800` | Seconds a presigned link stays valid (7 days is SigV4's ceiling). |
| `IMAGE_S3_PUBLIC_BASE_URL` | *(unset)* | A CDN in front of the bucket. Set it and stored URLs become `<base>/<key>` — public, and nothing expires. |
| `IMAGE_S3_ENDPOINT_URL` | *(unset)* | An S3-compatible endpoint (MinIO) instead of AWS. |
| `IMAGE_S3_MAX_ATTEMPTS` | `3` | Upload attempts before giving up on a transient failure. |

Every one of these can equally be set in `site_config.json` under the same name
lowercased (`bench --site $SITE set-config s3_bucket white-background`); the
environment wins where both are present.

**The main image is filed apart from the gallery**, because the two are different work
and a reviewer — or a lifecycle rule — wants to tell them apart without opening them:

```
images/generated/2026/08/listing-main-9f3ab21c-20260811120000123.jpg     ← white background
images/translated/2026/08/listing-gallery-4c81de02-20260811120000456.jpg ← translated
```

The caller's filename is kept, and a millisecond stamp is appended: a re-run on the
same photo writes a **new object**, never over the old one, so a bad result is always
recoverable and the supplier's original is never touched. Each object carries its
`sku`, `role` and `source-url` as S3 metadata.

**Objects are private, so a stored URL is an identity rather than a link.** Everything
that has to hand an image to something else goes through `images.public_image_url`,
which returns a presigned URL good for `IMAGE_S3_URL_EXPIRY`: the AlphaShop calls that
re-process a photo, and the reviewer's form, which asks
`api.image_view_links(sku)` for one link per row before drawing its thumbnails.

That leaves one thing to decide per deployment. **Amazon fetches an image URL for
itself**, on the connector's schedule, and a presigned link has a deadline. If that
schedule can run past `IMAGE_S3_URL_EXPIRY`, put a CDN in front of the bucket and set
`IMAGE_S3_PUBLIC_BASE_URL` — stored URLs become CDN URLs, are public, and never
expire. Without it, keep the connector's submission window inside the expiry.

Uploads retry the transient S3 failures (`SlowDown`, `ThrottlingException`, 5xx,
dropped connections) and log latency per object. An upload that still cannot land
falls back to a local File and writes to the **error log** rather than throwing away
imagery a paid service just produced — so an S3 misconfiguration shows up as a loud
log with the images intact, not as lost work.

#### Migrating a bench that already produced images locally

Nothing is deleted and nothing is rewritten in place, so this is safe to run twice and
to stop halfway.

```bash
bench --site $SITE set-config s3_bucket white-background
bench --site $SITE set-config image_s3_region ap-south-1

# What would move (no uploads):
bench --site $SITE execute alaiy_os_agent_amazon_listing.image_store.migrate_local_images

# Move them:
bench --site $SITE execute alaiy_os_agent_amazon_listing.image_store.migrate_local_images \
  --kwargs "{'dry_run': False}"
```

It uploads every `Amazon Enriched Listing Image` row whose url is still a site-relative
File path and repoints the row at the object, keeping the File itself — so restoring
the previous urls is enough to undo it. Pass `limit` to move a first batch and check
it. Already-approved listings keep whatever url they published with; re-approve a
listing if you want its Amazon Product Listing rows repointed too.

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
