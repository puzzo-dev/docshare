# Copyright (c) 2026, I-Varse Technologies NG
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class DocShareViewLog(Document):
	def before_insert(self):
		"""Populate party, IP, geolocation, and owner details.

		This runs before insert so the native Frappe Notification (event='New')
		and WhatsApp Notification (doctype_event='After Insert') see all fields
		when they fire on insert.
		"""
		self.viewed_on = frappe.utils.now_datetime()
		self._resolve_party()
		self._resolve_owner_details()
		self._capture_ip_and_geo()

	def _resolve_party(self):
		"""Resolve the customer/lead/party from the shared document.

		Uses ERPNext's native field patterns:
		- customer / customer_name (Sales Invoice, Sales Order, Delivery Note)
		- party_type / party / party_name (Payment Entry, Journal Entry)
		- quotation_to / party_name (Quotation)
		- opportunity_from / party_name (Opportunity)
		"""
		if not self.ref_doctype or not self.ref_docname:
			return
		if not frappe.db.exists(self.ref_doctype, self.ref_docname):
			return

		doc = frappe.get_doc(self.ref_doctype, self.ref_docname)

		# Try ERPNext's get_party() helper for accounting controllers.
		party_type, party = None, None
		try:
			from erpnext.controllers.accounts_controller import AccountsController

			if isinstance(doc, AccountsController):
				party_type, party = doc.get_party()
		except Exception:
			pass

		# Fall back to field inspection.
		if not party_type:
			if doc.meta.get_field("customer") and doc.get("customer"):
				party_type, party = "Customer", doc.get("customer")
			elif doc.meta.get_field("supplier") and doc.get("supplier"):
				party_type, party = "Supplier", doc.get("supplier")
			elif doc.meta.get_field("party_type") and doc.get("party_type"):
				party_type, party = doc.get("party_type"), doc.get("party")
			elif doc.meta.get_field("quotation_to") and doc.get("quotation_to"):
				party_type, party = doc.get("quotation_to"), doc.get("party_name")
			elif doc.meta.get_field("opportunity_from") and doc.get("opportunity_from"):
				party_type, party = doc.get("opportunity_from"), doc.get("party_name")

		if party_type and party:
			self.party_type = party_type
			self.party_name = party
			# Resolve display name.
			display = party
			for fname in ("customer_name", "supplier_name", "lead_name", "customer", "supplier"):
				if doc.meta.get_field(fname) and doc.get(fname):
					display = doc.get(fname)
					break
			self.party_display_name = display

	def _resolve_owner_details(self):
		"""Resolve the share link owner's email and phone for notification templates."""
		if not self.share_link_owner:
			return
		user = frappe.get_cached_doc("User", self.share_link_owner)
		self.share_link_owner_email = user.email
		# Phone may be on the user or their linked Contact.
		phone = user.phone or user.mobile_no
		if not phone:
			# Try the user's primary contact.
			contact = frappe.get_all(
				"Contact",
				filters={"user": user.name},
				pluck="name",
				limit=1,
			)
			if contact:
				phones = frappe.get_all(
					"Contact Phone",
					filters={"parent": contact[0], "is_primary_phone": 1},
					pluck="phone",
					limit=1,
				)
				if phones:
					phone = phones[0]
		self.share_link_owner_phone = phone or ""

	def _capture_ip_and_geo(self):
		"""Capture IP address and geolocation using native Frappe functions."""
		settings = frappe.get_cached_doc("DocShare Settings", "DocShare Settings")
		if not settings.track_ip_address:
			return

		self.ip_address = getattr(frappe.local, "request_ip", None) or "127.0.0.1"

		# Geolocation via Frappe's bundled GeoLite2.
		try:
			from frappe.sessions import get_geo_from_ip

			geo = get_geo_from_ip(self.ip_address)
			if geo:
				self.country = geo.get("country", {}).get("names", {}).get("en", "")
				city_data = geo.get("city", {})
				self.city = city_data.get("names", {}).get("en", "")
		except Exception:
			pass

		# User agent.
		if frappe.request:
			self.user_agent = frappe.get_request_header("User-Agent", "")
