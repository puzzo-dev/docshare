# Copyright (c) 2026, I-Varse Technologies NG
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class DocShareSettings(Document):
	def get_default_expiry_days(self):
		"""Return the configured default expiry in days (0 = never)."""
		return int(self.default_expiry_days or 0)

	def is_linked_docs_enabled(self):
		return bool(self.share_linked_documents)

	def is_notify_on_view_enabled(self):
		return bool(self.notify_on_view)

	def is_ip_tracking_enabled(self):
		return bool(self.track_ip_address)


def get_settings():
	"""Return the single DocShare Settings record (cached)."""
	return frappe.get_cached_doc("DocShare Settings", "DocShare Settings")
