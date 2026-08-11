# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The Amazon listing agent: its prompt, its schema, its tools — and how a customer
app overrides the prompt.

There is ONE Amazon listing agent per site, `amazon_listing`. This app defines all
of it. A customer app changes it by dropping a single markdown file at:

    <customer_app>/agents/amazon_listing.md

Whatever is in that file is appended to the vanilla prompt below, so it says what is
true of that seller: who they are, their house style, their brand name, and their
marketplaces and categories. No registration, no hook, no config — the file being
there is the whole mechanism.

It may start with optional frontmatter, for the two things a prompt cannot express:

    ---
    model: claude-opus-4-8
    description: Shown in the Agents hub.
    ---
    Everything from here down is appended to the vanilla prompt.

All tools are registered on every site, and an override cannot add or remove one.
What it changes is the copy: the brand name, the house voice, and any category rules
the vanilla prompt cannot know.
"""

import json
from pathlib import Path

import frappe

_APP = "alaiy_os_agent_amazon_listing"
_APP_DIR = Path(__file__).resolve().parent

# Where a customer app puts its override, relative to its own package directory.
OVERRIDE_PATH = ("agents", "amazon_listing.md")

# Frontmatter keys we honour. Anything else in there is a typo, so say so.
OVERRIDE_KEYS = ("model", "description")


def read_text(relpath):
	"""Read a file relative to THIS app's package directory."""
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


# The one Amazon listing agent per site: the OS Agent Registry primary key, and what
# you pass as `agent` to alaiy_os.api.agents.run_agent.
AGENT_ID = "amazon_listing"
AGENT_NAME = "Amazon Listing"
AGENT_ICON = "sparkles"

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_MAX_TURNS = 8
DEFAULT_DESCRIPTION = (
	"Generates a structured, Amazon-ready product listing from raw listing data — "
	"title, bullet points, description and backend search keywords — for admin "
	"review."
)

BASE_PROMPT = read_text("prompts/system.md")
BASE_SCHEMA = json.loads(read_text("schemas/output.json"))

_HANDLERS = f"{_APP}.tools.handlers"
_IMAGE = f"{_APP}.tools.image_prepare"


# ── the tools ─────────────────────────────────────────────────────────────────
# Every tool the listing agent has, keyed by tool_id.
#
# There is exactly ONE image tool, and it is not optional in the way the Shopify
# agent's pair is. Amazon treats the main image and the gallery differently — the
# main tile must be the variant's own photo on a plain white background, the gallery
# is the family's photos with their printed text translated — so "which image step"
# is not a per-customer judgement call and is not left to the prompt. Offering a
# free-form retouch step beside it would let a run produce a main image Amazon
# suppresses, so no such step is registered.
#
# An entry may carry `input_option`, the per-request toggle the desk surfaces render
# for it (see api.py).
#
# It is a function, not a constant, only because save_listing's `listing` argument is
# the output schema itself.


def tool_catalog(output_schema):
	return {
		"get_product": {
			"description": (
				"Fetch a product's Amazon Product Listing by its seller SKU, returning the "
				"listing's current data — title, ASIN, marketplace, listing status, "
				"condition, price, quantity, description, existing bullet points and "
				"search keywords, its Amazon product type where Amazon has already "
				"classified it (write the copy for that category), and "
				"Amazon's own open issues (suppression reasons "
				"and warnings) — together with its product photos as images you can "
				"look at. ALWAYS call this first when the input contains a sku, read "
				"the `issues` before writing anything (they are why a listing is "
				"suppressed), and study the photos: they are the primary evidence for "
				"material, colour, pattern, construction, what is in the box, and any "
				"spec text printed onto the image."
			),
			"handler": f"{_HANDLERS}.get_product",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"sku": {
						"type": "string",
						"description": "The seller SKU to enrich; also the name of its Amazon Product Listing.",
					},
				},
				"required": ["sku"],
			},
		},
		"get_reference_values": {
			"description": (
				"Return existing catalog vocabulary already in use on this seller "
				"account — the backend search keywords already applied to other "
				"listings, and the Amazon marketplaces sold in. Call this before "
				"finalising the keywords so your output stays consistent with existing "
				"listings instead of inventing new variants of the same term, and so "
				"you write the copy in the right language and spelling for the "
				"marketplace."
			),
			"handler": f"{_HANDLERS}.get_reference_values",
			"parameters_schema": {"type": "object", "properties": {}},
		},
		"view_image": {
			"description": (
				"Fetch an external image URL (e.g. an `image_url` given in the input) "
				"and show it to you as an actual image, not just a string. ALWAYS call "
				"this before writing anything if your only product evidence is a URL "
				"rather than a sku — you cannot accurately describe or enrich a "
				"product you have never looked at."
			),
			"handler": f"{_HANDLERS}.view_image",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"image_url": {
						"type": "string",
						"description": "The image URL to fetch and look at.",
					},
				},
				"required": ["image_url"],
			},
		},
		"prepare_product_images": {
			"description": (
				"Prepare this listing's photos for Amazon. It produces TWO kinds of "
				"image in one call: the MAIN image — this variant's own photo, placed "
				"on a plain white background, which is what a shopper sees in search "
				"results — followed by the GALLERY, the family's remaining photos with "
				"their printed foreign-language text translated into English. Call it "
				"ONCE for the whole listing. You do NOT choose which photo is the main "
				"one, you do NOT describe the imagery you want, and you do NOT reorder "
				"the result: the tool resolves the variant's own photo itself and "
				"returns the entries in the exact order they must appear on the "
				"listing. Pass `sku` (for a sku run — the tool reads the listing's own "
				"photos and the variant's) and `prepare_images` copied verbatim from "
				"the input (default false). For a URL-only product, pass the photo URLs "
				"as `image_urls` instead, first one treated as the main image. Whether "
				"anything happens is decided by the tool, NOT by you: it runs ONLY when "
				"the listing has photos AND prepare_images is true. Returns {images: "
				"[{role, kind, source_url, url, note}, ...]}; copy that list into the "
				"final `images` array VERBATIM, in the same order, INCLUDING each "
				"entry's `role`. EXPECT url TO BE null: the photos are processed in the "
				"background after this run finishes and are attached to the listing "
				"then. That is success, not failure — do NOT retry the tool, do NOT "
				"call it a second time, do NOT list the images in needs_review, and do "
				"NOT describe them as missing or failed anywhere in your output. Some "
				"entries MAY come back with a real url: those were processed by an "
				"earlier run and reused instead of being paid for twice. A mix of real "
				"urls and nulls in one result is normal. If the tool's note says the "
				"variant had no dedicated photo, or that the main image could not be "
				"placed on a white background, follow that note — those DO belong in "
				"needs_review. If it returns an empty list with a note (no photos, or "
				"the toggle is off), that is also expected — set images to [] and "
				"record the note."
			),
			"handler": f"{_IMAGE}.prepare_product_images",
			"input_option": {
				"fieldname": "prepare_images",
				"label": "Prepare images for Amazon",
				"description": (
					"Puts this variant's own photo on a white background as the main "
					"image and translates the rest of the gallery. Costs money per "
					"image, and only works for photos reachable from the public "
					"internet."
				),
				"default": 0,
			},
			"parameters_schema": {
				"type": "object",
				"properties": {
					"sku": {
						"type": "string",
						"description": (
							"The seller SKU being enriched (= its Amazon Product Listing name). "
							"The tool reads that listing's photos and resolves the "
							"variant's own photo itself; if there are no photos, nothing "
							"is done."
						),
					},
					"prepare_images": {
						"type": "boolean",
						"description": (
							"The per-request opt-in toggle, copied verbatim from the "
							"input (default false). Images are processed only when this "
							"is true AND the listing has photos."
						),
					},
					"image_urls": {
						"type": "array",
						"items": {"type": "string"},
						"description": (
							"Only for a URL-only product with no sku: the photo URLs to "
							"prepare, the first treated as the main image. Ignored when "
							"sku resolves to a listing with photos."
						),
					},
				},
			},
		},
		"save_listing": {
			"description": (
				"Persist the finished listing into the Amazon Enriched Listing DocType "
				"for admin review. Call this ONCE as your FINAL action, after you have "
				"assembled the complete listing (including any images), passing the "
				"`sku` and the exact same object you are about to return as `listing`. "
				"It also settles the listing's Amazon product type, classifying it from "
				"the title you wrote — which is why it runs after the copy is finished "
				"and not before. "
				"It upserts by sku — re-running on the same listing updates its row "
				"rather than creating a duplicate — and lands the row in 'Needs "
				"Review' status. After it returns, reply with the listing JSON as "
				"usual. Skip this tool ONLY when there is no sku (a URL-only input), "
				"since the record is keyed to the listing's SKU."
			),
			"handler": f"{_HANDLERS}.save_listing",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"sku": {
						"type": "string",
						"description": "The seller SKU this listing is for (upsert key).",
					},
					"listing": output_schema,
				},
				"required": ["sku", "listing"],
			},
		},
	}

# ── the customer override ─────────────────────────────────────────────────────


def find_override():
	"""
	The installed app that overrides the listing agent, and its markdown file.

	Discovery is just "does the file exist", so a customer app needs no hook and no
	Python. Returns (app, Path) or (None, None).

	Two apps overriding one agent is a mistake worth shouting about: their prompts
	would silently concatenate in installed-app order.
	"""
	found = []
	for app in frappe.get_installed_apps():
		if app == _APP:
			continue
		path = Path(frappe.get_app_path(app, *OVERRIDE_PATH))
		if path.exists():
			found.append((app, path))

	if len(found) > 1:
		frappe.throw(
			"More than one app overrides the Amazon listing agent: "
			f"{[app for app, _ in found]}. A site has one Amazon listing agent, so "
			"leave only the customer app whose seller account this site is."
		)
	return found[0] if found else (None, None)


def parse_override(text):
	"""
	Split an override file into (frontmatter dict, prompt body).

	Frontmatter is optional, `key: value` per line between two `---` lines. Kept
	deliberately dumb — it exists only for `model` and `description`, everything else
	belongs in the prompt itself.
	"""
	meta, body = {}, text

	if text.lstrip().startswith("---"):
		stripped = text.lstrip()
		end = stripped.find("\n---", 3)
		if end != -1:
			block = stripped[3:end]
			body = stripped[end + 4:].lstrip("-").lstrip("\n")
			for line in block.strip().splitlines():
				line = line.strip()
				if not line or line.startswith("#"):
					continue
				key, _, value = line.partition(":")
				key = key.strip()
				if key not in OVERRIDE_KEYS:
					frappe.throw(
						f"Unknown key '{key}' in an agents/amazon_listing.md "
						f"frontmatter. Supported: {', '.join(OVERRIDE_KEYS)}."
					)
				meta[key] = value.strip()

	return meta, body.strip()


def build_agent_meta():
	"""
	The registration manifest setup/install.py upserts into alaiy_os's OS Agent
	Registry (and its OS Agent Tool child rows): the vanilla agent, with this site's
	override appended to its prompt.

	Credentials are NOT part of this, and this app holds none. Everything — the
	agent's text turns and both image tools — goes through Alaiy OS core's
	`ai_client` seam, so whichever client is installed supplies the credential:
	the managed client routes text via the LiteLLM gateway and images via the
	billing service, and a BYOK bench uses its own site_config keys.
	"""
	app, path = find_override()
	meta, body = parse_override(path.read_text(encoding="utf-8")) if path else ({}, "")

	prompt = f"{BASE_PROMPT.rstrip()}\n\n{body}\n" if body else BASE_PROMPT
	tools = [dict(spec, tool_id=tool_id, connector=None)
	         for tool_id, spec in tool_catalog(BASE_SCHEMA).items()]

	return {
		"agent_id": AGENT_ID,
		"agent_name": AGENT_NAME,
		"description": meta.get("description") or DEFAULT_DESCRIPTION,
		"icon": AGENT_ICON,
		# Reached through this app's run-amazon-agent desk page and the Agents hub.
		"page": None,
		# No settings DocType: the agent stores no credentials.
		"settings_doctype": None,
		"model": meta.get("model") or DEFAULT_MODEL,
		"max_turns": DEFAULT_MAX_TURNS,
		"system_prompt": prompt,
		"output_format": "JSON",
		"output_schema": BASE_SCHEMA,
		"tools": tools,
		# A consequence of the tools, not a separate declaration.
		"input_options": [t["input_option"] for t in tools if t.get("input_option")],
		# Not registry fields; useful to whoever is debugging why a prompt looks the
		# way it does.
		"override_app": app,
	}
