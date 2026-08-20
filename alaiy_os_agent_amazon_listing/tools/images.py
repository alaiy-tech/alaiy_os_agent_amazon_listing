# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Image plumbing shared by the agent's two image tools.

Nothing here is a tool: these are the primitives the image tools are built out
of. What counts as "the image step" differs per seller — one retouches the supplier's
own photographs so they pass Amazon's main-image standards, another translates the
text printed on them — so both are registered and the prompt picks. What is
genuinely common is everything around them: turning a photo into something the
model can see, and re-hosting a result so it survives.

The provider CALLS are no longer part of that split. Both image steps now go
through Alaiy OS core's `ai_client` seam, so no app on the bench holds a provider
credential or speaks a provider's wire format — the managed client serves both
via the billing service. What stays here is the half the seam cannot do: reading
an image the site already holds, and storing a result so it survives.

Where a produced image is stored is `image_store`'s decision, not this module's: S3
when the bench configures a bucket, a local Frappe File when it does not. This module
is where that choice is made once, so the tools keep calling `save_public_image` and
`public_image_url` and neither knows which backend answered.

Names here are public (no leading underscore) because the tool modules import them.
"""

import base64
import os

import frappe

from alaiy_os_agent_amazon_listing import image_store

# Anthropic vision accepts JPEG, PNG, GIF, WEBP.
MEDIA_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".gif": "image/gif",
	".webp": "image/webp",
}
MEDIA_TYPES_BY_MIME = {v: k for k, v in MEDIA_TYPES.items()}

# Some product-photo CDNs block requests with no browser-like User-Agent
# (confirmed: Anthropic's own url-source fetch got refused on one such CDN) — so
# we always fetch external images ourselves rather than passing a bare URL for
# the model to fetch.
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AlaiyOS-AmazonListing/1.0)"}


def media_type(path_or_name):
	"""Guess an image media type from a filename or URL extension, or None."""
	ext = os.path.splitext(path_or_name or "")[1].lower()
	return MEDIA_TYPES.get(ext)


def image_block_from_file(file_name):
	"""Build a base64 Anthropic image block from a File docname, or None."""
	mime = media_type(file_name)
	try:
		file_doc = frappe.get_doc("File", file_name)
		mime = mime or media_type(file_doc.file_name or file_doc.file_url)
		if not mime:
			return None
		content = file_doc.get_content()  # bytes for a binary/image file
		if isinstance(content, str):
			content = content.encode("utf-8", "ignore")
		return {
			"type": "image",
			"source": {
				"type": "base64",
				"media_type": mime,
				"data": base64.b64encode(content).decode("ascii"),
			},
		}
	except Exception:
		return None


def fetch_image_bytes(image_url):
	"""Download an external image URL ourselves. Returns (bytes, media_type)."""
	import requests

	resp = requests.get(image_url, timeout=30, headers=FETCH_HEADERS)
	resp.raise_for_status()
	mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
	if not mime or not mime.startswith("image/"):
		mime = media_type(image_url) or "image/jpeg"
	return resp.content, mime


def fetch_image_block(image_url):
	"""Build a base64 Anthropic image block from an external image URL."""
	content, mime = fetch_image_bytes(image_url)
	return {
		"type": "image",
		"source": {
			"type": "base64",
			"media_type": mime,
			"data": base64.b64encode(content).decode("ascii"),
		},
	}


def local_file_bytes(url):
	"""Read a site-relative File url ('/files/x.jpg') as (bytes, media_type).

	Raises if there is no File behind the url. Used by the S3 migration, which has
	nothing to fall back on if an image it was told about is not there.
	"""
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if not file_name:
		frappe.throw(f"No File found for '{url}'.")
	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8", "ignore")
	return content, media_type(file_doc.file_name or file_doc.file_url) or "image/jpeg"


def stored_image_block(url):
	"""A vision block for an image this app produced and stored in S3, or None.

	Read with our own credentials rather than through a presigned URL: the bytes are
	wanted here, and a signed link we would immediately fetch ourselves is a round
	trip and an expiry window for nothing.
	"""
	if not image_store.is_stored_url(url):
		return None
	try:
		content, mime = image_store.download(url)
	except Exception:
		return None
	mime = mime or media_type(url) or "image/jpeg"
	return {
		"type": "image",
		"source": {
			"type": "base64",
			"media_type": mime,
			"data": base64.b64encode(content).decode("ascii"),
		},
	}


def image_block_from_url(url):
	"""
	Build a vision block from an image URL stored on a listing row: read an S3
	object of ours directly, resolve a local /files or /private/files URL to its
	File doc, otherwise fetch an external http(s) URL. Returns None if it cannot
	be read.
	"""
	if not url:
		return None
	block = stored_image_block(url)
	if block:
		return block
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if file_name:
		return image_block_from_file(file_name)
	if url.startswith("http"):
		try:
			return fetch_image_block(url)
		except Exception:
			return None
	return None


def reference_source(url):
	"""
	An Anthropic-style image `source` (base64) for a reference photo, resolving
	both a stored Frappe File url (e.g. '/files/x.jpg', which is not
	HTTP-fetchable on its own) by reading the File directly, and an external
	http(s) url by downloading it. Used to ground an image call in the real
	product photo.
	"""
	block = stored_image_block(url)
	if block:
		return block["source"]
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if file_name:
		block = image_block_from_file(file_name)
		if block:
			return block["source"]
	return fetch_image_block(url)["source"]


def reference_data_uri(url):
	"""`reference_source` as a data: URI, the form most image APIs want."""
	source = reference_source(url)
	return f"data:{source['media_type']};base64,{source['data']}"


def public_image_url(url):
	"""
	An absolute URL a third-party service can fetch for itself.

	An image of ours in S3 is private, so it comes back as a presigned URL valid for
	`IMAGE_S3_URL_EXPIRY` — that is what lets AlphaShop re-process a photo an earlier
	run produced, and what the connector hands Amazon at publish time.

	Supplier CDN photos are already absolute and pass straight through. A photo
	stored as a local Frappe File is only a site-relative path ('/files/x.jpg'),
	so we expand it against the site URL; that only actually resolves when the
	site is reachable from the public internet, which is why a local/dev site
	will fail for any service that fetches the image itself.
	"""
	if image_store.is_stored_url(url):
		return image_store.presigned_url(url)
	if url.startswith("http://") or url.startswith("https://"):
		return url
	return frappe.utils.get_url(url)


def save_public_image(prefix, content, mime, default_ext=".png", category=None, metadata=None):
	"""
	Store image bytes where this bench keeps produced images, and return the url to
	record on the listing row.

	S3 when a bucket is configured — durable, shared between instances, and something
	a CDN can sit in front of. Otherwise, and if S3 refuses the object after its
	retries, a standalone public Frappe File, which is what this app did before S3 and
	what a dev site or CI still wants.

	Standalone (attached to no doctype) on purpose: an image a run produced shows
	up in that run's own output instead of mutating the product it came from, and
	the original photo is never overwritten — so a bad result is always
	recoverable. The same holds for the S3 object: a new key per result, never an
	overwrite of the supplier's photo.
	"""
	from frappe.utils.file_manager import save_file

	ext = MEDIA_TYPES_BY_MIME.get(mime, default_ext)
	file_name = f"{prefix}-{frappe.generate_hash(length=8)}{ext}"

	stored = image_store.upload(file_name, content, mime, category=category, metadata=metadata)
	if stored:
		return stored

	return save_file(file_name, content, None, None, is_private=0).file_url
