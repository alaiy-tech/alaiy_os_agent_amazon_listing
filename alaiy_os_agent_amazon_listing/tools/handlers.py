# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The four catalog tools: read the product, look at a photo, read the seller's existing
vocabulary, save the result.

Each callable here is referenced by dotted path in agent_meta's tool catalog and
invoked by the Alaiy OS executor's tool loop as ``handler(**tool_input)``. A
handler either:

  • returns JSON-serializable data (dict/list/str/…), which is sent back to the
    model as the tool_result, or
  • returns a dict with a "_content_blocks" key holding ready-made Anthropic
    content blocks — used here so the model can actually *see* the product
    photos (vision), not just read their URLs.

Raising is fine: the executor catches the exception and feeds it back to the
model as an errored tool_result. We still prefer to degrade gracefully (skip an
unreadable image, guard optional rows) so a single bad attachment does not sink
the whole enrichment.

The source of truth read here is the **Amazon Listing** DocType (its `name` is the
seller SKU, autoname: field:sku), NOT the Item — the listing's own fields (title,
description, bullets, keywords, offer data) and its `images` child table are all we
look at. The agent does not edit the listing (or the Item behind it) and does not
submit anything to Amazon — that is the admin approval / connector step.

save_listing persists the finished enrichment into the Amazon Enriched Listing
DocType in "Needs Review" status, for the admin to edit and approve.
"""

import frappe

from alaiy_os_agent_amazon_listing.tools import images

# Cap how many photos we send to the model to keep token/latency cost bounded.
MAX_IMAGES = 7

# Amazon shows at most five "key product features". More than that is not a style
# preference — the extras are rejected on submission.
MAX_BULLETS = 5

# The DocType the agent reads from. Its `name` is the seller SKU (autoname:
# field:sku), so a caller's sku doubles as the listing name.
LISTING_DOCTYPE = "Amazon Listing"

# The DocType the agent writes to, for admin review.
ENRICHED_DOCTYPE = "Amazon Enriched Listing"


# ── listing photo access (also used by the image tools) ──────────────────────


def listing_image_rows(listing):
	"""The listing's images child rows, main image first, then in table order."""
	rows = list(listing.get("images") or [])
	return sorted(rows, key=lambda r: (0 if r.get("is_main") else 1, r.get("idx") or 0))


def listing_image_urls(listing):
	"""Every usable photo URL on the listing, main image first."""
	seen, urls = set(), []
	for row in listing_image_rows(listing):
		url = row.get("image_url")
		if url and url not in seen:
			seen.add(url)
			urls.append(url)
	return urls


def primary_listing_image_url(listing):
	"""
	A stable URL for the listing's main photo, for an image tool that edits a real
	photo rather than inventing one. Prefers the row flagged ``is_main``; falls back
	to the first image row. None if the listing has no usable photo.

	Amazon photos are normally absolute CDN urls, but a listing whose photos were
	uploaded locally will carry a Frappe File path instead — resolve either through
	images.reference_source or images.public_image_url depending on whether you or
	the service reads it.
	"""
	urls = listing_image_urls(listing)
	return urls[0] if urls else None


def get_listing(sku):
	"""The Amazon Listing for `sku`, or throw a useful message."""
	if not frappe.db.exists(LISTING_DOCTYPE, sku):
		frappe.throw(
			f"No {LISTING_DOCTYPE} found for sku '{sku}'. "
			"Check the input or ask the admin to confirm the product has a listing."
		)
	return frappe.get_doc(LISTING_DOCTYPE, sku)


def _collect_image_blocks(listing):
	"""
	Gather up to MAX_IMAGES photo blocks from an Amazon Listing's `images` child
	table, main image first. Each row's `image_url` is either an Amazon CDN url or a
	stored File. Labels say which one is the main image, because that is the photo
	the shopper sees in search results and the one the title has to agree with.
	"""
	blocks = []
	for row in listing_image_rows(listing):
		if len(blocks) >= MAX_IMAGES:
			break
		url = row.get("image_url")
		block = images.image_block_from_url(url)
		if block:
			label = f"{url} (main image)" if row.get("is_main") else url
			blocks.append((label, block))
	return blocks


# ── tools ─────────────────────────────────────────────────────────────────────


def get_product(sku):
	"""
	Return an Amazon Listing's data plus its product photos as vision content
	blocks. The listing's `name` is the seller SKU, so the caller's sku is used
	directly as the listing name. The model receives a text block of the structured
	data followed by one labelled image block per photo. Reads strictly from the
	listing — never the underlying Item.

	The listing's open `issues` (Amazon's own suppression reasons and warnings) are
	included deliberately: they are the closest thing to a brief this agent gets,
	and an ERROR there is usually why the listing is not selling.

	Both `image_urls` (all photos, main first) and `primary_image_url` (the best one
	to use as an edit base) are returned, so an image tool has whichever it needs
	without a second read.
	"""
	listing = get_listing(sku)

	data = {
		"sku": listing.name,
		"title": listing.get("title"),
		"asin": listing.get("asin"),
		"item_code": listing.get("product"),
		"marketplace": listing.get("marketplace"),
		"listing_status": listing.get("listing_status"),
		"fulfillment_channel": listing.get("fulfillment_channel"),
		"condition": listing.get("condition"),
		"price": listing.get("price"),
		"currency": listing.get("currency"),
		"quantity": listing.get("quantity"),
		"description": listing.get("description"),
		"bullet_points": [
			row.get("bullet") for row in (listing.get("bullet_points") or []) if row.get("bullet")
		],
		"keywords": [
			row.get("keyword") for row in (listing.get("keywords") or []) if row.get("keyword")
		],
		"image_urls": listing_image_urls(listing),
		"primary_image_url": primary_listing_image_url(listing),
		"issues": [
			{
				"code": row.get("code"),
				"severity": row.get("severity"),
				"message": row.get("message"),
				"attribute_names": row.get("attribute_names"),
			}
			for row in (listing.get("suppression_reasons") or [])
		],
	}

	labelled = _collect_image_blocks(listing)
	data["image_count"] = len(labelled)

	blocks = [
		{
			"type": "text",
			"text": "Amazon Listing data (JSON):\n" + frappe.as_json(data),
		}
	]
	if data["issues"]:
		blocks.append({
			"type": "text",
			"text": (
				"This listing has open Amazon issues (see `issues` above). Fix every one "
				"that names a content field you produce — title, bullet points, "
				"description or search terms — and record in `notes` which you addressed "
				"and which the admin still has to handle."
			),
		})

	if labelled:
		blocks.append({
			"type": "text",
			"text": f"\n{len(labelled)} product photo(s) follow. They are your primary "
			"visual evidence — study them, including any text printed onto the image:",
		})
		for idx, (label, image_block) in enumerate(labelled, start=1):
			blocks.append({"type": "text", "text": f"Photo {idx}: {label}"})
			blocks.append(image_block)
	else:
		blocks.append({
			"type": "text",
			"text": "No usable product photos are on the listing. Enrich from the text "
			"only and flag every visually-determined attribute in needs_review.",
		})

	return {"_content_blocks": blocks}


def view_image(image_url):
	"""
	Fetch an external image URL and hand it back as a vision block, so the model
	can actually look at a product it only knows as a bare URL (no sku / listing to
	read a photo from otherwise).
	"""
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Reference image ({image_url}):"},
			images.fetch_image_block(image_url),
		]
	}


def _distinct_values(doctype, column, limit=2000):
	"""
	Distinct non-empty values of a column. Raw SQL rather than frappe.get_all because
	one caller reads a CHILD table (Amazon Listing Keyword), which get_all refuses
	without a parent doctype.
	"""
	if not frappe.db.table_exists(doctype) or not frappe.db.has_column(doctype, column):
		return []
	rows = frappe.db.sql(
		f"select distinct `{column}` from `tab{doctype}` "
		f"where `{column}` is not null and `{column}` != '' limit {int(limit)}"
	)
	return sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()})


def get_reference_values():
	"""
	The vocabulary already in use for the exact fields the agent fills, so it reuses
	established terms instead of inventing near-duplicates.

	`keywords` are this seller's existing backend search terms across every listing.
	They are the one field where consistency across a catalog genuinely compounds:
	a shopper who finds one of these listings should find the neighbouring ones too.

	`marketplaces` decide the language and spelling the copy has to be written in —
	`en-GB` for amazon.co.uk, `en-US` for amazon.com — which is not something the
	product data itself says.

	Every lookup is guarded: these doctypes belong to the Amazon connector and may
	not be installed.
	"""
	return {
		"keywords": _distinct_values("Amazon Listing Keyword", "keyword"),
		"marketplaces": _distinct_values(LISTING_DOCTYPE, "marketplace"),
	}


def _flatten(value):
	"""One value as the plain text a grid cell (and Amazon) wants.

	The schema says these are strings, and they almost always are. A model that
	returns a list or an object anyway must not end up writing "['a', 'b']" into a
	bullet, so lists become a comma-separated line and anything else falls back to
	JSON rather than Python's repr.
	"""
	if value is None:
		return None
	if isinstance(value, str):
		return value
	if isinstance(value, (int, float, bool)):
		return str(value)
	if isinstance(value, (list, tuple)):
		return ", ".join(_flatten(v) or "" for v in value)
	return frappe.as_json(value)


def save_listing(listing, sku=None):
	"""
	Persist an enriched listing into the shared Amazon Enriched Listing DocType for
	admin review. Upserts by sku (one row per listing): re-running the listing agent
	on the same SKU updates the existing row instead of creating a duplicate.

	`listing` is the full enrichment object — the same shape the agent returns
	(schemas/output.json). `sku` identifies the source listing and is the upsert key;
	it falls back to listing["sku"] if not passed separately. The row lands in
	"Needs Review" status so an admin edits/approves it before anything is published.

	Every field written here corresponds to a field on the Amazon Listing itself
	(title -> title, description -> description, bullet_points -> bullet_points,
	keywords -> keywords, images -> images), plus the three review fields the
	approval step needs. The agent produces nothing else.

	List-valued fields (needs_review, notes) are flattened to one-per-line text for a
	readable Desk form. The ordered parts — the bullets and the keywords — are written
	BOTH as child tables, which is what the Desk form shows, what an admin edits, and
	what approval publishes, AND verbatim as JSON, which is the audit copy. The whole
	payload is kept as JSON too, so nothing is lost even if the flattened fields drift
	from the schema.

	Image rows use one shape — {kind, source_url, url, brief, note}, each column
	optional — so a single child table serves both image steps.

	Returns {name, status, url} pointing at the new/updated record.
	"""
	sku = sku or (listing or {}).get("sku")
	if not sku:
		frappe.throw(
			"save_listing needs a sku (pass it, or include it in the listing). This "
			"tool persists listings keyed to a product; a URL-only product has no "
			"record to write to — skip this tool and just return the JSON."
		)
	if not frappe.db.exists(LISTING_DOCTYPE, sku):
		frappe.throw(f"No {LISTING_DOCTYPE} found for sku '{sku}'; cannot save the listing.")

	if frappe.db.exists(ENRICHED_DOCTYPE, sku):
		doc = frappe.get_doc(ENRICHED_DOCTYPE, sku)
	else:
		doc = frappe.new_doc(ENRICHED_DOCTYPE)
		doc.sku = sku

	doc.status = "Needs Review"
	doc.title = listing.get("title")
	doc.description = listing.get("description")
	doc.confidence = listing.get("confidence")

	# list-valued fields -> one item per line for a readable Desk form
	doc.needs_review = "\n".join(listing.get("needs_review") or [])
	doc.notes = "\n".join(listing.get("notes") or [])

	# the ordered content -> pretty JSON; whole payload kept verbatim for audit
	doc.bullets_json = frappe.as_json(listing.get("bullet_points") or [])
	doc.keywords_json = frappe.as_json(listing.get("keywords") or [])
	doc.output_json = frappe.as_json(listing)

	# The same two things again, as child tables — a reviewer reads and edits rows,
	# not a JSON blob, exactly as they do on the Amazon Listing itself. These tables
	# are what approval publishes from (see AmazonEnrichedListing._sync_bullets /
	# _sync_keywords), so an edit made there reaches Amazon; the JSON fields beside
	# them stay the agent's own words.
	#
	# Amazon caps the bullets at five. A model that returns more has misread its
	# instructions, and silently publishing a sixth bullet would have the listing
	# rejected — so the extras are dropped here and called out for the reviewer.
	bullets = [b for b in (_flatten(b) for b in (listing.get("bullet_points") or [])) if b]
	dropped = bullets[MAX_BULLETS:]
	doc.set("bullet_points", [])
	for bullet in bullets[:MAX_BULLETS]:
		doc.append("bullet_points", {"bullet": bullet})
	if dropped:
		doc.needs_review = "\n".join(
			filter(None, [doc.needs_review, f"Bullet points (the agent returned {len(bullets)}; "
			f"only the first {MAX_BULLETS} were kept)"])
		)

	# Deduplicated case-insensitively: two spellings of one search term is a wasted
	# byte of a 250-byte budget, not two keywords.
	doc.set("keywords", [])
	seen = set()
	for keyword in (listing.get("keywords") or []):
		keyword = (_flatten(keyword) or "").strip()
		if not keyword or keyword.lower() in seen:
			continue
		seen.add(keyword.lower())
		doc.append("keywords", {"keyword": keyword})

	# rebuild the image child table from whatever the image tool produced
	doc.set("images", [])
	for img in (listing.get("images") or []):
		doc.append("images", {
			"kind": img.get("kind"),
			"source_url": img.get("source_url"),
			"url": img.get("url"),
			"brief": img.get("brief"),
			"note": img.get("note"),
		})

	# A row with no url is one the image step queued: the imagery is rendered after
	# this run finishes (see image_stage.py), so the listing is reviewable now and
	# says plainly that its pictures are still coming. Rows that already have a url
	# are ones the image step reused from an earlier run — nothing is owed for those,
	# so the listing is already Ready. Recomputed on every save, so a re-run that
	# queues fresh images resets a previous run's verdict.
	if any(not row.url for row in doc.images):
		doc.image_status = "Queued"
	elif doc.images:
		doc.image_status = "Ready"
	else:
		doc.image_status = "Not Required"
	doc.image_error = None

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"status": doc.status,
		"url": f"/app/amazon-enriched-listing/{doc.name}",
	}
