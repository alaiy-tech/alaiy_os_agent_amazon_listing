// What every desk surface needs before it can run the Amazon listing agent: the
// agent itself, and the dialog fields its tools declare.
//
// Shared so the single-listing form (listing_enrich.js) and the list-view bulk
// action (listing_bulk_enrich.js) can't drift on which toggles they offer or what
// the payload looks like. Nothing here names an agent_id or a tool.
//
// EVERYTHING lives inside the IIFE, and the only thing that escapes is
// `alaiy.amazon_listing_agent`. Two reasons, both learned the hard way:
//
//   1. Files loaded through `app_include_js` share ONE top-level script scope. The
//      Shopify listing agent ships a file of this same name that declares
//      `const AGENT_METHOD` and `let _agent_promise` at top level. A second
//      top-level `const AGENT_METHOD` here is a redeclaration, and that is a
//      SyntaxError — which aborts the WHOLE file before its first statement runs.
//      The symptom is vicious: the file is present, served, and 200s, but
//      `alaiy.amazon_listing_agent` is undefined and every surface silently hides
//      its button, while the server-side API keeps working perfectly.
//   2. The namespace is `alaiy.amazon_listing_agent`, not `alaiy.listing_agent`,
//      because the Shopify app owns the latter. Namespacing the object is not
//      enough on its own — see (1).
//
// So: no top-level declarations in this file, ever.

(function () {
	frappe.provide("alaiy.amazon_listing_agent");

	const AGENT_METHOD = "alaiy_os_agent_amazon_listing.api.get_listing_agent";

	// Cached for the session: list and form refreshes fire often, and the agent only
	// changes on a migrate or when an admin disables it — both a reload away.
	let agent_promise = null;

	// The agent, or null when it is disabled — callers hide their entry point rather
	// than offer a run that would throw.
	alaiy.amazon_listing_agent.get = function () {
		if (!agent_promise) {
			agent_promise = frappe.xcall(AGENT_METHOD).catch(() => null);
		}
		return agent_promise;
	};

	// One field per input option the agent's tools declare, plus the free-text notes
	// every surface offers.
	alaiy.amazon_listing_agent.option_fields = function (agent) {
		const fields = (agent.input_options || []).map((opt) => ({
			fieldname: opt.fieldname,
			fieldtype: opt.fieldtype || "Check",
			label: __(opt.label || opt.fieldname),
			description: opt.description ? __(opt.description) : undefined,
			default: opt.default === undefined ? 0 : opt.default,
		}));

		fields.push({
			fieldname: "notes",
			fieldtype: "Small Text",
			label: __("Notes for the agent"),
			description: __(
				"Optional — condition, provenance, or anything the listing data and photos won't capture."
			),
		});

		return fields;
	};

	// Dialog values -> the arguments the agent takes. Checks always go through so the
	// agent sees an explicit false; everything else only when the admin filled it in.
	alaiy.amazon_listing_agent.options_from = function (fields, values) {
		const options = {};
		fields.forEach((f) => {
			if (f.fieldtype === "Check") {
				options[f.fieldname] = !!values[f.fieldname];
			} else if (values[f.fieldname]) {
				options[f.fieldname] = values[f.fieldname];
			}
		});
		return options;
	};
})();
