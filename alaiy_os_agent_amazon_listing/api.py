# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
What the desk surfaces need to know about the listing agent, plus bulk enrichment.

The run-agent page and the Enrich buttons are shipped by this app and stay generic:
they do not hardcode an agent_id, and they render their per-request toggles from
whatever the agent's tools declare. `get_listing_agent` is the one endpoint they ask.

`bulk_enrich` is the many-listings entry point. It creates an Amazon Product Listing Bulk
Enrich and starts it; the listings then run on Frappe workers, one OS Agent Run each
(see bulk.py). Poll `get_bulk_status`.

`enrich_item` and `bulk_enrich_items` are the same two entry points reached from the
Item side, for the items that have no listing yet — the reason the Item surfaces exist
at all, since the listing list view can only offer listings that already exist. Each
resolves its items to skus, registering a listing row from the item where there is none
(item_listing.py), and then runs exactly the machinery above. They add no second way to
enrich: `bulk_enrich_items` delegates to `bulk_enrich` for the batch itself.
"""

import json

import frappe
from frappe.utils import cint, sbool

from alaiy_os_agent_amazon_listing.bulk import (
	BATCH_DOCTYPE,
	DEFAULT_BATCH_SIZE,
	ENRICHED_DOCTYPE,
	IMAGES_IN_FLIGHT,
)


def _flag(value):
	"""A checkbox argument as 0/1, however the caller expressed it.

	`cint` alone is not enough and fails silently, which is worse than throwing:
	frappe.call JSON-encodes any non-string argument, so a ticked box arrives as
	the string "true" — and cint("true") is 0, not 1. That turned every toggle
	from the Desk dialog off without a word. sbool resolves "true"/"false"/"1"/"0"
	first and passes anything else through for cint to handle.
	"""
	return cint(sbool(value))


@frappe.whitelist()
def get_listing_agent():
	"""
	The listing agent, or None when an admin has switched it off in the Desk form —
	in which case the surfaces hide their buttons instead of offering a run that
	would throw.

	    {agent_id, agent_name, icon,
	     input_options: [{fieldname, label, description, default}]}
	"""
	from alaiy_os_agent_amazon_listing.agent_meta import build_agent_meta

	meta = build_agent_meta()
	if not frappe.db.get_value("OS Agent Registry", meta["agent_id"], "is_enabled"):
		return None

	return {
		"agent_id": meta["agent_id"],
		"agent_name": meta["agent_name"],
		"icon": meta["icon"],
		"input_options": meta["input_options"],
	}


@frappe.whitelist()
def bulk_enrich(skus, notes=None, batch_size=None, skip_enriched=0, **toggles):
	"""
	Enrich many listings at once. Returns {batch, items, jobs}.

	`skus` are Amazon Product Listing names (a list, or a JSON array over REST). Extra
	keyword arguments are the agent's per-request toggles — whatever
	`get_listing_agent` reports in `input_options`, e.g. `prepare_images` — so this
	signature does not name a tool either.

	The work happens on workers: poll `get_bulk_status`, or open the returned batch.
	"""
	if not frappe.has_permission("OS Agent Run", "create"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(skus, str):
		skus = json.loads(skus)
	if not skus:
		frappe.throw("bulk_enrich needs at least one sku.")

	batch = frappe.new_doc(BATCH_DOCTYPE)
	batch.batch_size = cint(batch_size) or DEFAULT_BATCH_SIZE
	batch.skip_enriched = _flag(skip_enriched)
	batch.notes = notes
	for fieldname, value in toggles.items():
		# form_dict carries more than this signature declares (`cmd`), so only
		# arguments that are actually fields on the batch are honoured.
		if batch.meta.get_field(fieldname):
			batch.set(fieldname, _flag(value))
	for sku in skus:
		batch.append("items", {"sku": sku})
	batch.insert()

	return batch.start()


@frappe.whitelist()
def enrich_item(item_code, marketplace=None):
	"""
	The sku to enrich for one Item, creating its listing row if the Item has none.

	    {sku, created, adopted, listings, item_disabled}

	The Item form's entry point: the run page and the whole tool layer are keyed on an
	Amazon Product Listing, so an Item that has never been listed needs a row before a
	run can start. See item_listing for what that row does and does not claim.

	Idempotent — an Item that already has a listing gets that listing back untouched,
	so pressing the button twice can neither duplicate a row nor lose Amazon's own
	title, description or photos.
	"""
	from alaiy_os_agent_amazon_listing import item_listing

	if not frappe.has_permission("Item", "read", doc=item_code):
		frappe.throw("Not permitted.", frappe.PermissionError)

	# Checked here rather than inside item_listing, which saves with
	# ignore_permissions — the same split bulk_enrich and the connector use. Asked for
	# unconditionally: whether a row is about to be created is not knowable without
	# doing the resolve, and "may enrich" implies "may register a listing" either way.
	# After the connector check, because has_permission on a DocType this site does not
	# have would fail with something nobody can act on.
	if item_listing.connector_installed() and not frappe.has_permission(
		item_listing.LISTING_DOCTYPE, "create"
	):
		frappe.throw("Not permitted.", frappe.PermissionError)

	return item_listing.ensure_listing(item_code, marketplace=marketplace)


@frappe.whitelist()
def bulk_enrich_items(
	item_codes, notes=None, batch_size=None, skip_enriched=0, marketplace=None, **toggles
):
	"""
	Enrich many Items at once — the Item list view's action. Returns
	`{batch, items, jobs, resolved, created, ambiguous, errors}`.

	Each Item is resolved to a sku (creating its listing row when it has none) and the
	skus are then handed to `bulk_enrich`, so the batch document, the toggle handling
	and the fan-out onto workers all live in exactly one place. Nothing about bulk.py
	changes: it keys every row, every progress report and the image finalisation on
	`sku`, and resolving Items inside the worker would leave a batch whose rows have no
	sku until it ran.

	One bad Item does not cost the others their run — the same rule bulk.py already
	holds per row. Each resolve gets its own savepoint, so a half-built document is
	rolled back to just before itself rather than taking every listing created so far
	with it, and the Item is reported in `errors` instead.

	`ambiguous` names the Items that have more than one listing, with the skus, because
	only one of them is being enriched: the marketplace asked for, else the primary
	one, else the oldest.
	"""
	from alaiy_os_agent_amazon_listing import item_listing

	if not frappe.has_permission("OS Agent Run", "create"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)
	if not item_codes:
		frappe.throw("bulk_enrich_items needs at least one item.")

	skus, created, ambiguous, errors = [], [], {}, {}
	for n, item_code in enumerate(item_codes):
		savepoint = f"enrich_item_{n}"
		frappe.db.savepoint(savepoint)
		try:
			result = item_listing.ensure_listing(item_code, marketplace=marketplace)
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			errors[item_code] = str(e)
			continue

		# Two items resolving to one listing would run the agent twice over the same
		# sku and race on the single Amazon Enriched Listing it writes.
		if result["sku"] not in skus:
			skus.append(result["sku"])
		if result["created"]:
			created.append(result["sku"])
		if len(result["listings"]) > 1:
			ambiguous[item_code] = result["listings"]

	if not skus:
		# An empty batch would sit in Draft forever, saying nothing about why.
		frappe.throw(
			"None of these items could be enriched: "
			+ "; ".join(f"{item}: {error}" for item, error in errors.items())
		)

	batch = bulk_enrich(
		skus,
		notes=notes,
		batch_size=batch_size,
		skip_enriched=skip_enriched,
		**toggles,
	)

	return {
		**batch,
		"resolved": skus,
		"created": created,
		"ambiguous": ambiguous,
		"errors": errors,
	}


@frappe.whitelist()
def count_listed_items(item_codes):
	"""
	How many of these Items already have an Amazon Product Listing, or None.

	What the Item list view's dialog says before a bulk run starts: the rest of the
	selection is what this run will have to register from the item itself.

	Counted here rather than in the browser because the selection is routinely
	hundreds of item codes. `frappe.db.get_list` goes out as GET, so the whole
	`product in [...]` filter lands in the request line — past a hundred or so items
	nginx rejects it with "Request Line is too large" before the dialog can open. This
	takes the codes in a POST body instead.

	Counted over DISTINCT products, not rows: one Item can hold several listings, and
	it is still one item that needs nothing registered.

	None when the connector is not installed or the user cannot read listings — the
	caller says nothing rather than guessing a number.
	"""
	from alaiy_os_agent_amazon_listing import item_listing

	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)
	if not item_codes:
		return 0

	# Connector first: has_permission on a DocType this site does not have would fail
	# with something nobody can act on. Same ordering as enrich_item.
	if not item_listing.connector_installed():
		return None
	if not frappe.has_permission(item_listing.LISTING_DOCTYPE, "read"):
		return None

	# Deduplicated here rather than with `distinct=True`, which prepends DISTINCT while
	# leaving the default `order by modified desc` in place — a column that is not in
	# the select list, which MySQL rejects under ONLY_FULL_GROUP_BY. The row set is one
	# listing per selected item at worst, so a Python set costs nothing.
	products = frappe.get_all(
		item_listing.LISTING_DOCTYPE,
		filters={"product": ["in", item_codes]},
		pluck="product",
		limit_page_length=0,
	)
	return len(set(products))


@frappe.whitelist()
def approve_listings(names):
	"""
	Approve many enriched listings at once — the list view's "Approve" action.

	Each listing is approved through a normal document save, so the same
	on_update hook that fires for a one-at-a-time approval pushes each one to
	its Amazon Product Listing. Returns {approved, skipped, failed, errors}:
	already-approved rows are counted as skipped, and one bad listing does not
	stop the rest (its error is reported per name instead).
	"""
	if isinstance(names, str):
		names = json.loads(names)
	if not names:
		frappe.throw("approve_listings needs at least one listing name.")

	approved, skipped, errors = 0, 0, {}
	for name in names:
		doc = frappe.get_doc(ENRICHED_DOCTYPE, name)
		doc.check_permission("write")
		if doc.status == "Approved":
			skipped += 1
			continue
		try:
			doc.status = "Approved"
			doc.save()
			frappe.db.commit()
			approved += 1
		except Exception:
			frappe.db.rollback()
			errors[name] = str(frappe.get_traceback().splitlines()[-1])
			frappe.log_error(
				title=f"Bulk approve failed: {name}",
				message=frappe.get_traceback(),
			)

	return {
		"approved": approved,
		"skipped": skipped,
		"failed": len(errors),
		"errors": errors,
	}


@frappe.whitelist()
def image_view_links(sku):
	"""
	Viewable links for one enriched listing's images — `{stored_url: link}`.

	The images this app produces live in S3 and the objects are private, so the url on
	an image row is an identity, not something a browser can render. The reviewer's form
	asks for this before drawing its thumbnails and gets a presigned link per row,
	valid for `IMAGE_S3_URL_EXPIRY`.

	Scoped to one listing the caller may read, and only ever answers about urls that
	listing actually holds: presigning is handing out read access, so the caller must
	not be able to name an arbitrary key and be signed for it. Urls that are not ours
	(supplier CDN photos, local Files) come back unchanged, so the client can look
	every row up here without deciding which backend each one came from.

	A sku with no enriched listing yet answers `{}` rather than throwing: the run page
	asks while a run may still be in flight, and there is nothing to sign then.
	"""
	if not frappe.db.exists(ENRICHED_DOCTYPE, sku):
		return {}

	doc = frappe.get_doc(ENRICHED_DOCTYPE, sku)
	doc.check_permission("read")

	from alaiy_os_agent_amazon_listing.tools import images

	links = {}
	for row in doc.images or []:
		for url in (row.source_url, row.url):
			if url and url not in links:
				links[url] = images.public_image_url(url)
	return links


@frappe.whitelist()
def brand_context():
	"""What the reviewer's form shows beside the Brand field — `{is_configured, valid_brands}`.

	`is_configured` says whether this site has any house brands at all, so
	the form can explain an empty Brand field correctly either way: nothing
	to assign, or the agent looked and none of this site's house brands fit.
	`valid_brands` is that list, for the form to show as context. The agent
	classifies brand from the listing's title and description, never from
	its category, so there is nothing here for this endpoint to look up on a
	per-listing basis any more.
	"""
	from alaiy_os_agent_amazon_listing import brand as brands

	return {"is_configured": brands.is_configured(), "valid_brands": sorted(brands.valid_brands())}


@frappe.whitelist()
def get_bulk_status(batch):
	"""Progress of one bulk enrichment — the poll shape for a UI.

	Mirrors alaiy_os.api.agents.get_run, one level up: the batch's own state plus a
	row per listing with the run to open for its output.

	Imagery is produced after each run closes (see image_stage.py): a batch whose
	runs are all done but whose images are still rendering reports status
	"Generating Images" until stage two settles — `images_pending` is how many are
	left, and each row carries its own `image_status`.
	"""
	doc = frappe.get_doc(BATCH_DOCTYPE, batch)
	doc.check_permission("read")

	image_states = _image_states([row.sku for row in doc.items])
	return {
		"batch": doc.name,
		"status": doc.status,
		"total_items": doc.total_items,
		"succeeded": doc.succeeded,
		"failed": doc.failed,
		"skipped": doc.skipped,
		"images_pending": sum(1 for s in image_states.values() if s in IMAGES_IN_FLIGHT),
		"started_at": doc.started_at,
		"ended_at": doc.ended_at,
		"items": [
			{
				"sku": row.sku,
				"status": row.status,
				"run": row.run,
				"error": row.error,
				"image_status": image_states.get(row.sku),
			}
			for row in doc.items
		],
	}


def _image_states(skus):
	"""{sku: image_status} for whichever of these listings has been enriched."""
	if not skus:
		return {}
	rows = frappe.get_all(
		ENRICHED_DOCTYPE,
		filters={"name": ("in", list(skus))},
		fields=["name", "image_status"],
	)
	return {row.name: row.image_status for row in rows}
