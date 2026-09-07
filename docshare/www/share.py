# Copyright (c) 2026, I-Varse Technologies NG
# License: MIT. See LICENSE

"""Server-rendered guest view for /share/<token>.

The token is supplied by Frappe's dynamic route resolver via
`frappe.form_dict.token`. The page renders the full-bleed print HTML
with a minimal toolbar (Print / Download PDF). No Desk chrome is shown.
"""

import frappe
from frappe import _

from docshare.api import (
	_validate_guest_token,
	_build_share_url,
	_render_print_html,
	_increment_view_count,
	_render_linked_document_html,
	_linked_document_renderable,
	cached_print_fragment,
	enforce_guest_rate_limit,
)

# Standalone full-bleed page — no cache, no base template wrapping.
no_cache = 1


def get_context(context):
	"""Build the Jinja context for the share page."""
	context.no_cache = 1
	context.title = _("Shared Document")
	context.print_css = _get_print_css()
	# V4 mitigation. The page renders a document's print HTML verbatim, and that
	# HTML carries whatever an authenticated user put in a Text Editor field —
	# on a page served to anonymous visitors, same-origin with the desk. A
	# script-src nonce lets this page's own one-line print handler run while any
	# <script> or inline handler arriving inside the document is inert.
	# style-src is deliberately unrestricted: print formats are built on inline
	# styles, and restricting them would break every letterhead.
	context.csp_nonce = frappe.generate_hash(length=24)
	context.show_sidebar = False
	context.include_sidebar = False
	context.add_breadcrumbs = False

	# The page renders one print format per shared document, so throttle it on
	# the same counter the API endpoints use before doing any of that work.
	enforce_guest_rate_limit("view")

	token = frappe.form_dict.get("token") or ""
	token = str(token).strip()

	if not token:
		context.invalid = True
		context.message = _("This share link is no longer available.")
		return context

	try:
		doc = _validate_guest_token(token)
	except frappe.ValidationError:
		context.invalid = True
		context.message = _("This share link is no longer available.")
		return context

	# Branding follows the document's own company, then the site's setup.
	# Resolve the company once and hand it to both: each helper used to work it
	# out for itself, costing two meta lookups and two Company reads to render
	# one header line.
	company = _get_company(doc.ref_doctype, doc.ref_docname)
	context.brand_logo = _get_brand_logo(company)
	context.brand_name = _get_brand_name(company)
	context.favicon = _get_favicon(company)

	# One embeddable fragment per document — never a whole printview page.
	context.documents = [
		cached_print_fragment(
			doc.ref_doctype, doc.ref_docname, doc.print_format,
			doc.letterhead, doc.no_letterhead,
			lambda: _render_print_html(doc),
		)
	]
	context.token = token
	context.invalid = False
	context.pdf_url = f"/api/method/docshare.api.download_shared_pdf?token={token}"
	context.share_url = _build_share_url(token)

	# Linked documents render the same way, each in its own print format.
	if doc.linked_documents:
		for ld in doc.linked_documents:
			if not _linked_document_renderable(ld):
				continue
			context.documents.append(
				cached_print_fragment(
					ld.linked_doctype, ld.linked_docname, ld.print_format,
					ld.letterhead, ld.no_letterhead,
					lambda ld=ld: _render_linked_document_html(ld),
				)
			)

	# Print formats legitimately style bare `body` (Avril's sets font-size on
	# "body, .print-format, table, ..."). Injected inline they would land after
	# the page's own rules and restyle the shell, so hoist every fragment's CSS
	# into <head> *before* it, where the page stylesheet always wins.
	context.document_styles = "\n".join(d["styles"] for d in context.documents if d.get("styles"))

	# Increment view count + create view log after a successful render.
	_increment_view_count(doc.name)

	return context


def _get_company(ref_doctype: str, ref_docname: str) -> str | None:
	"""The company the shared document belongs to.

	Most ERPNext transactions carry a ``company`` field; fall back to the
	session/global default so single-company sites still get branding.
	"""
	try:
		meta = frappe.get_meta(ref_doctype)
		if meta.has_field("company"):
			company = frappe.db.get_value(ref_doctype, ref_docname, "company")
			if company:
				return company
	except Exception:
		pass

	try:
		return frappe.defaults.get_global_default("company")
	except Exception:
		return None


def _get_favicon(company: str | None) -> str | None:
	"""Favicon for the share page, derived rather than hardcoded.

	Website Settings first: that field exists to be a favicon, it is already
	square and small, and it is what the rest of the site shows. A company logo
	is usually a wide lockup that renders as an unreadable smear at 16px, so it
	is only the fallback, ahead of the app logo.

	Nothing is baked in — changing the favicon in Website Settings changes it
	here too, exactly as the header logo already follows setup.
	"""
	try:
		icon = frappe.db.get_single_value("Website Settings", "favicon")
		if icon:
			return icon
	except Exception:
		pass

	# No site favicon configured — fall back to whatever the header is showing,
	# so the tab at least carries the right brand.
	return _get_brand_logo(company)


def _get_brand_logo(company: str | None) -> str | None:
	"""Logo for the share page, preferring the document's own company.

	Company.company_logo first — a customer should see the company that issued
	the document. Otherwise ``get_app_logo``, which resolves Website Settings ->
	Navbar Settings -> the ``app_logo_url`` hook. Nothing is hardcoded, so
	changing the logo anywhere in setup changes it here too.
	"""
	try:
		if company:
			logo = frappe.db.get_value("Company", company, "company_logo")
			if logo:
				return logo
	except Exception:
		pass

	try:
		from frappe.core.doctype.navbar_settings.navbar_settings import get_app_logo

		return get_app_logo() or None
	except Exception:
		# Branding must never take the document down.
		return None


def _get_brand_name(company: str | None) -> str:
	"""Company name shown beside the logo, and the alt text for it."""
	try:
		if company:
			return frappe.db.get_value("Company", company, "company_name") or company
	except Exception:
		pass

	try:
		return (
			frappe.get_website_settings("app_name")
			or frappe.get_system_settings("app_name")
			or "Frappe"
		)
	except Exception:
		return "Frappe"


def _get_print_css() -> str:
	"""URL of frappe's print stylesheet, included once by the share page.

	Each printview fragment would otherwise carry its own <link> to this.
	"""
	try:
		from frappe.utils.jinja_globals import bundled_asset

		return bundled_asset("print.bundle.css")
	except Exception:
		return ""
