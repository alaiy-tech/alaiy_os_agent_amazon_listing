# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Unit tests for the house brand an enrichment is filed under.

The brand used to be the ninth key of the enrichment JSON, decided by the same
turn that wrote the title, the bullets, the description and the keywords, and
it lost: null on half the catalogue, and on the rest mostly a transcription of
whatever brand the incoming title already carried. It is now decided in its
own call, and these tests are about that call being the thing that reaches the
record.

**The classifier's answer wins over the agent's.** Both exist -- the agent is
shown the brand so it can open the title with it, and echoes it back in its
output -- but the echo is the copy that goes missing, so `_save_brand` fills
from the classifier and uses the echo only to notice a disagreement. That is
the property the whole change rests on, so it is tested first.

**A disagreement is a reviewer's problem, not a silent correction.** When the
two differ the record takes the classifier's brand while the title was already
written around the agent's, so the row is internally inconsistent and the half
that publishes is the wrong one. Writing the right brand and saying nothing
would hide a wrong title.

**A reply is matched, not trusted.** Asking a model for one name and nothing
else is not the same as getting it, so a reply that explains itself, or
changes the case, still resolves to the registered brand it names -- and a
reply naming nothing recognisable resolves to nothing at all.

**A failed classification costs the run its brand, never its enrichment.**
Same discipline product_type.py applies to an SP-API outage.

**A deployment with no house brands is left entirely alone**, including the
`brand` already on the record. That is not "nobody assigned a brand" but "this
site does not use brands", and a re-run that wiped a reviewer's value would be
data loss rather than a stricter check.

Pure transformation only -- no database, and no model calls.
"""

import frappe
from frappe.tests import UnitTestCase

from alaiy_os_agent_amazon_listing import brand
from alaiy_os_agent_amazon_listing.tools import handlers

BRANDS = {"Acme Home", "Acme Outdoor"}


def _doc(**kwargs):
	"""The subset of an Amazon Enriched Listing `_save_brand` touches."""
	values = {"brand": None, "needs_review": ""}
	values.update(kwargs)
	return frappe._dict(values)


def _listing(name="SKU-1", **kwargs):
	"""The subset of an Amazon Product Listing the classifier reads."""
	values = {"name": name, "title": "Dog Raincoat", "description": "", "bullet_points": []}
	values.update(kwargs)
	return frappe._dict(values)


def _reply(text):
	"""A model reply in the block shape every ai_client returns."""
	return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


class BrandCase(UnitTestCase):
	def setUp(self):
		# classify() memoizes per request; each test starts from a clean one.
		setattr(frappe.local, brand._CACHE, {})

	def registered(self, names):
		"""Stand in for this site's `amazon_listing_house_brands` hook."""
		original = brand.valid_brands
		brand.valid_brands = lambda: set(names)
		self.addCleanup(setattr, brand, "valid_brands", original)

	def answers(self, reply):
		"""Record every classification asked for, and answer with `reply`.

		Patches `_ask` rather than the LLM seam for the save-time tests: what
		they are about is which answer reaches the record, not how it was got.
		"""
		self.asked = []

		def fake(listing, names):
			self.asked.append(listing.get("name"))
			return reply(listing) if callable(reply) else reply

		original = brand._ask
		brand._ask = fake
		self.addCleanup(setattr, brand, "_ask", original)


class TestSaveBrand(BrandCase):
	def save(self, output, doc=None, classified="Acme Home", names=BRANDS):
		self.registered(names)
		self.answers(classified)
		doc = doc or _doc()
		handlers._save_brand(doc, output, _listing())
		return doc

	def test_the_classifier_fills_the_record_not_the_enrichment(self):
		# The property the change rests on: the agent's output is not consulted
		# for the value at all when there is a classification.
		doc = self.save({"brand": None}, classified="Acme Outdoor")

		self.assertEqual(doc.brand, "Acme Outdoor")

	def test_an_agreeing_enrichment_is_not_flagged(self):
		doc = self.save({"brand": "Acme Home"}, classified="Acme Home")

		self.assertEqual(doc.brand, "Acme Home")
		self.assertEqual(doc.needs_review, "")

	def test_a_disagreement_reaches_the_reviewer(self):
		# The record says one brand, the title was written around the other,
		# and the title is the half that publishes.
		doc = self.save({"brand": "Acme Outdoor"}, classified="Acme Home")

		self.assertEqual(doc.brand, "Acme Home")
		self.assertIn("Acme Home", doc.needs_review)
		self.assertIn("Acme Outdoor", doc.needs_review)

	def test_an_unregistered_echo_is_not_a_disagreement(self):
		# A name this deployment does not have is noise, not a competing
		# opinion, so it is dropped rather than argued with.
		doc = self.save({"brand": "Acme Aquatic"}, classified="Acme Home")

		self.assertEqual(doc.brand, "Acme Home")
		self.assertEqual(doc.needs_review, "")

	def test_a_failed_classification_falls_back_to_the_enrichment(self):
		doc = self.save({"brand": "Acme Outdoor"}, classified=None)

		self.assertEqual(doc.brand, "Acme Outdoor")
		self.assertEqual(doc.needs_review, "")

	def test_nothing_to_write_is_flagged(self):
		# A site with house brands should not have brandless rows -- that is
		# the bug this path exists to fix.
		doc = self.save({"brand": None}, classified=None)

		self.assertIsNone(doc.brand)
		self.assertIn("Brand", doc.needs_review)

	def test_a_flag_joins_what_the_agent_already_reported(self):
		doc = self.save({"brand": None}, doc=_doc(needs_review="Material"), classified=None)

		self.assertEqual(doc.needs_review.splitlines()[0], "Material")
		self.assertEqual(len(doc.needs_review.splitlines()), 2)

	def test_a_site_with_no_house_brands_is_left_alone(self):
		doc = self.save(
			{"brand": "Acme Home"}, doc=_doc(brand="Set By A Reviewer"), names=set()
		)

		self.assertEqual(doc.brand, "Set By A Reviewer")
		self.assertEqual(doc.needs_review, "")
		self.assertEqual(self.asked, [])


class TestClassify(BrandCase):
	def replies(self, text):
		"""Stand in for the model, at the ai_client seam."""
		from alaiy_os.engine import llm

		self.calls = []

		def fake(model, system, messages, tools=None):
			self.calls.append(messages)
			if isinstance(text, Exception):
				raise text
			return _reply(text)

		original = llm.complete
		llm.complete = fake
		self.addCleanup(setattr, llm, "complete", original)

		original_model = brand._model
		brand._model = lambda: "test-model"
		self.addCleanup(setattr, brand, "_model", original_model)

		original_instructions = brand._instructions
		brand._instructions = lambda names: "coverage"
		self.addCleanup(setattr, brand, "_instructions", original_instructions)

	def ask(self, text, listing=None, names=BRANDS):
		self.registered(names)
		self.replies(text)
		return brand.classify(listing or _listing())

	def test_a_bare_name_is_the_answer(self):
		self.assertEqual(self.ask("Acme Outdoor"), "Acme Outdoor")

	def test_a_model_that_explained_itself_anyway_is_still_read(self):
		self.assertEqual(
			self.ask("This is a pet product, so: Acme Outdoor."), "Acme Outdoor"
		)

	def test_case_is_forgiven_but_the_registered_spelling_is_written(self):
		# `_save_brand` matches against the hook exactly, so the answer has to
		# come back in the deployment's own spelling.
		self.assertEqual(self.ask("acme home"), "Acme Home")

	def test_a_reply_naming_nothing_registered_is_no_answer(self):
		self.assertIsNone(self.ask("None of these apply."))

	def test_a_failed_call_costs_the_brand_and_nothing_else(self):
		self.assertIsNone(self.ask(RuntimeError("provider down")))

	def test_a_site_with_no_house_brands_is_never_asked(self):
		self.assertIsNone(self.ask("Acme Home", names=set()))
		self.assertEqual(self.calls, [])

	def test_one_sku_is_classified_once_per_request(self):
		# get_product asks so the title can open with the brand, and save time
		# asks again so the record does not depend on the agent's echo. Those
		# have to be the same answer, and one call.
		self.registered(BRANDS)
		self.replies("Acme Home")

		first = brand.classify(_listing())
		second = brand.classify(_listing())

		self.assertEqual((first, second), ("Acme Home", "Acme Home"))
		self.assertEqual(len(self.calls), 1)

	def test_the_product_is_shown_and_the_listing_metadata_is_not(self):
		# Price, marketplace and status are facts about a listing, not about
		# what the thing is; each is a chance to answer a different question.
		self.registered(BRANDS)
		self.replies("Acme Home")

		brand.classify(_listing(title="Dog Raincoat", description="Waterproof pet jacket."))

		shown = self.calls[0][0]["content"]
		self.assertIn("Dog Raincoat", shown)
		self.assertIn("Waterproof pet jacket.", shown)
		self.assertNotIn("SKU-1", shown)
