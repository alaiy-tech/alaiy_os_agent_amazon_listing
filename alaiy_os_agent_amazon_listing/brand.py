# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Which house brand a listing's product belongs to, for the enrichment lifecycle.

Which categories map to which brands is a fact about one client's catalogue,
not about this agent, so this module owns none of it. It only knows two
things: how to read a listing's category (its product's Item Group), and how
to ask whatever the site has registered under `amazon_listing_brand_resolvers`
-- a hooks.py list of dotted paths to a `category -> brand or None` function,
the same shape `chat_tool_sources` and friends already use elsewhere in this
codebase. `alaiy_os_commerce.brand_mapping` is the one example this agent
ships without; a site with no brands to assign just never registers one, and
this module answers None for every listing rather than failing.

Like the product type (see product_type.py), the model has no opinion on
which house brand a listing's product sells under -- it is derived from a
fact the agent did not produce, so this is never part of the LLM's own
output.
"""

import frappe

LISTING_DOCTYPE = "Amazon Product Listing"

HOOK = "amazon_listing_brand_resolvers"


def is_configured():
	"""Whether this site has registered any brand mapping at all.

	Distinguishes "nobody assigned a brand" from "this deployment doesn't use
	brands" -- only the first is worth nagging a reviewer about.
	"""
	return bool(frappe.get_hooks(HOOK))


def category_of(source_listing):
	"""The Item Group this listing's product sells in, or None.

	Reads through `Amazon Product Listing.product` -- optional, per that
	field's own description, so a listing with no linked Item (not yet
	matched to one, or an offer-only row) has no category and therefore no
	brand. Guessing one from the title is exactly the kind of silent
	assignment this module exists to avoid.
	"""
	product = (source_listing or {}).get("product")
	if not product:
		return None
	return frappe.db.get_value("Item", product, "item_group")


def resolve(source_listing):
	"""The house brand for this listing's category, or None.

	None covers three different situations a caller does not need to tell
	apart: no site-registered mapping at all, a linked product with no
	category, or a category the mapping doesn't cover. All three mean the
	same thing here -- no brand to assign automatically.

	Every registered resolver is tried in order; the first to answer wins, so
	several sources compose the same way `chat_tool_sources` does. A resolver
	that raises is logged and skipped rather than failing the run -- a bad
	site-side mapping should cost this listing its brand, not its enrichment.
	"""
	category = category_of(source_listing)
	if not category:
		return None

	for entry in frappe.get_hooks(HOOK) or []:
		try:
			brand = frappe.get_attr(entry)(category)
		except Exception:
			frappe.log_error(
				title="Amazon listing agent: brand resolver failed",
				message=frappe.get_traceback(),
			)
			continue
		if brand:
			return brand
	return None
