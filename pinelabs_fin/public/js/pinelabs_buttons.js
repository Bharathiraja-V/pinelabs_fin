// -*- coding: utf-8 -*-
// Copyright (c) 2026, Pinelabs Fin contributors
// For license information, please see license.txt
//
// Phase 1 frontend driver. Adds two buttons to every doctype declared
// in Pinelabs Settings > Payable Documents:
//
//   1. Pay on Machine    -> Card / UPI selector -> polls until terminal status
//   2. Send Payment Link -> generates a Plural hosted link covering all
//                           enabled payment methods (no per-method dialog).
//
// All flow gating happens server-side. The buttons render whenever the
// doctype is in the payable list — the backend rejects with a clear
// error if eligibility/credentials/customer details are missing.
//
// Also exposes reusable utilities on `frappe.pinelabs` for any custom
// button / page to call:
//   frappe.pinelabs.start_terminal_payment(args, options)
//   frappe.pinelabs.open_terminal_modal(transaction_name, options)

// Namespace is declared OUTSIDE the IIFE so any other script that loads
// in the same desk session can read `frappe.pinelabs` even if the IIFE
// below hasn't finished executing yet. The utility functions get
// assigned to it from inside the IIFE.
window.frappe = window.frappe || {};
frappe.pinelabs = frappe.pinelabs || {};

(function () {
	if (!window.frappe) return;

	const LOG = (...a) => console.log("[Pinelabs]", ...a);

	// In-memory cache of doctypes the user has marked payable, keyed by
	// doctype name. Each entry carries the per-button visibility flags and
	// the allowed-docstatus set from the Payable Doctype row. Pre-seed with
	// the three doctypes most commonly used so the first-form-render race
	// never bites — the async fetch backfills any custom ones and overrides
	// the entry with the saved values. allowed_docstatus defaults to {1}
	// (Submitted only), matching the JSON default for show_on_docstatus.
	const defaultFlags = () => ({
		pay_on_machine: true,
		send_payment_link: true,
		allowed_docstatus: new Set([1]),
	});
	const PAYABLE = {
		flags: new Map([
			["Sales Invoice", defaultFlags()],
			["POS Invoice", defaultFlags()],
			["Payment Request", defaultFlags()],
		]),
		fetched: false,
	};

	// Parse `"0"`, `"1"`, or `"0,1"` from show_on_docstatus into a Set of
	// integers. Cancelled (2) is never accepted, even if the row somehow
	// contains it. Empty / unparseable input falls back to {1}.
	function parse_allowed_docstatus(raw) {
		const allowed = new Set();
		String(raw || "")
			.split(",")
			.forEach((part) => {
				const n = parseInt(String(part).trim(), 10);
				if (n === 0 || n === 1) allowed.add(n);
			});
		if (allowed.size === 0) allowed.add(1);
		return allowed;
	}

	// ─────────────────────────────────────────────────────────────────
	// Pre-register form handlers for the seeded doctypes synchronously.
	// This MUST run at script-load time so handlers are in place before
	// any form first renders.
	// ─────────────────────────────────────────────────────────────────

	for (const dt of PAYABLE.flags.keys()) register_handler(dt);

	// Async refresh from server — captures custom doctypes the user added.
	frappe.after_ajax(refresh_payable_list);

	// The deprecated current-form global is the only handle to a form that
	// is ALREADY on screen when this desk-wide script initialises (before
	// any form 'refresh' fires). It's wrapped once here so the rest of the
	// file never touches that global directly.
	function open_form() {
		return window.cur_frm || null; // nosemgrep: frappe-cur-frm-usage
	}

	function render_if_payable(frm, log_label) {
		if (frm && PAYABLE.flags.has(frm.doctype)) {
			if (log_label) LOG(log_label, frm.doctype);
			render_pinelabs_buttons(frm);
		}
	}

	// Also re-render on route change (helps if the JS loaded after the
	// initial form render — opening another doc and coming back works).
	if (frappe.router && frappe.router.on) {
		frappe.router.on("change", () => {
			render_if_payable(open_form());
		});
	}

	// Belt-and-braces: if a form is already on screen when this script
	// runs, render against it immediately.
	$(document).ready(() => {
		render_if_payable(open_form(), "rendering buttons for current form on doc-ready");
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
		// Fetch the parent Singleton — `frappe.db.get_list` on a child table
		// silently drops Link/custom fields (returns rows with only `name`),
		// so we'd never see doctype_name or the per-button flags. Pulling the
		// parent doc returns the full child rows.
		frappe.call({
			method: "frappe.client.get",
			args: { doctype: "Pinelabs Settings", name: "Pinelabs Settings" },
		})
			.then((r) => {
				const rows = (r && r.message && r.message.payable_doctypes) || [];
				PAYABLE.fetched = true;
				const enabled = rows.filter((row) => Number(row.is_enabled) === 1);

				LOG("payable doctypes from Settings:", enabled.map((row) => row.doctype_name));

				enabled.forEach((row) => {
					const dt = row.doctype_name;
					if (!dt) return;
					const flags = {
						pay_on_machine: Number(row.enable_pay_on_machine) === 1,
						send_payment_link: Number(row.enable_send_payment_link) === 1,
						allowed_docstatus: parse_allowed_docstatus(row.show_on_docstatus),
					};
					const isNew = !PAYABLE.flags.has(dt);
					PAYABLE.flags.set(dt, flags);
					if (isNew) register_handler(dt);
				});

				// If the current form just became payable, render now.
				render_if_payable(open_form());
			})
			.catch((err) => {
				console.error("[Pinelabs] failed to fetch Pinelabs Settings:", err);
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

		// Per-row config from the Payable Doctype row. Until the async fetch
		// lands, the seeded entry uses defaultFlags() (both buttons on,
		// allowed_docstatus = {1}) — matches the legacy behavior.
		const flags = PAYABLE.flags.get(frm.doctype) || defaultFlags();

		if (!flags.allowed_docstatus.has(frm.doc.docstatus)) {
			LOG(
				"skip: docstatus not allowed by row config",
				frm.doctype,
				frm.docname,
				"docstatus=",
				frm.doc.docstatus,
				"allowed=",
				Array.from(flags.allowed_docstatus),
			);
			return;
		}
		const amount = outstanding_amount(frm);
		if (amount <= 0) {
			LOG("skip: no outstanding amount on", frm.doctype, frm.docname);
			return;
		}

		LOG("rendering Pine Labs buttons on", frm.doctype, frm.docname, "amount=", amount, "flags=", flags);

		// add_custom_button is idempotent (shows the existing button when
		// called twice with the same label), so no extra dedup guard needed.
		if (flags.pay_on_machine) {
			frm.add_custom_button(__("Pay on Machine"), () => open_terminal_dialog(frm));
		}
		if (flags.send_payment_link) {
			frm.add_custom_button(__("Send Payment Link"), () => start_payment_link(frm, "all_methods"));
		}
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
						// Pine Labs Terminal modes are identified by name — no
						// custom fields required. Keep this list in sync with
						// PINELABS_TERMINAL_MODES in pinelabs_fin/api/transaction.py.
						return {
							filters: {
								enabled: 1,
								name: ["in", ["PineLabs - Card", "PineLabs - UPI"]],
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

	// Thin wrapper retained for the auto-injected Sales Invoice / POS Invoice
	// button path — defers to the global utility below.
	function open_polling_modal(frm, transaction_name) {
		frappe.pinelabs.open_terminal_modal(transaction_name, {
			on_settled: () => frm && frm.reload_doc && frm.reload_doc(),
			on_cancel: () => frm && frm.reload_doc && frm.reload_doc(),
		});
	}

	// ─────────────────────────────────────────────────────────────────────
	// Public utilities — callable from any custom button, any custom page,
	// any external code. The auto-button uses these too; there is no
	// behavioural fork.
	//
	//   frappe.pinelabs.open_terminal_modal(transaction_name, options)
	//     Show the locked "Waiting for terminal" dialog for an existing
	//     Pinelabs Transaction. Polls + listens for realtime + Cancel.
	//
	//   frappe.pinelabs.start_terminal_payment(args, options)
	//     One call to (a) initiate the payment via the API and (b) open
	//     the modal automatically on success. The preferred high-level
	//     entry point for new code.
	// ─────────────────────────────────────────────────────────────────────
	// (Namespace `frappe.pinelabs` is set up at the top of the file,
	// before this IIFE runs, so callers can always assume it exists.)

	frappe.pinelabs.open_terminal_modal = function (transaction_name, options) {
		options = options || {};
		const on_settled = typeof options.on_settled === "function" ? options.on_settled : function () {};
		const on_cancel = typeof options.on_cancel === "function" ? options.on_cancel : function () {};
		const show_alert = options.show_alert !== false;

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
					on_cancel(transaction_name);
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
			cleanup();
			modal.hide();
			if (show_alert) {
				const indicator = status === "SUCCESS" ? "green" : "red";
				frappe.show_alert({ message: __("Payment {0}", [status]), indicator });
			}
			try {
				on_settled(status, transaction_name);
			} catch (err) {
				console.error("[Pinelabs] on_settled threw:", err);
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

	// High-level helper: call the initiate API and, on success, open the
	// polling modal automatically. Use this from any custom button to get
	// the same UX as the built-in "Pay on Machine".
	//
	// Usage:
	//   frappe.pinelabs.start_terminal_payment(
	//     { reference_doctype, reference_name, mode_of_payment, amount? },
	//     { on_settled(status, txn) {...}, on_cancel(txn) {...}, on_error(msg) {...} }
	//   );
	frappe.pinelabs.start_terminal_payment = function (args, options) {
		options = options || {};
		const on_settled = options.on_settled || function () {};
		const on_cancel = options.on_cancel || function () {};
		const on_error = options.on_error || ((msg) => show_error(__("Terminal payment failed"), msg));

		return frappe.call({
			method: "pinelabs_fin.api.terminal.initiate_terminal_payment",
			args: args,
			freeze: true,
			freeze_message: __("Sending request to terminal..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					on_error(msg.message || msg.error || __("Unknown error"));
					return;
				}
				frappe.pinelabs.open_terminal_modal(msg.transaction_name, {
					on_settled: (status, txn) => on_settled(status, txn),
					on_cancel: (txn) => on_cancel(txn),
				});
			},
		});
	};

	// ─────────────────────────────────────────────────────────────────
	// Send Payment Link flow
	// ─────────────────────────────────────────────────────────────────

	// Thin wrapper for the auto-injected Send Payment Link button.
	// Defers to the global utility below — no behavioural fork.
	function start_payment_link(frm, flow) {
		frappe.pinelabs.start_payment_link(
			{
				reference_doctype: frm.doctype,
				reference_name: frm.docname,
				flow,
			},
			{
				on_link: () => frm.reload_doc(),
				on_error: (msg) => {
					show_error(__("Payment link failed"), msg);
					frm.reload_doc();
				},
			},
		);
	}

	// Public utility: call the initiate API for the Plural Pay-by-Link flow.
	// On success, shows the standard "Payment Link Generated" popup with the
	// URL. No polling modal — the customer pays asynchronously (SMS/email),
	// the webhook + 1-min cron finalize the transaction in the background.
	//
	// Usage:
	//   frappe.pinelabs.start_payment_link(
	//     { reference_doctype, reference_name, flow?: "all_methods", expiry_minutes?: 60 },
	//     {
	//       on_link(url, txn) { ... },    // success — link generated
	//       on_error(msg)     { ... },    // API rejected (validation, OAuth, ...)
	//       show_popup: true,             // default true; set false to suppress the standard popup
	//     }
	//   );
	frappe.pinelabs.start_payment_link = function (args, options) {
		options = options || {};
		const on_link = typeof options.on_link === "function" ? options.on_link : function () {};
		const on_error = typeof options.on_error === "function"
			? options.on_error
			: (msg) => show_error(__("Payment link failed"), msg);
		const show_popup = options.show_popup !== false;

		return frappe.call({
			method: "pinelabs_fin.api.plural.initiate_payment_link",
			args: args,
			freeze: true,
			freeze_message: __("Generating payment link..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					on_error(msg.message || msg.error || __("Unknown error"));
					return;
				}
				if (show_popup && msg.payment_link_url) {
					show_link_result(msg.payment_link_url);
				}
				on_link(msg.payment_link_url, msg.transaction_name);
			},
		});
	};

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
