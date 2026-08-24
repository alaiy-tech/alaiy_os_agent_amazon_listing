// "Enrich Amazon Listings" in the Item list view: tick a batch of items, pick the
// toggles once, and every one is enriched on the workers — including the items that
// have no Amazon listing yet, which get one registered from their own data first
// (api.bulk_enrich_items -> item_listing.py -> the same batch machinery as the
// listing list view's action).
//
// The Item list is where the items that are NOT on Amazon are found, which is the
// whole reason this surface exists: the Amazon Product Listing list can only ever
// offer the listings that already exist.
//
// MUTATED onto whatever is already registered, never assigned over it — and mutated
// on the existing object rather than a copy of it. `frappe.listview_settings["Item"]`
// is one plain object per doctype and this one is crowded: erpnext's item_list.js
// creates it (with `add_fields`, `get_indicator`, formatters), and at least two other
// apps here hang an `onload` off it — one of them by capturing the object in a `const`
// and mutating that reference. Replacing the object would strand that reference, so
// its bulk action would vanish the moment this app is installed.
//
// Wrapped in an IIFE so NOTHING is declared at top level. Desk scripts — both
// `app_include_js` and the per-doctype list scripts — are evaluated by injecting a
// <script> into the page, i.e. into ONE shared global scope. The Shopify listing agent
// ships near-identical files declaring the same names, so a top-level `const DOCTYPE`
// here redeclares theirs the moment both are evaluated in one SPA session. That is a
// SyntaxError, and it aborts the ENTIRE file before its first statement — the action
// silently never appears while the server-side API keeps working.

frappe.listview_settings["Item"] = frappe.listview_settings["Item"] || {};

(function () {
	const settings = frappe.listview_settings["Item"];
	const prior_onload = settings.onload;

	settings.onload = function (listview) {
		// Theirs first, and never behind our failure: an exception on our side must
		// not cost erpnext or another app its buttons.
		if (typeof prior_onload === "function") {
			prior_onload.call(this, listview);
		}

		// `alaiy.amazon_listing_agent` comes from app_include_js. If that asset has not
		// been built yet, reaching through it would throw here and take the rest of the
		// list view with it.
		if (!window.alaiy || !alaiy.amazon_listing_agent) return;

		alaiy.amazon_listing_agent.get().then((agent) => {
			if (!agent) return;
			// Lands in the standard Actions menu, which the list view reveals once
			// rows are ticked.
			listview.page.add_actions_menu_item(
				__("Enrich Amazon Listings"),
				() => prompt_and_run(listview, agent),
				false
			);
		});
	};

	function prompt_and_run(listview, agent) {
		const item_codes = listview.get_checked_items(true);
		if (!item_codes.length) {
			frappe.msgprint({
				title: __("Nothing selected"),
				message: __("Tick the items you want to enrich, then choose Enrich Amazon Listings again."),
				indicator: "orange",
			});
			return;
		}

		// How many listings this run will have to register is the one thing about an
		// item-side bulk that is not obvious from the selection, and it is the part
		// that writes to the catalog — so it is said before the dialog, not after.
		count_listed(item_codes).then((listed) => open_dialog(agent, item_codes, listed));
	}

	// How many of these items already have an Amazon Product Listing.
	//
	// Asked of the server, NOT of `frappe.db.get_list`, which hard-codes type: "GET" —
	// the whole `product in [...]` filter would go into the request line, and this
	// selection is routinely hundreds of item codes. Past a hundred or so nginx
	// rejects it with "Request Line is too large" (400) before the dialog can open,
	// and the `.catch` below would swallow it: the count would silently vanish exactly
	// when the selection is big enough to need it. frappe.xcall POSTs, so the codes
	// ride in the body. The server also counts the DISTINCT items, with no row limit
	// to truncate it.
	function count_listed(item_codes) {
		return frappe
			.xcall("alaiy_os_agent_amazon_listing.api.count_listed_items", {
				item_codes: item_codes,
			})
			// The endpoint answers null for "cannot tell" (no connector, no read access),
			// which arrives here as undefined — normalised so open_dialog has one case.
			.then((count) => (count === undefined || count === null ? null : count))
			.catch(() => null);
	}

	function open_dialog(agent, item_codes, listed) {
		const to_create = listed === null ? null : item_codes.length - listed;
		let intro = `<p class="text-muted">${__("{0} items selected.", [item_codes.length])}`;
		if (to_create) {
			intro += ` ${__(
				"{0} of them have no Amazon listing yet — one will be registered from the item, marked incomplete, with nothing sent to Amazon.",
				[to_create]
			)}`;
		}
		intro += "</p>";

		alaiy.amazon_listing_agent.bulk_dialog({
			agent: agent,
			title: __("Enrich {0} items", [item_codes.length]),
			intro: intro,
			on_start: (args) =>
				alaiy.amazon_listing_agent.run_bulk(
					"alaiy_os_agent_amazon_listing.api.bulk_enrich_items",
					{ item_codes: item_codes, ...args },
					report
				),
		});
	}

	// What the endpoint could not do. An item dropped from the batch has to be visible:
	// the batch itself has no row for it, so this is the only place it is ever said.
	// The same for an item with several listings, of which exactly one is being run.
	function report(result) {
		const errors = result.errors || {};
		const ambiguous = result.ambiguous || {};
		const resolved = new Set(result.resolved || []);
		const lines = [];

		Object.keys(errors).forEach((item) =>
			lines.push(`<b>${frappe.utils.escape_html(item)}</b>: ${frappe.utils.escape_html(errors[item])}`)
		);
		Object.keys(ambiguous).forEach((item) => {
			// Which of them ran is the server's choice, so it is read back off
			// `resolved` rather than guessed from the order of the list.
			const used = ambiguous[item].filter((sku) => resolved.has(sku));
			lines.push(
				__("{0} has {1} listings; {2} was enriched.", [
					`<b>${frappe.utils.escape_html(item)}</b>`,
					ambiguous[item].length,
					frappe.utils.escape_html(used[0] || ambiguous[item][0]),
				])
			);
		});
		if (!lines.length) return;

		frappe.msgprint({
			title: __("Some items need a look"),
			message: lines.join("<br>"),
			indicator: "orange",
		});
	}
})();
