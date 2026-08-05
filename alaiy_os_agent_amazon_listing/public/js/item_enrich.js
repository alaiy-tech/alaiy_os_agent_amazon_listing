// Adds an "Enrich Amazon Listing" button to the Item form that deep-links to the
// run-amazon-agent page with this item's SKU pre-selected. The page reads
// frappe.route_options on show (see run_amazon_agent.js#apply_route_options) and
// fills the SKU dropdown, so the user lands ready to click "Enrich" — and picks up
// whichever Amazon listing agent the site has registered, so nothing here names one.
//
// The agent is keyed on the seller SKU, not the item_code: an Item can be listed
// under several SKUs (one per marketplace, or a legacy SKU beside a current one),
// and each of those is a separate listing with its own copy to write. So the button
// resolves the item's listings first and only asks when there is more than one.
//
// Loaded via doctype_js in this app's hooks.py — no change to the Item doctype
// or to alaiy_os itself.

frappe.ui.form.on("Item", {
	refresh(frm) {
		// Nothing to enrich until the Item is actually saved (its listings are
		// linked to it by name).
		if (frm.is_new()) return;

		frm.add_custom_button(__("Enrich Amazon Listing"), () => enrich(frm), __("Amazon"));
	},
});

function enrich(frm) {
	frappe.db
		.get_list("Amazon Listing", {
			filters: { product: frm.doc.name },
			fields: ["name", "marketplace", "listing_status"],
			limit: 50,
		})
		.then((listings) => {
			if (!listings || !listings.length) {
				frappe.msgprint({
					title: __("No Amazon listing"),
					message: __(
						"This item has no Amazon Listing, so there is nothing for the agent to enrich. Sync the listing from Amazon first."
					),
					indicator: "orange",
				});
				return;
			}
			if (listings.length === 1) {
				go(listings[0].name);
				return;
			}
			pick(listings);
		});
}

function pick(listings) {
	const d = new frappe.ui.Dialog({
		title: __("Which listing?"),
		fields: [
			{
				fieldname: "sku",
				fieldtype: "Select",
				label: __("SKU"),
				reqd: 1,
				options: listings.map((l) => ({
					value: l.name,
					label: `${l.name} — ${l.marketplace || __("no marketplace")} (${l.listing_status || "?"})`,
				})),
				default: listings[0].name,
			},
		],
		primary_action_label: __("Continue"),
		primary_action(values) {
			d.hide();
			go(values.sku);
		},
	});
	d.show();
}

function go(sku) {
	frappe.route_options = { sku: sku };
	frappe.set_route("run-amazon-agent");
}
