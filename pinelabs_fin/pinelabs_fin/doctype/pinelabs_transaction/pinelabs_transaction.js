// -*- coding: utf-8 -*-
// Copyright (c) 2026, Pinelabs Fin contributors
// For license information, please see license.txt

frappe.ui.form.on("Pinelabs Transaction", {
	refresh(frm) {
		render_status_indicator(frm);
		render_buttons(frm);
		setup_realtime_autoreload(frm);
	},
	onload(frm) {
		// New record being viewed — also subscribe straight away. (`refresh`
		// fires after onload but we keep both for safety in case the form is
		// re-rendered without onload.)
		setup_realtime_autoreload(frm);
	},
});

// ─────────────────────────────────────────────────────────────────────────
// Auto-reload on status transitions
//
// Server-side `transaction._publish_status_update` broadcasts a
// `pinelabs_txn_update` socket event after every commit on a transition
// (mark_pending / mark_success / mark_failed / mark_cancelled). The cron
// reconcile (every minute), the foreground poll, and the webhook all flow
// through that same publisher — so subscribing here means the open form
// reflects the latest state without the user clicking Refresh Status.
// ─────────────────────────────────────────────────────────────────────────

function setup_realtime_autoreload(frm) {
	if (frm.is_new()) return;
	// Tear down any previous subscription before adding a new one — avoids
	// stacking handlers when the form re-renders.
	if (frm.__pinelabs_txn_handler) {
		frappe.realtime.off("pinelabs_txn_update", frm.__pinelabs_txn_handler);
	}
	const handler = (data) => {
		if (!data || data.transaction_name !== frm.doc.name) return;
		if (data.status === frm.doc.status && data.payment_entry === frm.doc.payment_entry) {
			return;
		}
		frm.reload_doc();
		const indicator = data.status === "SUCCESS"
			? "green"
			: data.status === "FAILED" || data.status === "CANCELLED"
				? "red"
				: "blue";
		frappe.show_alert({
			message: __("Status updated: {0}", [data.status]),
			indicator,
		});
	};
	frappe.realtime.on("pinelabs_txn_update", handler);
	frm.__pinelabs_txn_handler = handler;
}

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

	if (flow === "Plural" && (status === "PENDING" || status === "INITIATED")) {
		frm.add_custom_button(
			__("Refresh Status"),
			() => refresh_plural_status(frm),
			__("Actions"),
		);
	}

	if (flow === "Terminal" && (status === "INITIATED" || status === "PENDING")) {
		// Open the locked "Waiting for terminal" dialog for THIS in-flight
		// txn. Useful when the operator navigated away from the source-doc
		// modal and wants to come back to a running payment. The dialog is
		// idempotent — if another tab already has it open, this returns the
		// same modal instead of stacking a duplicate.
		frm.add_custom_button(
			__("Watch Terminal"),
			() => watch_terminal(frm),
			__("Actions"),
		);
		frm.add_custom_button(
			__("Cancel Terminal"),
			() => cancel_terminal(frm),
			__("Actions"),
		);
	}

	if (flow === "Terminal" && status === "FAILED") {
		// Re-initiate using the original source doc + mode_of_payment.
		// Creates a NEW Pinelabs Transaction row — the FAILED one stays
		// for audit. Pre-flight (eligibility, outstanding amount, active
		// txn guard) is re-run by initiate_terminal_payment.
		frm.add_custom_button(
			__("Retry Terminal"),
			() => retry_terminal(frm),
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

function watch_terminal(frm) {
	// Defensive: the global pinelabs_buttons.js bundle exposes
	// open_terminal_modal. If for any reason it isn't loaded on this page,
	// fail loud rather than silent.
	if (!(window.frappe && frappe.pinelabs && typeof frappe.pinelabs.open_terminal_modal === "function")) {
		frappe.msgprint({
			title: __("Cannot open dialog"),
			message: __("The Pine Labs UI bundle is not loaded on this page."),
			indicator: "red",
		});
		return;
	}
	frappe.pinelabs.open_terminal_modal(frm.doc.name, {
		on_settled: () => frm.reload_doc(),
		on_cancel: () => frm.reload_doc(),
	});
}

// Map the stored payment_method back to the Mode of Payment name expected
// by initiate_terminal_payment. Pine Labs only supports CARD/UPI for
// terminal flows (see PINELABS_TERMINAL_MODES in transaction.py).
const PAYMENT_METHOD_TO_MODE = {
	CARD: "PineLabs - Card",
	UPI: "PineLabs - UPI",
};

function retry_terminal(frm) {
	const mode = PAYMENT_METHOD_TO_MODE[String(frm.doc.payment_method || "").toUpperCase()];
	if (!mode) {
		frappe.msgprint({
			title: __("Cannot retry"),
			message: __("This transaction has no recognised Mode of Payment ({0}). Use the source document's Pay on Machine button instead.",
				[frm.doc.payment_method || "—"]),
			indicator: "red",
		});
		return;
	}
	if (!frm.doc.reference_doctype || !frm.doc.reference_name) {
		frappe.msgprint({
			title: __("Cannot retry"),
			message: __("This transaction has no source document reference."),
			indicator: "red",
		});
		return;
	}
	frappe.confirm(
		__("Retry terminal payment for {0} {1} using {2}?",
			[frm.doc.reference_doctype, frm.doc.reference_name, mode]),
		() => {
			frappe.pinelabs.start_terminal_payment(
				{
					reference_doctype: frm.doc.reference_doctype,
					reference_name: frm.doc.reference_name,
					mode_of_payment: mode,
				},
				{
					// New txn is created — navigate to it so the user can watch
					// the dialog finalize on the right row.
					on_settled: (_status, new_txn_name) => {
						if (new_txn_name && new_txn_name !== frm.doc.name) {
							frappe.set_route("Form", "Pinelabs Transaction", new_txn_name);
						} else {
							frm.reload_doc();
						}
					},
				},
			);
		},
	);
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
