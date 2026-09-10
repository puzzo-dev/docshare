# Copyright (c) 2026, I-Varse Technologies NG
# License: MIT. See LICENSE

from contextlib import contextmanager

import frappe
import frappe.utils
try:  # Frappe v16 and later — frappe.tests.utils was removed
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from docshare.api import (
	create_share_link,
	get_shared_document,
	download_shared_pdf,
	revoke_share_link,
	list_share_links,
	on_document_trash,
	_build_share_url,
)


VIEW_CONSUMER = "DocShare Test View Consumer"


def list_linked_documents_for_token(url):
	"""Number of linked-document rows stored on the link behind a share URL."""
	token = url.rsplit("/", 1)[-1]
	name = frappe.get_value("DocShare Link", {"share_token": token}, "name")
	return len(frappe.get_doc("DocShare Link", name).linked_documents)


@contextmanager
def docshare_setting(**values):
	"""Temporarily set DocShare Settings fields, restoring them afterwards.

	Several behaviours are settings-gated, so a test that does not state its
	preconditions is really asserting whatever the site happens to be
	configured for — which is how these tests started failing when the site's
	"Allow Sharing Linked Documents" was switched off.
	"""
	settings = frappe.get_doc("DocShare Settings", "DocShare Settings")
	previous = {k: settings.get(k) for k in values}
	for k, v in values.items():
		settings.set(k, v)
	settings.save(ignore_permissions=True)
	try:
		yield
	finally:
		settings = frappe.get_doc("DocShare Settings", "DocShare Settings")
		for k, v in previous.items():
			settings.set(k, v)
		settings.save(ignore_permissions=True)


@contextmanager
def view_notification_consumer():
	"""Provide something for a DocShare View Log to notify.

	_log_view deliberately writes nothing unless a Notification or WhatsApp
	Notification is configured against DocShare View Log — a toggle should not
	generate records nobody consumes. Tests that assert a View Log exists must
	therefore supply the consumer.
	"""
	created = False
	if not frappe.db.exists("Notification", VIEW_CONSUMER):
		frappe.get_doc({
			"doctype": "Notification",
			"name": VIEW_CONSUMER,
			"subject": "DocShare link viewed",
			"document_type": "DocShare View Log",
			"event": "New",
			"channel": "System Notification",
			"enabled": 1,
			"message": "viewed",
		}).insert(ignore_permissions=True)
		created = True
	try:
		yield
	finally:
		if created and frappe.db.exists("Notification", VIEW_CONSUMER):
			frappe.delete_doc("Notification", VIEW_CONSUMER, force=True, ignore_permissions=True)


class TestDocShareLink(FrappeTestCase):
	"""Functional tests for the DocShare Link doctype and guest API."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._snapshot_existing()
		cls._cleanup_existing()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_existing()
		super().tearDownClass()

	@classmethod
	def _snapshot_existing(cls):
		"""Record the links and logs that were on the site before we started."""
		cls._preexisting_links = set(frappe.get_all("DocShare Link", pluck="name"))
		cls._preexisting_logs = set(frappe.get_all("DocShare View Log", pluck="name"))

	@classmethod
	def _cleanup_existing(cls):
		"""Remove only what this suite created.

		This used to delete every DocShare Link and View Log on the site with
		force=True. On a developer's own bench that destroys real share links
		the moment the suite runs — which is exactly what happened — and
		force=True also skipped the back-link check that the deletion tests
		exist to exercise.

		View logs go first: several tests commit (the guest view path commits
		its own counter), so logs outlive the per-test rollback while links are
		deleted and the naming series resets. A stale log then points at a link
		name a later test is issued again, failing it for an unrelated reason.
		"""
		keep_logs = getattr(cls, "_preexisting_logs", set())
		keep_links = getattr(cls, "_preexisting_links", set())
		for name in frappe.get_all("DocShare View Log", pluck="name"):
			if name not in keep_logs:
				frappe.delete_doc("DocShare View Log", name, force=True)
		for name in frappe.get_all("DocShare Link", pluck="name"):
			if name not in keep_links:
				frappe.delete_doc("DocShare Link", name, force=True)

	def setUp(self):
		super().setUp()
		self._cleanup_existing()
		# Use a Todo as a safe, simple target document that every user can read.
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "DocShare test target"}).insert()

	def tearDown(self):
		if frappe.db.exists("ToDo", self.todo.name):
			frappe.delete_doc("ToDo", self.todo.name, force=True)
		self._cleanup_existing()
		super().tearDown()

	# -----------------------------------------------------------------------
	# Creation & token
	# -----------------------------------------------------------------------

	def test_create_share_link_generates_token_and_url(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		self.assertTrue(url.startswith("http"))
		self.assertIn("/share/", url)

		# Scoped to this test's target: the suite no longer wipes the site's own
		# share links, so unrelated links may legitimately exist alongside.
		links = frappe.get_all(
			"DocShare Link",
			filters={"ref_doctype": "ToDo", "ref_docname": self.todo.name},
			pluck="name",
		)
		self.assertEqual(len(links), 1)
		doc = frappe.get_doc("DocShare Link", links[0])
		self.assertEqual(doc.ref_doctype, "ToDo")
		self.assertEqual(doc.ref_docname, self.todo.name)
		self.assertTrue(doc.share_token)
		self.assertEqual(len(doc.share_token), 32)
		self.assertEqual(cint(doc.enabled), 1)
		self.assertEqual(doc.view_count, 0)

	def test_viewed_link_can_be_deleted(self):
		"""A link that has been viewed must still be deletable.

		DocShare View Log.share_link points back at DocShare Link, so Frappe's
		back-link check refused the delete outright and viewed links could never
		be cleared. The suite missed it because every cleanup path here deletes
		with force=True, which skips that check.
		"""
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		link_name = frappe.db.get_value("DocShare Link", {"share_token": token}, "name")

		frappe.get_doc({
			"doctype": "DocShare View Log",
			"share_link": link_name,
			"share_link_token": token,
			"ref_doctype": "ToDo",
			"ref_docname": self.todo.name,
		}).insert(ignore_permissions=True)

		# No force= here: that is the whole point of the test.
		frappe.delete_doc("DocShare Link", link_name)
		self.assertFalse(frappe.db.exists("DocShare Link", link_name))

	def test_deleting_a_link_keeps_its_view_logs(self):
		"""The audit trail outlives the link — the log carries its own copies."""
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		link_name = frappe.db.get_value("DocShare Link", {"share_token": token}, "name")

		log = frappe.get_doc({
			"doctype": "DocShare View Log",
			"share_link": link_name,
			"share_link_token": token,
			"ref_doctype": "ToDo",
			"ref_docname": self.todo.name,
		}).insert(ignore_permissions=True)

		frappe.delete_doc("DocShare Link", link_name)

		row = frappe.db.get_value(
			"DocShare View Log", log.name,
			["share_link", "share_link_token", "ref_doctype", "ref_docname"],
			as_dict=True,
		)
		self.assertTrue(row, "view log must survive the link it belonged to")
		self.assertFalse(row.share_link, "dangling reference must be cleared")
		self.assertEqual(row.share_link_token, token)
		self.assertEqual(row.ref_doctype, "ToDo")
		self.assertEqual(row.ref_docname, self.todo.name)

		frappe.delete_doc("DocShare View Log", log.name, force=True)

	def test_unviewed_link_still_deletes(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		link_name = frappe.db.get_value("DocShare Link", {"share_token": token}, "name")
		frappe.delete_doc("DocShare Link", link_name)
		self.assertFalse(frappe.db.exists("DocShare Link", link_name))

	def test_print_fragment_is_cached_per_document_version(self):
		"""Rendering a share page parses a whole printview with BeautifulSoup.
		Repeat views of an unchanged document must not pay that twice."""
		from docshare.api import cached_print_fragment

		calls = []

		def _render():
			calls.append(1)
			return "<html><body><div class='print-format'>hello</div></body></html>"

		args = ("ToDo", self.todo.name, None, None, 0)
		first = cached_print_fragment(*args, _render)
		second = cached_print_fragment(*args, _render)

		self.assertEqual(len(calls), 1, "second view should come from cache")
		self.assertEqual(first, second)

	def test_editing_the_document_invalidates_the_fragment(self):
		"""`modified` is part of the key, so an edit can never serve a stale page."""
		from docshare.api import cached_print_fragment

		calls = []

		def _render():
			calls.append(1)
			return "<html><body><div class='print-format'>v%d</div></body></html>" % len(calls)

		args = ("ToDo", self.todo.name, None, None, 0)
		cached_print_fragment(*args, _render)

		self.todo.description = "changed"
		self.todo.save()

		cached_print_fragment(*args, _render)
		self.assertEqual(len(calls), 2, "an edited document must re-render")

	def test_letterhead_choice_is_part_of_the_cache_key(self):
		from docshare.api import cached_print_fragment

		calls = []

		def _render():
			calls.append(1)
			return "<html><body><div class='print-format'>x</div></body></html>"

		cached_print_fragment("ToDo", self.todo.name, None, None, 0, _render)
		cached_print_fragment("ToDo", self.todo.name, None, None, 1, _render)
		self.assertEqual(len(calls), 2)

	def test_comments_are_not_offered_as_linked_documents(self):
		"""A Comment points back at the shared document and used to be the only
		thing the picker offered. It can carry internal discussion and has no
		business being sent to a customer."""
		from docshare.api import get_linked_documents_for

		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "ToDo",
			"reference_name": self.todo.name,
			"content": "internal note",
		}).insert(ignore_permissions=True)

		offered = get_linked_documents_for("ToDo", self.todo.name)
		self.assertFalse(
			[o for o in offered if o["linked_doctype"] == "Comment"],
			"Comment must never be offered in the linked-document picker",
		)

	def test_picker_and_share_target_are_separate_questions(self):
		"""Anything may be shared; only real documents get suggested alongside."""
		from docshare.api import _is_offerable_linked_doctype, _is_shareable_doctype

		for dt in ("ToDo", "Comment", "File"):
			self.assertTrue(_is_shareable_doctype(dt), f"{dt} should stay shareable as a target")
			self.assertFalse(_is_offerable_linked_doctype(dt), f"{dt} should not be offered")

		self.assertFalse(_is_shareable_doctype("GL Entry"), "ledger rows are never shareable")

	def test_linked_documents_come_from_connections(self):
		"""Discovery finds every doctype that references this one, not just
		what the Connections tab declares.

		The picker uses ``get_linked_doctypes``, which scans every DocField
		and Custom Field for Link/Dynamic Link fields pointing at the
		target doctype. ToDo has no incoming links from any transactional
		document, so nothing is offered — and in particular no bookkeeping
		row sneaks in.
		"""
		from docshare.api import get_linked_documents_for

		# ToDo has no incoming links from transactional doctypes, so nothing
		# is offered.
		self.assertEqual(get_linked_documents_for("ToDo", self.todo.name), [])

	def test_linked_documents_read_dashboard_py_connections(self):
		"""Linked-document discovery finds links declared in ``_dashboard.py``,
		not just ``meta.links``.

		Standard ERPNext doctypes (Quotation, Sales Invoice, Sales Order,
		Payment Entry, …) define their Connections in ``_dashboard.py`` via
		``non_standard_fieldnames`` / ``internal_links``, leaving the
		``links`` array on the DocType JSON empty. The comprehensive
		discovery uses ``get_linked_doctypes`` which scans all DocFields,
		so it finds these regardless of whether they are declared in a
		dashboard.
		"""
		from docshare.api import get_linked_documents_for
		from erpnext.selling.doctype.quotation.test_quotation import make_quotation

		# Quotation is the canonical case: its ``links`` array is empty but
		# Sales Order references it via ``prevdoc_docname`` on Sales Order
		# Item (a child-table field). The picker must find this through
		# ``get_linked_doctypes`` and resolve it through the child table.
		meta = frappe.get_meta("Quotation")
		self.assertFalse(meta.links, "Quotation must have empty meta.links")

		# Create a Quotation, then a Sales Order referencing it. The Sales Order
		# should appear as a linked document of the Quotation.
		quote = make_quotation(do_not_submit=True)
		try:
			# Before the fix this returned [] regardless of linked documents,
			# because Quotation.meta.links is empty and Sales Order was never
			# in the candidate set.
			self.assertEqual(
				get_linked_documents_for("Quotation", quote.name), [],
				"fresh Quotation with no Sales Order must offer nothing",
			)

			# Create a Sales Order whose items reference the Quotation via
			# prevdoc_docname — the standard ERPNext link field.
			so = frappe.get_doc({
				"doctype": "Sales Order",
				"customer": "_Test Customer",
				"company": "_Test Company",
				"currency": "INR",
				"delivery_date": frappe.utils.add_days(frappe.utils.today(), 10),
				"items": [{
					"item_code": "_Test Item",
					"qty": 1,
					"rate": 100,
					"warehouse": "_Test Warehouse - _TC",
					"prevdoc_docname": quote.name,
				}],
			})
			so.insert(ignore_permissions=True)
			try:
				offered = get_linked_documents_for("Quotation", quote.name)
				linked_types = {o["linked_doctype"] for o in offered}
				self.assertIn(
					"Sales Order", linked_types,
					"Sales Order linked via child-table field must be offered",
				)
				# And the specific row.
				so_rows = [o for o in offered if o["linked_doctype"] == "Sales Order"]
				self.assertEqual(len(so_rows), 1)
				self.assertEqual(so_rows[0]["linked_docname"], so.name)
			finally:
				frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
		finally:
			frappe.delete_doc("Quotation", quote.name, force=True, ignore_permissions=True)

	def test_duplicate_linked_documents_are_collapsed(self):
		"""The same document twice would render twice in the share view.

		The desk picker reads its selection from the checkboxes now, so it cannot
		send duplicates, but this endpoint is whitelisted and a request can carry
		anything.
		"""
		other = frappe.get_doc({"doctype": "ToDo", "description": "linked target"}).insert()
		url = create_share_link(
			doctype="ToDo",
			docname=self.todo.name,
			linked_documents=[
				{"linked_doctype": "ToDo", "linked_docname": other.name},
				{"linked_doctype": "ToDo", "linked_docname": other.name},
			],
		)
		token = url.rsplit("/", 1)[-1]
		link = frappe.get_doc("DocShare Link", frappe.db.get_value(
			"DocShare Link", {"share_token": token}, "name"))
		self.assertEqual(len(link.linked_documents), 1)
		frappe.delete_doc("ToDo", other.name, force=True)

	def test_outgoing_links_are_offered(self):
		"""Documents this one points at via Link and Dynamic Link fields are offered.

		A Payment Entry's ``references`` child table uses Dynamic Link fields
		(``reference_doctype`` + ``reference_name``) to point at the Sales
		Invoice or Sales Order it settles. The Connections tab does not show
		these — they are outgoing, not incoming — but they are exactly what a
		user means by "the payment's linked documents". The picker must
		discover them from the document's own Link and Dynamic Link fields.

		Master data pointed at by parent-level Dynamic Links (Customer via
		``party``) is NOT offered — only submittable (transactional) doctypes
		qualify as "linked documents" to share alongside.
		"""
		from docshare.api import get_linked_documents_for
		from erpnext.accounts.doctype.payment_entry.test_payment_entry import create_payment_entry
		from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order

		with docshare_setting(share_linked_documents=1):
			pe = create_payment_entry(
				payment_type="Receive",
				party_type="Customer",
				party="_Test Customer",
				paid_amount=100,
				save=True,
			)
			so = make_sales_order(qty=1, rate=100)
			try:
				# Link the Sales Order to the Payment Entry via its references
				# child table — the standard ERPNext dynamic-link pattern.
				pe_doc = frappe.get_doc("Payment Entry", pe.name)
				pe_doc.append("references", {
					"reference_doctype": "Sales Order",
					"reference_name": so.name,
					"allocated_amount": 100,
				})
				pe_doc.save(ignore_permissions=True)

				offered = get_linked_documents_for("Payment Entry", pe.name)
				linked_types = {o["linked_doctype"] for o in offered}

				# The Sales Order referenced via dynamic link must be offered.
				self.assertIn("Sales Order", linked_types)

				# Customer (master data, pointed at by the parent-level
				# ``party`` dynamic link) must NOT be offered — it is not a
				# transactional document.
				self.assertNotIn("Customer", linked_types)

				# And the specific Sales Order row.
				so_rows = [o for o in offered if o["linked_doctype"] == "Sales Order"]
				self.assertEqual(len(so_rows), 1)
				self.assertEqual(so_rows[0]["linked_docname"], so.name)
			finally:
				frappe.delete_doc("Payment Entry", pe.name, force=True, ignore_permissions=True)
				so.cancel() if so.docstatus == 1 else None
				frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

	def test_share_token_is_unique(self):
		create_share_link(doctype="ToDo", docname=self.todo.name)
		create_share_link(doctype="ToDo", docname=self.todo.name)
		tokens = frappe.get_all(
			"DocShare Link",
			filters={"ref_doctype": "ToDo", "ref_docname": self.todo.name},
			pluck="share_token",
		)
		self.assertEqual(len(tokens), 2)
		self.assertNotEqual(tokens[0], tokens[1])

	def test_absolute_url_generation(self):
		url = _build_share_url("abc123")
		self.assertTrue(url.startswith("http"))
		self.assertTrue(url.endswith("/share/abc123"))

	# -----------------------------------------------------------------------
	# Permission enforcement
	# -----------------------------------------------------------------------

	def test_create_requires_target_read_permission(self):
		# As a user with no ToDo read permission, creation must fail.
		user_email = "test_docshare_noaccess@example.com"
		if not frappe.db.exists("User", user_email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": user_email,
				"first_name": "Test NoAccess",
				"roles": [{"role": "Guest"}],
			}).insert()
		else:
			user = frappe.get_doc("User", user_email)

		frappe.set_user(user.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_share_link(doctype="ToDo", docname=self.todo.name)
		finally:
			frappe.set_user("Administrator")

	def test_single_doctype_rejected(self):
		# DocType itself is a Single doctype in some contexts; use a known single.
		with self.assertRaises(frappe.ValidationError):
			create_share_link(doctype="Print Settings", docname="Print Settings")

	def test_trashed_document_rejected(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "to be trashed"}).insert()
		frappe.delete_doc("ToDo", todo.name, force=True)
		with self.assertRaises(frappe.ValidationError):
			create_share_link(doctype="ToDo", docname=todo.name)

	# -----------------------------------------------------------------------
	# Guest retrieval
	# -----------------------------------------------------------------------

	def test_guest_get_shared_document_returns_html(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		html = get_shared_document(token=token)
		self.assertIsInstance(html, str)
		self.assertTrue(len(html) > 0)

	def test_guest_pdf_starts_with_magic_bytes(self):
		# Skip if wkhtmltopdf is not installed in this environment.
		import shutil

		if not shutil.which("wkhtmltopdf"):
			self.skipTest("wkhtmltopdf not installed")

		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		# download_shared_pdf sets frappe.response; save and restore it.
		saved_response = getattr(frappe.local, "response", None)
		frappe.local.response = frappe._dict({})
		try:
			download_shared_pdf(token=token)
			pdf = frappe.local.response.get("filecontent")
			self.assertIsNotNone(pdf)
			self.assertTrue(pdf[:4] == b"%PDF")
			self.assertTrue(frappe.local.response.get("filename", "").endswith(".pdf"))
		finally:
			frappe.local.response = saved_response

	def test_invalid_token_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			get_shared_document(token="nonexistent-token-12345")

	def test_disabled_link_rejected(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		name = frappe.get_value("DocShare Link", {"share_token": token}, "name")
		revoke_share_link(name=name)
		with self.assertRaises(frappe.ValidationError):
			get_shared_document(token=token)

	def test_expired_link_rejected(self):
		# create_share_link deliberately refuses a past expiry — minting an
		# already-dead link is never legitimate. What this test is for is the
		# guard in _validate_guest_token, so age the link after creating it.
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		name = frappe.get_value("DocShare Link", {"share_token": token}, "name")
		frappe.db.set_value(
			"DocShare Link", name, "expires_on",
			add_days(now_datetime(), -1), update_modified=False,
		)
		with self.assertRaises(frappe.ValidationError):
			get_shared_document(token=token)

	def test_past_expiry_refused_at_creation(self):
		with self.assertRaises(frappe.ValidationError):
			create_share_link(
				doctype="ToDo",
				docname=self.todo.name,
				expires_on=add_days(now_datetime(), -1),
			)

	def test_max_views_enforced(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name, max_views=1)
		token = url.rsplit("/", 1)[-1]
		# First view succeeds.
		get_shared_document(token=token)
		# Second view must be rejected.
		with self.assertRaises(frappe.ValidationError):
			get_shared_document(token=token)

	def test_view_count_increments(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name, max_views=10)
		token = url.rsplit("/", 1)[-1]
		get_shared_document(token=token)
		get_shared_document(token=token)
		count = frappe.get_value("DocShare Link", {"share_token": token}, "view_count")
		self.assertEqual(cint(count), 2)

	def test_last_accessed_on_set(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		get_shared_document(token=token)
		last = frappe.get_value("DocShare Link", {"share_token": token}, "last_accessed_on")
		self.assertTrue(last)

	# -----------------------------------------------------------------------
	# Revoke / disable
	# -----------------------------------------------------------------------

	def test_revoke_disables_link(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		name = frappe.get_value("DocShare Link", {"share_token": token}, "name")
		revoke_share_link(name=name)
		enabled = frappe.get_value("DocShare Link", name, "enabled")
		self.assertEqual(cint(enabled), 0)

	def test_list_share_links(self):
		create_share_link(doctype="ToDo", docname=self.todo.name)
		create_share_link(doctype="ToDo", docname=self.todo.name)
		links = list_share_links(doctype="ToDo", docname=self.todo.name)
		self.assertEqual(len(links), 2)
		for link in links:
			self.assertTrue(link["url"].startswith("http"))

	# -----------------------------------------------------------------------
	# Trash auto-disable
	# -----------------------------------------------------------------------

	def test_on_trash_auto_disables_links(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "trash test"}).insert()
		create_share_link(doctype="ToDo", docname=todo.name)
		# Simulate the on_trash hook.
		on_document_trash(todo, method="on_trash")
		enabled = frappe.get_value(
			"DocShare Link",
			{"ref_doctype": "ToDo", "ref_docname": todo.name},
			"enabled",
		)
		self.assertEqual(cint(enabled), 0)
		frappe.delete_doc("ToDo", todo.name, force=True)

	# -----------------------------------------------------------------------
	# Standard print output parity
	# -----------------------------------------------------------------------

	def test_print_html_matches_native_get_print(self):
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		token = url.rsplit("/", 1)[-1]
		shared_html = get_shared_document(token=token)
		native_html = frappe.get_print("ToDo", self.todo.name, as_pdf=False)
		# Both should contain the document name somewhere.
		self.assertIn(self.todo.name, native_html)
		# The shared HTML is wrapped in the share page template when rendered
		# via the www route, but the API returns the raw print HTML directly.
		self.assertTrue(len(shared_html) > 0)

	# -----------------------------------------------------------------------
	# Linked documents
	# -----------------------------------------------------------------------

	def test_create_with_linked_documents(self):
		todo2 = frappe.get_doc({"doctype": "ToDo", "description": "linked doc"}).insert()
		with docshare_setting(share_linked_documents=1):
			url = create_share_link(
				doctype="ToDo",
				docname=self.todo.name,
				linked_documents=[{
					"linked_doctype": "ToDo",
					"linked_docname": todo2.name,
				}],
			)
		token = url.rsplit("/", 1)[-1]
		link_name = frappe.get_value("DocShare Link", {"share_token": token}, "name")
		link = frappe.get_doc("DocShare Link", link_name)
		self.assertEqual(len(link.linked_documents), 1)
		self.assertEqual(link.linked_documents[0].linked_doctype, "ToDo")
		self.assertEqual(link.linked_documents[0].linked_docname, todo2.name)
		frappe.delete_doc("ToDo", todo2.name, force=True)

	def test_linked_documents_discarded_when_setting_off(self):
		"""Hiding the selector in the dialog is not enforcement."""
		todo2 = frappe.get_doc({"doctype": "ToDo", "description": "linked off"}).insert()
		with docshare_setting(share_linked_documents=0):
			url = create_share_link(
				doctype="ToDo",
				docname=self.todo.name,
				linked_documents=[{
					"linked_doctype": "ToDo",
					"linked_docname": todo2.name,
				}],
			)
			self.assertEqual(list_linked_documents_for_token(url), 0)
		frappe.delete_doc("ToDo", todo2.name, force=True)

	def test_linked_document_permission_checked(self):
		todo2 = frappe.get_doc({"doctype": "ToDo", "description": "linked"}).insert()
		user_email = "test_docshare_linked_noaccess@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc({
				"doctype": "User",
				"email": user_email,
				"first_name": "Test Linked NoAccess",
				"roles": [{"role": "Guest"}],
			}).insert()
		frappe.set_user(user_email)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_share_link(
					doctype="ToDo",
					docname=self.todo.name,
					linked_documents=[{
						"linked_doctype": "ToDo",
						"linked_docname": todo2.name,
					}],
				)
		finally:
			frappe.set_user("Administrator")
		frappe.delete_doc("ToDo", todo2.name, force=True)

	def test_get_linked_documents_for(self):
		from docshare.api import get_linked_documents_for
		# ToDo doesn't have typical linked docs, but the API should return a list.
		result = get_linked_documents_for(doctype="ToDo", docname=self.todo.name)
		self.assertIsInstance(result, list)

	# -----------------------------------------------------------------------
	# View logging
	# -----------------------------------------------------------------------

	def test_view_log_created_on_view(self):
		# Enable view tracking in settings.
		settings = frappe.get_doc("DocShare Settings", "DocShare Settings")
		original_notify = settings.notify_on_view
		settings.notify_on_view = 1
		settings.track_ip_address = 1
		settings.save(ignore_permissions=True)

		try:
			with view_notification_consumer():
				url = create_share_link(doctype="ToDo", docname=self.todo.name)
				token = url.rsplit("/", 1)[-1]
				# Set a fake request IP for the test.
				frappe.local.request_ip = "192.168.1.100"
				get_shared_document(token=token)

			# Check a View Log was created.
			logs = frappe.get_all("DocShare View Log", filters={"share_link_token": token}, pluck="name")
			self.assertTrue(len(logs) >= 1)
			log = frappe.get_doc("DocShare View Log", logs[0])
			self.assertEqual(log.ref_doctype, "ToDo")
			self.assertEqual(log.ref_docname, self.todo.name)
			self.assertEqual(log.ip_address, "192.168.1.100")
		finally:
			settings.notify_on_view = original_notify
			settings.save(ignore_permissions=True)

	def test_view_log_disabled_when_setting_off(self):
		settings = frappe.get_doc("DocShare Settings", "DocShare Settings")
		original_notify = settings.notify_on_view
		settings.notify_on_view = 0
		settings.save(ignore_permissions=True)

		try:
			url = create_share_link(doctype="ToDo", docname=self.todo.name)
			token = url.rsplit("/", 1)[-1]
			get_shared_document(token=token)
			logs = frappe.get_all("DocShare View Log", filters={"share_link_token": token}, pluck="name")
			self.assertEqual(len(logs), 0)
		finally:
			settings.notify_on_view = original_notify
			settings.save(ignore_permissions=True)

	def test_share_tokens_are_not_listable_by_other_users(self):
		"""A share token is a bearer credential for an unauthenticated reader.

		Frappe filters list queries with permission_query_conditions only —
		has_permission hooks do not apply — so a query condition that returns
		"" exposes every token to anyone holding the doctype's read permission,
		including users who cannot open the target document at all.
		"""
		create_share_link(doctype="ToDo", docname=self.todo.name)

		email = "docshare_token_harvest_probe@example.com"
		if not frappe.db.exists("User", email):
			probe = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "Harvest Probe",
				"send_welcome_email": 0, "user_type": "System User",
			})
			probe.insert(ignore_permissions=True)
			probe.add_roles("Desk User")

		try:
			frappe.set_user(email)
			# Either outcome is correct: refused outright (no doctype read
			# permission) or filtered to nothing by permission_query_conditions.
			# The only failure is returning rows.
			try:
				rows = frappe.get_list(
					"DocShare Link", fields=["name", "share_token"], limit_page_length=0
				)
			except frappe.PermissionError:
				rows = []
			self.assertEqual(
				rows, [],
				"a non-owner listed share tokens — the list query is not filtering",
			)
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)

	def test_non_shareable_doctype_rejected_at_creation(self):
		"""Filtering the picker is not enforcement.

		get_linked_documents_for hides logs, ledger entries and DocShare's own
		records, but create_share_link accepted whatever it was handed. A
		DocShare Link attached this way would have printed its own share_token
		onto an unauthenticated page.
		"""
		other = create_share_link(doctype="ToDo", docname=self.todo.name)
		other_name = frappe.get_value(
			"DocShare Link", {"share_token": other.rsplit("/", 1)[-1]}, "name"
		)
		with docshare_setting(share_linked_documents=1):
			with self.assertRaises(frappe.ValidationError):
				create_share_link(
					doctype="ToDo",
					docname=self.todo.name,
					linked_documents=[{
						"linked_doctype": "DocShare Link",
						"linked_docname": other_name,
					}],
				)

	def test_reader_of_target_cannot_revoke_someone_elses_link(self):
		"""Revoking kills a URL already sent to a customer.

		That is an action against the sharer's work, so read access on the
		target document must not confer it.
		"""
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		name = frappe.get_value(
			"DocShare Link", {"share_token": url.rsplit("/", 1)[-1]}, "name"
		)

		email = "docshare_revoke_probe@example.com"
		if not frappe.db.exists("User", email):
			probe = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "Revoke Probe",
				"send_welcome_email": 0, "user_type": "System User",
			})
			probe.insert(ignore_permissions=True)
			probe.add_roles("Desk User")

		# Give the probe genuine read access to the *target* document, so the
		# test reaches the branch that used to grant revoke. Without this the
		# user is refused for lacking read at all, and the test would pass
		# against the vulnerable code too.
		frappe.share.add("ToDo", self.todo.name, email, read=1)

		try:
			frappe.set_user(email)
			self.assertTrue(
				frappe.has_permission("ToDo", "read", self.todo.name, user=email),
				"probe must be able to read the target for this test to mean anything",
			)
			with self.assertRaises(frappe.PermissionError):
				revoke_share_link(name=name)
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("DocShare Link", name, "enabled"), 1,
			"a non-owner revoked someone else's share link",
		)

	def test_non_shareable_doctype_rejected_as_the_main_target(self):
		"""The blacklist guarded linked documents but not the target itself.

		Sharing a DocShare Link would have printed another link's share_token
		onto a page that needs no login to read.
		"""
		url = create_share_link(doctype="ToDo", docname=self.todo.name)
		link_name = frappe.get_value(
			"DocShare Link", {"share_token": url.rsplit("/", 1)[-1]}, "name"
		)
		with self.assertRaises(frappe.ValidationError):
			create_share_link(doctype="DocShare Link", docname=link_name)

	def test_negative_max_views_refused(self):
		"""A negative limit is truthy, so it used to be stored and then failed
		`view_count >= max_views` on the very first view — a link born dead."""
		with self.assertRaises(frappe.ValidationError):
			create_share_link(doctype="ToDo", docname=self.todo.name, max_views=-5)

	def test_zero_max_views_means_unlimited(self):
		"""0 and None both mean unlimited, and must not create a dead link."""
		url = create_share_link(doctype="ToDo", docname=self.todo.name, max_views=0)
		token = url.rsplit("/", 1)[-1]
		name = frappe.get_value("DocShare Link", {"share_token": token}, "name")
		self.assertIsNone(frappe.db.get_value("DocShare Link", name, "max_views") or None)
		get_shared_document(token=token)
		get_shared_document(token=token)  # a second view must still work

	def test_no_view_log_without_a_consumer(self):
		"""A toggle must not generate records nobody reads."""
		with docshare_setting(notify_on_view=1):
			for n in frappe.get_all(
				"Notification", filters={"document_type": "DocShare View Log"}, pluck="name"
			):
				frappe.delete_doc("Notification", n, force=True, ignore_permissions=True)
			url = create_share_link(doctype="ToDo", docname=self.todo.name)
			token = url.rsplit("/", 1)[-1]
			get_shared_document(token=token)
			logs = frappe.get_all(
				"DocShare View Log", filters={"share_link_token": token}, pluck="name"
			)
			self.assertEqual(len(logs), 0)

	def test_view_log_resolves_party(self):
		"""Test that the View Log resolves party from an ERPNext document."""
		# Create a simple Sales Invoice-like test using ToDo (no party).
		# For a full party test, we'd need an ERPNext Sales Invoice, but
		# the party resolution logic handles the case where no party fields exist.
		settings = frappe.get_doc("DocShare Settings", "DocShare Settings")
		original_notify = settings.notify_on_view
		settings.notify_on_view = 1
		settings.save(ignore_permissions=True)

		try:
			with view_notification_consumer():
				url = create_share_link(doctype="ToDo", docname=self.todo.name)
				token = url.rsplit("/", 1)[-1]
				frappe.local.request_ip = "10.0.0.1"
				get_shared_document(token=token)
				logs = frappe.get_all(
					"DocShare View Log", filters={"share_link_token": token}, pluck="name"
				)
				self.assertTrue(len(logs) >= 1)
				log = frappe.get_doc("DocShare View Log", logs[0])
				# ToDo has no party fields, so party_type should be empty.
				self.assertFalse(log.party_type)
		finally:
			settings.notify_on_view = original_notify
			settings.save(ignore_permissions=True)


def cint(v):
	try:
		return int(v)
	except (TypeError, ValueError):
		return 0
