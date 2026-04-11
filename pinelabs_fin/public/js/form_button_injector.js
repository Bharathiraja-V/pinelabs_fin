(function () {
	"use strict";

	const API = "pinelabs_fin.api.pos_endpoints";
	const DEBUG_KEY = "pinelabs_fin_debug";
	const BOOTSTRAP_RETRY_LIMIT = 40;
	const BOOTSTRAP_RETRY_DELAY = 250;
	var bootstrapped = false;

	setup_debug_api();
	start();

	function start() {
		if (window.jQuery) {
			$(document).one("app_ready", bootstrap);
			$(document).one("startup", bootstrap);
		}

		retry_bootstrap(BOOTSTRAP_RETRY_LIMIT);
	}

	function can_bootstrap() {
		return !!(window.frappe && frappe.call && frappe.ui && frappe.ui.form);
	}

	function retry_bootstrap(remaining_attempts) {
		if (bootstrapped) {
			return;
		}

		if (can_bootstrap()) {
			bootstrap();
			return;
		}

		if (remaining_attempts <= 0) {
			debug_log("bootstrap_aborted", {
				reason: "frappe_not_ready_after_retries",
			});
			return;
		}

		setTimeout(function () {
			retry_bootstrap(remaining_attempts - 1);
		}, BOOTSTRAP_RETRY_DELAY);
	}

	function bootstrap() {
		if (bootstrapped) {
			return;
		}

		if (!can_bootstrap()) {
			debug_log("bootstrap_waiting", {
				has_frappe: !!window.frappe,
				has_call: !!(window.frappe && frappe.call),
				has_form: !!(window.frappe && frappe.ui && frappe.ui.form),
			});
			return;
		}

		bootstrapped = true;
		debug_log("bootstrap", {
			current_route: window.location && window.location.pathname,
			current_doctype: window.cur_frm && cur_frm.doctype,
			current_docname: window.cur_frm && (cur_frm.docname || (cur_frm.doc && cur_frm.doc.name)),
		});

		frappe
			.call({ method: API + ".get_payable_doctypes" })
			.then(function (r) {
				var rows = (r && r.message) || [];
				set_debug_state({ rows: rows });
				debug_log("rows_fetched", { count: rows.length, rows: rows });

				rows.forEach(function (row) {
					register_hook(row);
					schedule_render_on_current_form(row);
				});
			})
			.catch(function (error) {
				set_debug_state({ fetch_error: stringify_error(error) });
				debug_log("rows_fetch_failed", { error: error });
			});
	}

	function register_hook(row) {
		debug_log("register_hook", { doctype_name: row && row.doctype_name, row: row });

		frappe.ui.form.on(row.doctype_name, {
			refresh: function (frm) {
				schedule_render(frm, row, "refresh");
			},
		});
	}

	function schedule_render_on_current_form(row) {
		if (!window.cur_frm || cur_frm.doctype !== row.doctype_name) {
			debug_log("current_form_skip", {
				reason: "doctype_mismatch_or_missing_cur_frm",
				current_doctype: window.cur_frm && cur_frm.doctype,
				row_doctype: row && row.doctype_name,
			});
			return;
		}

		debug_log("current_form_render", {
			doctype: cur_frm.doctype,
			docname: cur_frm.docname || (cur_frm.doc && cur_frm.doc.name),
		});
		schedule_render(cur_frm, row, "current_form");
	}

	function schedule_render(frm, row, source) {
		if (!frm) {
			return;
		}

		var docname = frm.docname || (frm.doc && frm.doc.name);
		var run = function () {
			if (!frm.doc) {
				return;
			}

			var active_docname = frm.docname || (frm.doc && frm.doc.name);
			if (docname && active_docname && docname !== active_docname) {
				debug_log("render_skipped", {
					reason: "doc_changed_before_render",
					source: source,
					expected_docname: docname,
					actual_docname: active_docname,
				});
				return;
			}

			render_button(frm, row);
		};

		debug_log("render_scheduled", {
			source: source,
			doctype: frm.doctype,
			docname: docname,
			row_doctype: row && row.doctype_name,
		});

		if (frm.$wrapper && frm.$wrapper.one) {
			frm.$wrapper.one("render_complete.pinelabs_injector", run);
		}

		frappe.after_ajax(function () {
			setTimeout(run, 0);
		});
	}

	function render_button(frm, row) {
		var button_label = get_button_label(row);
		var visibility = evaluate_visibility(frm, row);
		frm.remove_custom_button(button_label, __("Pinelabs"));
		debug_log("button_removed", {
			doctype: frm.doctype,
			docname: frm.docname || (frm.doc && frm.doc.name),
			button_label: button_label,
		});

		set_debug_state({
			last_render: {
				doctype: frm.doctype,
				docname: frm.docname || (frm.doc && frm.doc.name),
				button_label: button_label,
				visibility: visibility,
				row: row,
			},
		});
		debug_log("visibility_evaluated", {
			doctype: frm.doctype,
			docname: frm.docname || (frm.doc && frm.doc.name),
			button_label: button_label,
			docstatus: frm.doc && frm.doc.docstatus,
			allowed_docstatuses: visibility.allowed_docstatuses,
			amount_field: row && row.amount_field,
			payable_amount: get_payable_amount(frm, row),
			show: visibility.show,
			reason: visibility.reason,
		});

		if (!visibility.show) return;

		frm.add_custom_button(
			__(button_label),
			function () {
				if (!validate_amount(frm, row)) return;
				open_mode_picker(frm, row);
			},
			__("Pinelabs")
		);
		debug_log("button_added", {
			doctype: frm.doctype,
			docname: frm.docname || (frm.doc && frm.doc.name),
			button_label: button_label,
			group: "Pinelabs",
		});
	}

	function should_show(frm, row) {
		return evaluate_visibility(frm, row).show;
	}

	function evaluate_visibility(frm, row) {
		var allowed_docstatuses = get_allowed_docstatuses(row);
		var current_docstatus = String((frm.doc && frm.doc.docstatus) || 0);

		if (!row) {
			return {
				show: false,
				reason: "missing_row",
				allowed_docstatuses: allowed_docstatuses,
			};
		}

		if (!row.is_enabled) {
			return {
				show: false,
				reason: "row_disabled",
				allowed_docstatuses: allowed_docstatuses,
			};
		}

		if (frm.doc.docstatus === 2) {
			return {
				show: false,
				reason: "cancelled_doc",
				allowed_docstatuses: allowed_docstatuses,
			};
		}

		if (allowed_docstatuses.indexOf(current_docstatus) === -1) {
			return {
				show: false,
				reason: "docstatus_not_allowed",
				allowed_docstatuses: allowed_docstatuses,
			};
		}

		return {
			show: true,
			reason: "allowed",
			allowed_docstatuses: allowed_docstatuses,
		};
	}

	function get_button_label(row) {
		return row.button_label || "Pay via Pinelabs";
	}

	function get_allowed_docstatuses(row) {
		return String(row.show_on_docstatus || "1")
			.split(",")
			.map(function (value) {
				return value.trim();
			})
			.filter(Boolean);
	}

	function get_payable_amount(frm, row) {
		var fieldname = row && row.amount_field;
		var amount = fieldname ? parseFloat(frm.doc[fieldname] || 0) : 0;
		return isNaN(amount) ? 0 : amount;
	}

	function validate_amount(frm, row) {
		debug_log("validate_amount", {
			doctype: frm.doctype,
			docname: frm.docname || (frm.doc && frm.doc.name),
			amount_field: row && row.amount_field,
			amount: get_payable_amount(frm, row),
		});

		if (!row.amount_field) {
			debug_log("validate_amount_failed", { reason: "missing_amount_field" });
			frappe.msgprint(__("Payable amount field is not configured."));
			return false;
		}

		if (get_payable_amount(frm, row) <= 0) {
			debug_log("validate_amount_failed", {
				reason: "non_positive_amount",
				amount_field: row.amount_field,
				amount: get_payable_amount(frm, row),
			});
			frappe.msgprint(__("Document has no payable amount."));
			return false;
		}

		return true;
	}

	function get_pinelabs_api() {
		var root = window.pinelabs_fin || {};
		var payment = root.payment || {};

		return {
			get_enabled_modes: root.get_enabled_modes || payment.get_enabled_modes,
			run_mode_of_payment_api: root.run_mode_of_payment_api || payment.run_mode_of_payment_api,
			trigger: root.trigger || payment.trigger,
		};
	}

	function open_mode_picker(frm, row) {
		var pinelabs = get_pinelabs_api();
		debug_log("open_mode_picker", {
			doctype: frm.doctype,
			docname: frm.docname || (frm.doc && frm.doc.name),
			has_get_enabled_modes: !!pinelabs.get_enabled_modes,
		});

		if (!pinelabs.get_enabled_modes) {
			debug_log("open_mode_picker_failed", { reason: "missing_payment_helper" });
			frappe.msgprint(__("Pine Labs payment helper is not available."));
			return;
		}

		pinelabs.get_enabled_modes().then(function (modes) {
			debug_log("modes_loaded", {
				count: modes && modes.length,
				modes: modes,
			});

			if (!modes || !modes.length) {
				frappe.msgprint(__("No Pine Labs payment modes are configured."));
				return;
			}

			if (modes.length === 1) {
				fire_payment(frm, row, modes[0].mode_of_payment);
				return;
			}

			var d = new frappe.ui.Dialog({
				title: __("Select payment mode"),
				fields: [
					{
						fieldname: "mode_of_payment",
						fieldtype: "Select",
						label: __("Payment mode"),
						options: modes
							.map(function (m) {
								return m.mode_of_payment;
							})
							.join("\n"),
						reqd: 1,
					},
				],
				primary_action_label: __("Send to terminal"),
				primary_action: function (vals) {
					d.hide();
					fire_payment(frm, row, vals.mode_of_payment);
				},
			});

			d.show();
		});
	}

	function build_context(row) {
		return {
			payable_doctype_config: {
				doctype_name: row.doctype_name,
				button_label: get_button_label(row),
				show_on_docstatus: row.show_on_docstatus || "1",
				amount_field: row.amount_field,
				status_field: row.status_field,
				paid_status_value: row.paid_status_value,
				link_payment_entry_field: row.link_payment_entry_field,
				finalize_via_payment_entry: row.finalize_via_payment_entry,
			},
		};
	}

	function fire_payment(frm, row, mode_of_payment) {
		var pinelabs = get_pinelabs_api();
		var amount = get_payable_amount(frm, row);
		var context = build_context(row);
		debug_log("fire_payment", {
			doctype: frm.doctype,
			docname: frm.docname || (frm.doc && frm.doc.name),
			mode_of_payment: mode_of_payment,
			amount: amount,
			context: context,
			has_run_mode_of_payment_api: !!pinelabs.run_mode_of_payment_api,
			has_trigger: !!pinelabs.trigger,
		});

		if (pinelabs.run_mode_of_payment_api) {
			pinelabs.run_mode_of_payment_api({
				frm: frm,
				mode_of_payment: mode_of_payment,
				amount: amount,
				context: context,
			});
			return;
		}

		if (!pinelabs.trigger) {
			frappe.msgprint(__("Pine Labs payment helper is not available."));
			return;
		}

		pinelabs.trigger({
			source_doctype: frm.doctype,
			source_docname: frm.doc.name || frm.docname,
			amount: amount,
			mode_of_payment: mode_of_payment,
			context: context,
		});
	}

	function setup_debug_api() {
		window.pinelabs_fin_debug = window.pinelabs_fin_debug || {
			enable: function () {
				set_debug_enabled(true);
				console.info("[Pinelabs Debug] enabled");
			},
			disable: function () {
				set_debug_enabled(false);
				console.info("[Pinelabs Debug] disabled");
			},
			is_enabled: function () {
				return is_debug_enabled();
			},
			get_state: function () {
				return window.__pinelabs_fin_debug_state || {};
			},
		};
	}

	function set_debug_state(values) {
		window.__pinelabs_fin_debug_state = Object.assign(
			{},
			window.__pinelabs_fin_debug_state || {},
			values || {}
		);
	}

	function debug_log(event_name, payload) {
		if (!is_debug_enabled()) return;

		console.log("[Pinelabs Injector]", event_name, payload || {});
	}

	function is_debug_enabled() {
		try {
			if (
				window.location &&
				window.location.search &&
				window.location.search.indexOf("pinelabs_fin_debug=1") !== -1
			) {
				return true;
			}

			return window.localStorage && window.localStorage.getItem(DEBUG_KEY) === "1";
		} catch (error) {
			return false;
		}
	}

	function set_debug_enabled(enabled) {
		try {
			if (!window.localStorage) return;

			if (enabled) {
				window.localStorage.setItem(DEBUG_KEY, "1");
			} else {
				window.localStorage.removeItem(DEBUG_KEY);
			}
		} catch (error) {
			// ignore localStorage access errors
		}
	}

	function stringify_error(error) {
		if (!error) return null;
		if (error.message) return error.message;
		return String(error);
	}
})();
