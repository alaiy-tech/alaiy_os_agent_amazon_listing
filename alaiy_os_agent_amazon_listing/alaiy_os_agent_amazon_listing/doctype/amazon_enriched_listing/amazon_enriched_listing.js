// Renders the images child table as thumbnails, so a reviewer can see what a
// run actually produced without clicking through a URL per row. The child table
// itself is still there (collapsed) for editing the raw values.
//
// One renderer serves both image tools, because the child table carries both
// shapes they can produce:
//
//   • source_url AND url  -> a before/after pair (a retouched or translated
//                            supplier photo)
//   • url only            -> a single tile captioned with kind/brief
//   • no url              -> a placeholder carrying the row's note, so a failed
//                            image is visible rather than silently missing
//
// So neither tool needs its own form script; each just fills the columns that
// apply to what it produces.

frappe.ui.form.on("Amazon Enriched Listing", {
	refresh(frm) {
		render_image_preview(frm);
		render_product_type_help(frm);
	},
});

// The product type decides whether Amazon will accept ANY update to this listing,
// and approval writes it onto the listing — so a reviewer needs two things next to
// the field: which of Amazon's candidates they can pick from, and how much the
// value already there is worth. A suggested type is Amazon classifying the enriched
// title; a `listing` one is only the previous classification, kept because Amazon
// had nothing to say. Where the two disagree, Needs Review spells it out.
function render_product_type_help(frm) {
	const field = frm.get_field("product_type");
	if (!field) return;

	let suggestions = [];
	try {
		suggestions = JSON.parse(frm.doc.product_type_suggestions || "[]") || [];
	} catch (e) {
		suggestions = [];
	}

	const parts = [];
	if (frm.doc.product_type_source === "listing") {
		parts.push(
			__("Amazon had no answer for the enriched title — the type this listing already published with was kept.")
		);
	} else if (frm.doc.product_type_source === "suggested") {
		parts.push(
			__("Amazon's best match for the enriched title. Approval writes it onto the listing, so confirm it first.")
		);
	} else if (frm.doc.product_type_source === "reviewer") {
		parts.push(__("Set by hand. Approval writes it onto the listing."));
	}
	if (suggestions.length) {
		const options = suggestions
			.map((s) => `<b>${frappe.utils.escape_html(s.product_type)}</b>` +
				(s.display_name && s.display_name !== s.product_type
					? ` (${frappe.utils.escape_html(s.display_name)})`
					: ""))
			.join(", ");
		parts.push(`${__("Amazon's candidates, best match first")}: ${options}`);
	}
	if (!parts.length) return;

	field.set_new_description(parts.join("<br>"));
}

const THUMB = 190;

// A produced image lives in S3 and the object is private, so the url on the row
// identifies it but cannot be rendered. `image_view_links` trades this listing's urls
// for links a browser can load — presigned for an S3 object, unchanged for a supplier
// photo or a local File — and the thumbnails are drawn from those.
function view_links(frm) {
	if (frm.is_new() || !(frm.doc.images || []).length) return Promise.resolve({});
	return frappe
		.call({
			method: "alaiy_os_agent_amazon_listing.api.image_view_links",
			args: { sku: frm.doc.name },
		})
		.then((r) => r.message || {})
		// A failure here costs thumbnails, not the form: fall back to the raw urls,
		// which still render for anything that was not stored privately.
		.catch(() => ({}));
}

function pane(url, label, placeholder, links) {
	const caption = label
		? `<div style="font-size:11px;margin-top:4px;color:var(--text-muted);">${frappe.utils.escape_html(
				label
		  )}</div>`
		: "";

	if (!url) {
		return `
			<div style="width:${THUMB}px;">
				<div style="width:100%;height:${THUMB}px;border:1px dashed var(--border-color);border-radius:4px;
				            display:flex;align-items:center;justify-content:center;text-align:center;
				            color:var(--text-muted);font-size:12px;padding:8px;">
					${frappe.utils.escape_html(placeholder || "no image")}
				</div>
				${caption}
			</div>`;
	}

	const safe = frappe.utils.escape_html((links || {})[url] || url);
	return `
		<div style="width:${THUMB}px;">
			<a href="${safe}" target="_blank" rel="noopener">
				<img src="${safe}" loading="lazy"
				     style="width:100%;height:${THUMB}px;object-fit:contain;background:var(--control-bg);
				            border:1px solid var(--border-color);border-radius:4px;" />
			</a>
			${caption}
		</div>`;
}

function cap(s) {
	return s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : "";
}

function side_text(row) {
	// A note is only worth surfacing when the image is missing — otherwise the
	// thumbnail speaks for itself. The brief, where a tool records one, is
	// what the reviewer needs to judge a produced shot against.
	const parts = [];
	if (!row.url && row.note) parts.push(row.note);
	if (row.brief) parts.push(row.brief);
	if (!parts.length) return "";
	return `<div style="font-size:12px;color:var(--text-muted);white-space:pre-wrap;
	                    word-break:break-word;">${frappe.utils.escape_html(parts.join("\n\n"))}</div>`;
}

function render_image_preview(frm) {
	const field = frm.get_field("images_preview");
	if (!field) return;

	const wrapper = field.$wrapper;
	wrapper.empty();

	const rows = frm.doc.images || [];
	if (!rows.length) {
		wrapper.html(
			`<div style="color:var(--text-muted);font-size:12px;">${__(
				"No images on this listing."
			)}</div>`
		);
		return;
	}

	view_links(frm).then((links) => draw_image_preview(wrapper, rows, links));
}

function draw_image_preview(wrapper, rows, links) {
	const blocks = rows
		.map((row, idx) => {
			const paired = !!row.source_url;
			let result_label = row.kind ? cap(row.kind) : paired ? __("Result") : __("Generated");
			// The first row that has a url is the one approval publishes as the
			// listing's main image, so say which that is.
			if (row.url && rows.findIndex((r) => r.url) === idx) {
				result_label = `${result_label} — ${__("Main image")}`;
			}

			const panes = paired
				? `${pane(row.source_url, __("Source"), "", links)}
				   <div style="padding-top:${THUMB / 2 - 10}px;color:var(--text-muted);font-size:16px;">&rarr;</div>
				   ${pane(row.url, result_label, __("not produced"), links)}`
				: pane(row.url, result_label, __("not produced"), links);

			return `
				<div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:14px;
				            padding-bottom:14px;border-bottom:1px solid var(--border-color);">
					<div style="width:22px;padding-top:${THUMB / 2 - 8}px;color:var(--text-muted);font-size:12px;">
						${idx + 1}
					</div>
					${panes}
					<div style="flex:1;min-width:0;padding-top:2px;">${side_text(row)}</div>
				</div>`;
		})
		.join("");

	wrapper.html(`<div style="margin-top:8px;">${blocks}</div>`);
}
