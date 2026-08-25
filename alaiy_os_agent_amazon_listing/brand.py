# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Which house brand a listing's product belongs to, for the enrichment lifecycle.

Which house brands a deployment has, and what each one covers, is a fact
about one client's own brand portfolio, not about this agent, so this module
owns none of it -- it only knows the deployment-agnostic mechanism. A
deployment declares its house brands under `amazon_listing_house_brands` in
its own hooks.py (a flat list of the brand names themselves, e.g.
`["BLUEGEARS", "BLUETAILS"]` -- see `alaiy_os_commerce/hooks.py`) and
describes what each one covers in its own `agents/amazon_listing.md` prompt
override, the same file that carries its house style and category rules. A
site with no house brands just registers neither, and this agent enriches
listings with no brand opinion at all rather than failing.

Unlike the product type (see product_type.py), this genuinely IS the model's
own judgement: a category (Item Group) does not reliably say which house
brand a product belongs to -- this deployment's own catalogue has no
category that maps cleanly onto one -- so the model reads the title and
description it just wrote and decides, guided by whatever this deployment's
prompt override told it those brands cover. This module's only job at save
time is to trust that answer when it names one of this deployment's actual
brands, and otherwise treat it as no brand at all.
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
