# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The `translate_product_images` tool: supplier photos with their printed foreign-
language text rendered into English.

One of the two image steps this app ships. It does not retouch photos — that is
`generate_product_images`, a deliberately different capability. Both are always
registered; the agent's prompt is what decides which it calls — see agent_meta.py.

It matters more on Amazon than almost anywhere else: a photo carrying untranslated
supplier text is one of the things Amazon suppresses a listing for, and it is
invisible to any amount of work on the copy.

The translation itself goes through Alaiy OS core's `ai_client` seam
(`llm.translate_image`), the same seam the agent's text turns use. This app holds
no vendor credential: on a managed bench the call is served by the billing
service, which owns the key and meters the spend. A BYOK bench has no translation
provider and the tool reports that rather than half-working.

Each translated photo is stored as its own public File, so the original supplier
photo is never overwritten and a bad translation is always recoverable.

Two halves, split across two stages. `translate_product_images` runs inside the
agent's run: it decides whether translation happens at all, and queues it. The
actual work is `render_translated`, which runs later on the image queue — see
image_stage.py for why.
"""

from concurrent.futures import ThreadPoolExecutor

import frappe
from alaiy_os.engine import llm

from alaiy_os_agent_amazon_listing import image_stage
from alaiy_os_agent_amazon_listing.tools import handlers as base
from alaiy_os_agent_amazon_listing.tools import images

# Every photo a listing has is translated. There is deliberately no per-product cap:
# one untranslated photo in the carousel is enough to have the listing flagged, which
# is worse than the cost of translating it. Spend is bounded by gate 3 instead (a
# photo is never translated twice) and by the toggle being off by default.

# How many photos go through the service at once — see render_translated. This is a
# paid third-party API, and firing every photo of every product in a batch at it
# simultaneously is a good way to get throttled.
_RENDER_CONCURRENCY = 4

# What stage one puts on an image row that stage two has not produced yet. It is
# read by a human on the Desk form, so it says what is happening, not "queued".
_QUEUED_NOTE = "Being translated in the background; the image will appear here when ready."

# What goes on a photo an earlier run already translated. Also read by a human, so
# it explains why this one has a url when its siblings do not.
_REUSED_NOTE = "Translated on an earlier run; reused rather than translated again."


def translate_product_images(sku=None, image_urls=None, translate_images=False):
	"""
	Queue a listing's supplier photos to have their printed foreign-language text
	rendered into English, and return immediately. Returns
	{"images": [{source_url, url, note}, ...]} — copy that list verbatim into the
	final `images` array.

	`url` comes back null: the photos are translated after this run finishes, by
	image_stage.run_step, and attached to the listing then. That is by design — the
	translation service takes minutes, and holding the run open for it would block
	a worker that could be enriching other products. The work itself is
	render_translated() below, which goes through core's `ai_client` seam
	(llm.translate_image) and re-hosts each result.

	EVERY photo on the listing is translated, under the one toggle, with no
	per-product cap. A URL repeated in the images table is paid for once.

	The plan travels with the queued job: stage two writes the rows from that plan,
	not from the `images` array the model returns. So a translated photo reaches the
	listing even if the model drops the entry on the way out. The entries returned
	below are the same plan, for the model to report — its copy of the truth, not the
	truth itself.

	Nothing is written to the Amazon Listing before an admin approves the enriched
	listing (see AmazonEnrichedListing._sync_images).

	The exception is a URL-only product: it has no listing record for stage two to
	deliver into, so its photos are translated inline and come back with real urls.

	Whether we translate at all is decided HERE, deterministically — not left to the
	model's judgement — by three gates:

	  1. There must be at least one photo to translate. For a sku run those are the
	     listing's own photos (read from the Amazon Listing's images table); for a
	     URL-only product they are image_urls. No photos → empty list, nothing done.
	     This gate is airtight: it holds regardless of what the model passes.
	  2. Translation is opt-in per request: translate_images must be true. When
	     photos exist but the toggle is off, we return empty too.
	  3. A photo already translated on an earlier run is never translated again —
	     its existing result is returned as-is. This is per photo, not per product,
	     so a listing that gained a photo only pays for the new one. A photo that
	     FAILED has no url and so is not "already translated": it is retried.

	Per-image failures degrade rather than raise: that entry comes back with
	url=None and a note, and the remaining photos still process. If the service
	isn't configured at all (and both gates pass), the model is told via the thrown
	message not to retry and to fall back to null placeholders instead of stalling
	the rest of the listing.
	"""
	# ── Gate 1 (airtight): resolve the photos; no photos → nothing to do.
	targets = []
	if sku and frappe.db.exists(base.LISTING_DOCTYPE, sku):
		listing = frappe.get_doc(base.LISTING_DOCTYPE, sku)
		targets = [{"source_url": url} for url in base.listing_image_urls(listing)]
	if not targets and image_urls:
		targets = [{"source_url": u} for u in image_urls if u]
	if not targets:
		return {
			"images": [],
			"note": "This listing has no photos, so nothing was translated.",
		}

	# ── Gate 2: image translation is opt-in per request.
	if not translate_images:
		return {
			"images": [],
			"note": (
				"The listing has photos, but the translate_images toggle is off, so "
				"no images were translated."
			),
		}

	# Checked while the model is still listening, rather than leaving it to
	# discover a misconfigured site minutes later in the background. Only the
	# capability is checked, not a credential — the credential lives off-bench now.
	if not llm.image_client().image_support().get("translate"):
		frappe.throw(
			"Image translation is not available on this site (the active AI client "
			"cannot translate images). Do NOT retry; return each image with url=null "
			"so the team can translate it manually."
		)

	# A URL-only product has no Amazon Enriched Listing for stage two to patch, so
	# there is nowhere to deliver the results later — translate inline.
	if not sku:
		urls = [t["source_url"] for t in targets]
		return {"images": render_translated(None, {"urls": urls})["images"]}

	# ── Gate 3: never pay to translate the same photo twice.
	done = _already_translated(sku)
	todo = []
	for target in targets:
		url = target["source_url"]
		if url not in done and url not in todo:
			todo.append(url)

	# `targets` is the plan stage two delivers against. It is queued EVEN WHEN `todo`
	# is empty (everything was translated on an earlier run), because writing those
	# rows back onto the listing is the job's other half, and reconciling them costs
	# nothing when there is nothing left to translate.
	plan = [dict(target, url=done.get(target["source_url"])) for target in targets]
	image_stage.queue_step(sku, image_stage.TRANSLATE, {"urls": todo, "targets": plan})

	# The same plan, for the model to report. Every entry is returned — including the
	# ones reused from an earlier run — because save_listing rebuilds the image table
	# from what this run reports; a translation left out here would disappear from the
	# listing the model returns, even though stage two will still deliver it.
	result = {
		"images": [
			{
				"source_url": entry["source_url"],
				"url": entry["url"],
				"note": _REUSED_NOTE if entry["url"] else _QUEUED_NOTE,
			}
			for entry in plan
		]
	}

	notes = []
	if todo:
		notes.append(
			f"{len(todo)} photo(s) queued for translation. They are being processed in "
			"the background and will be attached to this listing when they are ready — "
			"this is normal and is NOT a failure. Copy these entries verbatim, leave "
			"url as null, and do NOT record them in needs_review."
		)
	reused = sum(1 for entry in plan if entry["url"])
	if reused:
		notes.append(
			f"{reused} entr(ies) were translated on an earlier run and are reused "
			"as-is, with their existing url. Copy them verbatim too."
		)
	if notes:
		result["note"] = " ".join(notes)

	return result


def _already_translated(sku):
	"""{source_url: url} for photos a previous run already translated.

	"Already done" means the listing holds a translated image for that exact source
	photo. A photo that failed has no url, so it is absent from this map and gets
	another attempt — which is the retry behaviour we want without a flag for it.
	"""
	if not frappe.db.exists(base.ENRICHED_DOCTYPE, sku):
		return {}

	rows = frappe.get_all(
		"Amazon Enriched Listing Image",
		filters={"parent": sku, "parenttype": base.ENRICHED_DOCTYPE},
		fields=["source_url", "url"],
	)
	return {row.source_url: row.url for row in rows if row.source_url and row.url}


def render_translated(sku, work):
	"""Translate the queued photos. Stage two's worker — see image_stage.py.

	`work` holds the photos to translate (`urls`) and, for a sku run, the plan they
	were queued for (`targets`: every photo as {source_url, url-already-known}).

	Returns {"images": [{source_url, url, note}, ...], "image_tokens": 0}. A photo
	that failed comes back with url=None and a note, so one bad photo costs one photo
	rather than the whole listing.

	The photos go through concurrently. Each is an independent call to a service
	that fetches, rewrites and returns an image — slow, and slow in parallel just
	as well.
	"""
	urls = work.get("urls") or []
	targets = work.get("targets")

	# Only a job with something left to translate needs the service. A job queued
	# purely to write an earlier run's results back onto the listing must not fail
	# because the capability has since gone away.
	client = llm.image_client() if urls else None
	if urls and not client.image_support().get("translate"):
		frappe.throw("Image translation is not available on this site.")

	# Both resolved on THIS thread, before the pool starts: the client reads site
	# config and the Frappe hook registry, and expanding a local File path needs
	# the site context. Neither works inside a worker thread — the client instance
	# is thread-safe by contract once built.
	sources = [(url, images.public_image_url(url)) for url in urls]

	results = []
	if sources:
		with ThreadPoolExecutor(max_workers=min(_RENDER_CONCURRENCY, len(sources))) as pool:
			results = list(pool.map(lambda pair: _try_translate(client, pair[1]), sources))

	fresh = {}
	for (url, _public), (payload, error) in zip(sources, results, strict=True):
		if error:
			# Logged here rather than in the worker thread: frappe.log_error needs
			# the request context that only this thread has.
			frappe.log_error(
				title="Amazon listing: image translation failed",
				message=f"{sku} / {url}\n{error}",
			)
			fresh[url] = {"url": None, "note": f"Translation failed: {error}"[:200]}
			continue

		out_bytes, out_media_type = payload
		# Saving writes a File row, so it stays on this thread too.
		fresh[url] = {
			"url": images.save_public_image(
				"listing-translated", out_bytes, out_media_type, default_ext=".jpg"
			),
			"note": None,
		}

	if targets is None:
		return {
			"images": [{"source_url": url, **fresh[url]} for url, _public in sources],
			"image_tokens": 0,
		}

	out = []
	for target in targets:
		source_url = target["source_url"]
		if source_url in fresh:
			produced = fresh[source_url]
		else:
			# Nothing was owed for this photo: an earlier run already translated it,
			# and the url came along in the plan.
			produced = {"url": target.get("url"), "note": _REUSED_NOTE if target.get("url") else None}
		out.append({"source_url": source_url, **produced})

	return {"images": out, "image_tokens": 0}


def _try_translate(client, public_url):
	"""One photo, in a worker thread. Returns (payload, error) — never raises.

	Both the translate call and the fetch of its result are pure HTTP, so both
	belong here; nothing in this function touches Frappe, which has no context in
	a worker thread. The client was built on the calling thread and is thread-safe
	by contract. Re-hosting the bytes is the caller's job.
	"""
	try:
		# The provider fetches the image itself, so hand it an absolute URL.
		result = client.translate_image(public_url)
		# Re-host the result: the provider's URL is theirs and may expire, and we
		# want the reviewed listing to keep working regardless.
		return images.fetch_image_bytes(result["translated_url"]), None
	except Exception as exc:
		return None, str(exc)
