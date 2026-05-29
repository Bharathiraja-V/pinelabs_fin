// -*- coding: utf-8 -*-
// Cypress UI tests for the "Waiting for terminal" failure UX added in
// pinelabs_buttons.js. When a Pine Labs terminal payment ends in FAILED
// (declined, aged-out, timeout) the locked dialog now stays open and shows
// Retry + Close instead of auto-hiding. Verified scenarios:
//
//   1. FAILED → modal stays open, shows failure reason + Retry + Close.
//   2. Close → modal hides, on_settled fires with "FAILED".
//   3. Retry → fetches txn details, re-initiates, new modal opens for new txn.
//
// Run via:
//   bench --site <site> run-ui-tests pinelabs_fin

context("Pinelabs Terminal Dialog — Failure UX", () => {
	const FAKE_TXN = "PLT-CYTEST-FAILURE";
	const FAILURE_REASON = "Card declined by issuer (test)";

	before(() => {
		cy.login();
		// Visit any desk page so app_include_js (pinelabs_buttons.js) loads
		// and frappe.pinelabs.open_terminal_modal is available on window.
		cy.visit("/app");
	});

	beforeEach(() => {
		// Per-test isolation: stub the network calls the dialog will make.
		// `cy.intercept` only matches requests fired AFTER it's set up, so
		// we put these before any window().then() that opens the modal.
		cy.intercept(
			"POST",
			"/api/method/pinelabs_fin.api.terminal.check_terminal_status",
			{
				statusCode: 200,
				body: {
					message: {
						success: true,
						status: "FAILED",
						response_code: 1,
						message: FAILURE_REASON,
					},
				},
			},
		).as("checkStatus");
	});

	afterEach(() => {
		// Close any leftover modal so the next test starts clean. Idempotent
		// per-modal cleanup is already wired in pinelabs_buttons.js, but a
		// belt-and-braces dismiss here keeps the spec resilient if an
		// assertion failed mid-modal.
		cy.window().then((win) => {
			try {
				win.$(".modal.show").modal("hide");
			} catch (_) { /* ignore */ }
		});
	});

	it("keeps the dialog open on FAILED and shows Retry + Close", () => {
		cy.window().then((win) => {
			win.frappe.pinelabs.open_terminal_modal(FAKE_TXN);
		});

		// Dialog appears with the initial "Waiting for terminal" body.
		cy.get(".modal.show").should("be.visible");
		cy.get(".modal-title").should("contain", "Waiting for terminal");

		// First poll fires at +500ms — wait for it to land and the
		// FAILED-branch to swap the body to the failure card.
		cy.wait("@checkStatus");

		cy.get(".modal-body", { timeout: 5000 }).should("contain", "Payment failed");
		cy.get(".modal-body").should("contain", FAILURE_REASON);

		// Both action buttons are present.
		cy.get(".modal-footer button").contains("Retry").should("be.visible");
		cy.get(".modal-footer button").contains("Close").should("be.visible");

		// Modal is still open (NOT auto-hidden the way SUCCESS/CANCELLED are).
		cy.get(".modal.show").should("be.visible");
	});

	it("Close button fires on_settled('FAILED', txn) and hides the modal", () => {
		// Spy on the on_settled callback to verify it receives the right args.
		cy.window().then((win) => {
			win.__pinelabs_test_on_settled = null;
			win.frappe.pinelabs.open_terminal_modal(FAKE_TXN, {
				on_settled: (status, txn) => {
					win.__pinelabs_test_on_settled = { status, txn };
				},
			});
		});

		cy.wait("@checkStatus");
		cy.get(".modal-body").should("contain", "Payment failed");

		cy.get(".modal-footer button").contains("Close").click();

		// Modal must close.
		cy.get(".modal.show").should("not.exist");

		// on_settled must have fired with the failed txn details.
		cy.window().then((win) => {
			expect(win.__pinelabs_test_on_settled).to.not.be.null;
			expect(win.__pinelabs_test_on_settled.status).to.equal("FAILED");
			expect(win.__pinelabs_test_on_settled.txn).to.equal(FAKE_TXN);
		});
	});

	it("Retry button fetches txn details and re-initiates with same Mode of Payment", () => {
		// retry_payment() fetches reference_doctype/reference_name/payment_method.
		cy.intercept(
			"POST",
			"/api/method/frappe.client.get_value",
			(req) => {
				if (req.body && /Pinelabs Transaction/.test(req.body)) {
					req.reply({
						message: {
							reference_doctype: "Sales Invoice",
							reference_name: "SINV-CYTEST-001",
							payment_method: "CARD",
						},
					});
				} else {
					req.continue();
				}
			},
		).as("getValue");

		// start_terminal_payment will call initiate_terminal_payment for the
		// retry. Return a fresh PLT- name so the response-shape interceptor
		// opens a new modal for it.
		const RETRY_TXN = "PLT-CYTEST-RETRY";
		cy.intercept(
			"POST",
			"/api/method/pinelabs_fin.api.terminal.initiate_terminal_payment",
			{
				statusCode: 200,
				body: {
					message: {
						success: true,
						transaction_name: RETRY_TXN,
						status: "PENDING",
						response_code: 0,
						message: "ok",
					},
				},
			},
		).as("retryInitiate");

		cy.window().then((win) => {
			win.frappe.pinelabs.open_terminal_modal(FAKE_TXN);
		});

		cy.wait("@checkStatus");
		cy.get(".modal-body").should("contain", "Payment failed");

		cy.get(".modal-footer button").contains("Retry").click();

		// Both API calls must fire in order: fetch details → re-initiate.
		cy.wait("@getValue").its("request.body").should("contain", "Pinelabs Transaction");
		cy.wait("@retryInitiate")
			.its("request.body")
			.should("contain", "PineLabs - Card");  // CARD → "PineLabs - Card" mapping

		// New modal opens for the new txn (response-shape interceptor +
		// idempotency registry handles this automatically).
		cy.get(".modal-body", { timeout: 5000 }).should("contain", RETRY_TXN);
	});

	it("Retry surfaces a clear error if the failed txn has no recognised payment_method", () => {
		// Make get_value return a bogus payment_method — retry_payment must
		// refuse rather than silently call initiate with garbage.
		cy.intercept(
			"POST",
			"/api/method/frappe.client.get_value",
			{
				statusCode: 200,
				body: {
					message: {
						reference_doctype: "Sales Invoice",
						reference_name: "SINV-CYTEST-001",
						payment_method: "XYZ-UNKNOWN",
					},
				},
			},
		).as("getValueBad");

		// initiate must NOT be called.
		cy.intercept(
			"POST",
			"/api/method/pinelabs_fin.api.terminal.initiate_terminal_payment",
			cy.spy().as("initiateSpy"),
		);

		cy.window().then((win) => {
			win.frappe.pinelabs.open_terminal_modal(FAKE_TXN);
		});
		cy.wait("@checkStatus");
		cy.get(".modal-footer button").contains("Retry").click();
		cy.wait("@getValueBad");

		// User-visible error must appear (frappe.msgprint shows in a modal
		// or alert). Assert the indicator-red text shows up.
		cy.contains("Cannot retry").should("be.visible");

		// initiate_terminal_payment must not have been called for the retry.
		cy.get("@initiateSpy").should("not.have.been.called");
	});
});
