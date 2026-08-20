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
// An item with NO listing is not a dead end: with the user's say-so, api.enrich_item
// registers one locally from the item's own data (see item_listing.py) and the run
// proceeds against it. Repeat presses reuse that row and never edit it.
//
// Loaded via doctype_js in this app's hooks.py — no change to the Item doctype
// or to alaiy_os itself.
//
// Wrapped in an IIFE so NOTHING is declared at top level. Desk scripts — both
// `app_include_js` and the per-doctype `doctype_js` — are evaluated by injecting a
// <script> into the page, i.e. into ONE shared global scope. The Shopify listing
// agent ships near-identical files declaring the same names, so a top-level
// `const ENRICHED_DOCTYPE` here redeclares theirs the moment both are evaluated in
// one SPA session (open a Shopify listing, then an Amazon one). That is a
// SyntaxError, and it aborts the ENTIRE file before its first statement — the
// button silently never appears while the server-side API keeps working.

(function () {
	frappe.ui.form.on("Item", {
		refresh(frm) {
			// Never let this cost the Item form its other buttons: erpnext and several
			// customer apps register on this same event, and the handlers share a
			// failure. (listing_enrich.js does the same for the connector's.)
			try {
				// Nothing to enrich until the Item is actually saved (its listings are
				// linked to it by name).
				if (frm.is_new()) return;
				if (!window.alaiy || !alaiy.amazon_listing_agent) return;

				alaiy.amazon_listing_agent.get().then((agent) => {
					// The button now has a side effect — it can register a listing —
					// so it must not be offered when the agent is switched off and the
					// run it leads to would throw.
					if (!agent) return;
					frm.add_custom_button(__("Enrich Amazon Listing"), () => enrich(frm), __("Amazon"));
				});
			} catch (e) {
				// eslint-disable-next-line no-console
				console.error("Amazon listing agent: could not add its Item button", e);
			}
		},
	});

	function enrich(frm) {
		frappe.db
			.get_list("Amazon Product Listing", {
				filters: { product: frm.doc.name },
				fields: ["name", "marketplace", "listing_status"],
				limit: 50,
			})
			.then((listings) => {
				if (!listings || !listings.length) {
					offer_to_create(frm);
					return;
				}
				if (listings.length === 1) {
					go(listings[0].name);
					return;
				}
				pick(listings);
			});
	}

	// An item nobody has listed on Amazon yet. The whole agent is keyed on a listing —
	// it reads the listing's own fields, and writes its enrichment against the sku — so
	// there is a row to register before a run can start. It is registered locally and
	// says so (marked incomplete, never synced); nothing is sent to Amazon here, and
	// the copy still only reaches Amazon when a reviewer approves it.
	function offer_to_create(frm) {
		frappe.confirm(
			__(
				"This item has no Amazon Product Listing yet. Create one from this item and enrich it?<br><br>The listing is created here only — marked <b>incomplete</b>, with nothing sent to Amazon."
			),
			() => {
				frappe.call({
					method: "alaiy_os_agent_amazon_listing.api.enrich_item",
					args: { item_code: frm.doc.name },
					freeze: true,
					freeze_message: __("Preparing the listing…"),
					callback: (r) => {
						const result = r.message || {};
						if (!result.sku) return;
						if (result.created) {
							frappe.show_alert({
								message: __("Amazon Product Listing {0} created.", [result.sku]),
								indicator: "green",
							});
						}
						go(result.sku);
					},
					// A server-side refusal (no connector, a variant template, a sku
					// that belongs to another item) raises its own dialog with the
					// reason — there is nothing useful to add on top of it.
				});
			}
		);
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
})();
