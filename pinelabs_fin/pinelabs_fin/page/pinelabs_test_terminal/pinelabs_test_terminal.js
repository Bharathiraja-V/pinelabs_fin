frappe.pages['pinelabs_test_terminal'].on_page_load = function(wrapper) {
	// Initialize the page using the standard Frappe UI factory
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Pinelabs Test Terminal'),
		single_column: true
	});

	// 1. Render the main UI container
	page.main.html(`
		<div style="padding: 40px; max-width: 600px; margin: 0 auto; text-align: center;">
			<div style="background: #f8f9fa; padding: 30px; border-radius: 10px; border: 1px solid #d1d8dd;">
				<h3 style="margin-bottom: 20px;">${__("Pine Labs Terminal Test")}</h3>
				<div class="form-group">
					<label style="font-weight: bold; display: block; margin-bottom: 10px;">${__("Enter Amount to Pay")}</label>
					<input type="number" id="test_amount_input" class="form-control" value="100.00" 
						style="font-size: 32px; height: 60px; text-align: center; font-weight: bold; color: #1d2124;">
				</div>
				<p class="text-muted" style="margin-top: 15px;">
					${__("Click a button below to send the amount to your Pine Labs machine.")}
				</p>
				
				<div style="display: flex; gap: 15px; margin-top: 30px;">
					<button class="btn btn-primary btn-lg" id="btn_trigger_upi" style="flex: 1; height: 60px; font-weight: bold;">
						<i class="fa fa-qrcode"></i> ${__("Pay via UPI")}
					</button>
					<button class="btn btn-default btn-lg" id="btn_trigger_card" style="flex: 1; height: 60px; font-weight: bold; border: 1px solid #ced4da;">
						<i class="fa fa-credit-card"></i> ${__("Pay via Card")}
					</button>
				</div>
			</div>
		</div>
	`);

	// 2. Event Handlers for the buttons
	page.main.find('#btn_trigger_upi').on('click', function() {
		run_payment(11, "UPI");
	});

	page.main.find('#btn_trigger_card').on('click', function() {
		run_payment(1, "Card");
	});

	function run_payment(code, label) {
		const amount = parseFloat(page.main.find('#test_amount_input').val());
		
		if (!amount || amount <= 0) {
			frappe.msgprint(__('Please enter a valid amount.'));
			return;
		}

		console.log(`Starting ${label} payment for ${amount}`);

		frappe.call({
			method: "pinelabs_fin.api.pos_endpoints.initiate_payment",
			args: {
				invoice_name: "TEST-" + Math.floor(Math.random() * 1000000),
				amount: amount,
				source_doctype: "Sales Invoice", 
				payment_mode: label,
				allowed_payment_mode: code
			},
			freeze: true,
			freeze_message: __("Connecting to terminal..."),
			callback: function(r) {
				if (r.message && r.message.success) {
					open_status_dialog(r.message.transaction_reference, amount);
				} else {
					frappe.msgprint(r.message ? r.message.error : __("Connection failed"));
				}
			}
		});
	}

	function open_status_dialog(ref, amount) {
		let dialog = new frappe.ui.Dialog({
			title: __('Terminal Status'),
			fields: [
				{
					fieldname: 'html',
					fieldtype: 'HTML',
					options: `<div class="text-center" style="padding: 20px;">
						<div class="spinner-border text-primary"></div>
						<h4 class="mt-3">${__("Terminal Ready")}</h4>
						<p>${__("Processing")} <b>${amount}</b></p>
						<p class="text-muted small">${ref}</p>
					</div>`
				}
			]
		});

		dialog.show();

		let poll = setInterval(() => {
			frappe.call({
				method: "pinelabs_fin.api.pos_endpoints.check_payment_status",
				args: { transaction_reference: ref },
				callback: function(res) {
					const status = res.message && res.message.status;
					if (status === "Approved") {
						clearInterval(poll);
						dialog.fields_dict.html.$wrapper.html(`
							<div class="text-center text-success" style="padding: 20px;">
								<i class="fa fa-check-circle fa-4x"></i>
								<h3 class="mt-3">${__("Approved!")}</h3>
							</div>
						`);
						setTimeout(() => dialog.hide(), 2000);
					} else if (["Declined", "Failed", "Timeout", "Cancelled"].includes(status)) {
						clearInterval(poll);
						dialog.fields_dict.html.$wrapper.html(`
							<div class="text-center text-danger" style="padding: 20px;">
								<i class="fa fa-times-circle fa-4x"></i>
								<h3 class="mt-3">${__(status)}</h3>
							</div>
						`);
					}
				}
			});
		}, 3000);

		dialog.onhide = () => clearInterval(poll);
	}
};
