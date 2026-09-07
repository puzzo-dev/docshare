// Copyright (c) 2026, I-Varse Technologies NG
// License: MIT. See LICENSE
//
// Adds a "Share Document" menu item to every non-single saved document form.
// Only shown where the user has read/print permission. The popup lets the
// user set expiry and max views, then generates a public share link. Print
// format and letterhead are deliberately NOT selectable — the shared view
// always renders with the document's own defaults, so the customer sees it
// exactly as it prints. Native Frappe printing behavior is preserved.

frappe.provide("docshare");

docshare.ShareDialog = class ShareDialog {
	constructor(frm) {
		this.frm = frm;
		this.dialog = null;
	}

	show() {
		const frm = this.frm;
		if (!frm || !frm.doc || frm.doc.__islocal) {
			frappe.msgprint(__("Please save the document before sharing."));
			return;
		}
		if (frappe.get_meta(frm.doctype).issingle) {
			frappe.msgprint(__("Single DocTypes cannot be shared."));
			return;
		}

		this.fetch_context().then(() => this.build_dialog());
	}

	fetch_context() {
		// One round trip. These three reads are independent and all concern the
		// same document, but they used to be chained — each waiting on the one
		// before — so the popup could not open until three sequential requests
		// had completed.
		return frappe.call({
			method: "docshare.api.get_share_context",
			args: { doctype: this.frm.doctype, docname: this.frm.doc.name },
			callback: (r) => {
				const ctx = r.message || {};
				const o = ctx.options || {};
				// Default to enabled if the call ever returns nothing, so the
				// feature degrades to its previous behaviour rather than vanishing.
				this.linked_docs_enabled = o.share_linked_documents === undefined
					? true
					: !!o.share_linked_documents;
				this.default_expiry_days = o.default_expiry_days;
				this.existing_links = ctx.existing_links || [];
				this.linked_documents = this.linked_docs_enabled
					? (ctx.linked_documents || [])
					: [];
			},
		});
	}

	refresh_existing_links() {
		// After generating or revoking, only the link list can have changed.
		return frappe.call({
			method: "docshare.api.list_share_links",
			args: { doctype: this.frm.doctype, docname: this.frm.doc.name },
			callback: (r) => {
				this.existing_links = r.message || [];
			},
		});
	}

	build_dialog() {
		const linked_fields = this.linked_docs_enabled
			? [
					{
						fieldtype: "Section Break",
						label: __("Linked Documents"),
					},
					{
						fieldtype: "HTML",
						fieldname: "linked_docs_html",
					},
			  ]
			: [];

		const fields = [
			{
				fieldtype: "Section Break",
				label: __("Existing Links"),
			},
			{
				fieldtype: "HTML",
				fieldname: "existing_links_html",
			},
			{
				fieldtype: "Section Break",
				label: __("Limits"),
			},
			{
				fieldtype: "Select",
				fieldname: "expiry_preset",
				label: __("Expiry"),
				options: ["Never", "1 day", "7 days", "30 days", "Custom"],
				default: this.default_expiry_preset(),
				onchange: () => this.toggle_custom_expiry(),
			},
			{
				fieldtype: "Datetime",
				fieldname: "expires_on",
				label: __("Custom Expiry"),
				default: this.default_expiry_preset() === "Custom"
					? this.custom_expiry_default()
					: null,
				depends_on: (doc) => doc.expiry_preset === "Custom",
			},
			{
				fieldtype: "Column Break",
			},
			{
				fieldtype: "Int",
				fieldname: "max_views",
				label: __("Max Views"),
				description: __("Leave empty for unlimited views."),
			},
			...linked_fields,
			{
				fieldtype: "Section Break",
			},
			{
				fieldtype: "HTML",
				fieldname: "result_html",
			},
		];

		this.dialog = new frappe.ui.Dialog({
			title: __("Share Document"),
			fields: fields,
			size: "large",
		});

		// Scope for the dialog's own CSS — see docshare.bundle.css. Long share
		// URLs and long linked-document names are the two things that can widen
		// this dialog, and neither should ever produce a horizontal scrollbar.
		this.dialog.$wrapper.addClass("docshare-dialog");

		this.dialog.set_primary_action(__("Generate Link"), () => this.generate_link());
		this.render_existing_links();
		if (this.linked_docs_enabled) this.render_linked_documents();
		this.dialog.show();
	}

	toggle_custom_expiry() {
		// The depends_on callback handles visibility; nothing else needed.
	}

	render_linked_documents() {
		const wrapper = this.dialog.get_field("linked_docs_html").$wrapper;
		if (!this.linked_documents || !this.linked_documents.length) {
			wrapper.html(`<p class="text-muted small">${__("No linked documents found.")}</p>`);
			return;
		}
		const rows = this.linked_documents
			.map(
				(ld) => `
			<label class="docshare-linked-doc-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;min-width:0;overflow-wrap:anywhere;">
				<input type="checkbox" class="docshare-linked-check"
					data-doctype="${frappe.utils.escape_html(ld.linked_doctype)}"
					data-docname="${frappe.utils.escape_html(ld.linked_docname)}">
				<span class="small">${frappe.utils.escape_html(ld.linked_doctype)}: ${frappe.utils.escape_html(ld.linked_docname)}</span>
			</label>`
			)
			.join("");
		wrapper.html(`
			<p class="text-muted small" style="margin-bottom:6px;">${__("Select linked documents to share alongside:")}</p>
			<div>${rows}</div>
		`);
		// No change handler: the checkboxes are the state, and selection is read
		// from them at submit. Tracking it incrementally in an array drifted from
		// what the user could see — re-checking a box pushed a second copy of the
		// same document, so it rendered twice in the share view, and unchecking
		// matched on docname alone, so it could drop a different doctype that
		// happened to share a name.
	}

	get_selected_linked_docs() {
		const wrapper = this.dialog.get_field("linked_docs_html").$wrapper;
		return wrapper
			.find(".docshare-linked-check:checked")
			.map((_, cb) => ({
				linked_doctype: cb.dataset.doctype,
				linked_docname: cb.dataset.docname,
			}))
			.get();
	}

	render_existing_links() {
		const wrapper = this.dialog.get_field("existing_links_html").$wrapper;
		if (!this.existing_links.length) {
			wrapper.html(`<p class="text-muted small">${__("No active share links yet.")}</p>`);
			return;
		}
		const rows = this.existing_links
			.map(
				(l) => `
			<div class="docshare-link-row" style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #eee;">
				<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;margin-right:8px;">
					<a href="${frappe.utils.escape_html(l.url)}" target="_blank" rel="noopener" class="small" style="word-break:break-all;">${frappe.utils.escape_html(l.url)}</a>
					<br><span class="text-muted small">${this.describe_link(l)}</span>
				</div>
				<button class="btn btn-xs btn-danger docshare-revoke" data-name="${frappe.utils.escape_html(l.name)}">${__("Revoke")}</button>
			</div>`
			)
			.join("");
		wrapper.html(`<div>${rows}</div>`);
		wrapper.find(".docshare-revoke").on("click", (e) => this.revoke_link(e));
	}

	describe_link(l) {
		const parts = [];
		if (l.expires_on) parts.push(__("expires {0}", [frappe.datetime.str_to_user(l.expires_on)]));
		if (l.max_views) parts.push(__("{0}/{1} views", [l.view_count || 0, l.max_views]));
		if (!parts.length) parts.push(__("no limits"));
		return parts.join(" · ");
	}

	revoke_link(e) {
		const name = $(e.currentTarget).data("name");
		frappe.confirm(__("Revoke this share link?"), () => {
			frappe.call({
				method: "docshare.api.revoke_share_link",
				args: { name: name },
				callback: () => {
					frappe.show_alert({ message: __("Link revoked"), indicator: "green" });
					this.refresh_existing_links().then(() => this.render_existing_links());
				},
			});
		});
	}

	generate_link() {
		const values = this.dialog.get_values();
		if (!values) return;

		const expires_on = this.compute_expiry(values);
		frappe.call({
			method: "docshare.api.create_share_link",
			args: {
				doctype: this.frm.doctype,
				docname: this.frm.doc.name,
				expires_on: expires_on,
				max_views: values.max_views || null,
				linked_documents: this.linked_docs_enabled ? this.get_selected_linked_docs() : [],
			},
			callback: (r) => {
				if (r.message) this.show_result(r.message);
				this.refresh_existing_links().then(() => this.render_existing_links());
			},
		});
	}

	default_expiry_preset() {
		// DocShare Settings > Default Expiry Days drives which preset opens
		// selected. 0 means "never expires"; a value with no matching preset
		// falls through to Custom, which compute_expiry() then resolves.
		const days = this.default_expiry_days;
		if (days === undefined || days === null || days === "") return "7 days";
		const n = parseInt(days, 10);
		if (isNaN(n)) return "7 days";
		if (n <= 0) return "Never";
		return { 1: "1 day", 7: "7 days", 30: "30 days" }[n] || "Custom";
	}

	custom_expiry_default() {
		const n = parseInt(this.default_expiry_days, 10);
		if (isNaN(n) || n <= 0) return null;
		return moment(frappe.datetime.now_datetime(), frappe.defaultDatetimeFormat)
			.add(n, "days")
			.format(frappe.defaultDatetimeFormat);
	}

	compute_expiry(values) {
		const preset = values.expiry_preset;
		if (preset === "Never") return null;
		if (preset === "Custom") return values.expires_on || null;
		const days = { "1 day": 1, "7 days": 7, "30 days": 30 }[preset];
		if (!days) return null;
		// NOT frappe.datetime.add_days(): it ends in moment's bare .format(),
		// which emits ISO-8601 with a UTC offset (2026-09-13T19:30:57+01:00).
		// MariaDB's DATETIME column rejects that with error 1292, so the insert
		// blows up. Emit Frappe's own system format instead.
		return moment(frappe.datetime.now_datetime(), frappe.defaultDatetimeFormat)
			.add(days, "days")
			.format(frappe.defaultDatetimeFormat);
	}

	show_result(url) {
		const wrapper = this.dialog.get_field("result_html").$wrapper;
		wrapper.html(`
			<div style="margin-top:8px;padding:12px;background:#f8f9fa;border:1px solid #e2e8f0;border-radius:6px;">
				<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
					<input class="form-control docshare-url-input" value="${frappe.utils.escape_html(url)}" readonly style="flex:1;min-width:0;">
					<button class="btn btn-sm btn-primary docshare-copy">${__("Copy")}</button>
					<a class="btn btn-sm btn-default" href="${frappe.utils.escape_html(url)}" target="_blank" rel="noopener">${__("Open")}</a>
				</div>
				<p class="text-muted small" style="margin:0;">${__("Anyone with this link can view the printed document without logging in.")}</p>
			</div>
		`);
		wrapper.find(".docshare-copy").on("click", (e) => this.copy_url(e, url));
		wrapper.find(".docshare-url-input").on("click", (e) => $(e.currentTarget).select());
	}

	copy_url(e, url) {
		const btn = $(e.currentTarget);
		if (navigator.clipboard && navigator.clipboard.writeText) {
			navigator.clipboard
				.writeText(url)
				.then(() => this.flash_copied(btn))
				.catch(() => this.fallback_copy(url, btn));
		} else {
			this.fallback_copy(url, btn);
		}
	}

	flash_copied(btn) {
		const original = btn.html();
		btn.html(__("Copied!"));
		setTimeout(() => btn.html(original), 1500);
	}

	fallback_copy(url, btn) {
		const tmp = document.createElement("textarea");
		tmp.value = url;
		tmp.style.position = "fixed";
		tmp.style.opacity = "0";
		document.body.appendChild(tmp);
		tmp.select();
		try {
			document.execCommand("copy");
			this.flash_copied(btn);
		} catch (err) {
			frappe.msgprint(__("Copy failed — please copy the link manually."));
		}
		document.body.removeChild(tmp);
	}
};

// ---------------------------------------------------------------------------
// Menu injection
// ---------------------------------------------------------------------------
//
// `frappe.ui.form.on("*")` does NOT work: ScriptManager.get_handlers() looks up
// frappe.ui.form.handlers[doctype][event] by the form's real doctype, so a
// handler registered under the literal key "*" is never found. The supported
// way to hook every form is the global `form-refresh` jQuery event, which
// Form.render_form() fires right after refresh_header() has rebuilt the menu
// (see frappe/public/js/frappe/form/form.js). Because it runs after the
// rebuild, our item survives the toolbar's clear_menu() on every refresh.

docshare.add_share_menu = function (frm) {
	if (!frm || !frm.page || !frm.doc || !frm.doctype) return;
	if (frm.doc.__islocal || frm.is_new()) return;

	const meta = frappe.get_meta(frm.doctype);
	if (!meta || meta.issingle || meta.istable) return;

	// Mirror the toolbar's own gate for the Print item.
	if (!frappe.model.can_print(null, frm)) return;

	// add_dropdown_item() already dedups by label, but check explicitly so we
	// never re-bind the click handler on a repeated refresh.
	if (frm.page.menu && frm.page.menu.find(".docshare-menu-item").length) return;

	const $item = frm.page.add_menu_item(
		__("Share Document"),
		() => new docshare.ShareDialog(frm).show(),
		true
	);
	if ($item) $item.addClass("docshare-menu-item");

	// Toolbar.refresh() decides whether to show the menu button *before* we add
	// our item, so on a doctype whose menu would otherwise be empty the group
	// stays hidden. Re-show it now that there is at least one entry.
	frm.page.show_menu();
};

$(document).on("form-refresh", function (e, frm) {
	docshare.add_share_menu(frm);
});
