# Copyright (c) 2026, I-Varse Technologies NG
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class DocShareLink(Document):
	def onload(self):
		"""Expose the full share URL to the form.

		Sent via __onload rather than stored in a field: the URL is derived from
		the site's host at read time, so persisting it would go stale the moment
		host_name, the port, or the protocol changes.
		"""
		if not self.share_token:
			return
		from docshare.api import _build_share_url

		self.set_onload("share_url", _build_share_url(self.share_token))

	def validate(self):
		self.validate_target()
		self.validate_print_format_available()
		self.validate_limits()

	def validate_limits(self):
		"""Structural invariants for the view limit.

		create_share_link normalises its inputs, but a direct write through
		/api/resource would bypass that, so the rule belongs here too.

		Deliberately no "expiry must be in the future" rule: validate runs on
		every save, and that check would make an already-expired link
		impossible to re-save — including to revoke it. Refusing a past expiry
		is a creation-time concern and lives in create_share_link.
		"""
		if self.max_views is not None and cint(self.max_views) < 0:
			frappe.throw(_("Max Views cannot be negative."))

	def validate_target(self):
		"""Reject Single doctypes, trashed documents, and missing documents."""
		if not self.ref_doctype or not self.ref_docname:
			frappe.throw(_("Reference DocType and Document are required."))

		meta = frappe.get_meta(self.ref_doctype)
		if meta.issingle:
			frappe.throw(_("Links to Single DocTypes are not allowed."))

		exists = frappe.db.exists(self.ref_doctype, self.ref_docname)
		if not exists:
			frappe.throw(_("Reference document {0} {1} does not exist.").format(
				self.ref_doctype, self.ref_docname))

		docstatus = frappe.db.get_value(self.ref_doctype, self.ref_docname, "docstatus")
		if docstatus == 2:
			frappe.throw(_("Reference document {0} {1} is trashed and cannot be shared.").format(
				self.ref_doctype, self.ref_docname))

	def validate_print_format_available(self):
		"""Ensure the target doctype has at least one available print format (Standard counts)."""
		# Standard print format is always available for any submittable/printable doctype.
		# Custom print formats are checked only when explicitly chosen.
		if self.print_format:
			pf_exists = frappe.db.exists("Print Format", {
				"name": self.print_format,
				"doc_type": self.ref_doctype,
				"docstatus": ["!=", 2],
			})
			if not pf_exists:
				frappe.throw(_("Print Format {0} is not available for {1}.").format(
					self.print_format, self.ref_doctype))

	def before_insert(self):
		"""Generate the share token if not already set."""
		if not self.share_token:
			self.share_token = frappe.generate_hash("", 32)

	def on_trash(self):
		"""Release the view logs so the link can actually be deleted.

		DocShare View Log.share_link is a Link to this doctype, so Frappe's
		back-link check refuses to delete any link that has ever been viewed —
		the previous comment here ("nothing extra to clean up") was wrong, and
		the practical effect was that viewed links piled up forever with no way
		to clear them.

		The logs are kept, not deleted. A view log is an audit record and it
		already stands on its own: the token, the owner and their contact
		details, the shared document, the IP, the party and the timestamp are
		all copied onto the row at insert time precisely so it does not depend
		on the link surviving. Cascading the delete would destroy that history
		to satisfy a foreign key.

		One UPDATE rather than a load-and-save per row: this only has to clear a
		column, and the logs carry no lifecycle of their own to run.
		"""
		frappe.db.sql(
			"UPDATE `tabDocShare View Log` SET share_link = NULL WHERE share_link = %s",
			self.name,
		)
