// -*- coding: utf-8 -*-
// Copyright (c) 2026, Pinelabs Fin contributors
// For license information, please see license.txt

frappe.ui.form.on("Pinelabs Transaction", {
	refresh(frm) {
		render_status_indicator(frm);
		render_buttons(frm);
	},
});

function render_status_indicator(frm) {
	if (!frm.doc.status) return;
	const map = {
		INITIATED: "gray",
		PENDING: "orange",
		SUCCESS: "green",
		FAILED: "red",
		CANCELLED: "darkgray",
	};
	const color = map[frm.doc.status] || "gray";
	frm.dashboard.set_headline_alert(
		`<span class="indicator-pill ${color}">${frm.doc.status}</span>` +
		` &middot; ${frappe.utils.escape_html(frm.doc.flow_type || "")}` +
		(frm.doc.payment_method ? ` &middot; ${frappe.utils.escape_html(frm.doc.payment_method)}` : ""),
	);
}

function render_buttons(frm) {
	if (frm.doc.payment_entry) {
		frm.add_custom_button(
			__("View Payment Entry"),
			() => frappe.set_route("Form", "Payment Entry", frm.doc.payment_entry),
		);
	}
	if (frm.doc.reference_doctype && frm.doc.reference_name) {
		frm.add_custom_button(__("View Source Document"), () =>
			frappe.set_route("Form", frm.doc.reference_doctype, frm.doc.reference_name),
		);
	}

	const status = frm.doc.status;
	const flow = frm.doc.flow_type;

	if ((flow === "Plural" || flow === "Direct NB") && (status === "PENDING" || status === "INITIATED")) {
		frm.add_custom_button(
			__("Refresh Status"),
			() => refresh_plural_status(frm),
			__("Actions"),
		);
	}

	if (flow === "Terminal" && (status === "INITIATED" || status === "PENDING")) {
		frm.add_custom_button(
			__("Cancel Terminal"),
			() => cancel_terminal(frm),
			__("Actions"),
		);
	}

	if (status === "FAILED" && is_recent(frm.doc.created_at, 24)) {
		frm.add_custom_button(
			__("Retry on Source Document"),
			() => frappe.set_route("Form", frm.doc.reference_doctype, frm.doc.reference_name),
			__("Actions"),
		);
	}
}

function refresh_plural_status(frm) {
	frappe.call({
		method: "pinelabs_fin.api.plural.refresh_payment_link_status",
		args: { transaction_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Refreshing status..."),
		callback(r) {
			const msg = r.message || {};
			const indicator = msg.status === "SUCCESS" ? "green" : msg.status === "FAILED" ? "red" : "blue";
			frappe.show_alert({
				message: msg.status ? __("Status: {0}", [msg.status]) : __("Refreshed"),
				indicator,
			});
			frm.reload_doc();
		},
	});
}

function cancel_terminal(frm) {
	frappe.confirm(__("Cancel this terminal payment?"), () => {
		frappe.call({
			method: "pinelabs_fin.api.terminal.cancel_terminal_payment",
			args: { transaction_name: frm.doc.name, reason: "cancelled from transaction record" },
			freeze: true,
			freeze_message: __("Cancelling..."),
			callback() {
				frm.reload_doc();
			},
		});
	});
}

function is_recent(datetime_str, hours) {
	if (!datetime_str) return false;
	const t = frappe.datetime.str_to_obj(datetime_str);
	if (!t) return false;
	const ms = Date.now() - t.getTime();
	return ms < hours * 3600 * 1000;
}
