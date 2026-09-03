# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Which house brand a listing's product belongs to, for the enrichment lifecycle.

Which house brands a deployment has, and what each one covers, is a fact
about one client's own brand portfolio, not about this agent, so this module
owns none of it -- it only knows the deployment-agnostic mechanism. A
deployment declares its house brands under `amazon_listing_house_brands` in
its own hooks.py (a flat list of the brand names themselves -- see whichever
customer app overrides this agent, per `agent_meta.find_override`) and
describes what each one covers in its own `agents/amazon_listing.md` prompt
override, the same file that carries its house style and category rules. A
site with no house brands just registers neither, and this agent enriches
listings with no brand opinion at all rather than failing.

Unlike the product type (see product_type.py), this genuinely IS the model's
own judgement: a category (Item Group) is a fact about where a product sells
today, not about which of a seller's own house brands it belongs to, and the
two need not line up at all. So the model decides from what the product IS --
the source listing and its photos -- guided by whatever the deployment's
prompt override told it those brands cover. It decides BEFORE writing the
title, because the title opens with the brand. This module's only job at save
time is to trust that answer when it names one of the deployment's actual
registered brands, and otherwise treat it as no brand at all.
"""

import frappe

HOOK = "amazon_listing_house_brands"


def valid_brands():
	"""The set of house brand names this deployment actually has, or empty."""
	return set(frappe.get_hooks(HOOK) or [])


def is_configured():
	"""Whether this site has any house brands at all.

	Distinguishes "nobody assigned a brand" from "this deployment doesn't use
	brands" -- only the first is worth nagging a reviewer about.
	"""
	return bool(valid_brands())
