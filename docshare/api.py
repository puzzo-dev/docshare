# Copyright (c) 2026, I-Varse Technologies NG
# License: MIT. See LICENSE

"""DocShare public and desk API.

The module exposes whitelisted methods for creating share links and for
guest (unauthenticated) retrieval of the shared document's print HTML or
PDF. All guest access is gated by the share token only — the doctype and
document name are never exposed in the public URL.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import hashlib

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_system_timezone, now_datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rate limit for guest endpoints: 60 requests per hour per IP.
GUEST_RATE_LIMIT_MAX = 60
GUEST_RATE_LIMIT_WINDOW = 3600

# Generic error shown for invalid / expired / exhausted / disabled links.
GENERIC_INVALID_MESSAGE = _("This share link is no longer available.")

# The doctype notifications are configured against.
VIEW_LOG_DOCTYPE = "DocShare View Log"


# ---------------------------------------------------------------------------
# Desk API
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_share_link(
	doctype: str,
	docname: str,
	expires_on: str | None = None,
	max_views: int | None = None,
	linked_documents: list | None = None,
):
	"""Create a DocShare Link and return its absolute public URL.

	The shared document always renders with the target doctype's own defaults:
	its default print format (``meta.default_print_format``, falling back to
	Standard) and its default letter head. Neither is caller-selectable, so a
	customer always sees the document exactly as it prints.

	The caller must have read/print permission on the target document.
	Optionally accepts ``linked_documents`` — a list of dicts with
	``linked_doctype`` and ``linked_docname`` — to share alongside the main
	document. They render with their own doctype defaults too.
	"""
	doctype = str(doctype or "").strip()
	docname = str(docname or "").strip()
	if not doctype or not docname:
		frappe.throw(_("Reference DocType and Document are required."))

	# Permission check on the target document — not merely role membership.
	if not frappe.has_permission(doctype, ptype="read", doc=docname, user=frappe.session.user):
		raise frappe.PermissionError

	meta = frappe.get_meta(doctype)
	if meta.issingle:
		frappe.throw(_("Single DocTypes cannot be shared."))

	# The same gate the linked-document loop applies. Without it the main target
	# was unfiltered: sharing a DocShare Link would publish another link's
	# share_token on a page that needs no login to read.
	if not _is_shareable_doctype(doctype):
		frappe.throw(_("{0} cannot be shared.").format(doctype))

	if frappe.db.get_value(doctype, docname, "docstatus") == 2:
		frappe.throw(_("Trashed documents cannot be shared."))

	# Validate linked documents and permission on each.
	# `frappe.call` JSON-stringifies array/object args (see request.js), so this
	# arrives as a str over HTTP and as a real list on direct Python calls.
	linked_documents = frappe.parse_json(linked_documents) or []
	if not isinstance(linked_documents, list | tuple):
		linked_documents = []

	# Enforced server-side too: hiding the selector must not be the only thing
	# stopping a crafted request from attaching linked documents.
	if not cint(_settings().share_linked_documents):
		linked_documents = []

	linked_rows = []
	if linked_documents:
		# The cap the picker already applies when it *offers* linked documents
		# was never applied when accepting them. This endpoint is whitelisted,
		# so a crafted request could attach any number: each one costs a
		# permission check and a lookup here, a print render on every view of
		# the share page, and — on the PDF download — its own wkhtmltopdf
		# process. A few thousand entries in one call is a denial of service
		# that any user with share rights could mount.
		#
		# Refused rather than truncated: silently dropping documents the sender
		# chose would produce a share that is missing pages nobody can account
		# for.
		if len(linked_documents) > _MAX_LINKED_DOCUMENTS:
			frappe.throw(
				_("A share can include at most {0} linked documents.").format(
					_MAX_LINKED_DOCUMENTS
				)
			)

		# The same document twice would render twice in the share view. The desk
		# picker no longer sends duplicates, but this endpoint is whitelisted and
		# a request can carry anything.
		seen_linked = set()
		for ld in linked_documents:
			ld_doctype = str(ld.get("linked_doctype") or "").strip()
			ld_docname = str(ld.get("linked_docname") or "").strip()
			if not ld_doctype or not ld_docname:
				continue
			if (ld_doctype, ld_docname) in seen_linked:
				continue
			seen_linked.add((ld_doctype, ld_docname))
			if not _is_shareable_doctype(ld_doctype):
				frappe.throw(
					_("{0} cannot be shared alongside a document.").format(ld_doctype)
				)
			if not frappe.has_permission(ld_doctype, ptype="read", doc=ld_docname, user=frappe.session.user):
				raise frappe.PermissionError
			if frappe.db.get_value(ld_doctype, ld_docname, "docstatus") == 2:
				frappe.throw(_("Linked document {0} {1} is trashed.").format(ld_doctype, ld_docname))
			linked_rows.append({
				"linked_doctype": ld_doctype,
				"linked_docname": ld_docname,
				"print_format": None,
				"letterhead": None,
				"no_letterhead": 0,
			})

	doc = frappe.get_doc({
		"doctype": "DocShare Link",
		"ref_doctype": doctype,
		"ref_docname": docname,
		"print_format": None,
		"letterhead": None,
		"no_letterhead": 0,
		"expires_on": _normalize_expiry(expires_on),
		"max_views": _normalize_max_views(max_views),
		"enabled": 1,
		"linked_documents": linked_rows,
	})
	doc.insert(ignore_permissions=True)

	return _build_share_url(doc.share_token)


@frappe.whitelist()
def get_share_context(doctype: str, docname: str):
	"""Everything the Share dialog needs, in one round trip.

	The dialog previously chained three calls — options, existing links, then
	linked documents — each waiting on the one before, so the popup could not
	open until three sequential requests had completed. They are independent
	reads against the same document, so they belong in one response.

	Each part still applies its own permission check, because each is also
	callable on its own.
	"""
	return {
		"options": get_share_options(),
		"existing_links": list_share_links(doctype, docname),
		"linked_documents": get_linked_documents_for(doctype, docname),
	}


@frappe.whitelist()
def get_share_options():
	"""Configuration the Share dialog needs before it can build itself.

	The dialog cannot decide whether to render the Linked Documents selector, or
	which expiry preset to pre-select, without knowing DocShare Settings.
	"""
	settings = _settings()
	return {
		"share_linked_documents": cint(settings.share_linked_documents),
		"default_expiry_days": cint(settings.default_expiry_days),
	}


_MAX_LINKED_DOCUMENTS = 50


@frappe.whitelist()
def get_linked_documents_for(doctype: str, docname: str):
	"""Return linked documents for the share popup selector.

	Uses Frappe's native ``frappe.desk.form.linked_with.get`` API — the same
	one that powers the Connections tab on every form. Returns a list of
	dicts with ``doctype``, ``docname``, and ``docstatus`` for each linked
	document the user can read.
	"""
	doctype = str(doctype or "").strip()
	docname = str(docname or "").strip()
	if not doctype or not docname:
		return []
	if not frappe.has_permission(doctype, ptype="read", doc=docname, user=frappe.session.user):
		raise frappe.PermissionError

	# "Allow Sharing Linked Documents" off -> offer nothing.
	if not cint(_settings().share_linked_documents):
		return []

	result = []
	seen = set()

	def _add(linked_doctype, name, docstatus):
		key = (linked_doctype, name)
		if key in seen or docstatus == 2:
			return
		seen.add(key)
		result.append({
			"linked_doctype": linked_doctype,
			"linked_docname": name,
			"docstatus": docstatus,
		})

	# The document's own Connections, read the way the desk tab reads them.
	#
	# Only the reverse links defined in Connections. The document's own Link
	# fields were tried too and were wrong: they pull in Customer, Company,
	# Cost Center and Event Type — master records, not documents anyone would
	# call a connection. Anything that matters both ways (a Quotation) already
	# carries the reverse link, so Connections alone finds it.
	#
	# This used to rely on frappe.desk.form.linked_with.get, which returned
	# nothing useful here. Its link map merges a doctype's direct link field
	# with any child table that also carries one, and the child branch wins the
	# elif chain — so with an event_booking field present on both Quotation and
	# Sales Taxes and Charges, it searched the tax rows and found no Quotation.
	# The Connections definitions are what "documents in connection" means to
	# anyone looking at the form, so read those instead of inferring them.
	meta = frappe.get_meta(doctype)
	for link in (meta.links or []):
		link_doctype = getattr(link, "link_doctype", None)
		link_fieldname = getattr(link, "link_fieldname", None)
		if not link_doctype or not link_fieldname:
			continue
		if not _is_offerable_linked_doctype(link_doctype):
			continue
		if not frappe.has_permission(link_doctype, ptype="read"):
			continue
		try:
			rows = frappe.get_list(
				link_doctype,
				filters={link_fieldname: docname},
				fields=["name", "docstatus"],
				limit_page_length=_MAX_LINKED_DOCUMENTS,
				order_by="modified desc",
			)
		except Exception:
			# A Connections entry can name a field that no longer exists.
			continue
		for row in rows:
			_add(link_doctype, row.get("name"), row.get("docstatus"))

	return result


@frappe.whitelist()
def list_share_links(doctype: str, docname: str):
	"""List active DocShare Links for a given target document."""
	doctype = str(doctype or "").strip()
	docname = str(docname or "").strip()
	if not doctype or not docname:
		return []

	if not frappe.has_permission(doctype, ptype="read", doc=docname, user=frappe.session.user):
		raise frappe.PermissionError

	# get_all bypasses permission_query_conditions, so this has to apply the
	# ownership rule itself. Without it the dialog listed every user's tokens
	# for the document while the list view showed only your own — and since
	# revoking is owner-only, the extra rows were links you could not act on
	# anyway, handed over with their tokens.
	filters = {"ref_doctype": doctype, "ref_docname": docname, "enabled": 1}
	if "System Manager" not in frappe.get_roles(frappe.session.user):
		filters["owner"] = frappe.session.user

	links = frappe.get_all(
		"DocShare Link",
		filters=filters,
		fields=["name", "share_token", "expires_on", "max_views", "view_count", "last_accessed_on", "enabled"],
		order_by="creation desc",
	)
	for link in links:
		link["url"] = _build_share_url(link["share_token"])
	return links


@frappe.whitelist()
def revoke_share_link(name: str):
	"""Disable (revoke) a DocShare Link by its internal name."""
	name = str(name or "").strip()
	if not name:
		frappe.throw(_("DocShare Link name is required."))

	doc = frappe.get_doc("DocShare Link", name)
	# Permission is enforced by has_permission hook, but double-check explicitly.
	if not _user_can_manage_link(doc):
		raise frappe.PermissionError

	doc.enabled = 0
	doc.save(ignore_permissions=True)
	return True


# ---------------------------------------------------------------------------
# Guest API
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def get_shared_document(token: str):
	"""Return the print HTML for a shared document.

	Validates the link is enabled, not expired, and within the view limit.
	Increments the view counter and updates last_accessed_on.
	"""
	enforce_guest_rate_limit("view")
	doc = _validate_guest_token(token)

	# The same sanitised, cached fragment the share page serves — not the raw
	# printview. This returned frappe.get_print's whole document, scripts and
	# all, to an allow_guest endpoint: a print format carrying script (they are
	# user-editable HTML) executed in whatever page embedded the result, and
	# nothing stripped inline handlers or javascript: hrefs the way
	# extract_print_fragment does for the share page itself.
	#
	# Going through the cache also stops each call spawning a full printview
	# render, which on a guest endpoint is a lever of its own.
	fragment = cached_print_fragment(
		doc.ref_doctype, doc.ref_docname, doc.print_format,
		doc.letterhead, doc.no_letterhead,
		lambda: _render_print_html(doc),
	)
	_increment_view_count(doc.name)
	return fragment.get("html", "")


@frappe.whitelist(allow_guest=True)
def download_shared_pdf(token: str):
	"""Stream a PDF of the shared document to the guest."""
	enforce_guest_rate_limit("pdf")
	doc = _validate_guest_token(token)
	pdf = _render_print_pdf(doc)
	_increment_view_count(doc.name)

	frappe.response["filename"] = _pdf_filename(doc)
	frappe.response["filecontent"] = pdf
	frappe.response["type"] = "download"


# ---------------------------------------------------------------------------
# Document event hook
# ---------------------------------------------------------------------------

def on_document_trash(doc, method=None):
	"""Auto-disable DocShare Links when the target document is trashed.

	Wildcard doc_events hook — invoked for every doctype on_trash.
	"""
	if not doc or not doc.doctype or not doc.name:
		return
	try:
		frappe.db.set_value(
			"DocShare Link",
			{"ref_doctype": doc.doctype, "ref_docname": doc.name, "enabled": 1},
			"enabled",
			0,
			update_modified=False,
		)
	except Exception:
		# Hook must be exception-safe — never break the parent trash flow.
		frappe.log_error(title="DocShare on_trash hook failed", message=frappe.get_traceback())


# ---------------------------------------------------------------------------
# Permission hooks
# ---------------------------------------------------------------------------

def has_docshare_link_permission(doc, ptype="read", user=None):
	"""has_permission hook for DocShare Link.

	System Managers manage all links. For everyone else the answer depends on
	what is being asked, which this previously ignored — it checked read on the
	target regardless of ptype, so being able to read an invoice conferred
	write and delete on other people's links to it.

	- read: anyone who can read the target document.
	- anything else (write/delete/cancel/submit): the link's owner only,
	  matching _user_can_manage_link. Revoking or deleting a link kills a URL
	  already sent to a customer.
	"""
	if user is None:
		user = frappe.session.user
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return True

	if not doc or not getattr(doc, "ref_doctype", None) or not getattr(doc, "ref_docname", None):
		# Unsaved record — let standard role permissions decide.
		return None

	if ptype != "read":
		return getattr(doc, "owner", None) == user

	try:
		return frappe.has_permission(doc.ref_doctype, ptype="read", doc=doc.ref_docname, user=user)
	except frappe.PermissionError:
		return False


def has_docshare_view_log_permission(doc, ptype="read", user=None):
	"""has_permission hook for DocShare View Log.

	A view log carries `share_link_token` — the bearer credential for the
	public /share/<token> URL — alongside the viewer's IP, city, user agent and
	the link owner's email and phone. The doctype used to grant read to the
	`Desk User` role, which is every internal user: anyone could list the logs,
	lift a token and open, or forward, any customer's shared document without
	logging in at all. Verified on a live site before the role was removed.

	With that role gone the doctype is System Manager only, which would take the
	logs away from the people they are actually for. This hands them back to the
	one person entitled to them: whoever created the share link.
	"""
	if user is None:
		user = frappe.session.user
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return True
	if not doc:
		return None
	return getattr(doc, "share_link_owner", None) == user


def get_docshare_view_log_permission_query_conditions(user=None):
	"""Scope DocShare View Log lists to the links the user owns.

	The has_permission hook governs a single document; a list query never
	reaches it, so without this a report view would still expose every token on
	the site to anyone the role permissions let in.
	"""
	if user is None:
		user = frappe.session.user
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return ""
	return f"""`tabDocShare View Log`.share_link_owner = {frappe.db.escape(user)}"""


def get_docshare_link_permission_query_conditions(user=None):
	"""permission_query_conditions hook for DocShare Link list views.

	This must do the real filtering. Frappe applies ``has_permission`` hooks to
	single-document access only — list and report queries are constrained by
	this function alone. Returning "" therefore exposed every row, and with it
	every ``share_token``: a token is a bearer credential that grants
	unauthenticated access to the target document, so a user who could list
	tokens could read documents they have no permission to open.

	Non-managers see only the links they created. Ownership is the right test:
	a share link is an act by its creator, and read access to the target
	document does not imply the right to hand out a public URL for it.
	"""
	if user is None:
		user = frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return ""  # Managers administer all links.

	return f"""`tabDocShare Link`.`owner` = {frappe.db.escape(user)}"""



# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

# Modules that hold framework plumbing rather than business documents.
_INFRASTRUCTURE_MODULES = frozenset({
	"Core", "Automation", "Email", "Social", "Workflow", "Website",
	"Integrations", "Printing", "Desk", "Custom", "Contacts", "Geo",
	"Data Migration",
})


def _is_offerable_linked_doctype(doctype: str) -> bool:
	"""True if a doctype belongs in the linked-document picker.

	Stricter than _is_shareable_doctype, and deliberately a separate question.
	That one answers "may this be shared at all", which stays permissive — a
	user can share whatever document they are looking at. This one answers
	"should we suggest attaching this alongside it", where the bar is higher:
	Comment, File and ToDo all point back at a shared document and were all
	being offered, and a Comment can carry internal discussion that has no
	business travelling to a customer.

	Filtering by module catches the whole family at once instead of chasing
	names one at a time, and it is applied only here so it cannot block a
	legitimate share target.
	"""
	if not _is_shareable_doctype(doctype):
		return False
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return False
	return getattr(meta, "module", None) not in _INFRASTRUCTURE_MODULES


def _is_shareable_doctype(doctype: str) -> bool:
	"""True if a doctype is a real document a customer could meaningfully read.

	`frappe.desk.form.linked_with.get` returns everything that points at the
	document, including bookkeeping rows a customer must never be offered:
	ledger entries, audit logs, versions, and DocShare's own records.
	"""
	if not doctype:
		return False

	# Frappe's own audit/log doctypes.
	from frappe.model import log_types

	if doctype in log_types:
		return False

	# DocShare's own bookkeeping.
	if doctype.startswith("DocShare"):
		return False

	# Ledger/audit families. "... Entry" alone is too broad — Payment Entry and
	# Journal Entry are real documents a customer may legitimately receive.
	if doctype.endswith(" Ledger Entry") or doctype.endswith(" Log") or doctype == "Version":
		return False

	# Named ledger/bookkeeping doctypes the patterns above miss. "GL Entry" does
	# not end in " Ledger Entry", and Bin/Serial No are stock internals.
	if doctype in {"GL Entry", "Stock Ledger Entry", "Bin", "Serial No", "Batch"}:
		return False

	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return False

	if meta.istable or meta.issingle or getattr(meta, "is_virtual", 0):
		return False

	return True


def _normalize_max_views(value):
	"""Coerce a view limit, rejecting values that would mint a dead link.

	A negative number is truthy, so it used to be stored as-is and then failed
	`view_count >= max_views` on the very first view. Zero and None both mean
	unlimited and are stored as NULL, which the guard in _increment_view_count
	reads the same way.
	"""
	if value in (None, ""):
		return None

	limit = cint(value)
	if limit < 0:
		frappe.throw(_("Max Views cannot be negative."))

	return limit or None


def _normalize_expiry(value):
	"""Coerce a client-supplied expiry into a naive system-timezone datetime.

	``frappe.datetime.add_days`` (and ``obj_to_str``) call moment's bare
	``.format()``, which emits ISO-8601 *with a UTC offset*
	(``2026-09-13T19:30:57+01:00``). MariaDB's DATETIME column rejects that
	outright with error 1292, so the value has to be parsed and flattened here
	rather than handed straight to the column.
	"""
	if not value:
		return None

	try:
		parsed = get_datetime(value)
	except Exception:
		parsed = None

	if not parsed:
		frappe.throw(_("Could not understand the expiry date {0}.").format(value))

	if parsed.tzinfo is not None:
		import zoneinfo

		parsed = parsed.astimezone(zoneinfo.ZoneInfo(get_system_timezone())).replace(tzinfo=None)

	if parsed <= now_datetime():
		frappe.throw(_("Expiry must be in the future."))

	return parsed


def _reject(token: str, reason: str):
	"""Show the guest one generic message, but record *why* server-side.

	The visitor must never learn whether a token is unknown, revoked, expired
	or exhausted — that is an enumeration oracle. Operators still need the
	reason, so it goes to the "docshare" logger (logs/docshare.log). Logged at
	warning level deliberately: frappe's default log level is WARNING on a dev
	bench and ERROR in production, so info() would be silently dropped.
	"""
	frappe.logger("docshare").warning(f"share token {str(token)[:12]}... rejected: {reason}")
	frappe.throw(GENERIC_INVALID_MESSAGE)


def _validate_guest_token(token: str):
	"""Resolve and validate a share token. Returns the DocShare Link doc."""
	token = str(token or "").strip()
	if not token:
		frappe.throw(GENERIC_INVALID_MESSAGE)

	name = frappe.db.get_value("DocShare Link", {"share_token": token}, "name")
	if not name:
		_reject(token, "no link matches this token")

	doc = frappe.get_doc("DocShare Link", name)

	if not cint(doc.enabled):
		_reject(token, f"{doc.name} is disabled (revoked, or its target was trashed)")

	if doc.expires_on and get_datetime(doc.expires_on) <= now_datetime():
		_reject(token, f"{doc.name} expired on {doc.expires_on}")

	if doc.max_views and cint(doc.view_count) >= cint(doc.max_views):
		_reject(token, f"{doc.name} hit its view limit ({doc.view_count}/{doc.max_views})")

	# Confirm the target document still exists and is not trashed.
	if not frappe.db.exists(doc.ref_doctype, doc.ref_docname):
		_reject(token, f"{doc.ref_doctype} {doc.ref_docname} no longer exists")
	if frappe.db.get_value(doc.ref_doctype, doc.ref_docname, "docstatus") == 2:
		_reject(token, f"{doc.ref_doctype} {doc.ref_docname} is cancelled")

	return doc


def _increment_view_count(name: str):
	"""Atomically increment view_count and update last_accessed_on.

	Tolerates concurrent requests — uses a single UPDATE statement so
	race conditions cannot lose increments. Also creates a DocShare View
	Log (which fires native Frappe Notifications + WhatsApp Notifications
	on insert) if view tracking is enabled in settings.
	"""
	# The WHERE clause carries the limit so the database, not the earlier read
	# in _validate_guest_token, decides whether the view is counted. That read
	# and this write are a check-then-act pair: two concurrent viewers can both
	# pass the check before either increments. Guarding here means view_count
	# can never climb past max_views, so every subsequent request is rejected
	# correctly. The residue is that a tie may *serve* one page beyond the
	# limit — unavoidable without holding a row lock across the whole render,
	# which would be a far worse trade on a public endpoint.
	try:
		frappe.db.sql(
			"""UPDATE `tabDocShare Link`
			   SET view_count = view_count + 1,
			       last_accessed_on = %s
			   WHERE name = %s
			     AND (max_views IS NULL OR max_views = 0 OR view_count < max_views)""",
			(now_datetime(), name),
		)
		frappe.db.commit()
	except Exception:
		# Two people opening the same link at once collide on this row:
		# _validate_guest_token has already read it in this transaction, so
		# MariaDB raises 1020 "Record has changed since last read". The
		# document has been rendered by now and the visitor is entitled to it,
		# so a lost count is the right trade against a 500 on a link that was
		# sent to a customer. The cost is bounded: a contended view may go
		# uncounted, which can let a max_views link serve once more than its
		# cap — the same residual already documented on the WHERE clause above.
		frappe.db.rollback()
		frappe.logger("docshare").warning(
			f"view count not recorded for {name}: {frappe.get_traceback(with_context=False)}"
		)

	# Create a View Log — this fires native Notification (event="New")
	# and WhatsApp Notification (doctype_event="After Insert") on insert.
	_log_view(name)


def _has_view_notification_consumer() -> bool:
	"""True if anything is actually configured to act on a DocShare View Log.

	A View Log exists to trigger notifications: its insert is what fires a native
	Frappe Notification (event "New") or a WhatsApp Notification (doctype_event
	"After Insert"). With neither configured, writing one — and the timeline
	comment that follows it — is a side effect nobody asked for and nobody reads.
	So "Notify on View" stays inert until a consumer exists, rather than
	generating records on its own authority.
	"""
	# Deliberately not memoised on frappe.local. That saves one indexed exists()
	# per page view, but frappe.local outlives a whole worker job or test run,
	# so a cached False would silently stop writing the audit log for every
	# subsequent view once someone added a Notification. Not a trade worth
	# making for a single cheap lookup — unlike the batched settings toggle in
	# event_bookings, there is nothing here to amortise it across.
	if frappe.db.exists("Notification", {"document_type": VIEW_LOG_DOCTYPE, "enabled": 1}):
		return True

	# The WhatsApp app is optional; absence of the doctype is not an error.
	if frappe.db.exists("DocType", "WhatsApp Notification") and frappe.db.exists(
		"WhatsApp Notification", {"reference_doctype": VIEW_LOG_DOCTYPE, "disabled": 0}
	):
		return True

	return False


def _log_view(share_link_name: str):
	"""Create a DocShare View Log record.

	The View Log's after_insert fires native Frappe Notifications and
	WhatsApp Notifications configured against the DocShare View Log doctype.
	Also adds a timeline Comment on the original shared document.

	Both the setting *and* a configured consumer are required — see
	_has_view_notification_consumer().
	"""
	settings = _settings()
	if not settings.notify_on_view:
		return

	if not _has_view_notification_consumer():
		return

	link = frappe.get_doc("DocShare Link", share_link_name)
	if not link or not link.enabled:
		return

	try:
		view_log = frappe.get_doc({
			"doctype": "DocShare View Log",
			"share_link": link.name,
			"share_link_token": link.share_token,
			"share_link_owner": link.owner,
			"ref_doctype": link.ref_doctype,
			"ref_docname": link.ref_docname,
		})
		view_log.insert(ignore_permissions=True)
		# Committed before the timeline comment, and deliberately: the log is the
		# record that a view happened, and the comment is a convenience on top of
		# it. If the comment fails — a trashed target, a permission change — the
		# except below logs and moves on, and the view must still be on record
		# rather than rolled back with it. This is the same reasoning as the
		# view-count commit above, written down so it is not mistaken for debris.
		frappe.db.commit()

		# Add a timeline Comment on the original document.
		_add_timeline_comment(link, view_log)
	except Exception:
		frappe.log_error(title="DocShare view log failed", message=frappe.get_traceback())


def _add_timeline_comment(link, view_log):
	"""Add an Info comment on the original shared document's timeline."""
	if not frappe.db.exists(link.ref_doctype, link.ref_docname):
		return
	party_info = ""
	if view_log.party_display_name:
		party_info = f" by {view_log.party_display_name}"
	location_info = ""
	if view_log.country:
		location_info = f" from {view_log.country}"
	ip_info = ""
	if view_log.ip_address and view_log.ip_address != "127.0.0.1":
		ip_info = f" (IP: {view_log.ip_address})"

	content = f"DocShare link viewed{party_info}{location_info}{ip_info}"
	try:
		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Info",
			"comment_email": "Administrator",
			"reference_doctype": link.ref_doctype,
			"reference_name": link.ref_docname,
			"content": content,
		}).insert(ignore_permissions=True)
	except Exception:
		# Non-critical: the visitor has already been served, so this must not
		# break the view flow — but swallowing it silently meant a broken
		# timeline was invisible forever.
		frappe.logger("docshare").warning(
			f"timeline comment failed for {link.ref_doctype} {link.ref_docname}: "
			f"{frappe.get_traceback(with_context=False)}"
		)


def _build_share_url(token: str) -> str:
	"""Build an absolute public URL for a share token."""
	host = frappe.utils.get_url()
	host = host.rstrip("/")
	return f"{host}/share/{token}"


def _render_print_html(doc) -> str:
	"""Render the print HTML for a shared document with elevated permissions.

	The token validation is the authorization — once validated, we temporarily
	switch to Administrator to render the print view, since the guest user has
	no permission on the target document.
	"""
	with _print_permissions_bypassed():
		return frappe.get_print(
			doc.ref_doctype,
			doc.ref_docname,
			doc.print_format or None,
			as_pdf=False,
			no_letterhead=cint(doc.no_letterhead),
			letterhead=doc.letterhead or None,
		)


# printview ships a screen-only Mozilla workaround that stretches every image
# inside a table cell to fill it:
#     body:last-child .print-format td img { width: 100% !important; }
# On a print format that places a QR code or a company stamp in a table cell —
# even one carrying its own inline width — the !important wins and the image is
# blown up. It sits inside @media screen, which is why the PDF is unaffected and
# only the on-screen share view looks wrong. Strip that one declaration; every
# other print rule, including `.print-format img { max-width: 100% }`, stays.
_TABLE_IMAGE_HACK = re.compile(
	r"body:last-child\s+\.print-format\s+td\s+img\s*\{[^}]*\}",
	re.IGNORECASE,
)


def enforce_guest_rate_limit(scope: str = "view") -> None:
	"""Throttle unauthenticated access, per IP, per scope.

	Both guest surfaces are expensive: the share page renders a print format per
	document, and the PDF endpoint spawns wkhtmltopdf. A single valid token —
	including one legitimately sent to a customer — is otherwise enough to
	saturate the box by looping either one.

	Implemented here rather than with @frappe.rate_limit because /share/<token>
	is a website route, not a whitelisted method, and one mechanism across all
	three entry points beats two that can drift. make_key() prefixes the site's
	db_name, so the counter is tenant-scoped. INCR and EXPIRE are pipelined so
	there is no window in which a counter exists without a TTL.
	"""
	ip = getattr(frappe.local, "request_ip", None)
	if not ip:
		return

	key = frappe.cache.make_key(f"docshare:ratelimit:{scope}:{ip}")
	try:
		pipe = frappe.cache.pipeline()
		pipe.incr(key)
		pipe.ttl(key)
		count, ttl = pipe.execute()
		if ttl is None or ttl < 0:
			frappe.cache.expire(key, GUEST_RATE_LIMIT_WINDOW)
	except Exception:
		# A cache outage must not take the share page down with it.
		return

	if count and count > GUEST_RATE_LIMIT_MAX:
		frappe.throw(
			_("Too many requests. Please try again later."),
			frappe.TooManyRequestsError,
		)


def _settings():
	"""The DocShare Settings single, cached."""
	return frappe.get_cached_doc("DocShare Settings", "DocShare Settings")


def _drop_table_image_hack(styles: str) -> str:
	return _TABLE_IMAGE_HACK.sub("", styles or "")


@contextmanager
def _print_permissions_bypassed():
	"""Render a document the guest has no permission to read.

	The share token *is* the authorisation — by the time we render, the link has
	already been validated. What we must not do is switch users to achieve it:
	``frappe.set_user`` also assigns ``local.session.sid = username`` and clears
	``local.session.data`` (frappe/__init__.py). Inside a live web request that
	destroys the caller's session, which is later persisted and then fails to
	resume with "User None is disabled".

	``frappe.flags.ignore_print_permissions`` is the supported alternative:
	printview checks it in place of validate_print_permission() and nothing else
	about the request is disturbed. Restored to its previous value, not blindly
	to False, so nesting stays safe.
	"""
	previous = frappe.flags.ignore_print_permissions
	frappe.flags.ignore_print_permissions = True
	try:
		yield
	finally:
		frappe.flags.ignore_print_permissions = previous


# How long a rendered fragment is held. The key already carries the document's
# `modified`, so an edit invalidates immediately and this only bounds how long a
# fragment for a version nobody is viewing any more occupies memory.
# A day, not ten minutes. The key carries the document's `modified`, so an edit
# changes the key and a stale fragment can never be served — the TTL is only
# deciding how often an *unchanged* document pays for a full printview render
# and a BeautifulSoup parse of it. At ten minutes a link opened through the day
# re-rendered it constantly for no benefit.
_FRAGMENT_CACHE_TTL = 86400

# Bump when extract_print_fragment changes what it produces. The cache key is
# built from the document, not from this code, so without a version marker a
# sanitiser change keeps serving fragments produced by the previous rules until
# each document happens to be edited.
_FRAGMENT_VERSION = 2


def cached_print_fragment(
	ref_doctype: str,
	ref_docname: str,
	print_format: str | None,
	letterhead: str | None,
	no_letterhead,
	render,
) -> dict:
	"""Render a document to an embeddable fragment, reusing the last result.

	Rendering a share page means asking Frappe for a whole printview document
	and then parsing it with BeautifulSoup to pull the fragment back out. That
	ran on every guest view of the same unchanged document — the expensive half
	of serving a link that is, by design, opened repeatedly.

	The key covers everything the output depends on: the document, its
	`modified` timestamp, and the print format and letterhead choices recorded
	on the share link. A document edit changes `modified` and so changes the
	key, which means a stale fragment is never served rather than being served
	until some TTL expires.

	Cached content is a fully rendered document, so the key is site-scoped
	through make_key exactly as the rest of the app's cache use is.
	"""
	# get_cached_value, not db.get_value: this runs before the cache lookup, so
	# a plain read meant a database round trip on every *hit* — the cost the
	# cache exists to avoid, paid on the path that was supposed to be free.
	# Frappe serves this from Redis and invalidates it when the document is
	# saved, which is exactly the invalidation the key already relies on.
	modified = frappe.get_cached_value(ref_doctype, ref_docname, "modified")
	raw = "|".join([
		str(_FRAGMENT_VERSION),
		ref_doctype,
		ref_docname,
		str(modified),
		print_format or "",
		letterhead or "",
		str(cint(no_letterhead)),
	])
	# No make_key here: set_value/get_value namespace by site themselves. Passing
	# an already-namespaced key double-encodes it (make_key returns bytes, and
	# formatting those back into a string yields the b'...' repr) and the value
	# then never reads back — the cache silently does nothing.
	key = "docshare:fragment:" + hashlib.sha256(raw.encode()).hexdigest()

	# expires=True matters more than it looks. Without it, a miss is written
	# back into frappe.local.cache as None, and set_value with a TTL writes only
	# to Redis — so every later lookup in the same request keeps returning that
	# cached None and the entry is never seen. The flag tells Frappe not to hold
	# a value that has an expiry in request-local memory.
	cached = frappe.cache.get_value(key, expires=True)
	if cached:
		return cached

	fragment = extract_print_fragment(render())
	frappe.cache.set_value(key, fragment, expires_in_sec=_FRAGMENT_CACHE_TTL)
	return fragment


def extract_print_fragment(full_html: str) -> dict:
	"""Reduce a printview page to something embeddable.

	``frappe.get_print`` returns a *whole HTML document* (see
	frappe/www/printview.html): doctype, <head> with print.bundle.css, an
	``.action-banner`` carrying its own "Print" / "Get PDF" links, and a
	``.print-format-gutter`` painted #d1d8dd. Dropping three of those into one
	page nests three documents, duplicates the stylesheet, and lets the injected
	head styles repaint the whole page — which is what hid the DocShare header.

	So take only what is needed: the <style> rules and the .print-format element.
	"""
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(full_html or "", "html.parser")

	# The format's own CSS lives in <style> in the head; keep it, drop the
	# <link> to print.bundle.css (the share page includes that once itself).
	styles = "\n".join(tag.decode_contents() for tag in soup.find_all("style"))
	styles = _drop_table_image_hack(styles)

	# printview's own toolbar and its DOMContentLoaded script must not survive.
	for tag in soup.select(".action-banner"):
		tag.decompose()
	for tag in soup.find_all("script"):
		tag.decompose()

	# Inline event handlers and javascript: URLs, from every tag.
	#
	# Print formats carry these legitimately — the Avril Beetails format hides a
	# QR and a stamp image with onerror="this.style.display='none'" — but this
	# page is served to anyone holding the token, with no login, so any handler
	# reaching it would be script executing from document content on a public
	# page. That is the stored-XSS route the audit described, and stripping
	# scripts alone never closed it.
	#
	# The page's Content-Security-Policy already refuses to run them, so leaving
	# them in achieves nothing except a console full of violations and, because
	# the blocked handler was the one that hides a failed image, a broken-image
	# icon where the format expected nothing. share.html restores that behaviour
	# with a nonced handler instead.
	for tag in soup.find_all(True):
		for attr in [a for a in tag.attrs if a.lower().startswith("on")]:
			del tag[attr]
		for attr in ("href", "src", "xlink:href", "action", "formaction"):
			value = tag.get(attr)
			if isinstance(value, str) and value.strip().lower().startswith("javascript:"):
				del tag[attr]

	# Every <style> has been collected above and the caller hoists them into
	# <head>. Leaving the in-body copies (print formats often declare their own
	# <style> inside the format HTML) would put them *after* the page's rules,
	# where a bare `body` selector would restyle the share page itself.
	for tag in soup.find_all("style"):
		tag.decompose()

	node = soup.find(class_="print-format")
	if node is not None:
		body = node.decode_contents()
	else:
		# Defensive: an unexpected shape still renders something.
		body_tag = soup.find("body")
		body = body_tag.decode_contents() if body_tag else (full_html or "")

	return {"styles": styles, "html": body}


def _shared_pdf_targets(doc) -> list[tuple]:
	"""The documents a share link renders, in page order.

	The main document first, then each linked document that still exists and is
	not cancelled — the same set, in the same order, that the share page shows.
	"""
	targets = [(doc.ref_doctype, doc.ref_docname, cint(doc.no_letterhead), doc.letterhead or None)]

	for ld in doc.linked_documents or []:
		if not ld.linked_doctype or not ld.linked_docname:
			continue
		if not frappe.db.exists(ld.linked_doctype, ld.linked_docname):
			continue
		if frappe.db.get_value(ld.linked_doctype, ld.linked_docname, "docstatus") == 2:
			continue
		targets.append(
			(ld.linked_doctype, ld.linked_docname, cint(ld.no_letterhead), ld.letterhead or None)
		)

	return targets


def _render_print_pdf(doc) -> bytes:
	"""Render the shared document *and* its linked documents as one PDF.

	The download has to match what the page shows: the main document followed
	by every linked document, concatenated into a single file. Each document is
	rendered separately and merged into one PdfWriter — the same approach as
	``frappe.utils.print_format._download_multi_pdf`` — so every document keeps
	its own print format's page size, margins and orientation instead of being
	forced into the first one's layout.

	Goes through ``frappe.get_print(as_pdf=True)`` rather than calling
	``get_pdf`` on pre-rendered HTML, so each print format's own pdf_generator
	and page settings are honoured.
	"""
	from io import BytesIO

	from pypdf import PdfWriter

	writer = PdfWriter()
	with _print_permissions_bypassed():
		for ref_doctype, ref_docname, no_letterhead, letterhead in _shared_pdf_targets(doc):
			frappe.get_print(
				ref_doctype,
				ref_docname,
				None,
				as_pdf=True,
				output=writer,
				no_letterhead=no_letterhead,
				letterhead=letterhead,
			)

	# frappe.utils.print_format.read_multi_pdf is deprecated in v16 and goes in
	# v17, and it is three lines: write the merged writer into a buffer and hand
	# back the bytes. Inlined rather than left to warn now and break later.
	with BytesIO() as merged:
		writer.write(merged)
		return merged.getvalue()


def _linked_document_renderable(ld) -> bool:
	"""True if a linked-document row still points at something printable.

	Split out of the renderer so callers can check before reaching for a cached
	fragment — otherwise a row whose target has been deleted or cancelled would
	be looked up and cached as an empty render.
	"""
	if not ld or not ld.linked_doctype or not ld.linked_docname:
		return False
	if not frappe.db.exists(ld.linked_doctype, ld.linked_docname):
		return False
	return frappe.db.get_value(ld.linked_doctype, ld.linked_docname, "docstatus") != 2


def _render_linked_document_html(ld) -> str | None:
	"""Render the print HTML for a linked document row.

	Uses the same elevated-permission pattern as the main document.
	Returns None if the linked document no longer exists or is trashed.
	"""
	if not _linked_document_renderable(ld):
		return None

	with _print_permissions_bypassed():
		return frappe.get_print(
			ld.linked_doctype,
			ld.linked_docname,
			ld.print_format or None,
			as_pdf=False,
			no_letterhead=cint(ld.no_letterhead),
			letterhead=ld.letterhead or None,
		)


def _pdf_filename(doc) -> str:
	"""Build a friendly PDF filename for a shared document."""
	safe_name = str(doc.ref_docname).replace(" ", "_").replace("/", "-")
	return f"{doc.ref_doctype}-{safe_name}.pdf"


def _user_can_manage_link(doc) -> bool:
	"""True if the current user may revoke/manage this link.

	Deliberately narrower than read access on the target. Revoking kills a URL
	that has already been sent to a customer, so it is an action against the
	sharer's work — granting it to anyone who can merely *read* the invoice let
	any colleague silently break someone else's link.
	"""
	user = frappe.session.user
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return True
	return doc.owner == user
