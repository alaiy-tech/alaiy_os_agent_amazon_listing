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

The source of truth read here is the **Amazon Product Listing** DocType (its `name` is the
seller SKU, autoname: field:sku), NOT the Item — the listing's own fields (title,
description, bullets, keywords, offer data) and its `images` child table are all we
look at. The agent does not edit the listing (or the Item behind it) and does not
submit anything to Amazon — that is the admin approval / connector step.

save_listing persists the finished enrichment into the Amazon Enriched Listing
DocType in "Needs Review" status, for the admin to edit and approve.
"""

import frappe

from alaiy_os_agent_amazon_listing import brand as brands
from alaiy_os_agent_amazon_listing import product_type as product_types
from alaiy_os_agent_amazon_listing.tools import images

# Cap how many photos we send to the model to keep token/latency cost bounded.
MAX_IMAGES = 7

# Amazon shows at most five "key product features". More than that is not a style
# preference — the extras are rejected on submission.
MAX_BULLETS = 5

# The DocType the agent reads from. Its `name` is the seller SKU (autoname:
# field:sku), so a caller's sku doubles as the listing name.
LISTING_DOCTYPE = "Amazon Product Listing"

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


# ── the image plan: which photo is the main one ──────────────────────────────
# Amazon shows ONE image in search results, and for a child variant it has to be that
# variant's own photo — a shopper who picked "black" must not be shown the family's
# generic shot. That is a rule about which photo, not about how it is processed, so it
# is resolved here next to the rest of the listing reads rather than inside the image
# tool.
#
# Where the variant's own photo comes from, in order:
#
#   1. `skuImage` on the linked Item's variant specs. This is the authoritative
#      per-variant photo in the sourcing data. It arrives as a custom field
#      (`ng_specs_json`) owned by the customer app that ingests it, so it is read
#      defensively — a bench without that app simply falls through.
#   2. This listing's own row flagged `is_main`.
#   3. The first family photo — a fallback that IS logged and reported, because it
#      means the shopper sees a generic image where they should have seen theirs.
#
# "The family" is the parent listing, which the connector models natively:
# `parent_listing` links a child to its variation parent (`is_variation_parent`), and
# `variation_theme` says what the family varies by. So the gallery is the PARENT's
# images when there is a parent, and the listing's own when there is not — which is
# what "the remaining family images, Image 1..N, in order" actually means for a child.

# The custom field the sourcing app puts on Item. Absent on a bench without it.
_ITEM_SPECS_FIELD = "ng_specs_json"


def image_plan(main, gallery, main_fallback=False):
	"""The ordered plan the image step works from: main first, then the gallery.

	`targets` is what travels with the queued job — one entry per (photo, role) pair,
	in listing order — so stage two writes the listing's image rows from the plan
	rather than from whatever the model echoed back.

	A photo that is both the variant's own and the family's first appears twice, once
	per role, on purpose: the two roles want different processing (white background vs
	translated) and so are two different results.
	"""
	targets = []
	if main:
		targets.append({"role": "main", "source_url": main})
	for url in gallery:
		targets.append({"role": "gallery", "source_url": url})
	return {"targets": targets, "main": main, "gallery": list(gallery), "main_fallback": main_fallback}


def item_variant_specs(item_code):
	"""The variant's specifications from the linked Item, as {name: value}.

	Amazon's copy rules turn on these: a spec that IS provided must appear in the
	title, bullets and description, and one that is not must never be invented. So the
	agent is shown exactly what the catalog records and nothing more.

	Same guarded read as _item_specs_image — the field belongs to the sourcing app.
	"""
	specs = _item_specs(item_code)
	out = {}
	for spec in specs:
		if not isinstance(spec, dict):
			continue
		name = (spec.get("attributeName") or "").strip()
		value = spec.get("value")
		if name and value:
			out[name] = value
	return out


def _item_specs(item_code):
	"""The linked Item's raw variant specs list, or [] when unavailable."""
	import json

	if not item_code or not frappe.db.has_column("Item", _ITEM_SPECS_FIELD):
		return []
	raw = frappe.db.get_value("Item", item_code, _ITEM_SPECS_FIELD)
	if not raw:
		return []
	try:
		specs = json.loads(raw)
	except (ValueError, TypeError):
		return []
	return specs if isinstance(specs, list) else []


def _item_specs_image(item_code):
	"""The variant's own `skuImage` from the linked Item's specs, or None.

	The specs field is a JSON list of {attributeId, attributeName, value, skuImage}.
	Guarded end to end: the field belongs to the sourcing app and may not exist, and a
	malformed blob must cost this listing its variant photo, not its whole enrichment.
	"""
	for spec in _item_specs(item_code):
		if isinstance(spec, dict) and spec.get("skuImage"):
			return spec["skuImage"]
	return None


def family_image_urls(listing):
	"""The family's photos for this listing, in order — the gallery.

	For a CHILD listing that is the variation parent's image set: the parent carries
	the family shots (Image 1..N) and the child carries its own variant photo. For a
	standalone listing, or a parent itself, it is simply the listing's own images.

	Falls back to the listing's own photos when the parent link is dangling or the
	parent has no images of its own, so a broken parentage costs the gallery nothing.
	"""
	own = listing_image_urls(listing)
	parent = listing.get("parent_listing")
	if not parent or listing.get("is_variation_parent"):
		return own
	if not frappe.db.exists(LISTING_DOCTYPE, parent):
		return own
	family = listing_image_urls(frappe.get_doc(LISTING_DOCTYPE, parent))
	return family or own


def resolve_image_plan(listing):
	"""The image plan for one Amazon Product Listing: the variant's photo, then the
	family's.

	The variant photo is NOT removed from the gallery when it is also a family photo —
	Amazon's gallery is the family's images and the main tile is separate, so the same
	photo legitimately appears in both, processed differently for each.

	Falls back to the family's first photo when the variant has none, and says so in
	`main_fallback` so the tool can log it and put it in front of a human.
	"""
	family = family_image_urls(listing)
	own = listing_image_urls(listing)

	# 1. the variant's own skuImage, 2. this listing's own main row, 3. the family's
	# first photo — the last of which is the fallback worth shouting about.
	main = _item_specs_image(listing.get("product")) or None
	fallback = False
	if not main:
		main = own[0] if own else None
	if not main:
		main = family[0] if family else None
		fallback = bool(main)

	if fallback and main:
		frappe.log_error(
			title=f"Amazon listing images: no variant photo for {listing.name}",
			message=(
				f"{listing.name} has no skuImage on its linked Item "
				f"({listing.get('product') or 'no item'}) and no image of its own, so "
				f"the family's first photo ({main}) was used as the main image. The "
				"shopper will see a generic image rather than the variant they selected."
			),
		)

	return image_plan(main=main, gallery=family, main_fallback=fallback)


def get_listing(sku):
	"""The Amazon Product Listing for `sku`, or throw a useful message."""
	if not frappe.db.exists(LISTING_DOCTYPE, sku):
		frappe.throw(
			f"No {LISTING_DOCTYPE} found for sku '{sku}'. "
			"Check the input or ask the admin to confirm the product has a listing."
		)
	return frappe.get_doc(LISTING_DOCTYPE, sku)


def _collect_image_blocks(listing):
	"""
	Gather up to MAX_IMAGES photo blocks from an Amazon Product Listing's `images` child
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


def _product_type_block(product_type):
	"""What to tell the model about the listing's product type: the category, only.

	Classification is deterministic — `_save_product_type` derives it from the
	finished title — so how and when that happens is not the model's business and
	is not described here. The one thing worth spending tokens on is the category
	itself, which a static prompt cannot name because it differs per listing, and
	which genuinely changes the copy: a TOWEL and a POWER_TOOL want different
	specifications and different words.

	A listing with no product type gets nothing at all. There is no category to
	write to, and the behaviour that would matter — name the product plainly in
	the title — is a title rule, so it lives in the title rules.
	"""
	if not product_type:
		return None

	return (
		f"This listing sells as Amazon product type `{product_type}`. Write the copy for "
		"that category: the specifications that matter there, and the words a shopper in "
		"it searches with, are the ones to use. If the product is plainly NOT that thing, "
		"describe what it actually is and say so in `notes` — a reviewer settles it."
	)


# ── tools ─────────────────────────────────────────────────────────────────────


def get_product(sku):
	"""
	Return an Amazon Product Listing's data plus its product photos as vision content
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

	`product_type` is the category this listing sells in today, and it is context
	the copy depends on — a TOWEL and a POWER_TOOL want different bullets. It is
	reported, never looked up: classification runs on the enriched title at save
	time, for every listing, because the product type has to match the copy that
	will actually be published (see product_type.py).
	"""
	listing = get_listing(sku)

	data = {
		"sku": listing.name,
		"title": listing.get("title"),
		"asin": listing.get("asin"),
		"item_code": listing.get("product"),
		"marketplace": listing.get("marketplace"),
		"product_type": product_types.existing(listing),
		"listing_status": listing.get("listing_status"),
		"is_variation_parent": bool(listing.get("is_variation_parent")),
		"parent_asin": listing.get("parent_asin"),
		"parent_listing": listing.get("parent_listing"),
		"variation_theme": listing.get("variation_theme"),
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
		"variant_specifications": item_variant_specs(listing.get("product")),
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
			"text": "Amazon Product Listing data (JSON):\n" + frappe.as_json(data),
		}
	]
	if data["parent_listing"] and not data["is_variation_parent"]:
		blocks.append({
			"type": "text",
			"text": (
				f"This is a CHILD listing in the variation family {data['parent_listing']}"
				+ (f", which varies by {data['variation_theme']}. " if data["variation_theme"] else ". ")
				+ "Its title must be UNIQUE among its siblings and must carry this "
				"variant's own specification — two children with the same title are a "
				"duplicate-listing problem, not a style one."
			),
		})

	product_type_note = _product_type_block(data["product_type"])
	if product_type_note:
		blocks.append({"type": "text", "text": product_type_note})

	if data["variant_specifications"]:
		blocks.append({
			"type": "text",
			"text": (
				"`variant_specifications` above is what the catalog actually records for "
				"THIS variant. Every one of those values must appear naturally in the "
				"title, the bullets and the description. Any specification NOT listed "
				"there is not available — do not invent, assume or infer it; expand the "
				"copy with features, functionality, applications and target users "
				"instead, and add the missing field to needs_review."
			),
		})

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


def _save_product_type(doc, source_listing):
	"""Classify the enrichment, from the title it just produced.

	This is deliberately the LAST thing the flow does with a product type and the
	only time Amazon is asked. The question put to Amazon is "what is this
	listing?" — and the honest form of that question uses the title that is about
	to be published, not the raw one the agent was handed. Asking earlier would
	classify a listing that no longer exists by the time anything is written, and
	a product type that disagrees with the title beside it is exactly what Amazon
	rejects.

	It follows that the model does not answer this at all. It is derived, not
	produced: the agent's only influence is that it wrote a title saying what the
	product is, which is the influence that should count.

	Amazon is asked even when the listing already has a type, so a classification
	the rewrite has outgrown gets noticed. When the answer differs from what the
	listing publishes as today, that goes to `needs_review` — a recategorisation
	is a decision about a live listing and belongs to a human, not to a run.

	`product_type_suggestions` is stored either way. It is what the Desk form
	offers as alternatives, and it is the record of what Amazon said about this
	title on the day it was asked.
	"""
	# Says that what follows is a run's answer, not a reviewer's edit — the
	# doctype uses it to decide whether a changed product type is a human
	# override (see AmazonEnrichedListing._record_product_type_override).
	doc.flags.from_agent = True

	# doc.title, not listing["title"]: the same value, but read back after the
	# rest of save_listing has settled it, so the type is classified from exactly
	# what a reviewer will see.
	resolved = product_types.resolve(source_listing, doc.title)

	doc.product_type = resolved["product_type"]
	doc.product_type_display = resolved["product_type_display"]
	doc.product_type_suggestions = frappe.as_json(resolved["suggestions"])
	doc.product_type_source = resolved["source"]

	if not resolved["product_type"]:
		flag = "Product type"
	elif product_types.disagrees(resolved):
		flag = (
			f"Product type (this listing publishes as '{resolved['existing']}', but "
			f"Amazon classifies the new title as '{resolved['product_type']}' — confirm "
			"which is right before approving)"
		)
	else:
		return

	doc.needs_review = "\n".join(filter(None, [doc.needs_review, flag]))


def _save_brand(doc, listing):
	"""Assign the house brand the model decided this product belongs under.

	Lives on this enrichment record only -- see `_push_to_listing` in
	amazon_enriched_listing.py, which deliberately never writes it to the
	Amazon Product Listing.

	Unlike product type above, this IS the model's own judgement: it read the
	product off the source listing and its photos and picked the house brand
	(if any) whose coverage fits, per the HOUSE BRAND instructions in the
	prompt -- before writing the title, which opens with that brand. Anything
	other than one of this deployment's actual registered brands is treated as
	no brand at all, in case a bad response slips past the schema.

	Only flags `needs_review` when this site actually has house brands at all
	(`brand.is_configured()`) -- a deployment with none registered has nothing
	to say about brand, and nagging every listing about a field that
	deployment doesn't use would train reviewers to ignore the flag.
	"""
	valid = brands.valid_brands()
	if not valid:
		return

	candidate = (listing.get("brand") or "").strip()
	doc.brand = candidate if candidate in valid else None
	if not doc.brand:
		doc.needs_review = "\n".join(
			filter(None, [doc.needs_review, "Brand (doesn't clearly fit one of this site's house brands)"])
		)


def save_listing(listing, sku=None):
	"""
	Persist an enriched listing into the shared Amazon Enriched Listing DocType for
	admin review. Upserts by sku (one row per listing): re-running the listing agent
	on the same SKU updates the existing row instead of creating a duplicate.

	`listing` is the full enrichment object — the same shape the agent returns
	(schemas/output.json). `sku` identifies the source listing and is the upsert key;
	it falls back to listing["sku"] if not passed separately. The row lands in
	"Needs Review" status so an admin edits/approves it before anything is published.

	Every field written here corresponds to a field on the Amazon Product Listing itself
	(title -> title, description -> description, bullet_points -> bullet_points,
	keywords -> keywords, images -> images, product_type -> product_type), plus the
	three review fields the approval step needs and `brand` (see `_save_brand`) --
	the one field here that is never published to the Amazon Product Listing at all.

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

	source_listing = get_listing(sku)
	_save_product_type(doc, source_listing)
	_save_brand(doc, listing)

	# the ordered content -> pretty JSON; whole payload kept verbatim for audit
	doc.bullets_json = frappe.as_json(listing.get("bullet_points") or [])
	doc.keywords_json = frappe.as_json(listing.get("keywords") or [])
	doc.output_json = frappe.as_json(listing)

	# The same two things again, as child tables — a reviewer reads and edits rows,
	# not a JSON blob, exactly as they do on the Amazon Product Listing itself. These tables
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
	# `role` is what orders the listing's images on approval — the main image first,
	# then the gallery — so it is persisted even though the model only ever copies it.
	doc.set("images", [])
	for img in (listing.get("images") or []):
		doc.append("images", {
			"role": img.get("role") or "gallery",
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
