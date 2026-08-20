# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Unit tests for the product type an enrichment carries.

The properties worth pinning are each a way a listing gets broken rather than a
matter of taste.

**A listing is classified from its ENRICHED title, never its raw one.** Amazon
is asked "what is this product?" once, after the copy is written, because the
product type and the title are published together and have to agree. Ask with
the raw title — routinely keyword-stuffed, mistranslated, or about a different
product than the enrichment turns out to be — and the answer can contradict the
title it ends up attached to, which Amazon rejects. That ordering is the reason
`resolve` takes a title argument at all, so it is the first thing tested.

Amazon is asked on every run, including for a listing that already carries a
type — a stored type classifies the copy the listing used to have, and the rows
most likely to be wrong are precisely the ones a "skip if set" rule would never
re-check. What the stored value still buys is a comparison (`disagrees`, which
sends the conflict to a human) and a floor to fall back to. And a lookup that
fails must cost the run its product type, not its enrichment.

Pure transformation only — no SP-API calls, no database.
"""

import unittest

import frappe
from frappe.tests import UnitTestCase

from alaiy_os_agent_amazon_listing import product_type

RAW_TITLE = "Towel Set Bag Multipurpose Cotton Cloth Best Quality Pack Combo"
ENRICHED_TITLE = "Cotton Bath Towel | Quick-Dry Terry Bath Sheet | Pack of 2"

TOWEL = {"product_type": "TOWEL", "display_name": "Towel"}
BATH_LINEN = {"product_type": "BATH_LINEN", "display_name": "Bath Linen"}
LUGGAGE = {"product_type": "LUGGAGE", "display_name": "Luggage"}


def _listing(name="SKU-1", title=RAW_TITLE, product_type=None, marketplace="Amazon.in"):
	"""The subset of an Amazon Product Listing this module reads."""
	return frappe._dict(
		name=name, title=title, product_type=product_type, marketplace=marketplace
	)


class TestResolve(UnitTestCase):
	def setUp(self):
		# resolve() memoizes per request; each test starts from a clean one.
		frappe.local.amazon_listing_product_types = {}
		self.asked = []

	def patch_suggest(self, answer):
		"""Record every title Amazon is asked about, and answer with `answer`."""

		def fake(title, marketplace=None):
			self.asked.append(title)
			return answer(title) if callable(answer) else list(answer)

		original = product_type.suggest
		product_type.suggest = fake
		self.addCleanup(setattr, product_type, "suggest", original)

	def test_amazon_is_asked_about_the_enriched_title_not_the_raw_one(self):
		# The property the whole module is arranged around. Asking with RAW_TITLE
		# would classify a listing that is about to stop existing.
		self.patch_suggest([TOWEL])

		product_type.resolve(_listing(), ENRICHED_TITLE)

		self.assertEqual(self.asked, [ENRICHED_TITLE])
		self.assertNotIn(RAW_TITLE, self.asked)

	def test_the_raw_title_is_never_a_fallback(self):
		# A run that somehow reaches save with no title must produce no product
		# type at all, rather than quietly classifying from the old copy.
		self.patch_suggest([TOWEL])

		resolved = product_type.resolve(_listing(), None)

		self.assertEqual(self.asked, [])
		self.assertIsNone(resolved["product_type"])
		self.assertEqual(resolved["source"], "none")

	def test_an_already_classified_listing_is_asked_about_too(self):
		# The listings most likely to be misclassified are the ones that already
		# carry a type from badly-written copy, so skipping the call when one
		# exists would never re-check exactly the rows that need it.
		self.patch_suggest([TOWEL])

		resolved = product_type.resolve(_listing(product_type="LUGGAGE"), ENRICHED_TITLE)

		self.assertEqual(self.asked, [ENRICHED_TITLE])
		self.assertEqual(resolved["product_type"], "TOWEL")
		self.assertEqual(resolved["source"], "suggested")

	def test_the_type_a_listing_publishes_with_today_is_reported_back(self):
		# Kept so the caller can compare rather than apply blindly.
		self.patch_suggest([TOWEL])

		resolved = product_type.resolve(_listing(product_type="LUGGAGE"), ENRICHED_TITLE)

		self.assertEqual(resolved["existing"], "LUGGAGE")
		self.assertTrue(product_type.disagrees(resolved))

	def test_amazon_confirming_the_existing_type_is_not_a_disagreement(self):
		self.patch_suggest([TOWEL])

		resolved = product_type.resolve(_listing(product_type="towel"), ENRICHED_TITLE)

		self.assertFalse(product_type.disagrees(resolved))

	def test_a_first_classification_is_not_a_disagreement(self):
		self.patch_suggest([TOWEL])

		resolved = product_type.resolve(_listing(), ENRICHED_TITLE)

		self.assertIsNone(resolved["existing"])
		self.assertFalse(product_type.disagrees(resolved))

	def test_an_existing_type_is_the_floor_when_amazon_has_no_answer(self):
		# Dropping it would leave the listing unwritable on Amazon, which is a
		# worse outcome than keeping a classification nobody re-confirmed.
		self.patch_suggest([])

		resolved = product_type.resolve(_listing(product_type="LUGGAGE"), ENRICHED_TITLE)

		self.assertEqual(resolved["product_type"], "LUGGAGE")
		self.assertEqual(resolved["source"], "listing")

	def test_a_listing_without_one_takes_the_top_match_for_the_new_title(self):
		self.patch_suggest([TOWEL, BATH_LINEN])

		resolved = product_type.resolve(_listing(), ENRICHED_TITLE)

		self.assertEqual(resolved["product_type"], "TOWEL")
		self.assertEqual(resolved["product_type_display"], "Towel")
		self.assertEqual(resolved["source"], "suggested")
		# The whole shortlist travels with it: the reviewer picks from it, and
		# the order is Amazon's only confidence signal.
		self.assertEqual(resolved["suggestions"], [TOWEL, BATH_LINEN])

	def test_a_rewritten_title_can_change_the_answer(self):
		# Why the ordering earns its complexity: the raw title reads as luggage,
		# the enriched one as what the product actually is.
		self.patch_suggest(lambda title: [LUGGAGE] if title == RAW_TITLE else [TOWEL])

		self.assertEqual(
			product_type.resolve(_listing(), ENRICHED_TITLE)["product_type"], "TOWEL"
		)

	def test_auto_accept_off_keeps_the_shortlist_but_chooses_nothing(self):
		self.patch_suggest([TOWEL, BATH_LINEN])
		frappe.conf[product_type.AUTO_ACCEPT_KEY] = 0
		self.addCleanup(frappe.conf.pop, product_type.AUTO_ACCEPT_KEY, None)

		resolved = product_type.resolve(_listing(), ENRICHED_TITLE)

		self.assertIsNone(resolved["product_type"])
		self.assertEqual(resolved["source"], "none")
		self.assertEqual(resolved["suggestions"], [TOWEL, BATH_LINEN])

	def test_nothing_suggested_is_an_answer_not_an_error(self):
		self.patch_suggest([])

		resolved = product_type.resolve(_listing(), ENRICHED_TITLE)

		self.assertIsNone(resolved["product_type"])
		self.assertEqual(resolved["source"], "none")

	def test_the_same_title_is_only_looked_up_once(self):
		self.patch_suggest([TOWEL])

		product_type.resolve(_listing(), ENRICHED_TITLE)
		product_type.resolve(_listing(), ENRICHED_TITLE)

		self.assertEqual(len(self.asked), 1)

	def test_a_different_title_is_a_different_question(self):
		# A re-run that rewrites the title must not inherit the previous run's
		# classification out of the memo.
		self.patch_suggest([TOWEL])

		product_type.resolve(_listing(), ENRICHED_TITLE)
		product_type.resolve(_listing(), "Steel Vacuum Water Bottle | 1 Litre")

		self.assertEqual(len(self.asked), 2)


class TestExisting(UnitTestCase):
	def test_reports_what_amazon_already_decided(self):
		self.assertEqual(product_type.existing(_listing(product_type="TOWEL")), "TOWEL")

	def test_blank_is_none_rather_than_an_empty_string(self):
		# get_product puts this straight into the model's context; "" would read
		# as a classification rather than the absence of one.
		self.assertIsNone(product_type.existing(_listing(product_type="   ")))


try:
	import alaiy_os_connector_amazon_sp_api.spapi.product_types as connector
except ImportError:
	# This app installs without the Amazon connector, which is the whole reason
	# `suggest` guards its import — so the bench that proves it cannot run the
	# test that patches it.
	connector = None


class TestSuggestNeverRaises(UnitTestCase):
	@unittest.skipIf(connector is None, "the Amazon SP-API connector is not installed")
	def test_an_sp_api_failure_costs_the_product_type_not_the_run(self):
		def boom(*a, **k):
			raise RuntimeError("SP-API is down")

		original = connector.suggest_product_types
		connector.suggest_product_types = boom
		self.addCleanup(setattr, connector, "suggest_product_types", original)

		self.assertEqual(product_type.suggest(ENRICHED_TITLE), [])

	def test_no_title_is_not_worth_asking_about(self):
		self.assertEqual(product_type.suggest("   "), [])


class TestDisplayName(UnitTestCase):
	def test_falls_back_to_the_key_itself(self):
		self.assertEqual(product_type.display_name("TOWEL"), "TOWEL")

	def test_uses_amazons_name_when_the_shortlist_has_one(self):
		self.assertEqual(product_type.display_name("towel", [TOWEL]), "Towel")

	def test_no_product_type_has_no_name(self):
		self.assertIsNone(product_type.display_name(None, [TOWEL]))
