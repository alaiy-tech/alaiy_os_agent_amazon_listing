# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Where a produced image lives: S3, when the bench configures a bucket.

The images this agent produces — the white-background main image and the translated
gallery — used to be written to the site's own disk as Frappe Files. That made the
bench stateful in the one place it should not be: an image is produced on whichever
worker picked the job up, is read back minutes later by a reviewer and again at
approval time by the connector, and is expected to still be there after a redeploy.
Local disk gives none of that across instances, and nothing to put a CDN in front of.

So a produced image goes to S3 and the listing row stores the object's URL.

Two properties are worth stating because the rest of the module follows from them:

**Objects are private.** The bucket blocks public access, so a stored URL is not
fetchable on its own. Everything that reads an image goes through this module —
`presigned_url()` for the third-party services and the connector that must fetch it
over HTTP, `download()` for our own bytes — and never through a bare GET on the
stored URL. `IMAGE_S3_URL_EXPIRY` is what bounds the presigned window.

**S3 is opt-in and never fatal.** A bench with no `S3_BUCKET` keeps writing local
Files exactly as before, which is what dev sites and CI want. And a configured bucket
that refuses an upload after its retries falls back to a local File rather than
throwing away an image that cost real money to produce — loudly, in the error log,
because a site silently drifting back to local disk is the failure this whole module
exists to remove.

Configuration is read from the environment first, then from `site_config.json` under
the same names, lowercased (`s3_bucket`, `image_s3_region`, …). Credentials follow the
same rule but are *optional*: name `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
(/ `AWS_SESSION_TOKEN`) and they are handed to the client explicitly; leave them out —
the better choice — and an instance role, a profile or the ambient environment is
resolved by boto3's own chain. Either way nothing about them lands in this repo. See
the README for the full table.
"""

import os
import threading

import frappe

# The knobs, and what a bench gets when it does not set them. `S3_BUCKET` has no
# default on purpose: its presence IS the switch, so a site can never be surprised
# into uploading somewhere by a default nobody chose. The README documents the bucket
# to name here.
BUCKET_VAR = "S3_BUCKET"
REGION_VAR = "IMAGE_S3_REGION"
DEFAULT_REGION = "ap-south-1"

# Optional, in descending order of how often you will touch them.
PREFIX_VAR = "IMAGE_S3_PREFIX"
DEFAULT_PREFIX = "images/"
EXPIRY_VAR = "IMAGE_S3_URL_EXPIRY"
DEFAULT_EXPIRY = 7 * 24 * 3600  # SigV4's ceiling, and the shape of a review cycle.
ACL_VAR = "IMAGE_S3_ACL"
DEFAULT_ACL = "private"
NO_ACL = "none"  # IMAGE_S3_ACL=none sends no ACL at all; see `upload`.
ENDPOINT_VAR = "IMAGE_S3_ENDPOINT_URL"  # MinIO and friends; unset means real S3.
ATTEMPTS_VAR = "IMAGE_S3_MAX_ATTEMPTS"
DEFAULT_ATTEMPTS = 3

# Credentials. Normally you do NOT set these: boto3 finds an instance role, a profile
# or the standard environment on its own, and a role beats a stored secret every time.
# They exist because a Frappe bench is the awkward case — the upload happens on a
# supervisor-managed worker, which inherits neither your login shell's environment nor
# reliably your HOME, so "it works in the console" and "it works in a background job"
# can differ. Naming them here, under the same env-then-site_config rule as everything
# else, is the one place both processes are guaranteed to read the same thing.
ACCESS_KEY_VAR = "AWS_ACCESS_KEY_ID"
SECRET_KEY_VAR = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN_VAR = "AWS_SESSION_TOKEN"

# A CDN (or any read-only public origin) in front of the bucket. Set it and stored
# urls become `<base>/<key>`: already fetchable, so nothing is presigned and nothing
# expires — which is what you want once Amazon itself is pulling these images, since a
# presigned link that lapses before the connector submits costs the listing its photo.
# Leave it unset and objects stay private, reachable only through `presigned_url`.
PUBLIC_BASE_VAR = "IMAGE_S3_PUBLIC_BASE_URL"

# The two kinds of image this agent produces, and the key prefix each lands under. A
# reviewer and a lifecycle rule both want to tell them apart without opening them:
# `generated` is the composited white-background main image, `translated` is a
# supplier photo with its text rewritten.
GENERATED = "generated"
TRANSLATED = "translated"
CATEGORIES = (GENERATED, TRANSLATED)

# Errors worth trying again: throttling, a 5xx from S3, a dropped connection. Matched
# on the error code rather than the exception class because botocore reports all of
# these as one ClientError.
_RETRYABLE_CODES = {
	"InternalError",
	"RequestTimeout",
	"RequestTimeTooSkewed",
	"ServiceUnavailable",
	"SlowDown",
	"ThrottlingException",
	"Throttling",
	"503",
	"500",
}

# A bucket with Object Ownership set to "bucket owner enforced" — the default for
# buckets created since 2023 — rejects any request that carries an ACL at all. That is
# not a misconfiguration to fail on: such a bucket is private by construction, which
# is what the ACL was asking for. So the ACL is dropped and the upload retried once.
_ACL_UNSUPPORTED_CODES = {"AccessControlListNotSupported", "InvalidBucketAclWithObjectOwnership"}

# boto3 clients are documented as safe to *use* from several threads but not to
# build, and stage two renders photos on a pool. One client per (bucket, region,
# endpoint), built under a lock, reused after that.
_clients = {}
_clients_lock = threading.Lock()


def setting(name, default=None):
	"""One configuration value: environment first, then site_config, then the default.

	Environment wins because that is how a container is configured and how the issue
	that introduced S3 specified these; `site_config.json` is accepted under the same
	name lowercased so a bench can set them per site with `bench set-config`.
	"""
	value = os.environ.get(name)
	if value is None or value == "":
		value = frappe.conf.get(name.lower())
	if value is None or value == "":
		return default
	return value


def bucket():
	"""The configured bucket, or None when this bench stores images locally."""
	return setting(BUCKET_VAR)


def region():
	return setting(REGION_VAR, DEFAULT_REGION)


def enabled():
	"""Whether produced images go to S3 at all.

	Bucket configured and boto3 importable. The library is declared as a dependency of
	this app, but a bench that installed the app before that — or that never ran
	`bench setup requirements` — will not have it, and this is exactly how that
	looks: a configured bucket quietly writing local Files. So the missing library is
	said out loud, once, and degrades like any other S3 problem rather than raising
	inside an agent run.
	"""
	if not bucket():
		return False
	try:
		import boto3  # noqa: F401
	except ImportError:
		# Once per request, not once per photo: this is called for every image on
		# every listing, and a wall of identical rows buries the one thing to read.
		if not getattr(frappe.local, "amazon_listing_boto3_warned", False):
			frappe.local.amazon_listing_boto3_warned = True
			frappe.log_error(
				title="Amazon listing images: boto3 missing",
				message=(
					f"{BUCKET_VAR} is set to '{bucket()}' but boto3 is not installed in this "
					"bench's environment, so produced images are being stored as local Files "
					"instead. Install it (`./env/bin/pip install boto3`, or `bench setup "
					"requirements`) and restart."
				),
			)
		return False
	return True


def _int_setting(name, default):
	try:
		return int(setting(name, default))
	except (TypeError, ValueError):
		return default


def expiry():
	"""How long a presigned URL stays valid, in seconds."""
	return _int_setting(EXPIRY_VAR, DEFAULT_EXPIRY)


def max_attempts():
	return max(1, _int_setting(ATTEMPTS_VAR, DEFAULT_ATTEMPTS))


def credentials():
	"""Explicit credentials for the client, or {} to leave it to boto3's own chain.

	Both halves or neither: a key id with no secret is a misconfiguration that would
	otherwise present as an unsigned request, which is a confusing way to find out.
	A session token is only meaningful alongside them, so it rides along.
	"""
	access_key = setting(ACCESS_KEY_VAR)
	secret_key = setting(SECRET_KEY_VAR)
	if not access_key or not secret_key:
		return {}

	values = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key}
	token = setting(SESSION_TOKEN_VAR)
	if token:
		values["aws_session_token"] = token
	return values


def client():
	"""A cached S3 client for the configured bucket."""
	import boto3
	from botocore.config import Config

	endpoint = setting(ENDPOINT_VAR)
	creds = credentials()
	# The cache key covers the credential identity, not the secret: a site that
	# rotates its key must not keep signing with a client built from the old one.
	key = (bucket(), region(), endpoint, creds.get("aws_access_key_id"))
	if key in _clients:
		return _clients[key]

	with _clients_lock:
		if key not in _clients:
			_clients[key] = boto3.client(
				"s3",
				region_name=region(),
				endpoint_url=endpoint,
				# botocore's own retries sit underneath ours: these cover the
				# connection-level failures that never become a ClientError.
				config=Config(retries={"max_attempts": max_attempts(), "mode": "standard"}),
				**creds,
			)
	return _clients[key]


def _stamp():
	"""A sortable timestamp for a key. Its own function so tests can pin it."""
	return frappe.utils.now_datetime().strftime("%Y%m%d%H%M%S%f")[:-3]


def key_for(file_name, category=None):
	"""The object key a produced image goes under.

	`images/<category>/<YYYY>/<MM>/<name>-<timestamp><ext>` — the caller's filename is
	kept (it carries the role and a random hash, and is what a human recognises in the
	console) with a millisecond timestamp appended, so two runs producing the same
	name for the same photo cannot collide and the keys sort by when they were made.
	The date folders exist so a lifecycle rule can talk about age by prefix.
	"""
	prefix = str(setting(PREFIX_VAR, DEFAULT_PREFIX)).strip("/")
	category = category if category in CATEGORIES else GENERATED
	stem, ext = os.path.splitext(file_name or "image")
	stamp = _stamp()
	return f"{prefix}/{category}/{stamp[:4]}/{stamp[4:6]}/{stem}-{stamp}{ext}"


def public_base():
	base = setting(PUBLIC_BASE_VAR)
	return base.rstrip("/") if base else None


def object_url(key):
	"""The canonical URL for a key — what gets stored on the listing row.

	The CDN base when one is configured. Otherwise virtual-hosted style against the
	bucket's own region, so the URL says which bucket and region an image came from and
	stays meaningful if the app is pointed at a second bucket later. That form is not
	fetchable without a signature; `presigned_url` is.
	"""
	base = public_base()
	if base:
		return f"{base}/{key}"
	endpoint = setting(ENDPOINT_VAR)
	if endpoint:
		return f"{endpoint.rstrip('/')}/{bucket()}/{key}"
	return f"https://{bucket()}.s3.{region()}.amazonaws.com/{key}"


def is_stored_url(url):
	"""Whether this URL is an object in the configured bucket."""
	return key_from_url(url) is not None


def key_from_url(url):
	"""The object key inside a URL this module produced, or None."""
	if not url or not bucket():
		return None
	endpoint = setting(ENDPOINT_VAR)
	candidates = [f"https://{bucket()}.s3.{region()}.amazonaws.com/", f"https://{bucket()}.s3.amazonaws.com/"]
	if endpoint:
		candidates.insert(0, f"{endpoint.rstrip('/')}/{bucket()}/")
	if public_base():
		candidates.insert(0, f"{public_base()}/")
	for base in candidates:
		if url.startswith(base):
			# Split the query off, so a url that has already been presigned resolves
			# to the same key rather than to one with a signature glued onto it.
			return url[len(base) :].split("?")[0] or None
	return None


def upload(file_name, content, media_type, category=None, metadata=None):
	"""Store image bytes in the bucket and return the object's URL, or None.

	None means "this bench does not use S3" — the caller then writes a local File.
	Anything else that goes wrong is retried and then logged, and also comes back as
	None, because an image already produced is worth keeping on local disk more than
	the run is worth failing.

	`put_object` rather than a multipart upload: these are single product photos, a
	few hundred KB to a few MB, and one request that either lands or does not is
	easier to reason about than a session to abort. Anything large enough to want
	multipart is not an image this agent made.
	"""
	if not enabled():
		return None

	key = key_for(file_name, category)
	extra = {
		"Bucket": bucket(),
		"Key": key,
		"Body": content,
		"ContentType": media_type or "application/octet-stream",
		"Metadata": _metadata(file_name, category, metadata),
	}
	acl = setting(ACL_VAR, DEFAULT_ACL)
	# `none` is how a bench asks for no ACL header at all — an empty string cannot
	# say it, because an unset variable reads as empty everywhere else. The usual
	# reason is a bucket that enforces object ownership, which `_put_with_retries`
	# already discovers on its own; this is for saying so up front.
	if acl and str(acl).lower() != NO_ACL:
		extra["ACL"] = acl

	started = frappe.utils.now_datetime()
	try:
		_put_with_retries(extra)
	except Exception as exc:
		frappe.log_error(
			title="Amazon listing images: S3 upload failed",
			message=f"{bucket()}/{key} ({len(content or b'')} bytes)\n{exc}",
		)
		return None

	elapsed = (frappe.utils.now_datetime() - started).total_seconds()
	frappe.logger("amazon_listing_images").info(
		f"uploaded s3://{bucket()}/{key} ({len(content or b'')} bytes) in {elapsed:.2f}s"
	)
	return object_url(key)


def _metadata(file_name, category, metadata):
	"""User metadata for the object. S3 sends it back as headers, so values have to be
	ASCII and small; anything else is dropped rather than failing the upload."""
	values = {"original-filename": file_name or "", "category": category or GENERATED}
	for name, value in (metadata or {}).items():
		if value is None:
			continue
		value = str(value)
		if len(value) <= 1024 and value.isascii():
			values[str(name)] = value
	return {k: v for k, v in values.items() if v}


def _put_with_retries(extra):
	"""put_object, retried on the transient codes, with the ACL dropped if the bucket
	does not take one. Raises the last error when every attempt failed."""
	from botocore.exceptions import BotoCoreError, ClientError

	attempts = max_attempts()
	for attempt in range(1, attempts + 1):
		try:
			client().put_object(**extra)
			return
		except ClientError as exc:
			code = str(exc.response.get("Error", {}).get("Code", ""))
			if code in _ACL_UNSUPPORTED_CODES and "ACL" in extra:
				# The bucket enforces ownership, which is the private-by-default this
				# ACL was asking for. Retry the same object without it.
				extra.pop("ACL")
				continue
			if code in _RETRYABLE_CODES and attempt < attempts:
				continue
			raise
		except BotoCoreError:
			# Connection reset, DNS blip, read timeout — no error code to inspect.
			if attempt < attempts:
				continue
			raise


def presigned_url(url, seconds=None):
	"""A time-limited HTTPS URL anyone can GET, for an image stored in the bucket.

	Returns the URL unchanged when it is not one of ours, so callers can hand any
	image url — a supplier CDN photo, a local File path, an S3 object — to the same
	function. That is the whole reason this is shaped as a rewrite rather than a
	fetch: the code paths that need it (the AlphaShop calls, the connector publishing
	to Amazon) already had an "absolute URL a third party can fetch" step.
	"""
	key = key_from_url(url)
	if not key or not enabled():
		return url
	if public_base() and url.startswith(f"{public_base()}/"):
		# Already served publicly, and with no expiry to worry about. Signing it
		# would only add a deadline to a link that does not need one.
		return url
	try:
		return client().generate_presigned_url(
			"get_object",
			Params={"Bucket": bucket(), "Key": key},
			ExpiresIn=seconds or expiry(),
		)
	except Exception as exc:
		frappe.log_error(
			title="Amazon listing images: presign failed",
			message=f"{bucket()}/{key}\n{exc}",
		)
		return url


def download(url):
	"""Read an object back as (bytes, media_type), or None if this is not our URL.

	Used where we need the pixels rather than a link — building a vision block for the
	model out of an image an earlier run produced.
	"""
	key = key_from_url(url)
	if not key or not enabled():
		return None
	obj = client().get_object(Bucket=bucket(), Key=key)
	body = obj["Body"].read()
	return body, (obj.get("ContentType") or "").split(";")[0].strip() or None


def migrate_local_images(dry_run=True, limit=None):
	"""Copy already-produced images off local disk into the bucket.

	The one-off for a bench that ran this app before S3: every `Amazon Enriched
	Listing Image` row whose url is a site-relative File path is uploaded and the row
	repointed at the object. The File itself is left alone — nothing is deleted by a
	migration — so a bad run can be undone by restoring the previous urls.

	Run it from the bench (see the README); it is not a patch, because it costs
	bandwidth per image and a site that never produced images locally needs it never.
	Returns a summary dict.
	"""
	from alaiy_os_agent_amazon_listing.tools import images

	if not enabled():
		frappe.throw(f"{BUCKET_VAR} is not configured, so there is nowhere to migrate images to.")

	rows = frappe.get_all(
		"Amazon Enriched Listing Image",
		filters={"url": ["like", "/%files/%"]},
		fields=["name", "parent", "role", "url", "source_url"],
		limit_page_length=limit or 0,
	)

	summary = {"found": len(rows), "moved": 0, "failed": 0, "dry_run": bool(dry_run)}
	for row in rows:
		if dry_run:
			continue
		try:
			content, mime = images.local_file_bytes(row.url)
		except Exception as exc:
			frappe.log_error(title="Amazon listing images: migration read failed", message=f"{row.url}\n{exc}")
			summary["failed"] += 1
			continue

		category = GENERATED if (row.role or "") == "main" else TRANSLATED
		new_url = upload(
			os.path.basename(row.url),
			content,
			mime,
			category=category,
			metadata={"sku": row.parent, "role": row.role, "migrated-from": row.url},
		)
		if not new_url:
			summary["failed"] += 1
			continue

		frappe.db.set_value("Amazon Enriched Listing Image", row.name, "url", new_url, update_modified=False)
		summary["moved"] += 1

	if not dry_run:
		frappe.db.commit()
	return summary
