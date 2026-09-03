# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Unit tests for the house brand an enrichment is filed under.

The brand is the one field here that is the model's own judgement rather than a
lookup (see `brand.py`, and `product_type.py` for the deliberate counterpart).
So `_save_brand` is the only thing standing between an answer and the record,
and what it does with each shape of answer is worth pinning.

**A name that is not one of this deployment's registered brands is no brand at
all.** The model is told the names in prose, in a prompt override this app does
not own, so a name it invents or misspells reaches save time looking exactly
like a real one. Falling back to null and flagging the row is what keeps a
brand that does not exist off an enrichment.

**Missing, null and blank are the same answer.** `brand` is a required field in
the output schema, but a response can still arrive without it, and a reviewer
should not have to tell the difference between a model that declined and a
model that forgot.

**A deployment with no house brands is left entirely alone**, including the
`brand` already on the record. That case is not "nobody assigned a brand", it
is "this site does not use brands", and a re-run that wiped a value a reviewer
typed would be a data loss bug, not a stricter check.

Pure transformation only — no database.
"""

import frappe
from frappe.tests import UnitTestCase

from alaiy_os_agent_amazon_listing.tools import handlers

FLAG = "Brand (doesn't clearly fit one of this site's house brands)"
BRANDS = {"Acme Home", "Acme Outdoor"}


def _doc(**kwargs):
	"""The subset of an Amazon Enriched Listing `_save_brand` touches."""
	values = {"brand": None, "needs_review": ""}
	values.update(kwargs)
	return frappe._dict(values)


class TestSaveBrand(UnitTestCase):
	def registered(self, names):
		"""Stand in for this site's `amazon_listing_house_brands` hook."""
		original = handlers.brands.valid_brands
		handlers.brands.valid_brands = lambda: set(names)
		self.addCleanup(setattr, handlers.brands, "valid_brands", original)

	def save(self, listing, doc=None, names=BRANDS):
		self.registered(names)
		doc = doc or _doc()
		handlers._save_brand(doc, listing)
		return doc

	def test_a_registered_brand_is_assigned_and_not_flagged(self):
		doc = self.save({"brand": "Acme Home"})

		self.assertEqual(doc.brand, "Acme Home")
		self.assertNotIn(FLAG, doc.needs_review)

	def test_surrounding_whitespace_is_forgiven(self):
		# Worth allowing: it is the same answer, just untidily serialised.
		doc = self.save({"brand": "  Acme Outdoor "})

		self.assertEqual(doc.brand, "Acme Outdoor")

	def test_an_unregistered_name_is_no_brand_at_all(self):
		# The response passes the schema — `brand` is only typed as a string —
		# so this is the check that keeps an invented brand off the record.
		doc = self.save({"brand": "Acme Aquatic"})

		self.assertIsNone(doc.brand)
		self.assertIn(FLAG, doc.needs_review)

	def test_null_missing_and_blank_are_the_same_answer(self):
		for listing in ({"brand": None}, {}, {"brand": "   "}):
			with self.subTest(listing=listing):
				doc = self.save(listing)

				self.assertIsNone(doc.brand)
				self.assertIn(FLAG, doc.needs_review)

	def test_the_flag_joins_what_the_model_already_reported(self):
		# needs_review is flattened text by the time _save_brand sees it, and
		# the model's own entries have to survive.
		doc = self.save({"brand": None}, doc=_doc(needs_review="Material\nBullet 5"))

		self.assertEqual(doc.needs_review.splitlines(), ["Material", "Bullet 5", FLAG])

	def test_a_site_with_no_house_brands_is_left_alone(self):
		# Not "nobody assigned a brand" but "this deployment doesn't use
		# brands": nothing to nag about, and nothing to overwrite.
		doc = self.save({"brand": "Acme Home"}, doc=_doc(brand="Set By A Reviewer"), names=set())

		self.assertEqual(doc.brand, "Set By A Reviewer")
		self.assertEqual(doc.needs_review, "")
