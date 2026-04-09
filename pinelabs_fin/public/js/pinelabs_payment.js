/**
 * Shared PineLabs Mode of Payment flow helpers.
 *
 * This file exposes an explicit API only. Another app or custom script can
 * call these helpers when it decides the flow should start.
 */

frappe.provide("pinelabs_fin.payment");

(function () {
	const payment = pinelabs_fin.payment;
	const METHODS = {
		get_modes: "pinelabs_fin.api.pos_endpoints.get_pinelabs_payment_modes",
		get_context: "pinelabs_fin.api.pos_endpoints.get_trigger_context",
		initiate_payment: "pinelabs_fin.api.pos_endpoints.initiate_payment",
		check_status: "pinelabs_fin.api.pos_endpoints.check_payment_status",
		cancel_payment: "pinelabs_fin.api.pos_endpoints.cancel_payment",
	};
	const TERMINAL_POLL_INTERVAL = 3000;

	payment._modes_cache = null;
	payment._in_flight = {};

	function call(method, args) {
		return new Promise(function (resolve, reject) {
			frappe.call({
				method: method,
				args: args || {},
				callback: function (response) {
					resolve(response.message);
				},
				error: reject,
			});
		});
	}

	function show_message(title, message, indicator) {
		frappe.msgprint({
			title: title,
			message: message,
			indicator: indicator || "blue",
		});
	}

	function show_validation_message(message) {
		show_message(__("PineLabs Validation"), message, "orange");
	}

	function show_failure_message(message) {
		show_message(__("Payment Failed"), message, "red");
	}

	function get_in_flight_key(source_doctype, source_docname) {
		return [source_doctype, source_docname].join(":");
	}

	function clear_in_flight(key) {
		if (key) {
			delete payment._in_flight[key];
		}
	}

	function find_mode_config(mode_of_payment, payment_modes) {
		return (payment_modes || []).find(function (mode) {
			return mode.mode_of_payment === mode_of_payment;
		}) || null;
	}

	function normalize_mode_text(value) {
		return (value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
	}

	function looks_like_pinelabs_mode(mode_of_payment) {
		const value = (mode_of_payment || "").toLowerCase();
		return value.includes("pinelabs") || value.includes("pine labs");
	}

	function normalize_flow_options(opts) {
		const options = opts || {};
		const frm = options.frm;
		const doc = frm && frm.doc ? frm.doc : null;

		return Object.assign({}, options, {
			frm: frm,
			doc: doc,
			mode_of_payment: options.mode_of_payment || (doc && doc.mode_of_payment) || null,
			context: options.context || null,
			machine_name: options.machine_name || null,
		});
	}

	function ensure_document_is_ready(options, mode_config) {
		if (!options.frm || !options.doc) {
			show_validation_message(__("Document details are required."));
			if (options.on_invalid) {
				options.on_invalid({ reason: "missing_document" }, mode_config);
			}
			return false;
		}

		if (options.frm.is_new()) {
			show_validation_message(__("Document must be saved before PineLabs payment can start."));
			if (options.on_invalid) {
				options.on_invalid({ reason: "doc_unsaved" }, mode_config);
			}
			return false;
		}

		return true;
	}

	payment.get_enabled_modes = async function () {
		if (Array.isArray(payment._modes_cache)) {
			return payment._modes_cache;
		}

		try {
			const modes = await call(METHODS.get_modes);
			payment._modes_cache = Array.isArray(modes) ? modes : [];
		} catch (error) {
			payment._modes_cache = [];
		}

		return payment._modes_cache;
	};

	payment.get_mode_config = async function (mode_of_payment) {
		const payment_modes = await payment.get_enabled_modes();
		return find_mode_config(mode_of_payment, payment_modes);
	};

	payment.get_upi_mode_config = async function () {
		const payment_modes = await payment.get_enabled_modes();

		return (
			(payment_modes || []).find(function (mode) {
				const mode_name = normalize_mode_text(mode.mode_of_payment);
				const label = normalize_mode_text(mode.label);
				return mode_name.includes("upi") || label.includes("upi");
			}) || null
		);
	};

	payment.get_trigger_context = function (options) {
		return call(METHODS.get_context, {
			doctype: options.doc.doctype,
			docname: options.doc.name,
			mode_of_payment: options.mode_of_payment,
			context: options.context,
		});
	};

	payment.run_mode_of_payment_api = async function (opts) {
		const options = normalize_flow_options(opts);
		if (!options.mode_of_payment) {
			return null;
		}

		const payment_modes = await payment.get_enabled_modes();
		const mode_config = find_mode_config(options.mode_of_payment, payment_modes);

		if (!mode_config && !looks_like_pinelabs_mode(options.mode_of_payment)) {
			return null;
		}

		if (!mode_config) {
			show_validation_message(__("Selected Mode of Payment is not configured in Pinelabs Settings."));
			if (options.on_invalid) {
				options.on_invalid({ reason: "mode_not_configured" }, null);
			}
			return null;
		}

		if (!ensure_document_is_ready(options, mode_config)) {
			return null;
		}

		try {
			const context = await payment.get_trigger_context(options);
			if (!context || !context.can_trigger) {
				show_validation_message(
					(context && context.reason) || __("Document is not eligible for PineLabs payment.")
				);
				if (options.on_invalid) {
					options.on_invalid(context || { reason: "not_eligible" }, mode_config);
				}
				return context;
			}

			return payment.trigger({
				source_doctype: options.doc.doctype,
				source_docname: options.doc.name,
				amount: context.payable_amount,
				mode_of_payment: mode_config.mode_of_payment,
				allowed_payment_mode: mode_config.allowed_payment_mode,
				payment_mode_label: mode_config.label,
				machine_name: options.machine_name,
				context: options.context,
				on_success: function (result) {
					if (options.on_success) {
						options.on_success(result, context, mode_config);
					}
				},
				on_failure: function (result) {
					if (options.on_failure) {
						options.on_failure(result, context, mode_config);
					}
				},
				on_cancel: function (result) {
					if (options.on_cancel) {
						options.on_cancel(result, context, mode_config);
					}
				},
			});
		} catch (error) {
			show_validation_message(__("Unable to validate PineLabs configuration right now."));
			if (options.on_invalid) {
				options.on_invalid({ reason: "validation_request_failed" }, mode_config);
			}
			return null;
		}
	};

	payment.trigger = async function (opts) {
		const options = opts || {};
		if (!options.source_doctype || !options.source_docname) {
			show_validation_message(__("source_doctype and source_docname are required."));
			return null;
		}
		if (!options.mode_of_payment) {
			show_validation_message(__("mode_of_payment is required."));
			return null;
		}

		const in_flight_key = get_in_flight_key(options.source_doctype, options.source_docname);
		if (payment._in_flight[in_flight_key]) {
			frappe.show_alert({
				message: __("A PineLabs payment is already in progress for this document."),
				indicator: "orange",
			});
			return null;
		}

		payment._in_flight[in_flight_key] = true;

		try {
			const result = await call(METHODS.initiate_payment, {
				invoice_name: options.source_docname,
				amount: options.amount,
				payment_mode: options.payment_mode_label || "Card",
				allowed_payment_mode: options.allowed_payment_mode,
				mode_of_payment: options.mode_of_payment,
				source_doctype: options.source_doctype,
				source_docname: options.source_docname,
				machine_name: options.machine_name,
				context: options.context,
			});

			if (!result || !result.success) {
				clear_in_flight(in_flight_key);
				const error_message = (result && result.error) || __("Unknown error");
				show_failure_message(error_message);
				if (options.on_failure) {
					options.on_failure(result || { error: error_message });
				}
				return result || null;
			}

			payment.show_polling_dialog(result.transaction_reference, options, in_flight_key);
			return result;
		} catch (error) {
			clear_in_flight(in_flight_key);
			show_failure_message(__("Failed to initiate payment request."));
			if (options.on_failure) {
				options.on_failure({ error: "request_failed" });
			}
			return null;
		}
	};

	payment.show_polling_dialog = function (transaction_reference, options, in_flight_key) {
		const label = options.payment_mode_label || __("Payment");
		const dialog = new frappe.ui.Dialog({
			title: __("PineLabs {0} Payment", [label]),
			fields: [
				{
					fieldname: "status_html",
					fieldtype: "HTML",
					options:
						'<div class="text-center">' +
						'<div class="spinner-border text-primary" role="status"></div>' +
						"<p class='mt-3'>" +
						__("Waiting for customer to complete {0} payment on terminal...", [label]) +
						"</p></div>",
				},
			],
		});

		dialog.set_secondary_action_label(__("Cancel Payment"));
		dialog.set_secondary_action(function () {
			payment.cancel_transaction(dialog, transaction_reference, options, in_flight_key);
		});

		dialog.show();
		dialog.$wrapper.find(".modal-dialog").css("max-width", "400px");

		dialog._poll_interval = setInterval(function () {
			payment.poll_transaction_status(dialog, transaction_reference, options, in_flight_key);
		}, TERMINAL_POLL_INTERVAL);

		dialog.onhide = function () {
			clearInterval(dialog._poll_interval);
			clear_in_flight(in_flight_key);
		};
	};

	payment.cancel_transaction = async function (dialog, transaction_reference, options, in_flight_key) {
		clearInterval(dialog._poll_interval);

		try {
			const result = await call(METHODS.cancel_payment, {
				transaction_reference: transaction_reference,
			});
			dialog.hide();
			clear_in_flight(in_flight_key);

			if (result && result.success) {
				frappe.show_alert({
					message: __("Payment cancelled."),
					indicator: "orange",
				});
			}

			if (options.on_cancel) {
				options.on_cancel(result);
			}
		} catch (error) {
			dialog.hide();
			clear_in_flight(in_flight_key);
			show_failure_message(__("Failed to cancel payment."));
			if (options.on_failure) {
				options.on_failure({ error: "cancel_request_failed" });
			}
		}
	};

	payment.poll_transaction_status = async function (dialog, transaction_reference, options, in_flight_key) {
		try {
			const result = await call(METHODS.check_status, {
				transaction_reference: transaction_reference,
			});
			if (!result) {
				return;
			}

			const status = (result.status || "").toUpperCase();
			const label = options.payment_mode_label || __("Payment");

			if (status === "APPROVED") {
				clearInterval(dialog._poll_interval);
				clear_in_flight(in_flight_key);

				const payment_entry_info = result.payment_entry
					? " | " + __("Payment Entry: {0}", [result.payment_entry])
					: "";

				dialog.fields_dict.status_html.$wrapper.html(
					'<div class="text-center text-success">' +
						'<i class="fa fa-check-circle fa-3x"></i>' +
						"<p class='mt-3'><strong>" +
						__("{0} Payment Successful", [label]) +
						"</strong></p>" +
						"<p>" +
						frappe.format(result.amount || options.amount, { fieldtype: "Currency" }) +
						payment_entry_info +
						"</p></div>"
				);
				dialog.$wrapper.find(".btn-secondary-dark, .btn-secondary").hide();

				setTimeout(function () {
					dialog.hide();
					if (options.on_success) {
						options.on_success(result);
					}
				}, 2000);
				return;
			}

			if (["DECLINED", "FAILED", "TIMEOUT", "CANCELLED"].includes(status)) {
				clearInterval(dialog._poll_interval);
				clear_in_flight(in_flight_key);

				dialog.fields_dict.status_html.$wrapper.html(
					'<div class="text-center text-danger">' +
						'<i class="fa fa-times-circle fa-3x"></i>' +
						"<p class='mt-3'><strong>" + __(status) + "</strong></p>" +
						"<p>" + (result.error || result.message || __("Payment was not successful.")) + "</p></div>"
				);
				dialog.set_secondary_action_label(__("Close"));
				dialog.set_secondary_action(function () {
					dialog.hide();
				});

				if (options.on_failure) {
					options.on_failure(result);
				}
			}
		} catch (error) {
			clearInterval(dialog._poll_interval);
			clear_in_flight(in_flight_key);
			dialog.hide();
			show_failure_message(__("Failed to fetch transaction status."));
			if (options.on_failure) {
				options.on_failure({ error: "status_request_failed" });
			}
		}
	};

	payment.handle_mode_selection = payment.run_mode_of_payment_api;
	payment.validate_mode_selection = payment.run_mode_of_payment_api;
	payment._find_mode_config = find_mode_config;
	payment._looks_like_pinelabs_mode = looks_like_pinelabs_mode;
})();
