# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Which house brand a listing's product belongs to, for the enrichment lifecycle.

Which house brands a deployment has, and what each one covers, is a fact
about one client's own brand portfolio, not about this agent, so this module
owns none of it -- it only knows the deployment-agnostic mechanism. A
deployment declares its house brands under `amazon_listing_house_brands` in
its own hooks.py (a flat list of the brand names themselves) and describes
what each one covers in its own `agents/amazon_listing.md` prompt override,
the same file that carries its house style and category rules. A site with no
house brands registers neither, and this agent enriches listings with no
brand opinion at all rather than failing.

Unlike the product type (see product_type.py), which is a lookup against
Amazon's own register, this is a judgement: a category (Item Group) is a fact
about where a product sells today, not about which of a seller's own house
brands it belongs to, and the two need not line up at all.

**It is a judgement made in its own call, not as one field of the
enrichment.** It used to be the ninth key of the output JSON, decided by the
same turn that wrote the title, the bullets, the description and the
keywords, and it lost. The agent answered null on 977 of 1970 listings on a
catalogue that is 87% pet supplies, and on the rest it mostly transcribed
whatever brand was already in the incoming title -- which is not
classification, it is copying, and it is why a product whose title had never
carried the brand got nothing. A question that is asked on its own, with only
the product and the brand descriptions in front of it, gets answered.

So `classify` is the one place that answers "which house brand is this?", and
it answers it by asking a model directly. Nothing about that answer depends
on how the rest of the enrichment went.

**The answer is the record's, not the enrichment JSON's.** `_save_brand` in
tools/handlers.py writes what this module returns, and treats the `brand` the
agent put in its own output as a cross-check rather than as the value. The
agent is still shown the answer -- `get_product` hands it over, because the
title opens with the brand and cannot be written without it -- but the record
does not depend on it coming back intact.

**A deployment with house brands gets one on every product.** There is no
"none of these" answer where brands are registered: the classifier picks the
closest of them, because a null costs a reviewer a decision on every row and
this catalogue's nulls were overwhelmingly wrong ones. Null survives for the
deployment that registers no brands at all.

Every model call is guarded. A classification that fails must cost the run
its house brand, not its enrichment.
"""

import frappe

HOOK = "amazon_listing_house_brands"

# Per-request memo, keyed by sku. `classify` is asked twice in a normal run --
# once by get_product, so the agent can open the title with the brand, and
# again at save time, so the record carries the classifier's answer rather
# than the agent's echo of it. Those two have to agree, and paying for a
# second model call to find out is worse than remembering the first.
_CACHE = "amazon_listing_house_brand"

# How much of the listing's own copy the classifier is shown. It is deciding
# what kind of thing this is, which the opening of a description settles; the
# rest is specifications and boilerplate.
MAX_DESCRIPTION_CHARS = 800

# site_config override, for a deployment that wants the brand decided by a
# different model than the one enriching the copy. Unset means the agent's
# own model, so upgrading the agent upgrades this with it.
MODEL_KEY = "amazon_listing_brand_model"


def valid_brands():
	"""The set of house brand names this deployment actually has, or empty."""
	return set(frappe.get_hooks(HOOK) or [])


def is_configured():
	"""Whether this site has any house brands at all.

	Distinguishes "nobody assigned a brand" from "this deployment doesn't use
	brands" -- only the first is worth nagging a reviewer about.
	"""
	return bool(valid_brands())


def classify(listing):
	"""The house brand this listing's product belongs under, or None.

	None means one of two things, and both are the absence of an answer rather
	than an answer of "no brand": this deployment registers no house brands, or
	the classification call failed. A deployment that has brands gets one.

	Memoized per request (see `_CACHE`), so the two callers in a run share one
	model call.
	"""
	valid = valid_brands()
	if not valid:
		return None

	cache = getattr(frappe.local, _CACHE, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _CACHE, cache)

	sku = listing.get("name")
	if sku not in cache:
		cache[sku] = _ask(listing, sorted(valid))
	return cache[sku]


def _ask(listing, names):
	"""One model call: this product, those brands, one name back.

	Returns None on any failure. The enrichment is worth more than the brand
	on it, so a provider outage, a timeout or a reply that names nothing
	recognisable costs the run its house brand and nothing else -- the same
	discipline product_type.py applies to an SP-API outage.
	"""
	from alaiy_os.engine import llm

	try:
		result = llm.complete(
			model=_model(),
			system=_instructions(names),
			messages=[{"role": "user", "content": _product(listing)}],
		)
	except Exception:
		frappe.log_error(
			title="Amazon listing: house brand classification failed",
			message=f"sku={listing.get('name')}\n\n{frappe.get_traceback()}",
		)
		return None

	return _match(_text(result), names)


def _model():
	"""The model that answers the brand question.

	The agent's own by default, read from the registry rather than from
	agent_meta, so a deployment that overrode the model in its
	agents/amazon_listing.md frontmatter gets the model it asked for here too.
	"""
	from alaiy_os_agent_amazon_listing.agent_meta import AGENT_ID, DEFAULT_MODEL

	override = frappe.conf.get(MODEL_KEY)
	if override:
		return override
	return frappe.db.get_value("OS Agent Registry", AGENT_ID, "model") or DEFAULT_MODEL


def _instructions(names):
	"""The classifier's system prompt: the seller's own brand descriptions,
	and the one question being asked of them.

	The descriptions are lifted verbatim from the deployment's prompt override
	-- the same text the enrichment agent is given -- so there is one place a
	client says what its brands cover, and no second copy to drift from it.
	"""
	from alaiy_os_agent_amazon_listing.agent_meta import find_override, parse_override

	_, path = find_override()
	coverage = parse_override(path.read_text(encoding="utf-8"))[1] if path else ""

	return (
		"You classify a seller's own products into that seller's own house brands.\n\n"
		"The seller's description of their house brands follows. Read what each "
		"brand covers.\n\n"
		f"{coverage}\n\n"
		"---\n\n"
		"You will be shown ONE product. Decide which of the brands above it belongs "
		"under, from what the product IS: what the thing is, who it is for, and what "
		"it is used for.\n\n"
		f"Answer with EXACTLY one of these names, copied character for character: {', '.join(names)}\n\n"
		"Nothing else. No explanation, no punctuation, no quotes, no other words.\n\n"
		"Every product belongs to one of them. If none is an obvious fit, pick the "
		"closest -- the seller would rather file a product under the nearest brand "
		"than leave it unfiled. A brand name already present in the product's own "
		"title is not the answer and must not be copied; classify what the product is."
	)


def _product(listing):
	"""What the classifier is shown: the product, and only the product.

	No SKU, no ASIN, no price, no marketplace, no listing status. They are
	facts about a listing, not about what the thing is, and each one is
	another chance to answer a different question than the one being asked.
	"""
	description = (listing.get("description") or "")[:MAX_DESCRIPTION_CHARS]
	bullets = [row.get("bullet") for row in (listing.get("bullet_points") or []) if row.get("bullet")]

	lines = [f"Title: {listing.get('title') or '(none)'}"]
	if listing.get("product_type"):
		lines.append(f"Amazon product type: {listing.get('product_type')}")
	if description:
		lines.append(f"Description: {description}")
	if bullets:
		lines.append("Bullet points:\n" + "\n".join(f"- {b}" for b in bullets))
	return "\n\n".join(lines)


def _text(result):
	"""The reply's text, from the block shape every ai_client returns."""
	return "".join(
		block.get("text") or ""
		for block in (result or {}).get("content", [])
		if block.get("type") == "text"
	).strip()


def _match(reply, names):
	"""The registered brand a reply names, or None.

	Asking for one name and nothing else is not the same as getting it, so a
	reply is matched rather than trusted: exactly, then ignoring case, then by
	looking for a brand name inside a model that explained itself anyway.
	Longest first, so a brand whose name contains another brand's wins over it
	instead of losing to a substring.
	"""
	if not reply:
		return None

	if reply in names:
		return reply

	lowered = reply.lower()
	for name in sorted(names, key=len, reverse=True):
		if name.lower() == lowered:
			return name
	for name in sorted(names, key=len, reverse=True):
		if name.lower() in lowered:
			return name
	return None
