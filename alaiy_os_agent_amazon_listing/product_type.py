# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Which Amazon product type a listing belongs to, for the enrichment lifecycle.

Amazon's product type (SHIRT, TOWEL, LUGGAGE, …) is not decoration. Every write
through the Listings API has to declare one, so a listing without one cannot be
updated at all; and the type decides which attributes Amazon requires and which
copy conventions the category expects. The connector already stores it on
`Amazon Product Listing.product_type` — synced from Amazon's own summary — but
until now nothing in this app read it, so the agent wrote copy blind to the
category and a listing that arrived without a type kept none.

This module is the one place that answers "what product type is this listing?"
for the agent, and it answers it by asking Amazon — every time, whether or not
the listing already carries a type.

Always asking is the point. A stored product type is a classification of the
copy the listing had when it was synced, and enrichment rewrites that copy. A
towel listing whose old title read as luggage carries `LUGGAGE` today; the
enriched title says what the product actually is, and the lookup is the only
thing that will ever notice the difference. Skipping the call whenever a value
already exists would mean the listings most likely to be misclassified are
exactly the ones never re-checked.

A stored value is not discarded, though — it is what the answer is compared
against (`existing` in the result), so a disagreement reaches the reviewer as a
question rather than being applied silently, and it is the fallback when Amazon
has nothing to say.

**Which title, and when, is the whole design.** The lookup runs against the
ENRICHED title, after the agent has rewritten it — never against the raw one
the listing arrived with. A product type and the copy it is published alongside
have to describe the same product, and raw Amazon titles are routinely
keyword-stuffed, mistranslated or simply about a different thing than the
enriched listing ends up being. Classifying from the old title and then
publishing new copy is how a listing acquires a product type that contradicts
its own title, which Amazon rejects. So `resolve` takes the enriched title as an
argument rather than reading `listing.title` itself: the ordering is enforced by
the signature, not by remembering to call things in the right order.

Nothing here writes to `Amazon Product Listing`. Suggesting a type is not the
same act as assigning one: the enrichment records the answer, the admin approves
it, and only then does approval push it (see AmazonEnrichedListing).

Every connector call is guarded. This app stays installable without the Amazon
connector, and an SP-API outage must cost a run its product type, not its
enrichment.
"""

import frappe

LISTING_DOCTYPE = "Amazon Product Listing"

# How many suggestions to carry into the enrichment. The connector returns them
# best-match-first with no scores attached — order is the only confidence signal
# there is — so this is a "show the shortlist" cap, not a quality filter.
MAX_SUGGESTIONS = 5

# site_config flag. Amazon's top suggestion for a title is the answer we would
# pick by hand in almost every case, so the default is to accept it — but a
# seller in categories Amazon classifies badly can switch it off and get
# suggestions with no chosen value, which puts every product type in front of a
# human before approval.
AUTO_ACCEPT_KEY = "amazon_listing_product_type_auto_accept"


def auto_accept_enabled():
	"""Whether the top suggestion may be taken as the product type unreviewed."""
	value = frappe.conf.get(AUTO_ACCEPT_KEY)
	return True if value is None else bool(value)


def suggest(title, marketplace=None):
	"""Amazon's product types for a title, best match first, or [].

	Returns the connector's own [{product_type, display_name}] shape unchanged —
	this app does not maintain a second vocabulary of product types, and a
	mapping table of our own would drift from Amazon's register the day after it
	was written.

	Never raises. A bench without the connector, a seller without Product Type
	Definitions access, and an SP-API blip all mean the same thing here: no
	suggestions this run. That is a listing the reviewer types a product type
	into, not a failed enrichment.
	"""
	title = (title or "").strip()
	if not title:
		return []

	try:
		from alaiy_os_connector_amazon_sp_api.spapi.product_types import suggest_product_types
	except ImportError:
		return []

	try:
		return suggest_product_types(title, marketplace=marketplace, limit=MAX_SUGGESTIONS) or []
	except Exception:
		frappe.log_error(
			title="Amazon listing agent: product type lookup failed",
			message=frappe.get_traceback(),
		)
		return []


def display_name(product_type, suggestions=None):
	"""The human-friendly name for a product type, falling back to the key itself.

	Amazon only gives us a display name alongside a suggestion, so a type that
	came off the listing has none until it is looked up — and looking it up is
	not worth an API call for a label. `TOWEL` is a perfectly readable answer.
	"""
	if not product_type:
		return None
	for entry in suggestions or []:
		if (entry.get("product_type") or "").upper() == product_type.upper():
			return entry.get("display_name") or product_type
	return product_type


def existing(listing):
	"""The product type Amazon already has for this listing, or None.

	What `get_product` shows the agent: it costs a field read, it is available
	before any copy is written, and it is the category the listing sells in
	today. Whether it still describes the listing after enrichment is what
	`resolve` asks Amazon.
	"""
	return (listing.get("product_type") or "").strip() or None


def resolve(listing, enriched_title):
	"""This listing's product type once its copy is written, and the shortlist behind it.

	    {product_type, product_type_display, suggestions, source, existing}

	`enriched_title` is the title the agent just produced — the one that will be
	published. It is a required argument and the raw `listing.title` is never
	substituted for it: see the module docstring for why classifying from the old
	title is the bug this signature exists to prevent. No enriched title means no
	lookup at all.

	Amazon is asked whether or not the listing already has a type. The stored
	value does not shortcut the call; it comes back as `existing`, for the caller
	to compare against and to fall back on.

	`source` says where the answer came from, and is the field to read before
	trusting it:

	  "suggested" — Amazon's top match for the ENRICHED title, auto-accepted.
	                The normal answer, and one a reviewer should confirm.
	  "listing"   — Amazon said nothing usable, so the type the listing already
	                had stands. Not a fresh opinion; the previous one, kept.
	  "none"      — no answer and nothing stored, or auto-accept is off with
	                nothing to fall back on. The shortlist asks for a human.

	Memoized per (sku, title) so a run that saves twice pays for one lookup.
	"""
	sku = listing.get("name")
	enriched_title = (enriched_title or "").strip()
	stored = existing(listing)

	if not enriched_title:
		# No question to ask. Explicitly NOT `listing.title` — falling back to the
		# raw title here is the single mistake this module exists to make
		# impossible. The stored type stands because it is all there is, not
		# because it was checked.
		return _answer(stored, stored, [], "listing" if stored else "none", stored)

	cache = _memo()
	key = (sku, enriched_title)
	if key in cache:
		return cache[key]

	suggestions = suggest(enriched_title, listing.get("marketplace"))
	chosen = suggestions[0] if (suggestions and auto_accept_enabled()) else None

	if chosen:
		resolved = _answer(
			chosen["product_type"], chosen["display_name"], suggestions, "suggested", stored
		)
	elif stored:
		# Amazon had no answer for the new title, or auto-accept is off. Either
		# way, dropping a type the listing already publishes with would leave it
		# unwritable — the previous classification is the safe floor.
		resolved = _answer(stored, stored, suggestions, "listing", stored)
	else:
		resolved = _answer(None, None, suggestions, "none", stored)

	cache[key] = resolved
	return resolved


def disagrees(resolved):
	"""Whether Amazon classified the enriched title as something new.

	True only when there is a real conflict to put in front of a reviewer: the
	listing publishes as one thing today and its rewritten copy reads as another.
	A listing that had no type, or one Amazon simply confirmed, is not a conflict.
	"""
	chosen, stored = resolved["product_type"], resolved["existing"]
	return bool(chosen and stored and chosen.upper() != stored.upper())


def _answer(product_type, display, suggestions, source, stored):
	return {
		"product_type": product_type,
		"product_type_display": display,
		"suggestions": list(suggestions),
		"source": source,
		"existing": stored,
	}


def _memo():
	"""Per-request {(sku, title): resolved}. A worker gets a fresh one per job."""
	if not hasattr(frappe.local, "amazon_listing_product_types"):
		frappe.local.amazon_listing_product_types = {}
	return frappe.local.amazon_listing_product_types
