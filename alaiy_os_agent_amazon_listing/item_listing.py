# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""The listing row behind an Item, so enrichment can start from the Item itself.

Everything in this app is keyed on the seller SKU, i.e. on an Amazon Product Listing:
``handlers.get_listing`` throws without one, ``get_product`` reads strictly from the
listing and never from the Item behind it, and ``save_listing`` refuses to write an
enrichment for a sku that has no listing. So "enrich this Item" has to mean "enrich
the Item's listing", and an Item with no listing yet needs one before a run can start.

This module is the ONLY place a listing row is created without SP-API. It is local:
nothing is sent to Amazon, and the row it writes says so —

  • ``listing_status = "incomplete"``, never the doctype default ``"pending"``.
    ``pending`` means "a push Amazon has not confirmed yet", and the connector's
    reconcile deliberately SKIPS such rows (spapi/reconcile.py) so it cannot stamp
    on an in-flight write. A locally created row left at the default would be
    invisible to every reconcile, forever.
  • ``last_synced_at`` and ``catalog_synced_at`` stay empty. They would lie about a
    row that has never touched Amazon, and their emptiness is already the honest
    "this came from the Item, not from Amazon" signal — so no marker field is needed.

The other invariant is that an EXISTING listing is never edited here. Amazon's own
title, description, price and photos beat anything the Item can offer, and an
enrichment that quietly overwrote them would lose real data every time someone
pressed the button. So a resolved listing is returned untouched and `created` is
False; the single exception is a row that is already NAMED after the item but has no
`product` link, where that one field is filled so the Item form can find it next time.

The connector that owns Amazon Product Listing is optional (this app runs without it,
see setup/install.py), so every entry point here checks the DocType exists first and
throws something a human can act on rather than a MissingDoctypeError.
"""

import frappe

from alaiy_os_agent_amazon_listing.tools.handlers import LISTING_DOCTYPE

# What a row created from an Item claims about itself. See the module docstring: the
# doctype's own default ("pending") means something else entirely.
LOCAL_LISTING_STATUS = "incomplete"


def connector_installed():
	"""Whether Amazon Product Listing exists on this site at all.

	Same stance as setup.install.sync_custom_fields: this app does not depend on the
	SP-API connector, so its absence is a state to report, not a crash.
	"""
	return bool(frappe.db.exists("DocType", LISTING_DOCTYPE))


def default_marketplace():
	"""The marketplace a locally created listing belongs to, or None.

	The connection's primary marketplace, else the only marketplace on the site, else
	nothing. None is a legal answer: `marketplace` is not a required field, and the
	product-type suggestion degrades without one rather than failing.
	"""
	if not frappe.db.exists("DocType", "Amazon Marketplace"):
		return None

	if frappe.db.exists("DocType", "Amazon Connection"):
		primary = frappe.db.get_single_value("Amazon Connection", "primary_marketplace")
		if primary and frappe.db.exists("Amazon Marketplace", primary):
			return primary

	marketplaces = frappe.get_all("Amazon Marketplace", pluck="name", limit=2)
	return marketplaces[0] if len(marketplaces) == 1 else None


def listing_values_from_item(item, marketplace=None):
	"""The listing row for one Item, as a plain dict — the whole Item → listing mapping.

	Pure by design: no writes, and the only lookup is the marketplace's currency. That
	keeps the mapping testable on a bench without the connector installed, which is
	where the rules that matter (see below) would otherwise go unchecked.

	`item` is an Item document or any mapping with its fields.

	Two fields are dropped when they carry nothing but the item code. ERPNext defaults
	`item_name` to `item_code`, and routinely stores `description` as a copy of
	`item_name` — its own validation guards on exactly that. Copying either through
	would hand the agent "SKU-00123" as the seller's title and existing copy, and the
	model would try to honour it. An empty field is the honest input: `title_field`
	falls back to the row's name anyway.

	What is deliberately NOT mapped, and why:
	  • price / quantity — `standard_rate` is not an Amazon offer, and a fabricated one
	    would show up in get_product and mislead both the model and the reviewer.
	  • condition / fulfillment_channel — the doctype's defaults are the truthful
	    answer for a listing that is not live.
	  • asin / product_type — only Amazon can say, and the product type is derived from
	    the ENRICHED title at save time anyway (product_type.py).
	  • is_variation_parent / parent_asin / parent_listing / variation_theme — read-only
	    and connector-owned; parentage comes from Amazon, not from an Item template.
	  • bullet_points / keywords / suppression_reasons — the agent writes these to
	    Amazon Enriched Listing. Empty here means "the seller has no copy yet", which
	    is exactly the situation.
	  • brand / item_group / weight — no listing field to hold them, and the agent
	    already reads the far richer variant specs off the Item (handlers.item_variant_specs).
	"""
	item_code = item.get("name") or item.get("item_code")
	item_name = (item.get("item_name") or "").strip()
	description = (item.get("description") or "").strip()

	values = {
		"doctype": LISTING_DOCTYPE,
		"sku": item_code,
		"product": item_code,
		"listing_status": LOCAL_LISTING_STATUS,
	}

	if item_name and item_name != item_code:
		values["title"] = item_name
	if description and description not in (item_name, item_code):
		values["description"] = description

	if marketplace:
		values["marketplace"] = marketplace
		currency = frappe.db.get_value("Amazon Marketplace", marketplace, "currency")
		if currency:
			values["currency"] = currency

	# The Item's photo, as the main image. It is usually a Frappe File path rather than
	# a CDN url, which images.image_block_from_url already reads — so this is a photo
	# the model can actually see, not just a string it is told about.
	image = item.get("image")
	if image:
		values["images"] = [{"image_url": image, "is_main": 1}]

	return values


def resolve_listing(item_code, marketplace=None):
	"""(the sku to enrich for this Item, every sku it has). Reads only.

	An Item can carry several listings — one per marketplace, or a legacy SKU beside a
	current one — and each is a separate listing with its own copy to write. Picking one
	rather than all of them is what keeps a 50-item bulk selection producing 50 runs;
	letting the user choose several is a later iteration, which is why the full list
	comes back too.

	The choice is deterministic so a rerun lands on the same listing: the marketplace
	asked for, else the primary marketplace's, else the only one, else the oldest. A
	variation parent is never preferred — it is a family, not a buyable offer, and has
	no copy of its own to fix — but it is still returned when it is all there is.
	"""
	if not connector_installed():
		return None, []

	listings = frappe.get_all(
		LISTING_DOCTYPE,
		filters={"product": item_code},
		fields=["name", "marketplace", "is_variation_parent"],
		order_by="creation asc",
	)
	if not listings:
		return None, []

	names = [row.name for row in listings]
	candidates = [row for row in listings if not row.is_variation_parent] or listings

	for wanted in (marketplace, default_marketplace()):
		if not wanted:
			continue
		for row in candidates:
			if row.marketplace == wanted:
				return row.name, names

	return candidates[0].name, names


def ensure_listing(item_code, marketplace=None):
	"""The sku to enrich for this Item, creating its listing row if it has none.

	Returns ``{sku, created, adopted, listings, item_disabled}``. Idempotent: call it
	twice and the second call resolves the row the first one made and writes nothing.

	Uniqueness is left to the database rather than argued from the exists() check
	above it. `Amazon Product Listing` is ``autoname: field:sku`` with a unique index,
	so two simultaneous callers cannot both insert — the loser gets a
	DuplicateEntryError and reads the winner's row, which is the only race-free answer.

	Does not commit: the caller's transaction is the boundary. bulk_enrich_items relies
	on that, so listings, batch and jobs all become visible together.
	"""
	if not connector_installed():
		frappe.throw(
			f"The Amazon SP-API connector is not installed, so there is no "
			f"{LISTING_DOCTYPE} to enrich for '{item_code}'."
		)
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(f"No Item '{item_code}'.")

	item = frappe.get_doc("Item", item_code)
	result = {
		"sku": None,
		"created": False,
		"adopted": False,
		"listings": [],
		# A paused SKU is still worth writing copy for, so this is reported rather
		# than refused — the surfaces say so instead of hiding the run.
		"item_disabled": bool(item.get("disabled")),
	}

	sku, listings = resolve_listing(item_code, marketplace=marketplace)
	result["listings"] = listings
	if sku:
		# The anti-clobber path: an existing listing is enriched exactly as it is.
		result["sku"] = sku
		return result

	# A row named after the item but not linked to it. Adopting it is one field, and it
	# is the field the Item form searches on — so without this the next visit would try
	# to create the same row again and hit the unique index.
	if frappe.db.exists(LISTING_DOCTYPE, item_code):
		owner_item = frappe.db.get_value(LISTING_DOCTYPE, item_code, "product")
		if owner_item and owner_item != item_code:
			frappe.throw(
				f"{LISTING_DOCTYPE} '{item_code}' already belongs to item "
				f"'{owner_item}'. Link this item's listing manually, or enrich it "
				f"from the listing itself."
			)
		if not owner_item:
			frappe.db.set_value(LISTING_DOCTYPE, item_code, "product", item_code)
			frappe.log_error(
				title=f"Amazon listing {item_code}: adopted by its item",
				message=(
					f"{LISTING_DOCTYPE} '{item_code}' had no linked Item, so enriching "
					f"item '{item_code}' claimed it rather than creating a second row. "
					"Nothing else on the listing was changed."
				),
			)
			result["adopted"] = True
		result["sku"] = item_code
		result["listings"] = [item_code]
		return result

	if item.get("has_variants"):
		# Amazon sells the child, not the template. A template's listing row would be a
		# parent, which is connector-owned read-only territory.
		frappe.throw(
			f"'{item_code}' is a variant template, not a sellable SKU. Enrich its variants instead."
		)

	values = listing_values_from_item(item, marketplace=marketplace or default_marketplace())
	try:
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Someone inserted the same sku between the exists() above and here.
		result["sku"] = item_code
		result["listings"] = [item_code]
		return result

	# The row itself records who and when (owner/creation), and `listing_status` plus
	# the empty sync stamps record where it came from — but a bulk run registering
	# forty listings deserves a line each somewhere greppable, not just forty new rows.
	frappe.logger("amazon_listing").info(
		f"registered {LISTING_DOCTYPE} '{doc.name}' from item '{item_code}' "
		f"(marketplace: {doc.marketplace or 'none'})"
	)

	result["sku"] = doc.name
	result["created"] = True
	result["listings"] = [doc.name]
	return result
