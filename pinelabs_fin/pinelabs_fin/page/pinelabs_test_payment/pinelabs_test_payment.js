// Pinelabs Test Payment — desk page demoing the payment APIs from a custom UI.
//
// Three inputs (amount, email, mobile) + three buttons. Each button calls a
// different whitelisted endpoint with the right mode hardcoded:
//
//   Pay with Card  →  initiate_terminal_payment  + mode = PineLabs - Card
//   Pay with UPI   →  initiate_terminal_payment  + mode = PineLabs - UPI
//   Send Link      →  initiate_payment_link      + flow = all_methods
//
// Workflow on each click:
//   1. Create a fresh Test Payment record with the form inputs.
//   2. Submit it.
//   3. Call the matching API.
//   4. For Card/UPI: poll check_terminal_status until terminal state.
//      For Link:    show the generated payment_link_url.

frappe.pages["pinelabs-test-payment"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Pinelabs Test Payment"),
		single_column: true,
	});
	new PinelabsTestPaymentPage(page);
};

class PinelabsTestPaymentPage {
	constructor(page) {
		this.page = page;
		this.poll_timer = null;
		this.last_txn_name = null;
		this.render_layout();
		this.attach_handlers();
	}

	render_layout() {
		const $body = $(this.page.body);
		$body.empty();
		$body.append(`
			<div class="pinelabs-test-payment" style="max-width:680px; margin:24px auto; padding:0 16px;">

				<div class="form-card" style="background:#fff; border:1px solid #d1d9e0; border-radius:8px; padding:24px;">
					<h4 style="margin:0 0 4px;">${__("Take a Pine Labs payment")}</h4>
					<div class="text-muted" style="margin-bottom:20px;">
						${__("Fill the three fields below and click one of the buttons. Each button calls a different API.")}
					</div>

					<div class="form-row" style="margin-bottom:14px;">
						<label style="display:block; font-weight:600; margin-bottom:4px;">${__("Amount (INR)")}</label>
						<input type="number" min="1" step="0.01" class="form-control input-amount"
							placeholder="${__("e.g. 250.00")}" />
					</div>

					<div class="form-row" style="margin-bottom:14px;">
						<label style="display:block; font-weight:600; margin-bottom:4px;">${__("Customer")}</label>
						<div class="input-customer-wrapper"></div>
						<div class="text-muted" style="font-size:0.85em; margin-top:4px;">
							${__("Required when create_payment_entry = 1. Maps to Payment Entry's party (party_type = Customer).")}
						</div>
					</div>

					<div class="form-row" style="margin-bottom:14px;">
						<label style="display:block; font-weight:600; margin-bottom:4px;">${__("Customer Email")}</label>
						<input type="email" class="form-control input-email"
							placeholder="${__("customer@example.com")}" />
						<div class="text-muted" style="font-size:0.85em; margin-top:4px;">
							${__("Required only for Send Payment Link. Optional for Pay on Machine.")}
						</div>
					</div>

					<div class="form-row" style="margin-bottom:20px;">
						<label style="display:block; font-weight:600; margin-bottom:4px;">${__("Customer Mobile")}</label>
						<input type="tel" class="form-control input-mobile"
							placeholder="${__("10-digit mobile number")}" />
						<div class="text-muted" style="font-size:0.85em; margin-top:4px;">
							${__("Required only for Send Payment Link. Optional for Pay on Machine.")}
						</div>
					</div>

					<div class="button-row" style="display:flex; gap:10px;">
						<button class="btn btn-primary btn-lg btn-pay-upi" style="flex:1; padding:14px;">
							<i class="fa fa-qrcode" style="margin-right:6px;"></i>
							${__("Pay with UPI")}
						</button>
						<button class="btn btn-primary btn-lg btn-pay-card" style="flex:1; padding:14px;">
							<i class="fa fa-credit-card" style="margin-right:6px;"></i>
							${__("Pay with Card")}
						</button>
						<button class="btn btn-default btn-lg btn-send-link" style="flex:1; padding:14px;">
							<i class="fa fa-link" style="margin-right:6px;"></i>
							${__("Send Link")}
						</button>
					</div>
				</div>

				<div class="status-card" style="display:none; margin-top:16px; padding:16px; border:1px solid #d1d9e0; border-radius:8px; background:#fff;">
					<h5 style="margin:0 0 8px;">${__("Status")}</h5>
					<div class="status-body"></div>
					<div class="status-link" style="display:none; margin-top:10px;"></div>
				</div>
			</div>
		`);
	}

	attach_handlers() {
		const $body = $(this.page.body);

		// Render the Customer Link autocomplete inside its wrapper. Using
		// frappe.ui.form.make_control gives the same search-as-you-type
		// behaviour the standard form has — much friendlier than asking
		// the operator to type the Customer doc name verbatim.
		this.customer_field = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				fieldname: "customer",
				options: "Customer",
				placeholder: __("Pick a Customer"),
			},
			parent: $body.find(".input-customer-wrapper")[0],
			render_input: true,
		});

		$body.on("click", ".btn-pay-card", () =>
			this.handle_terminal("PineLabs - Card", "CARD"),
		);
		$body.on("click", ".btn-pay-upi", () =>
			this.handle_terminal("PineLabs - UPI", "UPI"),
		);
		$body.on("click", ".btn-send-link", () => this.handle_link());
	}

	// ──────────────────────────────────────────────────────────────────────
	// Form helpers
	// ──────────────────────────────────────────────────────────────────────

	read_inputs() {
		const $body = $(this.page.body);
		return {
			amount: parseFloat($body.find(".input-amount").val()) || 0,
			email: ($body.find(".input-email").val() || "").trim(),
			mobile: ($body.find(".input-mobile").val() || "").trim(),
			customer: (this.customer_field && this.customer_field.get_value()) || "",
		};
	}

	disable_buttons(disabled) {
		$(this.page.body)
			.find(".btn-pay-upi, .btn-pay-card, .btn-send-link")
			.prop("disabled", disabled)
			.toggleClass("disabled", disabled);
	}

	show_status(html_or_node) {
		const $body = $(this.page.body);
		$body.find(".status-card").show();
		$body.find(".status-body").html(html_or_node);
		$body.find(".status-link").hide().empty();
	}

	show_link(url) {
		const $body = $(this.page.body);
		$body
			.find(".status-link")
			.show()
			.html(
				`<a href="${frappe.utils.escape_html(url)}" target="_blank" rel="noopener">
					${frappe.utils.escape_html(url)}
				</a>`,
			);
	}

	// ──────────────────────────────────────────────────────────────────────
	// Save + submit a Test Payment record from the form inputs.
	//
	// This page does NOT duplicate the server's validation. Required-set
	// checks (amount > 0, customer contact for Plural, mode-of-payment
	// is PineLabs-enabled, finalization config, etc.) all live on the API
	// — that way every API caller (custom buttons, ERPNext hooks, external
	// systems) benefits from the same rules. Errors come back from the
	// API call below and are rendered via show_status(text-danger).
	// ──────────────────────────────────────────────────────────────────────

	async create_record({ amount, email, mobile, customer, label }) {
		const stamp = frappe.datetime.now_datetime().replace(/[: ]/g, "-");
		const doc = await frappe.db.insert({
			doctype: "Test Payment",
			title: `${label} ${stamp}`,
			amount,
			customer,
			customer_email: email,
			customer_mobile: mobile,
		});
		// Submit so docstatus = 1 — the API requires that.
		await frappe.call({
			method: "frappe.client.submit",
			args: { doc },
		});
		return doc.name;
	}

	// ──────────────────────────────────────────────────────────────────────
	// Button 1 + 2 — terminal payment (one button per mode of payment)
	// ──────────────────────────────────────────────────────────────────────

	async handle_terminal(mode_of_payment, label) {
		const inputs = this.read_inputs();
		// Fail fast with a helpful message if the shared utility isn't loaded
		// — usually a stale asset bundle that didn't pick up the latest
		// pinelabs_buttons.js. Tell the operator exactly how to fix it.
		if (typeof frappe.pinelabs?.start_terminal_payment !== "function") {
			this.show_status(
				`<div class="text-danger">
					<strong>${__("App utility missing")}.</strong>
					${__("The shared payment helper has not loaded in this browser tab.")}<br>
					<small>${__("Run")}: <code>bench build --app pinelabs_fin</code>
					${__("then hard-reload this page (Ctrl+Shift+R, or open DevTools → right-click reload → Empty Cache and Hard Reload).")}</small>
				</div>`,
			);
			return;
		}
		this.disable_buttons(true);
		this.show_status(
			`<div class="text-muted"><i class="fa fa-spinner fa-spin"></i>
				${__("Creating Test Payment record…")}</div>`,
		);
		try {
			const name = await this.create_record({ ...inputs, label });

			// Delegate to the global utility: it calls the API, opens the
			// shared "Waiting for terminal" modal on success, and invokes
			// our callbacks when the customer is done. No duplicated polling.
			frappe.pinelabs.start_terminal_payment(
				{
					reference_doctype: "Test Payment",
					reference_name: name,
					mode_of_payment,
					// Opt the test-page Card / UPI buttons into PE creation
					// on success. The server-side account-mapping pre-flight
					// fires before contacting Pine Labs (so if the Mode of
					// Payment's company-account isn't configured, the customer
					// is never asked to tap).
					create_payment_entry: 1,
				},
				{
					on_settled: (status, txn) => {
						this.disable_buttons(false);
						if (status === "SUCCESS") {
							// If the Payable Documents row is configured for
							// Test Payment, the server already wrote payment_status
							// = "Paid" via mapped-finalization. If not, the server
							// took the permissive path and left it alone — so we
							// nudge it ourselves here. Double-write is harmless
							// (same value). Errors are swallowed (read-only field
							// on submitted docs, etc.).
							frappe.db
								.set_value("Test Payment", name, "payment_status", "Paid")
								.catch(() => {});
							this.show_status(
								`<div class="text-success"><strong>${__("Payment SUCCESS")}</strong>
									· ${label}<br>
									<small>${__("Transaction")}: ${txn}</small></div>`,
							);
						} else {
							this.show_status(
								`<div class="text-danger"><strong>${__("Payment {0}", [status])}</strong>
									· ${label}<br>
									<small>${__("Transaction")}: ${txn}</small></div>`,
							);
						}
					},
					on_cancel: (txn) => {
						this.disable_buttons(false);
						this.show_status(
							`<div class="text-muted">${__("Cancelled")} · <small>${__("Transaction")}: ${txn}</small></div>`,
						);
					},
					on_error: (errmsg) => {
						this.disable_buttons(false);
						this.show_status(
							`<div class="text-danger"><strong>${__("Failed")}:</strong> ${frappe.utils.escape_html(errmsg)}</div>`,
						);
					},
				},
			);
		} catch (err) {
			this.show_status(
				`<div class="text-danger"><strong>${__("Error")}:</strong>
					${frappe.utils.escape_html(err.message || String(err))}</div>`,
			);
			this.disable_buttons(false);
		}
	}

	// ──────────────────────────────────────────────────────────────────────
	// Button 3 — send payment link
	// ──────────────────────────────────────────────────────────────────────

	async handle_link() {
		const inputs = this.read_inputs();
		// Same defensive check as handle_terminal — fail fast with a clear
		// message if the shared utility hasn't loaded into this browser tab.
		if (typeof frappe.pinelabs?.start_payment_link !== "function") {
			this.show_status(
				`<div class="text-danger">
					<strong>${__("App utility missing")}.</strong>
					${__("The shared payment helper has not loaded in this browser tab.")}<br>
					<small>${__("Run")}: <code>bench build --app pinelabs_fin</code>
					${__("then hard-reload this page (Ctrl+Shift+R, or open DevTools → right-click reload → Empty Cache and Hard Reload).")}</small>
				</div>`,
			);
			return;
		}
		this.disable_buttons(true);
		this.show_status(
			`<div class="text-muted"><i class="fa fa-spinner fa-spin"></i>
				${__("Creating Test Payment record…")}</div>`,
		);
		try {
			const name = await this.create_record({ ...inputs, label: "LINK" });

			this.show_status(
				`<div class="text-muted"><i class="fa fa-spinner fa-spin"></i>
					${__("Generating payment link…")}<br>
					<small>${__("Source doc")}: ${name}</small></div>`,
			);

			// Delegate to the global utility — same call the built-in Send
			// Payment Link button uses. The page passes Email / Mobile
			// directly to the API so the server's Customer Contact Field
			// Mapping resolver is bypassed for this caller.
			//
			// show_popup is left at its default (true) so the standard
			// green "Payment Link Generated" popup with the URL appears,
			// identical to the one on Sales Invoice's built-in button.
			frappe.pinelabs.start_payment_link(
				{
					reference_doctype: "Test Payment",
					reference_name: name,
					flow: "all_methods",
					customer_email: inputs.email,
					customer_mobile: inputs.mobile,
				},
				{
					on_link: (url, txn) => {
						this.disable_buttons(false);
						this.show_status(
							`<div class="text-success">
								<strong>${__("Link generated")}</strong>
								· ${__("Pine Labs has sent it to the customer by SMS and email.")}<br>
								<small>${__("Transaction")}: ${txn}</small>
							</div>`,
						);
						if (url) this.show_link(url);
					},
					on_error: (errmsg) => {
						this.disable_buttons(false);
						this.show_status(
							`<div class="text-danger"><strong>${__("Failed")}:</strong>
								${frappe.utils.escape_html(errmsg)}</div>`,
						);
					},
				},
			);
		} catch (err) {
			this.disable_buttons(false);
			this.show_status(
				`<div class="text-danger"><strong>${__("Error")}:</strong>
					${frappe.utils.escape_html(err.message || String(err))}</div>`,
			);
		}
	}
}
