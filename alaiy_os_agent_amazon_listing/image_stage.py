# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Stage two: the images, rendered after the listing text is already saved.

The agent's run is stage one. It reads the listing, writes the copy, and — when an
image toggle is on — resolves *which photos* the work applies to: the ones to
retouch, or the ones to translate. It does not wait for that imagery. The image tool
returns placeholders with ``url: None`` and queues the work here, so a run finishes
in the ~35s its LLM turns actually take instead of the ~5 minutes the image API
takes.

Why split at all: the two halves have opposite shapes. Stage one is LLM-bound and
short; stage two is minutes of pure HTTP waiting on a paid image service. Sharing
one worker pool means a single image product holds a slot that could have cleared
nine text listings, and it delays the listing text — the part a human actually
reviews — behind imagery they will look at later.

The handoff is deliberately not a protocol the model can corrupt. The tool enqueues
the job itself with ``enqueue_after_commit=True``, so it fires on ``save_listing``'s
commit — the enriched listing is guaranteed to exist by then — and is dropped
entirely if the run fails first, because ``db.rollback()`` resets the after-commit
callbacks. Everything stage two needs travels in the job's own arguments; nothing is
reconstructed by guessing at saved rows.
"""

import frappe

ENRICHED_DOCTYPE = "Amazon Enriched Listing"

# Stage two runs on its own queue when the bench defines one, because the whole
# point is that image work cannot starve anything else. Falls back to `long` so the
# app still works on a stock bench — see the README on adding a dedicated queue.
QUEUE_KEY = "listing_image_queue"
DEFAULT_QUEUE = "long"

# Generous: a listing with many photos is that many sequential-worst-case renders
# plus the re-hosting of each result.
JOB_TIMEOUT = 1800

# The step stage two knows how to render. One step, not two: the main image and the
# gallery are different operations on the same product and are queued together, so
# there is nothing to dispatch between — see tools/image_prepare.py.
PREPARE = "prepare"


def image_queue():
	"""The queue stage two runs on, falling back to `long` if it isn't configured.

	A custom queue only exists if the bench declares it under `workers` in
	common_site_config.json — Frappe validates the name and raises otherwise. That
	raise would happen inside the agent's run and fail an otherwise good
	enrichment, so a queue named here but never provisioned degrades to `long`
	instead of taking the listing down with it.
	"""
	from frappe.utils.background_jobs import get_queues_timeout

	queue = frappe.conf.get(QUEUE_KEY)
	if queue and queue in get_queues_timeout():
		return queue
	if queue:
		frappe.log_error(
			title="Amazon listing images: unknown queue",
			message=(
				f"'{queue}' is set as {QUEUE_KEY} but is not declared under `workers` "
				f"in common_site_config.json; falling back to '{DEFAULT_QUEUE}'."
			),
		)
	return DEFAULT_QUEUE


def queue_step(sku, step, work):
	"""Queue this listing's image work, to run once the listing itself is saved.

	Called by the image tools mid-run. `work` is everything the renderer needs —
	the resolved photo urls and the plan of which row each belongs to — so stage two
	never has to re-derive intent from what the model chose to write down.
	"""
	frappe.enqueue(
		"alaiy_os_agent_amazon_listing.image_stage.run_step",
		queue=image_queue(),
		timeout=JOB_TIMEOUT,
		# One image job per listing. A re-run that queues again while the first is
		# still waiting replaces nothing and adds nothing.
		job_id=f"amazon-listing-images::{sku}",
		deduplicate=True,
		sku=sku,
		step=step,
		work=work,
		# Fires on save_listing's commit; discarded if the run rolls back first.
		enqueue_after_commit=True,
	)


def run_step(sku, step, work):
	"""Worker entry point: render this listing's images and patch them in."""
	if not frappe.db.exists(ENRICHED_DOCTYPE, sku):
		# The only way here is a run that saved its listing and had it deleted
		# before this job ran. Nothing to patch, and nothing worth failing over.
		_nudge_batches(sku)
		return

	_set_state(sku, "Running", None)

	try:
		result = _render(step, sku, work)
	except Exception as exc:
		# The service is down, unconfigured, or refused the whole listing. The
		# listing text is already saved and reviewable — only the imagery is lost.
		frappe.log_error(title=f"Amazon listing images {sku}: {step} failed")
		_set_state(sku, "Failed", _summary(str(exc)))
		_publish(sku, "Failed")
		_nudge_batches(sku)
		return

	rendered = result["images"]
	produced = _apply(sku, rendered)
	failed = len(rendered) - produced

	if not produced:
		status = "Failed" if rendered else "Ready"
	elif failed:
		status = "Partial"
	else:
		status = "Ready"

	_set_state(
		sku,
		status,
		_first_note(rendered) if failed else None,
		# The image spend happens out here now, after the Run is closed, so it is
		# recorded against the listing instead of vanishing from the accounting.
		tokens=result.get("image_tokens") or 0,
	)
	_publish(sku, status)
	_nudge_batches(sku)


def _nudge_batches(sku):
	"""Tell bulk enrichment this listing's imagery is settled — a batch whose runs
	are all done parks in "Generating Images" and waits for exactly this (see
	bulk._finalize). Never allowed to fail the job: the images themselves are
	already applied by the time this runs."""
	try:
		from alaiy_os_agent_amazon_listing.bulk import finalize_images

		finalize_images(sku)
	except Exception:
		frappe.log_error(title=f"Amazon listing images {sku}: batch nudge failed")


def _render(step, sku, work):
	"""Dispatch to the module that owns this step. Returns the finished image rows."""
	if step == PREPARE:
		from alaiy_os_agent_amazon_listing.tools.image_prepare import render_prepared

		return render_prepared(sku, work)

	frappe.throw(f"Unknown image step '{step}'.")


def _apply(sku, rendered):
	"""Write the rendered urls onto the listing's image rows. Returns how many worked.

	Rows are matched to what stage one already wrote — by `source_url` WITHIN the same
	`role` — and appended if not there, so a rendered image is never silently thrown
	away. Role scoping is what keeps the two results for one shared photo apart: the
	white-background main image and the translated gallery copy have the same
	source_url and must never overwrite each other.

	Appending is not an edge case: the rows are rebuilt by save_listing from what the
	MODEL wrote down, and a model that dropped an entry — or its role — would otherwise
	cost that photo its result, or worse, its place in the image order. Here the plan
	the tool queued wins, and the listing ends up with the rows it should have
	regardless of what the model reported.

	The rows are then re-ordered main-first, because `idx` is the listing's image
	order and an appended row would otherwise land after the gallery.
	"""
	doc = frappe.get_doc(ENRICHED_DOCTYPE, sku)
	existing = list(doc.images or [])
	produced = 0

	for image in rendered:
		rows = _match(existing, image)
		if not rows:
			row = doc.append("images", {"role": image.get("role")})
			existing.append(row)
			rows = [row]

		for row in rows:
			row.role = image.get("role") or row.role
			row.kind = image.get("kind") or row.kind
			row.source_url = image.get("source_url") or row.source_url
			row.brief = image.get("brief") or row.brief
			row.url = image.get("url")
			row.note = image.get("note")
		if image.get("url"):
			produced += 1

	_reorder(doc)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return produced


def _reorder(doc):
	"""Main image first, then the gallery in the order it was queued.

	Amazon takes the first image as the search-results tile, and approval publishes
	these rows in table order (see AmazonEnrichedListing._sync_images), so the order
	here is not cosmetic.
	"""
	rows = sorted(doc.images or [], key=lambda r: (0 if (r.role or "") == "main" else 1, r.idx or 0))
	for idx, row in enumerate(rows, start=1):
		row.idx = idx
	doc.set("images", rows)


def _match(rows, image):
	"""The row(s) this rendered image belongs in: the placeholders stage one wrote
	for it, plus any row that already holds exactly this result.

	Scoped to the rendered image's own role first. Without that, the main image and
	the gallery copy of the same supplier photo match each other, and a translated
	photo can end up as the listing's search-results tile.

	Matching a row that already holds this url is what makes re-delivery a no-op: a
	job queued only to reconcile rows an earlier run produced must not append a
	second copy of every one of them.
	"""
	role = image.get("role")
	if role:
		rows = [row for row in rows if (row.get("role") or "gallery") == role]

	for key in ("source_url", "kind"):
		value = image.get(key)
		if not value:
			continue
		matched = [
			row
			for row in rows
			if row.get(key) == value and (not row.url or row.url == image.get("url"))
		]
		if matched:
			return matched
	return []


def _set_state(sku, status, error, tokens=None):
	"""Image state is written straight to the database, never through the document.

	Loading a fresh doc to save one field would fight with `_apply`'s save, and the
	reviewer's own edits to the listing must not be clobbered by a background job.
	"""
	values = {"image_status": status, "image_error": error}
	if tokens:
		values["image_tokens"] = tokens
	frappe.db.set_value(ENRICHED_DOCTYPE, sku, values, update_modified=False)
	frappe.db.commit()


def _publish(sku, status):
	"""So an open listing form can show the images arriving without polling."""
	frappe.publish_realtime(
		"amazon_listing_images_done",
		{"sku": sku, "status": status},
		doctype=ENRICHED_DOCTYPE,
		docname=sku,
	)


def _first_note(rendered):
	for image in rendered:
		if not image.get("url") and image.get("note"):
			return _summary(image["note"])
	return None


def _summary(text):
	return (text or "").strip()[:500] or None
