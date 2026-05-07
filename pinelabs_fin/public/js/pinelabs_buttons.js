// -*- coding: utf-8 -*-
// Copyright (c) 2026, Pinelabs Fin contributors
// For license information, please see license.txt
//
// Phase 1 frontend driver. Adds two buttons to every doctype declared
// in Pinelabs Settings > Payable Documents:
//
//   1. Pay on Machine    -> Card / UPI selector -> polls until terminal status
//   2. Send Payment Link -> {All Methods, Quick NB, UPI Only, Card Only}
//                           quick_nb opens a bank picker.
//
// All flow gating happens server-side. The buttons render whenever the
// doctype is in the payable list — the backend rejects with a clear
// error if eligibility/credentials/customer details are missing.

(function () {
	if (!window.frappe) return;

	const LOG = (...a) => console.log("[Pinelabs]", ...a);

	// In-memory cache of doctypes the user has marked payable.
	// Pre-seed with the three doctypes most commonly used so the first-form-render
	// race never bites — the async fetch backfills any custom ones.
	const PAYABLE = {
		set: new Set(["Sales Invoice", "POS Invoice", "Payment Request"]),
		fetched: false,
	};

	// ─────────────────────────────────────────────────────────────────
	// Pre-register form handlers for the seeded doctypes synchronously.
	// This MUST run at script-load time so handlers are in place before
	// any form first renders.
	// ─────────────────────────────────────────────────────────────────

	PAYABLE.set.forEach(register_handler);

	// Async refresh from server — captures custom doctypes the user added.
	frappe.after_ajax(refresh_payable_list);

	// Also re-render on route change (helps if the JS loaded after the
	// initial form render — opening another doc and coming back works).
	if (frappe.router && frappe.router.on) {
		frappe.router.on("change", () => {
			if (cur_frm && PAYABLE.set.has(cur_frm.doctype)) {
				render_pinelabs_buttons(cur_frm);
			}
		});
	}

	// Belt-and-braces: if a form is already on screen when this script
	// runs, render against it immediately.
	$(document).ready(() => {
		if (cur_frm && PAYABLE.set.has(cur_frm.doctype)) {
			LOG("rendering buttons for current form on doc-ready", cur_frm.doctype);
			render_pinelabs_buttons(cur_frm);
		}
	});

	function register_handler(doctype) {
		LOG("registering form handler for", doctype);
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				render_pinelabs_buttons(frm);
			},
		});
	}

	function refresh_payable_list() {
		frappe.db
			.get_list("Pinelabs Payable Doctype", {
				fields: ["doctype_name", "is_enabled"],
				filters: { parenttype: "Pinelabs Settings" },
				limit: 100,
			})
			.then((rows) => {
				PAYABLE.fetched = true;
				const enabled = (rows || [])
					.filter((r) => Number(r.is_enabled) === 1)
					.map((r) => r.doctype_name)
					.filter(Boolean);

				LOG("payable doctypes from Settings:", enabled);

				enabled.forEach((dt) => {
					if (!PAYABLE.set.has(dt)) {
						PAYABLE.set.add(dt);
						register_handler(dt);
					}
				});

				// If the current form just became payable, render now.
				if (cur_frm && PAYABLE.set.has(cur_frm.doctype)) {
					render_pinelabs_buttons(cur_frm);
				}
			})
			.catch((err) => {
				console.error("[Pinelabs] failed to fetch Payable Doctypes:", err);
			});
	}

	// ─────────────────────────────────────────────────────────────────
	// Buttons
	// ─────────────────────────────────────────────────────────────────

	function render_pinelabs_buttons(frm) {
		if (!frm || !frm.doc) return;

		// Frappe wipes custom buttons on every refresh, so always clean up
		// any leftovers before deciding whether to add ours back. This also
		// makes the buttons disappear correctly when the doc transitions out
		// of an eligible state (e.g. fully paid, cancelled).
		frm.remove_custom_button(__("Pay on Machine"));
		frm.remove_custom_button(__("Send Payment Link"));

		if (frm.doc.docstatus !== 1) {
			LOG("skip: not submitted", frm.doctype, frm.docname, "docstatus=", frm.doc.docstatus);
			return;
		}
		const amount = outstanding_amount(frm);
		if (amount <= 0) {
			LOG("skip: no outstanding amount on", frm.doctype, frm.docname);
			return;
		}

		LOG("rendering Pine Labs buttons on", frm.doctype, frm.docname, "amount=", amount);

		// add_custom_button is idempotent (shows the existing button when
		// called twice with the same label), so no extra dedup guard needed.
		frm.add_custom_button(__("Pay on Machine"), () => open_terminal_dialog(frm));
		frm.add_custom_button(__("Send Payment Link"), () => open_payment_link_dialog(frm));
	}

	function outstanding_amount(frm) {
		const candidates = ["outstanding_amount", "grand_total", "amount", "paid_amount"];
		for (const f of candidates) {
			const v = frm.doc[f];
			if (v != null) return Number(v) || 0;
		}
		return 0;
	}

	// ─────────────────────────────────────────────────────────────────
	// Terminal flow
	// ─────────────────────────────────────────────────────────────────

	function open_terminal_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Pay on Machine"),
			fields: [
				{
					fieldname: "mode_of_payment",
					fieldtype: "Link",
					label: __("Mode of Payment"),
					options: "Mode of Payment",
					reqd: 1,
					get_query() {
						return {
							filters: {
								enabled: 1,
								pinelabs_enabled: 1,
								pinelabs_flow_type: ["in", ["Terminal", "Both"]],
							},
						};
					},
				},
			],
			primary_action_label: __("Start Payment"),
			primary_action(values) {
				dialog.hide();
				start_terminal_payment(frm, values.mode_of_payment);
			},
		});
		dialog.show();
	}

	function start_terminal_payment(frm, mode_of_payment) {
		frappe.call({
			method: "pinelabs_fin.api.terminal.initiate_terminal_payment",
			args: {
				reference_doctype: frm.doctype,
				reference_name: frm.docname,
				mode_of_payment,
			},
			freeze: true,
			freeze_message: __("Sending request to terminal..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					show_error(__("Terminal payment failed"), msg.message || msg.error);
					if (msg.transaction_name) frm.reload_doc();
					return;
				}
				open_polling_modal(frm, msg.transaction_name);
			},
		});
	}

	function open_polling_modal(frm, transaction_name) {
		let settled = false;  // true once a terminal status is observed (success/fail/cancel)
		const t0 = performance.now();
		const t = () => `+${Math.round(performance.now() - t0)}ms`;
		LOG(t(), "polling modal opened for", transaction_name);
		const modal = new frappe.ui.Dialog({
			title: __("Waiting for terminal"),
			static: true,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "body",
					options: `
						<div style="text-align:center; padding: 16px 8px;">
							<div class="text-muted" style="font-size: 0.85em;">${__("Transaction")}: ${frappe.utils.escape_html(transaction_name)}</div>
							<div style="margin: 16px 0;"><i class="fa fa-spinner fa-spin fa-2x"></i></div>
							<div id="pinelabs-modal-status" class="text-muted">${__("Waiting for customer to complete payment...")}</div>
						</div>
					`,
				},
			],
			primary_action_label: __("Cancel"),
			primary_action() {
				settled = true;
				cleanup();
				cancel_terminal_payment(transaction_name, () => {
					modal.hide();
					frm.reload_doc();
				});
			},
		});
		modal.show();

		// Lock the modal: backdrop click / Esc / X must NOT dismiss it.
		// `static: true` doesn't always cover every Bootstrap path, so
		// belt-and-brace with a hide.bs.modal guard.
		modal.$wrapper.modal({ backdrop: "static", keyboard: false });
		modal.$wrapper.find(".modal-header .btn-modal-close").hide();
		modal.$wrapper.on("hide.bs.modal.pinelabs", (e) => {
			if (!settled) {
				e.preventDefault();
				return false;
			}
		});

		const status_el = () => document.getElementById("pinelabs-modal-status");
		const update_status_text = (status) => {
			const el = status_el();
			if (!el) return;
			el.textContent = status === "PENDING" || !status
				? __("Waiting for customer to complete payment...")
				: __("Status: {0}", [status]);
		};

		const finalize = (status, source) => {
			LOG(t(), "finalize called", { status, source, already_settled: settled });
			if (settled) return;
			settled = true;
			LOG(t(), "finalize: cleanup() start");
			cleanup();
			LOG(t(), "finalize: cleanup() done, hiding modal");
			modal.hide();
			LOG(t(), "finalize: modal.hide() returned");
			const indicator = status === "SUCCESS" ? "green" : "red";
			frappe.show_alert({ message: __("Payment {0}", [status]), indicator });
			LOG(t(), "finalize: calling frm.reload_doc()");
			const reload_promise = frm.reload_doc();
			if (reload_promise && reload_promise.then) {
				reload_promise.then(
					() => LOG(t(), "finalize: frm.reload_doc() resolved"),
					(err) => LOG(t(), "finalize: frm.reload_doc() rejected", err),
				);
			} else {
				LOG(t(), "finalize: frm.reload_doc() returned (no promise)");
			}
		};

		// Realtime: instant cross-context updates (e.g. cron reconcile,
		// other tab). Status transitions on our server publish this event.
		const realtime_handler = (data) => {
			LOG(t(), "realtime event received", data);
			if (!data || data.transaction_name !== transaction_name) {
				LOG(t(), "realtime event ignored (txn mismatch)");
				return;
			}
			update_status_text(data.status);
			if (["SUCCESS", "FAILED", "CANCELLED"].includes(data.status)) {
				finalize(data.status, "realtime");
			}
		};
		frappe.realtime.on("pinelabs_txn_update", realtime_handler);
		LOG(t(), "realtime subscribed to pinelabs_txn_update");

		// Fixed-interval poll using setInterval — fires every 3s regardless
		// of whether the previous response is back. This matches the old
		// fast-feedback pattern: chained setTimeout was effectively
		// (round-trip + 2s) per cycle, which stretched out the perceived
		// lag once the terminal finished. check_terminal_status is
		// idempotent, so overlapping in-flight requests are safe.
		const POLL_INTERVAL_MS = 3000;
		let poll_seq = 0;
		const tick = () => {
			if (settled) return;
			const seq = ++poll_seq;
			const tick_start = performance.now();
			LOG(t(), `poll #${seq} -> check_terminal_status`);
			frappe.call({
				method: "pinelabs_fin.api.terminal.check_terminal_status",
				args: { transaction_name },
				callback(r) {
					const round_trip = Math.round(performance.now() - tick_start);
					LOG(t(), `poll #${seq} <- response (round-trip ${round_trip}ms)`, r && r.message);
					if (settled) {
						LOG(t(), `poll #${seq} dropped (already settled)`);
						return;
					}
					const msg = r.message || {};
					update_status_text(msg.status);
					if (["SUCCESS", "FAILED", "CANCELLED"].includes(msg.status)) {
						finalize(msg.status, `poll#${seq}`);
					}
				},
			});
		};
		// First poll fires almost immediately to catch any state that
		// settled in the gap between transaction creation and the
		// realtime subscription being live.
		setTimeout(tick, 500);
		const poll_interval = setInterval(tick, POLL_INTERVAL_MS);
		LOG(t(), `polling armed (interval=${POLL_INTERVAL_MS}ms)`);

		function cleanup() {
			LOG(t(), "cleanup: detaching realtime + clearing interval");
			frappe.realtime.off("pinelabs_txn_update", realtime_handler);
			clearInterval(poll_interval);
			modal.$wrapper.off("hide.bs.modal.pinelabs");
			LOG(t(), "cleanup: done");
		}
	}

	function cancel_terminal_payment(transaction_name, on_done) {
		frappe.call({
			method: "pinelabs_fin.api.terminal.cancel_terminal_payment",
			args: { transaction_name, reason: "user cancelled from modal" },
			freeze: true,
			freeze_message: __("Cancelling..."),
			always() {
				if (on_done) on_done();
			},
		});
	}

	// ─────────────────────────────────────────────────────────────────
	// Send Payment Link flow
	// ─────────────────────────────────────────────────────────────────

	function open_payment_link_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Send Payment Link"),
			fields: [
				{
					fieldname: "flow",
					fieldtype: "Select",
					label: __("Flow"),
					reqd: 1,
					default: "all_methods",
					options: [
						"all_methods",
						"card_only",
						"upi_only",
						"nb_only",
						"quick_nb",
					].join("\n"),
					description: __(
						"all_methods sends a Plural hosted page with all payment options. quick_nb redirects the customer straight to a bank you choose.",
					),
				},
				{
					fieldname: "bank_code",
					fieldtype: "Data",
					label: __("Bank Code (NB####)"),
					depends_on: "eval:doc.flow=='quick_nb'",
					mandatory_depends_on: "eval:doc.flow=='quick_nb'",
					description: __(
						"Pine Labs net banking pay code. Common values: NB1004 (Axis), NB1007 (HDFC), NB1016 (ICICI), NB1531 (SBI), NB1565 (Kotak). Full list at developer.pinelabsonline.com/docs/supported-bank.",
					),
				},
			],
			primary_action_label: __("Continue"),
			primary_action(values) {
				dialog.hide();
				start_payment_link(frm, values.flow, values.bank_code);
			},
		});
		dialog.show();
	}

	function start_payment_link(frm, flow, bank_code) {
		frappe.call({
			method: "pinelabs_fin.api.plural.initiate_payment_link",
			args: {
				reference_doctype: frm.doctype,
				reference_name: frm.docname,
				flow,
				bank_code: bank_code || null,
			},
			freeze: true,
			freeze_message: __("Generating payment link..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					show_error(__("Payment link failed"), msg.error || msg.message);
					if (msg.transaction_name) frm.reload_doc();
					return;
				}
				if (flow === "quick_nb" && msg.redirect_url) {
					frappe.show_alert({
						message: __("Redirecting customer to bank..."),
						indicator: "blue",
					});
					window.open(msg.redirect_url, "_blank");
				} else if (msg.payment_link_url) {
					show_link_result(msg.payment_link_url);
				}
				frm.reload_doc();
			},
		});
	}

	function show_link_result(url) {
		frappe.msgprint({
			title: __("Payment Link Generated"),
			message: `
				<p>${__("Pine Labs has sent the link to the customer via SMS/Email.")}</p>
				<p><a href="${frappe.utils.escape_html(url)}" target="_blank">${frappe.utils.escape_html(url)}</a></p>
			`,
			indicator: "green",
		});
	}

	// ─────────────────────────────────────────────────────────────────
	// Shared
	// ─────────────────────────────────────────────────────────────────

	function show_error(title, message) {
		frappe.msgprint({
			title,
			message: message || __("Unknown error"),
			indicator: "red",
		});
	}

	LOG("pinelabs_buttons.js loaded");
})();
