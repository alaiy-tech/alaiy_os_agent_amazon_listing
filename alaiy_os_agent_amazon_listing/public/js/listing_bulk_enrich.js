// "Enrich Listings" in the Amazon Product Listing list view: tick a batch of listings,
// pick the toggles once, and every one is enriched on the workers.
//
// The run itself is the backend's job (api.bulk_enrich -> bulk.py). This only
// collects the selection and opens the batch, which shows its own live progress.

frappe.listview_settings["Amazon Product Listing"] = {
	onload(listview) {
		alaiy.amazon_listing_agent.get().then((agent) => {
			if (!agent) return;
			// Lands in the standard Actions menu, which the list view reveals once
			// rows are ticked.
			listview.page.add_actions_menu_item(
				__("Enrich Listings"),
				() => prompt_and_run(listview, agent),
				false
			);
		});
	},
};

function prompt_and_run(listview, agent) {
	const skus = listview.get_checked_items(true);
	if (!skus.length) {
		frappe.msgprint({
			title: __("Nothing selected"),
			message: __("Tick the listings you want to enrich, then choose Enrich Listings again."),
			indicator: "orange",
		});
		return;
	}

	const option_fields = alaiy.amazon_listing_agent.option_fields(agent);
	const fields = [
		{
			fieldtype: "HTML",
			options: `<p class="text-muted">${__("{0} listings selected.", [skus.length])}</p>`,
		},
		...option_fields,
		{ fieldtype: "Section Break", label: __("Batch"), collapsible: 1 },
		{
			fieldname: "skip_enriched",
			fieldtype: "Check",
			label: __("Skip already enriched"),
			description: __("Leave listings that already have an enriched result untouched."),
			default: 0,
		},
		{
			fieldname: "batch_size",
			fieldtype: "Int",
			label: __("Listings per job"),
			default: 5,
			description: __(
				"How many listings each background job handles. Lower spreads the work over more workers; higher queues fewer jobs."
			),
		},
	];

	const d = new frappe.ui.Dialog({
		title: __("Enrich {0} listings", [skus.length]),
		fields: fields,
		primary_action_label: __("Start"),
		primary_action(values) {
			d.hide();
			start(skus, {
				...alaiy.amazon_listing_agent.options_from(option_fields, values),
				skip_enriched: values.skip_enriched ? 1 : 0,
				batch_size: values.batch_size || 5,
			});
		},
	});
	d.show();
}

function start(skus, args) {
	frappe.call({
		method: "alaiy_os_agent_amazon_listing.api.bulk_enrich",
		args: { skus: skus, ...args },
		freeze: true,
		freeze_message: __("Queueing enrichment…"),
		callback: (r) => {
			const result = r.message || {};
			if (!result.batch) return;

			frappe.show_alert({
				message: __("{0} listings queued across {1} jobs", [result.items, result.jobs]),
				indicator: "green",
			});
			// The batch form follows its own progress, so that is where to land.
			frappe.set_route("Form", "Amazon Listing Bulk Enrich", result.batch);
		},
		// frappe.call raises its own dialog for server errors (no permission, agent
		// disabled) — nothing to add here.
	});
}
