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
		fieldtypes: ["Data", "Read Only", "Small Text", "Text"],
	},
];

const SUBMITTABLE_DOCSTATUS_OPTIONS = [
	{ label: "0", value: "0" },
	{ label: "1", value: "1" },
	{ label: "0,1", value: "0,1" },
];

const NON_SUBMITTABLE_DOCSTATUS_OPTIONS = [{ label: "0", value: "0" }];

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
		refresh_payable_row_controls(frm, cdt, cdn);
	},

	doctype_name(frm, cdt, cdn) {
		clear_dependent_row_values(cdt, cdn);

		refresh_payable_row_controls(frm, cdt, cdn);
	},

	status_field(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "paid_status_value", "");
		refresh_payable_row_controls(frm, cdt, cdn);
	},
});

function clear_dependent_row_values(cdt, cdn) {
	for (const fieldname of ["amount_field", "status_field", "paid_status_value", "link_payment_entry_field"]) {
		frappe.model.set_value(cdt, cdn, fieldname, "");
	}
}

function refresh_payable_row_controls(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const grid = frm.fields_dict.payable_doctypes.grid;

	if (!row.doctype_name) {
		update_payable_grid_options(frm, cdn, {
			show_on_docstatus: SUBMITTABLE_DOCSTATUS_OPTIONS,
			amount_field: [""],
			status_field: [""],
			paid_status_value: [""],
			link_payment_entry_field: [""],
		});
		return;
	}

	frappe.model.with_doctype(row.doctype_name, function () {
		const meta = frappe.get_meta(row.doctype_name);
		const options_by_field = {
			show_on_docstatus: get_docstatus_options(meta),
			paid_status_value: get_paid_status_value_options(meta, row),
		};

		for (const config of PAYABLE_FIELD_SELECTS) {
			options_by_field[config.fieldname] = get_source_field_options(meta, config.fieldtypes);
			clear_missing_field_value(frm, cdt, cdn, row, config.fieldname, options_by_field[config.fieldname]);
		}

		normalize_docstatus_value(cdt, cdn, row, options_by_field.show_on_docstatus);
		clear_missing_field_value(frm, cdt, cdn, row, "paid_status_value", options_by_field.paid_status_value);

		update_payable_grid_options(frm, cdn, options_by_field);
		grid.refresh();
	});
}

function get_docstatus_options(meta) {
	return meta && meta.is_submittable ? SUBMITTABLE_DOCSTATUS_OPTIONS : NON_SUBMITTABLE_DOCSTATUS_OPTIONS;
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

function get_paid_status_value_options(meta, row) {
	const options = [""];
	if (!row || !row.status_field) {
		return options;
	}

	const status_df = get_docfield(meta, row.status_field);
	const field_options = get_docfield_value_options(status_df);

	if (field_options.length) {
		return options.concat(field_options);
	}

	return options;
}

function get_docfield(meta, fieldname) {
	if (!meta || !fieldname) {
		return null;
	}

	return (meta.fields || []).find(function (df) {
		return df.fieldname === fieldname;
	}) || null;
}

function get_docfield_value_options(df) {
	if (!df || !df.options) {
		return [];
	}

	const raw_options = Array.isArray(df.options) ? df.options : String(df.options).split("\n");

	return raw_options
		.map(function (option) {
			if (typeof option === "string") {
				const trimmed_option = option.trim();
				if (!trimmed_option) {
					return null;
				}

				return {
					label: trimmed_option,
					value: trimmed_option,
				};
			}

			if (!option || !option.value) {
				return null;
			}

			return {
				label: option.label || option.value,
				value: option.value,
			};
		})
		.filter(Boolean);
}

function normalize_docstatus_value(cdt, cdn, row, options) {
	if (!row) {
		return;
	}

	const allowed_values = options.map(function (option) {
		return typeof option === "string" ? option : option.value;
	});

	if (allowed_values.includes(row.show_on_docstatus)) {
		return;
	}

	const next_value = allowed_values.includes("1") ? "1" : allowed_values[0] || "";
	frappe.model.set_value(cdt, cdn, "show_on_docstatus", next_value);
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
			if (typeof control.set_data === "function") {
				control.set_data(options);
			}
			control.refresh();
		}
	}
}
