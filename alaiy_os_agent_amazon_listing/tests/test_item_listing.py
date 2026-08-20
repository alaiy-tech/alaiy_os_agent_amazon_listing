# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Resolving the listing behind an Item, when there is a real catalog to resolve in.

The rule the whole feature rests on is that pressing "Enrich Amazon Listing" twice
must be indistinguishable from pressing it once. An Item that has no listing gets one;
an Item that has one gets that one back, **untouched**. Amazon's own title, description
and photos beat anything an Item can offer, so a second run that "refreshed" them from
the Item would silently destroy real data every time — which is why the clobber test
here sets its own copy on the listing first and then asserts it survived.

The rest is the ambiguity a seller's catalog actually contains: several listings for
one Item (marketplaces, legacy SKUs), a listing row that exists under the item code but
was never linked, a row that belongs to somebody else's item, and a variant template
that is not a sellable SKU at all. Each has one right answer and no safe default.

Touches the database; no SP-API calls. Skipped whole when the Amazon connector is not
installed — it owns Amazon Product Listing, and this app runs without it.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from alaiy_os_agent_amazon_listing import item_listing

LISTING_DOCTYPE = "Amazon Product Listing"

if not frappe.db.exists("DocType", LISTING_DOCTYPE):
	raise unittest.SkipTest(f"{LISTING_DOCTYPE} is not installed (the Amazon connector is optional).")


class ItemListingCase(IntegrationTestCase):
	def item(self, **kwargs):
		"""An Item, cleaned up after the test (and after any listing made from it)."""
		values = {
			"doctype": "Item",
			"item_code": f"TEST-{frappe.generate_hash(length=8)}",
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"item_name": "Cotton Bath Towel",
			"description": "Quick-dry terry bath sheet, pack of 2.",
			"is_stock_item": 0,
		}
		values.update(kwargs)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Item", doc.name, force=True)
		return doc

	def listing(self, sku, **kwargs):
		values = {"doctype": LISTING_DOCTYPE, "sku": sku, "listing_status": "active"}
		values.update(kwargs)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, LISTING_DOCTYPE, doc.name, force=True)
		return doc

	def marketplace(self, **kwargs):
		values = {
			"doctype": "Amazon Marketplace",
			"marketplace_id": f"MP-{frappe.generate_hash(length=8)}",
			"country": "India",
			"region": "FE",
			"currency": "INR",
		}
		values.update(kwargs)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Amazon Marketplace", doc.name, force=True)
		return doc

	def ensure(self, item_code, marketplace=None, default=None):
		"""ensure_listing with the site's primary marketplace stubbed out.

		The connection is a Single whose real value belongs to whoever set the bench
		up, and the choice being tested is about listings, not about connections.
		"""
		with patch.object(item_listing, "default_marketplace", return_value=default):
			result = item_listing.ensure_listing(item_code, marketplace=marketplace)
		# Registered rows are cleaned up like any other, whether the test made them
		# directly or the code under test did.
		if result.get("created"):
			self.addCleanup(frappe.delete_doc, LISTING_DOCTYPE, result["sku"], force=True)
		return result


class TestEnsureListing(ItemListingCase):
	def test_an_item_with_no_listing_gets_one_named_after_it(self):
		item = self.item(image="/files/towel.jpg")

		result = self.ensure(item.name)

		self.assertTrue(result["created"])
		self.assertEqual(result["sku"], item.name)
		listing = frappe.get_doc(LISTING_DOCTYPE, item.name)
		self.assertEqual(listing.product, item.name)
		self.assertEqual(listing.title, "Cotton Bath Towel")
		self.assertEqual(listing.description, "Quick-dry terry bath sheet, pack of 2.")
		self.assertEqual([row.image_url for row in listing.images], ["/files/towel.jpg"])
		# Never `pending` — see test_item_listing_mapping for what that would cost.
		self.assertEqual(listing.listing_status, "incomplete")
		self.assertFalse(listing.last_synced_at)

	def test_a_second_call_returns_the_same_listing_and_creates_nothing(self):
		item = self.item()

		first = self.ensure(item.name)
		second = self.ensure(item.name)

		self.assertEqual(second["sku"], first["sku"])
		self.assertFalse(second["created"])
		self.assertEqual(frappe.db.count(LISTING_DOCTYPE, {"product": item.name}), 1)

	def test_an_existing_listing_keeps_its_own_copy(self):
		# The clobber guard. Amazon's copy is the better copy, and an enrichment that
		# refreshed it from the Item would lose it on every run.
		item = self.item()
		self.listing(
			item.name,
			product=item.name,
			title="Amazon's own title",
			description="Amazon's own description",
		)

		result = self.ensure(item.name)

		self.assertFalse(result["created"])
		listing = frappe.get_doc(LISTING_DOCTYPE, item.name)
		self.assertEqual(listing.title, "Amazon's own title")
		self.assertEqual(listing.description, "Amazon's own description")
		self.assertEqual(listing.listing_status, "active")

	def test_a_listing_under_another_sku_is_reused_not_duplicated(self):
		# One Item, listed as a different seller SKU — the normal case for a legacy
		# SKU or a marketplace-specific one.
		item = self.item()
		listing = self.listing(f"LEGACY-{frappe.generate_hash(length=6)}", product=item.name)

		result = self.ensure(item.name)

		self.assertEqual(result["sku"], listing.name)
		self.assertFalse(result["created"])
		self.assertFalse(frappe.db.exists(LISTING_DOCTYPE, item.name))

	def test_an_unlinked_row_of_the_same_name_is_adopted(self):
		# A listing synced from Amazon before anyone linked it to the Item. Claiming
		# it is one field; creating a second row is impossible (the sku is the name).
		item = self.item()
		self.listing(item.name, title="Synced from Amazon")

		result = self.ensure(item.name)

		self.assertTrue(result["adopted"])
		self.assertFalse(result["created"])
		self.assertEqual(result["sku"], item.name)
		listing = frappe.get_doc(LISTING_DOCTYPE, item.name)
		self.assertEqual(listing.product, item.name)
		self.assertEqual(listing.title, "Synced from Amazon")

	def test_a_row_belonging_to_another_item_is_refused(self):
		other = self.item()
		item = self.item(item_code=f"OWNED-{frappe.generate_hash(length=8)}")
		self.listing(item.name, product=other.name)

		with self.assertRaises(frappe.ValidationError):
			self.ensure(item.name)

	def test_a_variant_template_is_refused(self):
		# Amazon sells the child, not the template.
		item = self.item(has_variants=1, variant_based_on="Manufacturer")

		with self.assertRaises(frappe.ValidationError):
			self.ensure(item.name)

		self.assertFalse(frappe.db.exists(LISTING_DOCTYPE, item.name))

	def test_a_missing_marketplace_still_yields_a_saveable_row(self):
		# A bench with no Amazon connection yet is a normal place to write copy from.
		item = self.item()

		result = self.ensure(item.name, default=None)

		self.assertTrue(result["created"])
		self.assertFalse(frappe.db.get_value(LISTING_DOCTYPE, result["sku"], "marketplace"))

	def test_the_marketplace_reaches_the_row(self):
		item = self.item()
		mp = self.marketplace()

		result = self.ensure(item.name, default=mp.name)

		listing = frappe.get_doc(LISTING_DOCTYPE, result["sku"])
		self.assertEqual(listing.marketplace, mp.name)
		self.assertEqual(listing.currency, "INR")

	def test_a_disabled_item_is_enriched_and_said_to_be_disabled(self):
		item = self.item(disabled=1)

		result = self.ensure(item.name)

		self.assertTrue(result["created"])
		self.assertTrue(result["item_disabled"])


class TestResolveListing(ItemListingCase):
	"""Which listing, when the Item has more than one.

	One is picked rather than all of them, so a fifty-item selection produces fifty
	runs and not an unpredictable multiple of that. The pick has to be deterministic,
	or a rerun enriches a different listing than the run before it.
	"""

	def test_the_primary_marketplaces_listing_wins(self):
		item = self.item()
		home = self.marketplace()
		away = self.marketplace()
		self.listing(f"AWAY-{frappe.generate_hash(length=6)}", product=item.name, marketplace=away.name)
		mine = self.listing(
			f"HOME-{frappe.generate_hash(length=6)}", product=item.name, marketplace=home.name
		)

		with patch.object(item_listing, "default_marketplace", return_value=home.name):
			sku, listings = item_listing.resolve_listing(item.name)

		self.assertEqual(sku, mine.name)
		# All of them come back regardless: the caller reports the ambiguity, and
		# letting the user pick several is the next iteration of this.
		self.assertEqual(len(listings), 2)

	def test_the_marketplace_asked_for_beats_the_primary_one(self):
		item = self.item()
		home = self.marketplace()
		away = self.marketplace()
		self.listing(f"HOME-{frappe.generate_hash(length=6)}", product=item.name, marketplace=home.name)
		theirs = self.listing(
			f"AWAY-{frappe.generate_hash(length=6)}", product=item.name, marketplace=away.name
		)

		with patch.object(item_listing, "default_marketplace", return_value=home.name):
			sku, _ = item_listing.resolve_listing(item.name, marketplace=away.name)

		self.assertEqual(sku, theirs.name)

	def test_a_variation_parent_is_not_preferred_over_its_child(self):
		# A parent is a family, not a buyable offer: it has no copy of its own to fix.
		item = self.item()
		parent = self.listing(
			f"PARENT-{frappe.generate_hash(length=6)}", product=item.name, is_variation_parent=1
		)
		child = self.listing(f"CHILD-{frappe.generate_hash(length=6)}", product=item.name)

		with patch.object(item_listing, "default_marketplace", return_value=None):
			sku, listings = item_listing.resolve_listing(item.name)

		self.assertEqual(sku, child.name)
		self.assertIn(parent.name, listings)

	def test_a_parent_is_still_used_when_it_is_all_there_is(self):
		item = self.item()
		parent = self.listing(
			f"PARENT-{frappe.generate_hash(length=6)}", product=item.name, is_variation_parent=1
		)

		with patch.object(item_listing, "default_marketplace", return_value=None):
			sku, _ = item_listing.resolve_listing(item.name)

		self.assertEqual(sku, parent.name)

	def test_an_item_with_no_listings_resolves_to_nothing(self):
		item = self.item()

		with patch.object(item_listing, "default_marketplace", return_value=None):
			self.assertEqual(item_listing.resolve_listing(item.name), (None, []))
