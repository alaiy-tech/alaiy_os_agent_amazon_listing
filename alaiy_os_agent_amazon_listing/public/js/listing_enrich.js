// Adds an "Enrich Listing" button to the Amazon Product Listing form, which runs this
// site's Amazon listing agent against the listing and links to the resulting
// Amazon Enriched Listing for review.
//
// It sits on Amazon Product Listing because that is the doctype the agent actually reads.
// The Item form gets a lighter entry point that deep-links to the run-amazon-agent
// page instead (public/js/item_enrich.js).
//
// Nothing here knows an agent_id or a toggle name. Both come from
// api.get_listing_agent via public/js/listing_agent.js, which the list view's bulk
// action shares — so both surfaces offer exactly the same toggles.

const ENRICHED_DOCTYPE = "Amazon Enriched Listing";
const POLL_INTERVAL_MS = 3000;
// Runs are bounded by the agent's max_turns, but a stuck worker must not leave the
// form polling forever.
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

// This doctype belongs to the Amazon connector, which registers its own `refresh`
// handler for "Push Update to Amazon", "Sync from Amazon" and "End Listing".
// Frappe runs every registered handler for an event in sequence, in installed-app
// order, and an exception in one stops the ones behind it — so a throw in here can
// silently delete the connector's buttons. Nothing in this handler is allowed to
// escape: the agent's own button is worth less than the connector's still working.
frappe.ui.form.on("Amazon Product Listing", {
	refresh(frm) {
		try {
			add_enrich_buttons(frm);
		} catch (e) {
			// eslint-disable-next-line no-console
			console.error("Amazon listing agent: could not add its buttons", e);
		}
	},
});

function add_enrich_buttons(frm) {
	// Nothing to enrich until the listing exists — the agent looks it up by name.
	if (frm.is_new()) return;

	// `alaiy.amazon_listing_agent` comes from app_include_js. If that asset has not
	// been built yet, reaching through it throws — hence the guard as well as the
	// try/catch above.
	if (window.alaiy && alaiy.amazon_listing_agent) {
		alaiy.amazon_listing_agent.get().then((agent) => {
			if (!agent) return;
			frm.add_custom_button(__("Enrich Listing"), () => prompt_and_run(frm, agent));
		});
	}

	// Only offer the review shortcut once there is something to review.
	frappe.db.exists(ENRICHED_DOCTYPE, frm.doc.name).then((exists) => {
		if (!exists || frm.__listing_review_btn) return;
		frm.__listing_review_btn = true;
		frm.add_custom_button(__("View Enriched Listing"), () =>
			frappe.set_route("Form", ENRICHED_DOCTYPE, frm.doc.name)
		);
	});
}

function prompt_and_run(frm, agent) {
	const fields = alaiy.amazon_listing_agent.option_fields(agent);

	const d = new frappe.ui.Dialog({
		title: __("Enrich {0}", [frm.doc.name]),
		fields: fields,
		primary_action_label: __("Run Agent"),
		primary_action(values) {
			d.hide();

			const payload = {
				sku: frm.doc.name,
				...alaiy.amazon_listing_agent.options_from(fields, values),
			};

			start_run(frm, agent.agent_id, payload);
		},
	});
	d.show();
}

function start_run(frm, agent, payload) {
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline(__("Enrichment queued…"));

	frappe.call({
		method: "alaiy_os.api.agents.run_agent",
		args: { agent: agent, payload: payload },
		freeze: true,
		freeze_message: __("Starting enrichment…"),
		callback: (r) => {
			const run = r.message && r.message.run;
			if (!run) {
				frm.dashboard.clear_headline();
				frappe.msgprint({
					title: __("Could not start"),
					message: __("The agent did not return a run id."),
					indicator: "red",
				});
				return;
			}
			poll(frm, run, Date.now());
		},
		// frappe.call shows its own dialog for server errors (agent missing or
		// disabled, no permission) — just drop the headline.
		error: () => frm.dashboard.clear_headline(),
	});
}

function poll(frm, run, started_at) {
	if (Date.now() - started_at > POLL_TIMEOUT_MS) {
		frm.dashboard.clear_headline();
		frappe.msgprint({
			title: __("Still running"),
			message: __(
				"Run {0} has not finished yet. It will keep going in the background — open it to check.",
				[run]
			),
			indicator: "orange",
		});
		return;
	}

	frappe.call({
		method: "alaiy_os.api.agents.get_run",
		args: { run: run },
		callback: (r) => {
			const data = r.message || {};

			if (data.status === "Success") {
				frm.dashboard.clear_headline();
				frappe.show_alert({ message: __("Enrichment complete"), indicator: "green" });
				show_result(frm, run);
			} else if (data.status === "Failed") {
				frm.dashboard.clear_headline();
				frappe.msgprint({
					title: __("Enrichment failed"),
					message: `<pre style="white-space:pre-wrap;">${frappe.utils.escape_html(
						data.error || __("No error detail was recorded.")
					)}</pre>`,
					indicator: "red",
				});
			} else {
				frm.dashboard.set_headline(__("Enrichment {0}…", [data.status || "running"]));
				setTimeout(() => poll(frm, run, started_at), POLL_INTERVAL_MS);
			}
		},
		error: () => setTimeout(() => poll(frm, run, started_at), POLL_INTERVAL_MS),
	});
}

function show_result(frm, run) {
	const listing = `/app/amazon-enriched-listing/${encodeURIComponent(frm.doc.name)}`;
	const run_url = `/app/os-agent-run/${encodeURIComponent(run)}`;

	frappe.msgprint({
		title: __("Enrichment complete"),
		indicator: "green",
		message: `
			<p>${__("The enriched listing is saved in <b>Needs Review</b> status.")}</p>
			<p>
				<a href="${listing}" class="btn btn-primary btn-sm">${__("Review listing")}</a>
				&nbsp;
				<a href="${run_url}" class="btn btn-default btn-sm">${__("View run")}</a>
			</p>`,
	});

	// Surface the "View Enriched Listing" button without a manual reload.
	frm.__listing_review_btn = false;
	frm.refresh();
}
