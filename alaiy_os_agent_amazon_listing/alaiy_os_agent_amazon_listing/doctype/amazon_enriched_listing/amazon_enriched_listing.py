# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

LISTING_DOCTYPE = "Amazon Listing"

# Amazon accepts at most five key product features; a sixth is rejected on
# submission, so approval never publishes one.
MAX_BULLETS = 5


class AmazonEnrichedListing(Document):
	def on_update(self):
		# on_update fires after the row is written, so the database already says
		# "Approved" — the previous status must come from the pre-save snapshot.
		# A brand-new doc has no snapshot; an agent never inserts as Approved, so
		# only a real transition (anything -> Approved) pushes.
		before = self.get_doc_before_save()
		if self.status == "Approved":
			if before and before.status != "Approved":
				self._push_to_listing()
		elif before is None or before.status == "Approved":
			# The listing no longer carries approved content: either the agent
			# re-ran (save_listing resets status to "Needs Review", and a fresh
			# insert after a delete has no snapshot) or an admin un-approved it.
			self._clear_enriched_flag()

	def on_trash(self):
		# Deleting the enrichment record means nothing vouches for the listing's
		# content any more.
		self._clear_enriched_flag()

	def _clear_enriched_flag(self):
		if frappe.db.exists(LISTING_DOCTYPE, self.sku):
			frappe.db.set_value(
				LISTING_DOCTYPE, self.sku, "is_enriched", 0, update_modified=False
			)

	def _push_to_listing(self):
		"""Push approved enrichment back to the Amazon Listing.

		Only the four content fields the agent produces are written. Offer data
		(price, quantity, condition, fulfillment channel), the ASIN and the
		marketplace belong to the connector and are never touched here — and neither
		is the listing's own `suppression_reasons`, which only Amazon clears.

		This writes to the local Amazon Listing record, not to Amazon. Submitting it
		is the connector's job, on its own schedule.
		"""
		if not frappe.db.exists(LISTING_DOCTYPE, self.sku):
			frappe.throw(f"{LISTING_DOCTYPE} '{self.sku}' not found.")

		listing_doc = frappe.get_doc(LISTING_DOCTYPE, self.sku)

		listing_doc.is_enriched = 1
		listing_doc.title = self.title
		listing_doc.description = self.description

		self._sync_bullets(listing_doc)
		self._sync_keywords(listing_doc)
		self._sync_images(listing_doc)

		listing_doc.save(ignore_permissions=True)
		frappe.db.commit()

	def _sync_bullets(self, listing_doc):
		"""Replace the listing's key product features with the approved ones.

		The child table is the source, not `bullets_json`: the table is what the Desk
		form shows and what a reviewer corrects before approving, so it is what has to
		reach Amazon. The JSON stays the agent's untouched original.

		An enrichment that produced no bullets leaves the listing's existing ones
		alone — publishing an empty feature list would strip a working listing of its
		bullets, which is never what an approval means.
		"""
		bullets = [row.bullet.strip() for row in (self.bullet_points or []) if (row.bullet or "").strip()]
		if not bullets:
			return

		listing_doc.set("bullet_points", [])
		for bullet in bullets[:MAX_BULLETS]:
			listing_doc.append("bullet_points", {"bullet": bullet})

	def _sync_keywords(self, listing_doc):
		"""Replace the listing's backend search terms with the approved ones.

		Same rule as the bullets: the table wins over the JSON, and an empty result
		leaves what is already on the listing in place. A row enriched before the
		table existed has only the JSON, so an empty table falls back to it rather
		than silently publishing nothing.
		"""
		keywords = [
			row.keyword.strip() for row in (self.keywords or []) if (row.keyword or "").strip()
		]
		if not keywords:
			keywords = [k for k in (self._keywords_from_json() or []) if k]
		if not keywords:
			return

		listing_doc.set("keywords", [])
		for keyword in keywords:
			listing_doc.append("keywords", {"keyword": keyword})

	def _keywords_from_json(self):
		try:
			return json.loads(self.keywords_json or "[]") or []
		except (json.JSONDecodeError, ValueError):
			frappe.msgprint("Warning: keywords_json is not valid JSON, skipping keyword sync.")
			return []

	def _sync_images(self, listing_doc):
		"""Replace the listing's images with the ones this run produced.

		Order is the point of this method, not a side effect. Amazon shows the first
		image as the search-results tile, and for a child variant that has to be the
		variant's OWN photo — so the row whose role is `main` goes first and carries
		`is_main`, and the gallery follows in its own order. A run whose main image
		failed does NOT promote a gallery photo into its place: publishing the family's
		generic shot as the tile is the exact failure this ordering exists to prevent,
		so the listing keeps its current images and the reviewer is told.

		Only rows that actually have a url count — a queued or failed image is not an
		image. If none of them do, the listing keeps the photos it already has: an
		enrichment that ran without an image step, or whose imagery failed, must never
		leave a listing with no pictures at all.
		"""
		produced = [row for row in (self.images or []) if row.url]
		if not produced:
			return

		main = next((row for row in produced if (row.role or "") == "main"), None)
		gallery = [row for row in produced if row is not main]

		if not main:
			frappe.msgprint(
				"This enrichment has no main image, so the listing's existing images "
				"were left alone. Amazon shows the first image in search results and it "
				"must be this variant's own photo — re-run the image step, or set one "
				"row's Role to 'main', before approving the imagery."
			)
			return

		listing_doc.set("images", [])
		listing_doc.append("images", {"image_url": main.url, "is_main": 1})
		for row in gallery:
			listing_doc.append("images", {"image_url": row.url, "is_main": 0})
