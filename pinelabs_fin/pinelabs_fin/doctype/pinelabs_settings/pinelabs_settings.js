// Copyright (c) 2026, pine and contributors
// For license information, please see license.txt

const PAYABLE_FIELD_SELECTS = [
	{
		fieldname: "amount_field",
		fieldtypes: ["Currency", "Float", "Int", "Percent"],
	},
	{
		fieldname: "status_field",
		fieldtypes: ["Data", "Select", "Link", "Dynamic Link", "Read Only", "Small Text", "Text"],
	},
	{
		fieldname: "link_payment_entry_field",
		fieldtypes: ["Data", "Link", "Dynamic Link", "Read Only"],
	},
];

frappe.ui.form.on("Pinelabs Settings", {
	setup(frm) {
		frm.set_query("doctype_name", "payable_doctypes", function () {
			return {
				filters: [
					["DocType", "istable", "=", 0],
					["DocType", "issingle", "=", 0],
				],
			};
		});
	},
});

frappe.ui.form.on("Pinelabs Payable Doctype", {
	form_render(frm, cdt, cdn) {
		set_payable_field_options(frm, cdt, cdn);
	},

	doctype_name(frm, cdt, cdn) {
		const row = locals[cdt][cdn];

		for (const config of PAYABLE_FIELD_SELECTS) {
			frappe.model.set_value(cdt, cdn, config.fieldname, "");
		}

		if (row.doctype_name) {
			set_payable_field_options(frm, cdt, cdn);
		}
	},
});

function set_payable_field_options(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const grid = frm.fields_dict.payable_doctypes.grid;

	if (!row.doctype_name) {
		update_payable_grid_options(frm, cdn, {
			amount_field: [""],
			status_field: [""],
			link_payment_entry_field: [""],
		});
		return;
	}

	frappe.model.with_doctype(row.doctype_name, function () {
		const meta = frappe.get_meta(row.doctype_name);
		const options_by_field = {};

		for (const config of PAYABLE_FIELD_SELECTS) {
			options_by_field[config.fieldname] = get_source_field_options(meta, config.fieldtypes);
			clear_missing_field_value(frm, cdt, cdn, row, config.fieldname, options_by_field[config.fieldname]);
		}

		update_payable_grid_options(frm, cdn, options_by_field);
		grid.refresh();
	});
}

function get_source_field_options(meta, allowed_fieldtypes) {
	const options = [""];

	for (const df of meta.fields || []) {
		if (!df.fieldname || !allowed_fieldtypes.includes(df.fieldtype)) {
			continue;
		}

		options.push({
			label: `${df.label || df.fieldname} (${df.fieldname})`,
			value: df.fieldname,
		});
	}

	return options;
}

function clear_missing_field_value(frm, cdt, cdn, row, fieldname, options) {
	if (!row[fieldname]) {
		return;
	}

	const allowed_values = options.map(function (option) {
		return typeof option === "string" ? option : option.value;
	});

	if (!allowed_values.includes(row[fieldname])) {
		frappe.model.set_value(cdt, cdn, fieldname, "");
	}
}

function update_payable_grid_options(frm, cdn, options_by_field) {
	const grid = frm.fields_dict.payable_doctypes.grid;
	const grid_row = grid.grid_rows_by_docname[cdn];

	for (const fieldname of Object.keys(options_by_field)) {
		const options = options_by_field[fieldname];
		grid.update_docfield_property(fieldname, "options", options);

		if (grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict[fieldname]) {
			const control = grid_row.grid_form.fields_dict[fieldname];
			control.df.options = options;
			control.refresh();
		}
	}
}
