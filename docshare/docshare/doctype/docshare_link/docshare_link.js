// Copyright (c) 2026, I-Varse Technologies NG
// License: MIT. See LICENSE

frappe.ui.form.on("DocShare Link", {
	refresh(frm) {
		frm.trigger("render_share_url");
	},

	render_share_url(frm) {
		const field = frm.get_field("share_url_html");
		if (!field) return;

		const url = frm.doc.__onload && frm.doc.__onload.share_url;
		if (!url || frm.is_new()) {
			field.$wrapper.html(
				`<p class="text-muted small">${__("The share link appears once this record is saved.")}</p>`
			);
			return;
		}

		field.$wrapper.html(`
			<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
				<input class="form-control docshare-url" value="${frappe.utils.escape_html(url)}" readonly
					style="flex:1;min-width:240px;font-family:monospace;">
				<button class="btn btn-sm btn-default docshare-copy">${__("Copy")}</button>
				<a class="btn btn-sm btn-default" href="${frappe.utils.escape_html(url)}"
					target="_blank" rel="noopener">${__("Open")}</a>
			</div>
			<p class="text-muted small" style="margin:6px 0 0;">
				${__("Anyone with this link can view the document without logging in.")}
			</p>
		`);

		field.$wrapper.find(".docshare-url").on("click", (e) => $(e.currentTarget).select());
		field.$wrapper.find(".docshare-copy").on("click", () => {
			frappe.utils.copy_to_clipboard(url);
		});
	},
});
