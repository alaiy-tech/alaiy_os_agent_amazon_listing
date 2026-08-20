# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""How an enrichment's product type behaves once it is a real record.

Three things the pure logic cannot show. A reviewer who changes the product type
must be recorded as having done so — that is the difference between a confirmed
answer and Amazon's unexamined first guess. A re-run must NOT be mistaken for a
reviewer, even when it lands on a different type than the run before it. And
approval must actually publish the reviewed type onto the listing, including
when it replaces one: the disagreement was put in front of the reviewer when the
run saved, so approving it is the decision, and a push that quietly declined to
apply it would make asking pointless.

Touches the database; no SP-API calls.
"""

import frappe
from frappe.tests import IntegrationTestCase

ENRICHED_DOCTYPE = "Amazon Enriched Listing"
LISTING_DOCTYPE = "Amazon Product Listing"


class TestProductTypeOverride(IntegrationTestCase):
	def enrichment(self, **kwargs):
		doc = frappe.get_doc(
			{
				"doctype": ENRICHED_DOCTYPE,
				"sku": frappe.generate_hash(length=10),
				"title": "Cotton Bath Towel",
				"product_type": "TOWEL",
				"product_type_source": "suggested",
				**kwargs,
			}
		)
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, ENRICHED_DOCTYPE, doc.name, force=True)
		return doc

	def test_a_reviewers_change_is_recorded_as_theirs(self):
		doc = self.enrichment()

		doc.product_type = "BATH_LINEN"
		doc.save(ignore_permissions=True)

		self.assertEqual(doc.product_type_source, "reviewer")

	def test_clearing_the_product_type_leaves_nobody_holding_it(self):
		doc = self.enrichment()

		doc.product_type = None
		doc.save(ignore_permissions=True)

		self.assertEqual(doc.product_type_source, "none")

	def test_a_re_run_landing_elsewhere_is_still_the_agent(self):
		doc = self.enrichment()

		# What save_listing does: sets both fields, and says it is a run.
		doc.product_type = "BATH_LINEN"
		doc.product_type_source = "suggested"
		doc.flags.from_agent = True
		doc.save(ignore_permissions=True)

		self.assertEqual(doc.product_type_source, "suggested")

	def test_editing_something_else_leaves_the_source_alone(self):
		doc = self.enrichment()

		doc.notes = "Checked against the supplier spec sheet."
		doc.save(ignore_permissions=True)

		self.assertEqual(doc.product_type_source, "suggested")


class TestApprovalPublishesProductType(IntegrationTestCase):
	"""What approval writes onto the listing.

	Driven through `_sync_product_type` against a stand-in listing rather than a
	real one: `Amazon Product Listing` belongs to the Amazon connector, which a
	bench running this app need not have installed, and the rule being pinned is
	about which value wins — not about Frappe's ability to save a document.
	"""

	def sync(self, enrichment_type, listing_type):
		doc = frappe.get_doc(
			{
				"doctype": ENRICHED_DOCTYPE,
				"sku": frappe.generate_hash(length=10),
				"title": "Cotton Bath Towel | Quick-Dry Terry Bath Sheet | Pack of 2",
				"product_type": enrichment_type,
			}
		)
		listing = frappe._dict(product_type=listing_type)
		doc._sync_product_type(listing)
		return listing.product_type

	def test_fills_a_listing_that_had_none(self):
		# Without this the listing cannot be updated on Amazon at all.
		self.assertEqual(self.sync("TOWEL", None), "TOWEL")

	def test_replaces_a_classification_the_rewrite_outgrew(self):
		# The reviewer saw both types in needs_review and approved anyway, which
		# is the decision — declining to apply it would make asking pointless.
		self.assertEqual(self.sync("TOWEL", "LUGGAGE"), "TOWEL")

	def test_an_enrichment_with_no_product_type_never_blanks_the_listing(self):
		# Says nothing about the classification, so it must not destroy one the
		# listing is publishing with today.
		self.assertEqual(self.sync(None, "TOWEL"), "TOWEL")

	def test_agreement_is_left_untouched(self):
		self.assertEqual(self.sync("TOWEL", "TOWEL"), "TOWEL")
