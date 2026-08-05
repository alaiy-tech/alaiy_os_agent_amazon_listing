# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The `prepare_product_images` tool: the variant's own photo as a white-background
main image, and the family's photos translated behind it.

This is the ONLY image step this agent has, and it is deliberately one step rather
than two toggles, because Amazon does not treat the two roles the same way:

  • MAIN   — what a shopper sees in search results. Amazon requires the product on a
             pure white background with nothing else in the shot, so the main image
             goes through background extraction (AlphaShop `extract_object` with
             transparent=False), not translation.
  • GALLERY — the remaining family photos, in order. Supplier photos from Alibaba
             carry Chinese text baked into the pixels, so these go through
             translation (AlphaShop `translate_image`).

Which photo is the main one is decided in code, not by the model, and not by
whichever row the connector happened to flag: it is **the variant's own photo**, so a
shopper who picked "black" sees the black one first. See handlers.resolve_image_plan
for where that comes from and what it falls back to.

Both provider calls go through Alaiy OS core's `ai_client` seam, so this app holds no
AlphaShop credential — on a managed bench they are served by the billing service.

  ⚠ The seam currently implements `translate_image` but NOT background extraction.
    Until core grows it (see `_EXTRACT_CAP`), a site reports `extract: False` and the
    main image degrades to a translated image with a note and a `needs_review` entry,
    rather than silently shipping a main image that Amazon will suppress.

Each processed photo is stored as its own public File, so the supplier's original is
never overwritten and a bad result is always recoverable.

Two halves, split across two stages. `prepare_product_images` runs inside the agent's
run: it decides whether anything happens at all, resolves the plan, and queues it. The
actual work is `render_prepared`, which runs later on the image queue — see
image_stage.py for why.
"""

from concurrent.futures import ThreadPoolExecutor

import frappe
from alaiy_os.engine import llm

from alaiy_os_agent_amazon_listing import image_stage
from alaiy_os_agent_amazon_listing.tools import handlers as base
from alaiy_os_agent_amazon_listing.tools import images

# How many photos go through the service at once. These are paid third-party APIs,
# and firing every photo of every product in a batch at them simultaneously is a good
# way to get throttled.
_RENDER_CONCURRENCY = 4

# The seam capability each role needs. "translate" exists today; "extract" is the
# white-background op and is not implemented in alaiy_os yet — see the module
# docstring.
_TRANSLATE_CAP = "translate"
_EXTRACT_CAP = "extract"

# The two roles, and the `kind` recorded for each so a reviewer can see at a glance
# what was done to a photo.
MAIN = "main"
GALLERY = "gallery"
KIND_BY_ROLE = {MAIN: "white-background", GALLERY: "translated"}

# What stage one puts on an image row that stage two has not produced yet. It is read
# by a human on the Desk form, so it says what is happening, not "queued".
_QUEUED_NOTE = "Being processed in the background; the image will appear here when ready."

# What goes on a photo an earlier run already processed. Also read by a human, so it
# explains why this one has a url when its siblings do not.
_REUSED_NOTE = "Processed on an earlier run; reused rather than paid for again."

# What goes on a main image that had to be translated instead of background-extracted.
_NO_EXTRACT_NOTE = (
	"Background extraction is not available on this site, so this main image was "
	"only translated. Amazon requires the main image on a plain white background — "
	"replace it before publishing."
)


def prepare_product_images(sku=None, image_urls=None, prepare_images=False, **_retired):
	"""
	Queue a listing's photos for Amazon and return immediately. Returns
	{"images": [{role, kind, source_url, url, note}, ...]} — copy that list verbatim
	into the final `images` array, `role` included.

	**The first entry is always the main image**, and it is the variant's own photo;
	the rest are the family gallery in order. That ordering is the whole point of this
	tool and it is settled here, in code — the model neither chooses it nor is trusted
	to preserve it, because the plan travels with the queued job and stage two writes
	the rows from the plan rather than from what the model reported.

	`url` comes back null: the photos are processed after this run finishes, by
	image_stage.run_step, and attached to the listing then. That is by design — the
	image services take minutes, and holding the run open for them would block a
	worker that could be enriching other products. The work itself is
	render_prepared() below.

	A URL shared by two roles is paid for once per operation. A photo used as the main
	image AND as a gallery image is genuinely two different results (white background
	vs translated), so it is processed once for each.

	Whether we do anything at all is decided HERE, deterministically — not left to the
	model's judgement — by three gates:

	  1. There must be at least one photo. For a sku run those come from
	     handlers.resolve_image_plan (the variant's own photo plus the family
	     gallery); for a URL-only product they are image_urls, first one as main. No
	     photos → empty list, nothing done. This gate is airtight: it holds regardless
	     of what the model passes.
	  2. Processing is opt-in per request: prepare_images must be true. When photos
	     exist but the toggle is off, we return empty too.
	  3. A photo already processed for that same role on an earlier run is never
	     processed again — its existing result is returned as-is. Per photo and role,
	     not per product, so a listing that gained a photo only pays for the new one. A
	     photo that FAILED has no url and so is not "already processed": it is retried.

	Per-image failures degrade rather than raise: that entry comes back with url=None
	and a note, and the remaining photos still process.
	"""
	# ── Gate 1 (airtight): resolve the plan; no photos → nothing to do.
	if sku and frappe.db.exists(base.LISTING_DOCTYPE, sku):
		plan = base.resolve_image_plan(frappe.get_doc(base.LISTING_DOCTYPE, sku))
	else:
		urls = [u for u in (image_urls or []) if u]
		plan = base.image_plan(main=urls[0] if urls else None, gallery=urls[1:])

	if not plan["targets"]:
		return {
			"images": [],
			"note": (
				"This listing has no photos, so nothing was prepared — this agent only "
				"ever processes an existing photograph, and never creates product "
				"imagery from scratch."
			),
		}

	# ── Gate 2: image processing is opt-in per request.
	if not prepare_images:
		return {
			"images": [],
			"note": (
				"The listing has photos, but the prepare_images toggle is off, so no "
				"images were processed."
			),
		}

	# Checked here, while the model is still listening, rather than leaving it to
	# discover a misconfigured site minutes later in the background. Only the
	# capability is checked, not a credential — the credential lives off-bench.
	support = llm.image_client().image_support()
	if not support.get(_TRANSLATE_CAP):
		frappe.throw(
			"Image preparation is not available on this site (the active AI client "
			"cannot translate images). Do NOT retry; return each image with url=null "
			"so the team can prepare them manually."
		)

	# A URL-only product has no Amazon Enriched Listing for stage two to patch, so
	# there is nowhere to deliver the results later — process inline.
	if not sku:
		return {"images": render_prepared(None, {"targets": plan["targets"]})["images"]}

	# ── Gate 3: never pay for the same photo/role pair twice.
	done = _already_prepared(sku)
	targets = [dict(t, url=done.get((t["source_url"], t["role"]))) for t in plan["targets"]]

	# Queued EVEN WHEN everything was processed on an earlier run, because writing
	# those rows back onto the listing is the job's other half, and reconciling them
	# costs nothing when there is nothing left to render.
	image_stage.queue_step(sku, image_stage.PREPARE, {"targets": targets})

	# The same plan, for the model to report. Every entry is returned — including the
	# ones reused from an earlier run — because save_listing rebuilds the image table
	# from what this run reports; an entry left out here would disappear from the
	# listing the model returns, even though stage two will still deliver it.
	result = {
		"images": [
			{
				"role": t["role"],
				"kind": KIND_BY_ROLE[t["role"]],
				"source_url": t["source_url"],
				"url": t["url"],
				"note": _REUSED_NOTE if t["url"] else _QUEUED_NOTE,
			}
			for t in targets
		]
	}

	notes = [
		"The FIRST entry is the main image and the rest are the gallery, in order. "
		"Copy them verbatim, in this order, with each entry's `role` — the ordering "
		"is the listing's image order and you must not rearrange it."
	]
	todo = sum(1 for t in targets if not t["url"])
	if todo:
		notes.append(
			f"{todo} photo(s) queued. They are being processed in the background and "
			"will be attached to this listing when they are ready — this is normal and "
			"is NOT a failure. Leave url as null and do NOT record them in needs_review."
		)
	reused = sum(1 for t in targets if t["url"])
	if reused:
		notes.append(
			f"{reused} entr(ies) were processed on an earlier run and are reused as-is, "
			"with their existing url. Copy them verbatim too."
		)
	if plan.get("main_fallback"):
		# The rule is that the shopper sees the exact variant they picked first. When
		# there is no dedicated variant photo we still produce a main image, but the
		# admin has to be told it is the family's generic shot.
		notes.append(
			"This variant has no dedicated variant image, so the family's first photo "
			"was used as the main image. Add 'Main image (no variant photo)' to "
			"needs_review and say so in notes."
		)
	if not support.get(_EXTRACT_CAP):
		notes.append(
			"Background extraction is unavailable on this site, so the main image will "
			"be translated rather than placed on a white background. Add 'Main image "
			"background' to needs_review and say so in notes."
		)
	result["note"] = " ".join(notes)

	return result


def _already_prepared(sku):
	"""{(source_url, role): url} for photos a previous run already processed.

	Keyed on the ROLE as well as the photo: the same supplier photo used as the main
	image and as a gallery image is two different results, and reusing one for the
	other would put a translated photo on the search-results tile.

	A photo that failed has no url, so it is absent from this map and gets another
	attempt — which is the retry behaviour we want without a flag for it.
	"""
	if not frappe.db.exists(base.ENRICHED_DOCTYPE, sku):
		return {}

	rows = frappe.get_all(
		"Amazon Enriched Listing Image",
		filters={"parent": sku, "parenttype": base.ENRICHED_DOCTYPE},
		fields=["source_url", "role", "url"],
	)
	return {
		(row.source_url, row.role or GALLERY): row.url
		for row in rows
		if row.source_url and row.url
	}


def render_prepared(sku, work):
	"""Process the queued photos. Stage two's worker — see image_stage.py.

	`work["targets"]` is the ordered plan: {role, source_url, url-already-known}, main
	first. Returns {"images": [{role, kind, source_url, url, note}, ...],
	"image_tokens": int} in that same order, so image_stage._apply can write the rows
	back in listing order. A photo that failed comes back with url=None and a note, so
	one bad photo costs one photo rather than the whole listing.

	The photos go through concurrently. Each is an independent call to a service that
	fetches, rewrites and returns an image — slow, and slow in parallel just as well.
	"""
	targets = work.get("targets") or []
	todo = [t for t in targets if not t.get("url")]

	# Only a job with something left to do needs the service. A job queued purely to
	# write an earlier run's results back onto the listing must not fail because the
	# capability has since gone away.
	client = llm.image_client() if todo else None
	support = client.image_support() if client else {}
	can_extract = bool(support.get(_EXTRACT_CAP))

	# Resolved on THIS thread, before the pool starts: expanding a local File path
	# reads site config and the Frappe hook registry, neither of which works inside a
	# worker thread. The client instance is thread-safe by contract once built.
	jobs = [(t, images.public_image_url(t["source_url"])) for t in todo]

	results = []
	if jobs:
		with ThreadPoolExecutor(max_workers=min(_RENDER_CONCURRENCY, len(jobs))) as pool:
			results = list(
				pool.map(lambda j: _process(client, j[0]["role"], j[1], can_extract), jobs)
			)

	fresh = {}
	total_tokens = 0
	for (target, _public), (payload, error) in zip(jobs, results, strict=True):
		key = (target["source_url"], target["role"])
		if error:
			# Logged here rather than in the worker thread: frappe.log_error needs the
			# request context that only this thread has.
			frappe.log_error(
				title="Amazon listing: image preparation failed",
				message=f"{sku} / {target['role']} / {target['source_url']}\n{error}",
			)
			fresh[key] = {"url": None, "note": f"Image preparation failed: {error}"[:200]}
			continue

		out_bytes, out_media_type, tokens, degraded = payload
		total_tokens += tokens
		# Saving writes a File row, so it stays on this thread too.
		fresh[key] = {
			"url": images.save_public_image(
				f"listing-{target['role']}", out_bytes, out_media_type, default_ext=".jpg"
			),
			"note": _NO_EXTRACT_NOTE if degraded else None,
		}

	out = []
	for target in targets:
		key = (target["source_url"], target["role"])
		if key in fresh:
			produced = fresh[key]
		else:
			# Nothing was owed for this photo: an earlier run already processed it,
			# and the url came along in the plan.
			produced = {
				"url": target.get("url"),
				"note": _REUSED_NOTE if target.get("url") else None,
			}
		out.append({
			"role": target["role"],
			"kind": KIND_BY_ROLE[target["role"]],
			"source_url": target["source_url"],
			**produced,
		})

	return {"images": out, "image_tokens": total_tokens}


def _process(client, role, public_url, can_extract):
	"""One photo, in a worker thread. Returns (payload, error) — never raises.

	payload is (bytes, media_type, tokens, degraded). `degraded` is True when a main
	image had to be translated because the site cannot extract backgrounds.

	Nothing in here touches Frappe, which has no context in a worker thread. The
	client was built on the calling thread and is thread-safe by contract. Re-hosting
	the bytes is the caller's job.
	"""
	try:
		if role == MAIN and can_extract:
			# Amazon's main image must be the product alone on pure white — hence
			# transparent=False, which returns the white-matted version rather than
			# an alpha cutout.
			result = client.extract_object(public_url, transparent=False)
			return _fetch(result, tokens=0, degraded=False), None

		# Gallery photos, and a main image on a site that cannot extract, get the
		# text translated instead. The provider fetches the image itself, so it is
		# handed an absolute URL.
		result = client.translate_image(public_url)
		return _fetch(result, tokens=0, degraded=(role == MAIN)), None
	except Exception as exc:
		return None, str(exc)


def _fetch(result, tokens, degraded):
	"""Re-host a provider result: their URL is theirs and may expire, and we want the
	reviewed listing to keep working regardless."""
	url = result.get("translated_url") or result.get("url") or result.get("image_url")
	content, media_type = images.fetch_image_bytes(url)
	return content, media_type, tokens, degraded
