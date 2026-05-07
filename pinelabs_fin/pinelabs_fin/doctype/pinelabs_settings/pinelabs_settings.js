// Copyright (c) 2026, pine and contributors
// For license information, please see license.txt

const PINELABS_MAPPING_TYPE_TO_DOCTYPE = {
	User: "User",
	Warehouse: "Warehouse",
	Company: "Company",
	"POS Profile": "POS Profile",
};

// Auto-fill reference_doctype on the inline Machine Mappings grid so the
// Dynamic Link picker for reference_name shows the right options as soon as
// the user selects a mapping_type.
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
