// Copyright (c) 2026, pine and contributors
// For license information, please see license.txt

// ─────────────────────────────────────────────────────────────────────────
// Pinelabs Settings form — Plural API Test Connection button
// ─────────────────────────────────────────────────────────────────────────

frappe.ui.form.on("Pinelabs Settings", {
	refresh(frm) {
		render_plural_test_button(frm);
	},
	plural_client_id(frm) {
		render_plural_test_button(frm);
	},
	enable_payment_links(frm) {
		render_plural_test_button(frm);
	},
});

function render_plural_test_button(frm) {
	const label = __("Test Plural Connection");
	frm.remove_custom_button(label);
	if (!frm.doc.enable_payment_links) return;
	if (!(frm.doc.plural_client_id || "").trim()) return;

	frm.add_custom_button(label, () => run_plural_connection_test(frm));
}

function run_plural_connection_test(frm) {
	if (frm.is_dirty()) {
		frappe.show_alert({
			message: __("Save Pinelabs Settings before testing — unsaved credential changes won't be used."),
			indicator: "orange",
		});
		return;
	}
	frappe.call({
		method: "pinelabs_fin.api.config.test_plural_connection",
		freeze: true,
		freeze_message: __("Contacting Plural..."),
		callback(r) {
			const msg = r.message || {};
			const indicator = msg.success ? "green" : "red";
			const prefix = msg.success ? "✅" : "❌";
			frappe.msgprint({
				title: __("Plural Connection Test"),
				message: `${prefix} ${frappe.utils.escape_html(msg.message || "")}`,
				indicator,
			});
		},
	});
}

const PINELABS_MAPPING_TYPE_TO_DOCTYPE = {
	User: "User",
	Warehouse: "Warehouse",
	Company: "Company",
	"POS Profile": "POS Profile",
};

// ─────────────────────────────────────────────────────────────────────────
// Machine Mappings grid — auto-fill reference_doctype from mapping_type
// ─────────────────────────────────────────────────────────────────────────

frappe.ui.form.on("Pinelabs Machine Mapping", {
	mapping_type(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const target = PINELABS_MAPPING_TYPE_TO_DOCTYPE[row.mapping_type] || "";
		if (row.reference_doctype !== target) {
			frappe.model.set_value(cdt, cdn, "reference_doctype", target);
			// Clear the stale reference_name when the type changes so the
			// Dynamic Link picker doesn't keep a value pointing at the wrong
			// doctype.
			frappe.model.set_value(cdt, cdn, "reference_name", null);
		}
	},
	machine_mappings_add(frm, cdt, cdn) {
		// Fresh row default — User is the most common use case.
		const row = locals[cdt][cdn];
		if (!row.mapping_type) {
			frappe.model.set_value(cdt, cdn, "mapping_type", "User");
		}
	},
});

// ─────────────────────────────────────────────────────────────────────────
// Payable Doctypes grid — Customer Contact Field Mapping cascade
// ─────────────────────────────────────────────────────────────────────────
//
// Cascade rules (per row):
//   doctype_name change       → fetch fields from that doctype, populate
//                               mobile_link_field / email_link_field /
//                               mobile_direct_field / email_direct_field
//                               option lists. Clear all dependents.
//   *_fetch_from change       → clear that field's dependents.
//   *_link_field change       → look up the link target via meta and
//                               auto-fill *_linked_doctype.
//   *_linked_doctype change   → fetch valid mobile/email fields on the
//                               linked doctype, populate *_linked_field.
//   form_render (row open)    → re-populate the option lists for whatever
//                               doctype/linked_doctype is already set, so
//                               existing rows show correct dropdowns.

const PAYABLE_TABLE_FIELD = "payable_doctypes";

const MOBILE_DEPENDENTS = [
	"mobile_direct_field",
	"mobile_link_field",
	"mobile_linked_doctype",
	"mobile_linked_field",
];
const EMAIL_DEPENDENTS = [
	"email_direct_field",
	"email_link_field",
	"email_linked_doctype",
	"email_linked_field",
];
// Cleared on doctype_name change — they're scoped to the source doctype's
// fieldnames, so a switch invalidates them.
const SOURCE_FIELD_DEPENDENTS = [
	"amount_field",
	"status_field",
	"link_payment_entry_field",
];

frappe.ui.form.on("Pinelabs Payable Doctype", {
	doctype_name(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		// Clear everything downstream — old options would be misleading.
		[
			...SOURCE_FIELD_DEPENDENTS,
			...MOBILE_DEPENDENTS,
			...EMAIL_DEPENDENTS,
		].forEach((f) => {
			frappe.model.set_value(cdt, cdn, f, null);
		});
		populate_source_options(frm, cdn, row.doctype_name);
	},

	mobile_fetch_from(frm, cdt, cdn) {
		MOBILE_DEPENDENTS.forEach((f) => {
			frappe.model.set_value(cdt, cdn, f, null);
		});
	},
	email_fetch_from(frm, cdt, cdn) {
		EMAIL_DEPENDENTS.forEach((f) => {
			frappe.model.set_value(cdt, cdn, f, null);
		});
	},

	mobile_link_field(frm, cdt, cdn) {
		autofill_linked_doctype(frm, cdt, cdn, "mobile");
	},
	email_link_field(frm, cdt, cdn) {
		autofill_linked_doctype(frm, cdt, cdn, "email");
	},

	mobile_linked_doctype(frm, cdt, cdn) {
		populate_linked_field_options(frm, cdt, cdn, "mobile");
	},
	email_linked_doctype(frm, cdt, cdn) {
		populate_linked_field_options(frm, cdt, cdn, "email");
	},

	form_render(frm, cdt, cdn) {
		// Repopulate options whenever a row opens so the user sees a
		// pre-filtered dropdown for the values already on the row.
		const row = locals[cdt][cdn];
		if (row.doctype_name) {
			populate_source_options(frm, cdn, row.doctype_name);
		}
		if (row.mobile_linked_doctype) {
			populate_linked_field_options(frm, cdt, cdn, "mobile");
		}
		if (row.email_linked_doctype) {
			populate_linked_field_options(frm, cdt, cdn, "email");
		}
	},
});

function populate_source_options(frm, cdn, doctype) {
	if (!doctype) return;
	frappe.call({
		method: "pinelabs_fin.api.customer_contact.list_doctype_fields",
		args: { doctype },
		callback(r) {
			const data = r.message || {};
			// Existing-section fields driven from the source doctype.
			set_row_options(frm, cdn, "amount_field", data.amount_fields);
			set_row_options(frm, cdn, "status_field", data.status_fields);
			set_row_options(frm, cdn, "link_payment_entry_field", data.reference_fields);
			// Customer Contact section.
			set_row_options(frm, cdn, "mobile_link_field", data.link_fields);
			set_row_options(frm, cdn, "email_link_field", data.link_fields);
			set_row_options(frm, cdn, "mobile_direct_field", data.mobile_fields);
			set_row_options(frm, cdn, "email_direct_field", data.email_fields);
		},
	});
}

function autofill_linked_doctype(frm, cdt, cdn, kind) {
	const row = locals[cdt][cdn];
	const link_field = row[`${kind}_link_field`];
	if (!row.doctype_name || !link_field) {
		// Nothing to auto-fill from. Clear the linked target + field so the
		// row doesn't keep a stale value.
		frappe.model.set_value(cdt, cdn, `${kind}_linked_doctype`, null);
		frappe.model.set_value(cdt, cdn, `${kind}_linked_field`, null);
		return;
	}
	frappe.call({
		method: "pinelabs_fin.api.customer_contact.get_link_field_target",
		args: { doctype: row.doctype_name, fieldname: link_field },
		callback(r) {
			const target = r.message || "";
			// Always reset linked_field — it was scoped to the previous target.
			frappe.model.set_value(cdt, cdn, `${kind}_linked_field`, null);
			if (target) {
				frappe.model.set_value(cdt, cdn, `${kind}_linked_doctype`, target);
			} else {
				// Dynamic Link or non-Link picked — user must enter the
				// linked doctype manually.
				frappe.model.set_value(cdt, cdn, `${kind}_linked_doctype`, null);
			}
		},
	});
}

function populate_linked_field_options(frm, cdt, cdn, kind) {
	const row = locals[cdt][cdn];
	const linked_dt = row[`${kind}_linked_doctype`];
	if (!linked_dt) return;
	frappe.call({
		method: "pinelabs_fin.api.customer_contact.list_doctype_fields",
		args: { doctype: linked_dt },
		callback(r) {
			const data = r.message || {};
			const list = kind === "mobile" ? data.mobile_fields : data.email_fields;
			set_row_options(frm, cdn, `${kind}_linked_field`, list);
		},
	});
}

function set_row_options(frm, cdn, fieldname, items) {
	const grid = frm.fields_dict[PAYABLE_TABLE_FIELD]
		&& frm.fields_dict[PAYABLE_TABLE_FIELD].grid;
	if (!grid) return;
	const row = grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn];
	if (!row) return;

	const safe_items = (items || []).filter((f) => f && f.fieldname);
	const options_str = safe_items.map((f) => f.fieldname).join("\n");
	// Awesomplete entries: `value` is what gets written to the model; `label`
	// is what the user sees. Showing "(Label)" alongside the fieldname makes
	// the dropdown human-readable without changing the stored value.
	const options_arr = safe_items.map((f) => ({
		value: f.fieldname,
		label: f.label && f.label !== f.fieldname
			? `${f.fieldname} — ${f.label}`
			: f.fieldname,
	}));

	console.log(
		"[Pinelabs] set_row_options",
		fieldname,
		"→",
		safe_items.length,
		"items",
	);

	// 1. Stamp options onto the row's docfield clone — read on next render.
	if (row.docfields && Array.isArray(row.docfields)) {
		const df = row.docfields.find((d) => d.fieldname === fieldname);
		if (df) df.options = options_str;
	}

	// 2. Update every live field instance we can find. Different Frappe
	// versions / row-states put the control in different places:
	//   - row.fields_dict       → inline-strip controls (collapsed row)
	//   - row.grid_form         → expanded form-view modal
	//   - row.grid_form.fields  → an array form of the same controls
	const live = [];
	if (row.fields_dict && row.fields_dict[fieldname]) {
		live.push(row.fields_dict[fieldname]);
	}
	if (row.grid_form
		&& row.grid_form.fields_dict
		&& row.grid_form.fields_dict[fieldname]) {
		live.push(row.grid_form.fields_dict[fieldname]);
	}
	if (row.grid_form && Array.isArray(row.grid_form.fields)) {
		const match = row.grid_form.fields.find(
			(f) => f && f.df && f.df.fieldname === fieldname,
		);
		if (match && !live.includes(match)) live.push(match);
	}

	live.forEach((field) => {
		field.df.options = options_str;
		// `set_data` is the canonical Autocomplete API — it pushes the array
		// straight into the underlying Awesomplete instance and works even
		// when the control is already mounted and won't re-read df.options.
		if (typeof field.set_data === "function") {
			try { field.set_data(options_arr); } catch (e) { /* fall through */ }
		}
		if (typeof field.refresh === "function") field.refresh();
	});
}
