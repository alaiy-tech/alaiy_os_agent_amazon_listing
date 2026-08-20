# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Enriching a selection of Items: what ends up in the batch, and what is said about
what did not.

The batch is the existing one — `bulk_enrich_items` resolves each Item to a sku and
then hands the skus to `bulk_enrich`, so nothing about the fan-out, the toggles or the
progress reporting is duplicated for items. What is new is everything that can go
wrong *before* a row exists: an Item that cannot be listed at all, two Items that turn
out to share one listing, a selection where nothing is enrichable.

The rule inherited from bulk.py is that one bad row never costs the others their run.
Here that has teeth, because resolving writes: an Item whose resolve throws half way
must be rolled back to just before itself, not take every listing registered so far
with it. That is the savepoint test, and it is the reason this module exists.

Touches the database. No agent runs and no jobs: the enqueue is patched out, so this
is about what the batch says, not about the workers.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from alaiy_os_agent_amazon_listing import api

BATCH_DOCTYPE = "Amazon Listing Bulk Enrich"
LISTING_DOCTYPE = "Amazon Product Listing"
ENQUEUE = (
	"alaiy_os_agent_amazon_listing.alaiy_os_agent_amazon_listing.doctype."
	"amazon_listing_bulk_enrich.amazon_listing_bulk_enrich.enqueue_chunks"
)

if not frappe.db.exists("DocType", LISTING_DOCTYPE):
	raise unittest.SkipTest(f"{LISTING_DOCTYPE} is not installed (the Amazon connector is optional).")


class TestBulkEnrichItems(IntegrationTestCase):
	def item(self, **kwargs):
		values = {
			"doctype": "Item",
			"item_code": f"TEST-{frappe.generate_hash(length=8)}",
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"item_name": "Cotton Bath Towel",
			"is_stock_item": 0,
		}
		values.update(kwargs)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Item", doc.name, force=True)
		return doc

	def listing(self, sku, **kwargs):
		doc = frappe.get_doc({"doctype": LISTING_DOCTYPE, "sku": sku, **kwargs})
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, LISTING_DOCTYPE, doc.name, force=True)
		return doc

	def enrich(self, item_codes, **kwargs):
		"""bulk_enrich_items with the workers and the primary marketplace stubbed out."""
		with (
			patch(ENQUEUE, return_value=0),
			patch("alaiy_os_agent_amazon_listing.item_listing.default_marketplace", return_value=None),
		):
			result = api.bulk_enrich_items(item_codes, **kwargs)

		self.addCleanup(frappe.delete_doc, BATCH_DOCTYPE, result["batch"], force=True)
		for sku in result.get("created") or []:
			self.addCleanup(frappe.delete_doc, LISTING_DOCTYPE, sku, force=True)
		return result

	def test_every_selected_item_becomes_one_batch_row(self):
		items = [self.item(), self.item(), self.item()]

		result = self.enrich([item.name for item in items])

		batch = frappe.get_doc(BATCH_DOCTYPE, result["batch"])
		self.assertEqual([row.sku for row in batch.items], [item.name for item in items])
		self.assertEqual(sorted(result["created"]), sorted(item.name for item in items))

	def test_an_item_that_cannot_be_listed_does_not_take_the_batch_down(self):
		good = self.item()
		# A variant template is not a sellable SKU, so resolving it throws.
		bad = self.item(has_variants=1, variant_based_on="Manufacturer")
		also_good = self.item()

		result = self.enrich([good.name, bad.name, also_good.name])

		batch = frappe.get_doc(BATCH_DOCTYPE, result["batch"])
		self.assertEqual([row.sku for row in batch.items], [good.name, also_good.name])
		self.assertIn(bad.name, result["errors"])
		# The savepoint rolled back the failure only: the listing registered before it
		# is still there, and so is the one registered after.
		self.assertTrue(frappe.db.exists(LISTING_DOCTYPE, good.name))
		self.assertTrue(frappe.db.exists(LISTING_DOCTYPE, also_good.name))

	def test_an_item_that_is_already_listed_is_not_listed_again(self):
		item = self.item()
		listing = self.listing(f"LEGACY-{frappe.generate_hash(length=6)}", product=item.name)

		result = self.enrich([item.name])

		self.assertEqual(result["resolved"], [listing.name])
		self.assertEqual(result["created"], [])
		self.assertFalse(frappe.db.exists(LISTING_DOCTYPE, item.name))

	def test_two_items_sharing_one_listing_are_enriched_once(self):
		# Two runs over one sku would race on the single Amazon Enriched Listing the
		# agent writes, and one of them would lose. The shared resolve is stubbed —
		# what is being pinned is the de-duplication, not how two items came to point
		# at one listing.
		first = self.item()
		second = self.item()
		shared = self.listing(f"SHARED-{frappe.generate_hash(length=6)}", product=first.name)

		with (
			patch(ENQUEUE, return_value=0),
			patch(
				"alaiy_os_agent_amazon_listing.item_listing.resolve_listing",
				return_value=(shared.name, [shared.name]),
			),
		):
			result = api.bulk_enrich_items([first.name, second.name])

		self.addCleanup(frappe.delete_doc, BATCH_DOCTYPE, result["batch"], force=True)
		self.assertEqual(result["resolved"], [shared.name])
		batch = frappe.get_doc(BATCH_DOCTYPE, result["batch"])
		self.assertEqual([row.sku for row in batch.items], [shared.name])

	def test_an_item_with_several_listings_says_which_one_ran(self):
		item = self.item()
		first = self.listing(f"AAA-{frappe.generate_hash(length=6)}", product=item.name)
		self.listing(f"BBB-{frappe.generate_hash(length=6)}", product=item.name)

		result = self.enrich([item.name])

		self.assertEqual(result["resolved"], [first.name])
		self.assertEqual(len(result["ambiguous"][item.name]), 2)

	def test_a_selection_that_resolves_to_nothing_creates_no_batch(self):
		# An empty batch would sit in Draft forever, saying nothing about why.
		bad = self.item(has_variants=1, variant_based_on="Manufacturer")
		before = frappe.db.count(BATCH_DOCTYPE)

		with patch(ENQUEUE, return_value=0):
			with self.assertRaises(frappe.ValidationError):
				api.bulk_enrich_items([bad.name])

		self.assertEqual(frappe.db.count(BATCH_DOCTYPE), before)

	def test_the_batch_knobs_and_toggles_survive_the_delegation(self):
		item = self.item()

		result = self.enrich(
			[item.name],
			batch_size=2,
			skip_enriched="true",
			notes="Supplier spec sheet attached.",
			prepare_images="true",
		)

		batch = frappe.get_doc(BATCH_DOCTYPE, result["batch"])
		self.assertEqual(batch.batch_size, 2)
		# "true" is what frappe.call sends for a ticked box, and cint("true") is 0 —
		# the reason _flag exists at all.
		self.assertEqual(batch.skip_enriched, 1)
		self.assertEqual(batch.prepare_images, 1)
		self.assertEqual(batch.notes, "Supplier spec sheet attached.")
