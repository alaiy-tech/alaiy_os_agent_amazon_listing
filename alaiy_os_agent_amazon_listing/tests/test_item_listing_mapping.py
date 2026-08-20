# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""What a listing built from an Item may and may not claim about itself.

Every property here is a way the catalog gets quietly damaged rather than a matter
of taste.

**The row is `incomplete`, never `pending`.** The doctype's own default is `pending`,
and to the connector that means "a push Amazon has not confirmed yet" — its reconcile
deliberately skips such rows so it cannot stamp on an in-flight write. A row created
from an Item and left at the default would therefore be invisible to every reconcile
that ever runs, which is the most expensive mistake available in this mapping and so
the first thing tested. For the same reason the sync timestamps stay empty: they are
what says this row has never been near Amazon.

**Nothing is invented.** ERPNext hands out `item_name` defaulted to `item_code` and
`description` defaulted to `item_name`, so copying them through would tell the agent
that the seller's existing title and copy are "SKU-00123" — and the model would try to
honour it. An empty field is the honest input. A price would be worse still: a
standard rate is not an Amazon offer, but it reads as one in get_product.

Pure transformation. No database beyond the marketplace's currency, which is patched.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from alaiy_os_agent_amazon_listing import item_listing


def _item(item_code="SKU-1", **kwargs):
	"""The subset of an Item this mapping reads."""
	values = {
		"name": item_code,
		"item_code": item_code,
		"item_name": "Cotton Bath Towel",
		"description": "Quick-dry terry bath sheet, pack of 2.",
		"image": None,
		"disabled": 0,
		"has_variants": 0,
	}
	values.update(kwargs)
	return frappe._dict(values)


class TestListingValuesFromItem(UnitTestCase):
	def values(self, item=None, marketplace=None, currency="INR"):
		with patch.object(frappe.db, "get_value", return_value=currency):
			return item_listing.listing_values_from_item(item or _item(), marketplace=marketplace)

	def test_a_locally_created_listing_is_incomplete_not_pending(self):
		# `pending` means Amazon has not confirmed a push, and the connector's
		# reconcile skips those rows on purpose — so a row that claimed it would
		# never be reconciled again.
		self.assertEqual(self.values()["listing_status"], "incomplete")

	def test_a_row_that_never_touched_amazon_claims_no_sync_time(self):
		values = self.values()

		self.assertNotIn("last_synced_at", values)
		self.assertNotIn("catalog_synced_at", values)

	def test_the_sku_and_the_item_link_are_both_the_item_code(self):
		values = self.values()

		self.assertEqual(values["sku"], "SKU-1")
		# What the Item form filters on, so the next visit finds this row instead of
		# trying to create it again.
		self.assertEqual(values["product"], "SKU-1")

	def test_the_items_name_becomes_the_title(self):
		self.assertEqual(self.values()["title"], "Cotton Bath Towel")

	def test_an_item_name_that_is_just_the_code_is_not_a_title(self):
		values = self.values(_item(item_name="SKU-1"))

		self.assertNotIn("title", values)

	def test_a_description_echoing_the_item_name_is_not_copied(self):
		values = self.values(_item(description="Cotton Bath Towel"))

		self.assertNotIn("description", values)

	def test_the_items_photo_becomes_the_main_image(self):
		values = self.values(_item(image="/files/towel.jpg"))

		self.assertEqual(values["images"], [{"image_url": "/files/towel.jpg", "is_main": 1}])

	def test_an_item_without_a_photo_gets_no_image_rows(self):
		self.assertNotIn("images", self.values())

	def test_no_price_or_quantity_is_invented(self):
		values = self.values()

		self.assertNotIn("price", values)
		self.assertNotIn("quantity", values)

	def test_no_asin_or_product_type_is_invented(self):
		values = self.values()

		self.assertNotIn("asin", values)
		self.assertNotIn("product_type", values)

	def test_currency_comes_from_the_marketplace(self):
		values = self.values(marketplace="A21TJRUUN4KGV", currency="INR")

		self.assertEqual(values["marketplace"], "A21TJRUUN4KGV")
		self.assertEqual(values["currency"], "INR")

	def test_a_row_without_a_marketplace_is_still_a_row(self):
		# `marketplace` is not required, and an unconnected site is a normal state to
		# enrich from — the copy does not depend on it.
		values = self.values(marketplace=None)

		self.assertNotIn("marketplace", values)
		self.assertNotIn("currency", values)
