# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Unit tests for where a produced image is stored.

The properties pinned here are each a way the image half of this app breaks, and
they are the reason images moved off local disk at all:

**A bench with no bucket keeps writing local Files.** S3 is opt-in, and the switch
is the presence of `S3_BUCKET` — never a default nobody chose. Dev sites and CI
depend on this, and so does the fallback path below.

**A produced image never overwrites another.** Keys carry the caller's filename,
the role's category prefix and a millisecond stamp, so two runs on the same photo
are two objects. Overwriting is how the supplier's original, or an earlier good
result, gets lost.

**An upload that fails does not cost the image.** Transient S3 errors are retried;
a bucket that enforces object ownership takes the same object without an ACL; and
an upload that still cannot land falls back to a local File rather than throwing
away pixels a paid service just produced.

**Reading goes back through the store.** The objects are private, so anything that
hands an image to a third party (AlphaShop, the connector, the reviewer's browser)
gets a presigned link, and anything that wants the bytes reads them with our own
credentials. A url that is not ours passes through untouched — that is what lets
one call site serve supplier photos, local Files and S3 objects alike.

No network and no bucket: the S3 client is a stub that records what it was asked.
"""

import os
import unittest
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from alaiy_os_agent_amazon_listing import image_store
from alaiy_os_agent_amazon_listing.tools import images

BUCKET = "white-background"
REGION = "ap-south-1"
PNG = b"\x89PNG\r\n\x1a\nfake"


def _unset_conf(test, var):
	"""Take a knob out of the site's own config for the length of one test.

	`setting()` reads the environment first and site_config second, so a site that
	happens to configure one of these would otherwise answer for a test that is
	asserting about the environment (or about nothing being configured at all).
	"""
	key = var.lower()
	if key in frappe.conf:
		previous = frappe.conf[key]
		test.addCleanup(frappe.conf.__setitem__, key, previous)
	frappe.conf.pop(key, None)


def _set_conf(test, var, value):
	"""The mirror image: put a knob in site_config for one test."""
	key = var.lower()
	_unset_conf(test, var)
	frappe.conf[key] = value
	test.addCleanup(frappe.conf.pop, key, None)


class FakeClientError(Exception):
	"""Stands in for botocore's ClientError, which is what this code matches on."""

	def __init__(self, code):
		super().__init__(code)
		self.response = {"Error": {"Code": code}}


class FakeBody:
	def __init__(self, content):
		self.content = content

	def read(self):
		return self.content


class FakeS3:
	"""Records every call, and fails the first `fail_times` puts with `fail_code`."""

	def __init__(self, fail_code=None, fail_times=0):
		self.fail_code = fail_code
		self.fail_times = fail_times
		self.puts = []
		self.presigned = []
		self.objects = {}

	def put_object(self, **kwargs):
		self.puts.append(kwargs)
		if self.fail_times:
			self.fail_times -= 1
			raise FakeClientError(self.fail_code)
		self.objects[kwargs["Key"]] = kwargs
		return {}

	def generate_presigned_url(self, op, Params=None, ExpiresIn=None):  # noqa: N803
		self.presigned.append((op, Params, ExpiresIn))
		# Shaped like the real thing — the object's own url with the signature in the
		# query string — because code downstream has to cope with that.
		return (
			f"https://{Params['Bucket']}.s3.{REGION}.amazonaws.com/{Params['Key']}"
			f"?X-Amz-Signature=fake&X-Amz-Expires={ExpiresIn}"
		)

	def get_object(self, Bucket=None, Key=None):  # noqa: N803
		stored = self.objects.get(Key)
		if not stored:
			raise FakeClientError("NoSuchKey")
		return {"Body": FakeBody(stored["Body"]), "ContentType": stored.get("ContentType")}


class StoreTestCase(UnitTestCase):
	"""Configures a bucket for the test and hands every call the same fake client."""

	env = {image_store.BUCKET_VAR: BUCKET, image_store.REGION_VAR: REGION}

	def setUp(self):
		self.s3 = FakeS3()
		patches = [
			patch.dict(os.environ, self.env, clear=False),
			patch.object(image_store, "client", lambda: self.s3),
			# botocore is the only thing that raises the real ClientError; the
			# retry loop has to match the stub's instead.
			patch(
				"botocore.exceptions.ClientError",
				FakeClientError,
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		# site_config must not answer for these while the environment is what is
		# under test (setting() falls through to it), and must be as it was after.
		for var in (image_store.PREFIX_VAR, image_store.ACL_VAR, image_store.PUBLIC_BASE_VAR):
			_unset_conf(self, var)


class NoBucketTestCase(UnitTestCase):
	"""A bench that has not configured S3 at all — the pre-S3 behaviour."""

	def setUp(self):
		p = patch.dict(os.environ, {}, clear=False)
		p.start()
		self.addCleanup(p.stop)
		os.environ.pop(image_store.BUCKET_VAR, None)
		_unset_conf(self, image_store.BUCKET_VAR)


class TestEnablement(NoBucketTestCase):
	def test_no_bucket_means_local_storage(self):
		self.assertFalse(image_store.enabled())
		self.assertIsNone(image_store.upload("x.png", PNG, "image/png"))

	def test_bucket_from_site_config_when_env_is_unset(self):
		"""A bench that configures the app with `bench set-config` works too."""
		_set_conf(self, image_store.BUCKET_VAR, BUCKET)
		self.assertEqual(image_store.bucket(), BUCKET)
		self.assertTrue(image_store.enabled())

	def test_environment_wins_over_site_config(self):
		_set_conf(self, image_store.REGION_VAR, "us-east-1")
		os.environ[image_store.REGION_VAR] = REGION
		self.assertEqual(image_store.region(), REGION)

	def test_region_defaults_to_the_documented_one(self):
		os.environ.pop(image_store.REGION_VAR, None)
		_unset_conf(self, image_store.REGION_VAR)
		self.assertEqual(image_store.region(), image_store.DEFAULT_REGION)


class TestKeys(StoreTestCase):
	def test_role_decides_the_prefix(self):
		"""The white-background main image and a translated gallery photo are filed
		apart, because that is what a reviewer and a lifecycle rule both ask for."""
		self.assertTrue(
			image_store.key_for("listing-main-abcd1234.jpg", image_store.GENERATED).startswith(
				"images/generated/"
			)
		)
		self.assertTrue(
			image_store.key_for("listing-gallery-abcd1234.jpg", image_store.TRANSLATED).startswith(
				"images/translated/"
			)
		)

	def test_key_keeps_the_filename_and_extension(self):
		key = image_store.key_for("listing-main-abcd1234.jpg", image_store.GENERATED)
		self.assertIn("listing-main-abcd1234", key)
		self.assertTrue(key.endswith(".jpg"))

	def test_key_carries_a_timestamp_so_the_same_name_cannot_collide(self):
		with patch.object(image_store, "_stamp", side_effect=["20260811120000001", "20260811120000002"]):
			first = image_store.key_for("same.jpg", image_store.GENERATED)
			second = image_store.key_for("same.jpg", image_store.GENERATED)
		self.assertNotEqual(first, second)
		self.assertEqual(first, "images/generated/2026/08/same-20260811120000001.jpg")

	def test_unknown_category_is_filed_as_generated_rather_than_loose(self):
		self.assertTrue(image_store.key_for("x.jpg", None).startswith("images/generated/"))

	def test_prefix_is_configurable(self):
		with patch.dict(os.environ, {image_store.PREFIX_VAR: "amazon/pics/"}, clear=False):
			self.assertTrue(image_store.key_for("x.jpg", image_store.TRANSLATED).startswith("amazon/pics/translated/"))


class TestUpload(StoreTestCase):
	def test_upload_sends_content_type_acl_and_metadata(self):
		url = image_store.upload(
			"listing-main-abcd1234.png",
			PNG,
			"image/png",
			category=image_store.GENERATED,
			metadata={"sku": "SKU-1", "role": "main"},
		)
		self.assertEqual(len(self.s3.puts), 1)
		put = self.s3.puts[0]
		self.assertEqual(put["Bucket"], BUCKET)
		self.assertEqual(put["Body"], PNG)
		self.assertEqual(put["ContentType"], "image/png")
		self.assertEqual(put["ACL"], "private")
		self.assertEqual(put["Metadata"]["sku"], "SKU-1")
		self.assertEqual(put["Metadata"]["role"], "main")
		self.assertEqual(put["Metadata"]["original-filename"], "listing-main-abcd1234.png")
		self.assertEqual(url, f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{put['Key']}")

	def test_non_ascii_metadata_is_dropped_not_fatal(self):
		"""S3 returns metadata as headers, so a Chinese supplier filename in there
		would fail the upload rather than the value."""
		image_store.upload("x.png", PNG, "image/png", metadata={"source-url": "https://cdn/毛巾.jpg"})
		self.assertNotIn("source-url", self.s3.puts[0]["Metadata"])

	def test_transient_failure_is_retried(self):
		self.s3 = FakeS3(fail_code="SlowDown", fail_times=2)
		url = image_store.upload("x.png", PNG, "image/png")
		self.assertEqual(len(self.s3.puts), 3)
		self.assertTrue(url)

	def test_upload_gives_up_after_max_attempts_and_returns_none(self):
		self.s3 = FakeS3(fail_code="SlowDown", fail_times=99)
		with patch.dict(os.environ, {image_store.ATTEMPTS_VAR: "2"}, clear=False):
			with patch.object(frappe, "log_error") as logged:
				self.assertIsNone(image_store.upload("x.png", PNG, "image/png"))
		self.assertEqual(len(self.s3.puts), 2)
		self.assertTrue(logged.called, "a bench silently losing images is the failure to avoid")

	def test_a_permanent_error_is_not_retried(self):
		self.s3 = FakeS3(fail_code="AccessDenied", fail_times=99)
		with patch.object(frappe, "log_error"):
			self.assertIsNone(image_store.upload("x.png", PNG, "image/png"))
		self.assertEqual(len(self.s3.puts), 1)

	def test_bucket_that_enforces_ownership_takes_the_object_without_an_acl(self):
		self.s3 = FakeS3(fail_code="AccessControlListNotSupported", fail_times=1)
		url = image_store.upload("x.png", PNG, "image/png")
		self.assertTrue(url)
		self.assertEqual(len(self.s3.puts), 2)
		self.assertIn("ACL", self.s3.puts[0])
		self.assertNotIn("ACL", self.s3.puts[1])

	def test_acl_can_be_switched_off(self):
		"""`none`, not an empty string: an unset variable already reads as empty, so
		empty cannot mean "deliberately no ACL"."""
		with patch.dict(os.environ, {image_store.ACL_VAR: image_store.NO_ACL}, clear=False):
			image_store.upload("x.png", PNG, "image/png")
		self.assertNotIn("ACL", self.s3.puts[0])

	def test_an_empty_acl_setting_still_means_private(self):
		with patch.dict(os.environ, {image_store.ACL_VAR: ""}, clear=False):
			image_store.upload("x.png", PNG, "image/png")
		self.assertEqual(self.s3.puts[0]["ACL"], "private")


class TestRetrieval(StoreTestCase):
	def setUp(self):
		super().setUp()
		self.url = image_store.upload("listing-main-abcd1234.png", PNG, "image/png")

	def test_a_stored_url_is_recognised_and_maps_back_to_its_key(self):
		self.assertTrue(image_store.is_stored_url(self.url))
		self.assertEqual(image_store.key_from_url(self.url), self.s3.puts[0]["Key"])

	def test_someone_elses_url_is_not_ours(self):
		for url in ("https://cdn.supplier.com/a.jpg", "/files/a.jpg", f"https://other.s3.{REGION}.amazonaws.com/a.jpg"):
			self.assertFalse(image_store.is_stored_url(url), url)
			self.assertIsNone(image_store.key_from_url(url), url)

	def test_presigned_url_signs_our_object_for_the_configured_window(self):
		with patch.dict(os.environ, {image_store.EXPIRY_VAR: "600"}, clear=False):
			signed = image_store.presigned_url(self.url)
		self.assertEqual(self.s3.presigned[0][1], {"Bucket": BUCKET, "Key": self.s3.puts[0]["Key"]})
		self.assertEqual(self.s3.presigned[0][2], 600)
		self.assertNotEqual(signed, self.url)

	def test_an_already_signed_url_resolves_to_the_same_key(self):
		"""Signing is idempotent in the only way that matters: the signature must not
		become part of the key."""
		signed = image_store.presigned_url(self.url)
		self.assertEqual(image_store.key_from_url(signed), self.s3.puts[0]["Key"])

	def test_presigned_url_leaves_a_foreign_url_alone(self):
		"""So one call site can hand any image url to it — that is the whole point."""
		self.assertEqual(image_store.presigned_url("https://cdn.supplier.com/a.jpg"), "https://cdn.supplier.com/a.jpg")
		self.assertFalse(self.s3.presigned)

	def test_a_failed_presign_returns_the_stored_url_rather_than_raising(self):
		with patch.object(self.s3, "generate_presigned_url", side_effect=FakeClientError("AccessDenied")):
			with patch.object(frappe, "log_error"):
				self.assertEqual(image_store.presigned_url(self.url), self.url)

	def test_download_reads_the_object_back(self):
		self.assertEqual(image_store.download(self.url), (PNG, "image/png"))

	def test_download_declines_a_url_that_is_not_ours(self):
		self.assertIsNone(image_store.download("https://cdn.supplier.com/a.jpg"))


class TestCdnBase(StoreTestCase):
	"""With a CDN in front of the bucket the urls are already public, so nothing is
	signed and nothing expires — which is what the connector handing Amazon an image
	url needs."""

	env = dict(StoreTestCase.env, IMAGE_S3_PUBLIC_BASE_URL="https://cdn.example.com/img")

	def test_stored_url_is_the_cdn_url(self):
		url = image_store.upload("x.png", PNG, "image/png", category=image_store.TRANSLATED)
		self.assertEqual(url, f"https://cdn.example.com/img/{self.s3.puts[0]['Key']}")
		self.assertTrue(image_store.is_stored_url(url))

	def test_a_cdn_url_is_never_signed(self):
		url = image_store.upload("x.png", PNG, "image/png")
		self.assertEqual(image_store.presigned_url(url), url)
		self.assertFalse(self.s3.presigned)

	def test_bytes_still_come_from_the_bucket(self):
		url = image_store.upload("x.png", PNG, "image/png")
		self.assertEqual(image_store.download(url), (PNG, "image/png"))


class TestImagesSeam(StoreTestCase):
	"""What the image tools actually call. They must not know which backend answered."""

	def test_save_public_image_stores_in_s3_and_returns_the_object_url(self):
		with patch("frappe.utils.file_manager.save_file") as save_file:
			url = images.save_public_image(
				"listing-main", PNG, "image/png", category=image_store.GENERATED, metadata={"sku": "SKU-1"}
			)
		self.assertFalse(save_file.called, "a configured bucket must not also write local disk")
		self.assertEqual(url, image_store.object_url(self.s3.puts[0]["Key"]))
		self.assertTrue(self.s3.puts[0]["Key"].startswith("images/generated/"))
		self.assertTrue(self.s3.puts[0]["Key"].endswith(".png"))

	def test_a_refused_upload_falls_back_to_a_local_file(self):
		"""The pixels cost real money; keeping them locally beats losing them."""
		self.s3 = FakeS3(fail_code="AccessDenied", fail_times=99)
		with patch("frappe.utils.file_manager.save_file") as save_file:
			save_file.return_value = frappe._dict(file_url="/files/listing-main-abcd1234.png")
			with patch.object(frappe, "log_error"):
				url = images.save_public_image("listing-main", PNG, "image/png")
		self.assertEqual(url, "/files/listing-main-abcd1234.png")

	def test_public_image_url_signs_our_object_and_passes_others_through(self):
		stored = image_store.upload("x.png", PNG, "image/png")
		self.assertNotEqual(images.public_image_url(stored), stored)
		self.assertEqual(images.public_image_url("https://cdn.supplier.com/a.jpg"), "https://cdn.supplier.com/a.jpg")

	def test_vision_block_for_a_stored_image_reads_it_with_our_own_credentials(self):
		import base64

		stored = image_store.upload("x.png", PNG, "image/png")
		block = images.image_block_from_url(stored)
		self.assertEqual(block["source"]["media_type"], "image/png")
		self.assertEqual(base64.b64decode(block["source"]["data"]), PNG)

	def test_an_unreadable_stored_image_is_none_rather_than_an_exception(self):
		"""A vision block is an optimisation for the model, never a reason to fail a
		run."""
		missing = image_store.object_url("images/generated/2026/08/gone.png")
		self.assertIsNone(images.stored_image_block(missing))


class TestLocalFallback(NoBucketTestCase):
	def test_without_a_bucket_save_public_image_writes_a_file_as_before(self):
		with patch("frappe.utils.file_manager.save_file") as save_file:
			save_file.return_value = frappe._dict(file_url="/files/listing-main-abcd1234.png")
			url = images.save_public_image("listing-main", PNG, "image/png")
		self.assertEqual(url, "/files/listing-main-abcd1234.png")
		self.assertTrue(save_file.called)

	def test_without_a_bucket_nothing_is_treated_as_stored(self):
		url = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/images/generated/x.png"
		self.assertFalse(image_store.is_stored_url(url))
		self.assertEqual(images.public_image_url(url), url)


if __name__ == "__main__":
	unittest.main()
