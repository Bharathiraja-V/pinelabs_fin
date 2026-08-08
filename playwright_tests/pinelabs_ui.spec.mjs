// Playwright UI test suite for pinelabs_fin — drives the Frappe Desk like a user.
// Maps to the manual test cases in pinelabs_test_cases.xlsx (UI-possible subset).
// Auth via storageState.json (minted session). Fixtures from fixtures.json
// (created by setup_fixtures.py). Run single-worker; some tests share data.
import { test, expect } from '@playwright/test';
import fs from 'fs';

const F = JSON.parse(fs.readFileSync(new URL('./fixtures.json', import.meta.url)));
const DT = encodeURIComponent('Pinelabs Transaction');

// ── helpers ────────────────────────────────────────────────────────────
async function openForm(page, route) {
  await page.goto(`/app/${route}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.page-head, .layout-main', { timeout: 30000 });
  await page.waitForTimeout(3500); // let the refresh event + custom buttons render
}

async function hasButton(page, label) {
  return (await page.locator(`button:has-text("${label}"), .btn:has-text("${label}")`).count()) > 0;
}

async function persistedStatus(page, txn) {
  return page.evaluate(async ({ dt, t }) => {
    const r = await fetch(`/api/resource/${dt}/${t}?fields=["status"]`);
    const j = await r.json();
    return (j.data && j.data.status) || '?';
  }, { dt: DT, t: txn });
}

async function editSelectAndSave(page, txn, fieldname, target) {
  await openForm(page, `pinelabs-transaction/${txn}`);
  const sel = page.locator(`[data-fieldname="${fieldname}"] select`).first();
  await sel.selectOption(target).catch(async () => { await sel.selectOption({ label: target }); });
  await page.waitForTimeout(400);
  await page.keyboard.press('Control+s');
  await page.waitForTimeout(2500);
  let errText = '';
  const modal = page.locator('.modal.show .modal-body, .msgprint');
  if (await modal.count()) errText = (await modal.first().innerText().catch(() => '')).trim();
  return errText.replace(/\s+/g, ' ');
}

// Open Pay on Machine, optionally pick a mode, fire Start Payment; capture the
// initiate_terminal_payment response text.
async function payOnMachine(page, invoice, mode) {
  let raw = '';
  const onResp = async (r) => {
    if (r.url().includes('initiate_terminal_payment')) {
      try { raw = await r.text(); } catch { /* ignore */ }
    }
  };
  page.on('response', onResp);
  await openForm(page, `sales-invoice/${invoice}`);
  await page.locator('button:has-text("Pay on Machine")').first().click();
  await page.waitForTimeout(1200);
  if (mode) {
    const input = page.locator('.modal.show [data-fieldname="mode_of_payment"] input').first();
    await input.click();
    await input.fill(mode);
    await page.waitForTimeout(1500);
    await page
      .locator(`.modal.show [data-fieldname="mode_of_payment"] .awesomplete li:has-text("${mode}"), ul[role="listbox"] li:has-text("${mode}")`)
      .first().click().catch(() => {});
    await page.waitForTimeout(500);
  }
  const fired = await page.evaluate(() => {
    if (!window.cur_dialog) return 'no-dialog';
    const vals = cur_dialog.get_values(); // null when a required field (mode) is unset
    if (!vals) return 'no-values';
    cur_dialog.primary_action(vals);
    return 'fired';
  });
  await page.waitForTimeout(4500);
  page.off('response', onResp);
  return { fired, raw };
}

// ── Sanity: authenticated ──────────────────────────────────────────────
test('AUTH: session is logged in as Administrator', async ({ page }) => {
  await page.goto('/api/method/frappe.auth.get_logged_user');
  const body = await page.textContent('body');
  expect(body).toContain('Administrator');
});

// ── Buttons ────────────────────────────────────────────────────────────
test.describe('Buttons', () => {
  test('TC-BTN-01: both buttons on a submitted unpaid invoice', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_clean}`);
    expect(await hasButton(page, 'Pay on Machine')).toBe(true);
    expect(await hasButton(page, 'Send Payment Link')).toBe(true);
  });

  test('TC-BTN-02: no buttons on a fully paid invoice', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_paid}`);
    expect(await hasButton(page, 'Pay on Machine')).toBe(false);
    expect(await hasButton(page, 'Send Payment Link')).toBe(false);
  });

  test('TC-BTN-03: buttons render on a draft (show_on_docstatus=0,1)', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_draft}`);
    // BUG-13 refuted: outstanding_amount is populated on a saved draft, so buttons appear.
    expect(await hasButton(page, 'Pay on Machine')).toBe(true);
    expect(await hasButton(page, 'Send Payment Link')).toBe(true);
  });

  test('TC-BTN-05: no buttons on a cancelled invoice', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_cancelled}`);
    expect(await hasButton(page, 'Pay on Machine')).toBe(false);
    expect(await hasButton(page, 'Send Payment Link')).toBe(false);
  });

  test('TC-BTN-07: no buttons on a doctype without a Payable row', async ({ page }) => {
    await openForm(page, `customer/${encodeURIComponent(F.non_payable_doctype_record)}`);
    expect(await hasButton(page, 'Pay on Machine')).toBe(false);
    expect(await hasButton(page, 'Send Payment Link')).toBe(false);
  });
});

// ── State machine (Pinelabs Transaction form) ──────────────────────────
test.describe('State machine', () => {
  // TC-SM-02 (manual->SUCCESS) and TC-SM-03 (illegal transition) are no longer
  // UI-drivable now that the status field is read-only (see TC-SM-04). The
  // controller-level transition guard is covered by test_backend_api.py
  // (BE-SM-02 / BE-SM-03).

  test('TC-SM-05: changing flow_type after creation is rejected', async ({ page }) => {
    const err = await editSelectAndSave(page, F.txn_reject, 'flow_type', 'Plural');
    expect(err).toMatch(/flow_type is locked after creation/i);
  });

  test('TC-SM-04: manual PENDING->FAILED no longer takes effect (BUG-03 fixed)', async ({ page }) => {
    await openForm(page, `pinelabs-transaction/${F.txn_fail}`);
    // The status field is now read-only in the UI...
    const readOnly = await page.evaluate(() => {
      const f = window.cur_frm && cur_frm.get_field ? cur_frm.get_field('status') : null;
      return f ? !!f.df.read_only : null;
    });
    expect(readOnly).toBe(true);
    // ...and even a forced Select change + save cannot flip it (controller guard).
    const sel = page.locator('[data-fieldname="status"] select').first();
    if (await sel.count()) {
      await sel.selectOption('FAILED').catch(() => {});
      await page.keyboard.press('Control+s');
      await page.waitForTimeout(2000);
    }
    expect(await persistedStatus(page, F.txn_fail)).toBe('PENDING');
  });

  test('TC-SM-06: deleting a transaction linked to a Payment Entry is blocked', async ({ page }) => {
    await openForm(page, `pinelabs-transaction/${F.txn_with_pe}`);
    // Trigger delete via the form and confirm the framework blocks it.
    const blocked = await page.evaluate(async (t) => {
      try {
        await frappe.call({ method: 'frappe.client.delete', args: { doctype: 'Pinelabs Transaction', name: t } });
        return { ok: false }; // delete succeeded (unexpected)
      } catch (e) {
        return { ok: true, msg: (frappe.last_response && JSON.stringify(frappe.last_response)) || String(e) };
      }
    }, F.txn_with_pe);
    expect(blocked.ok).toBe(true);
    // Still exists.
    const stillThere = await page.evaluate(async ({ dt, t }) => {
      const r = await fetch(`/api/resource/${dt}/${t}`);
      return r.status === 200;
    }, { dt: DT, t: F.txn_with_pe });
    expect(stillThere).toBe(true);
  });
});

// ── Terminal (Pay on Machine) ──────────────────────────────────────────
test.describe('Terminal', () => {
  test('TC-TERM-03: Start Payment with no mode selected does not proceed', async ({ page }) => {
    const { fired } = await payOnMachine(page, F.inv_clean, null);
    expect(fired).toBe('no-values'); // required mode blocks the action
    // The Pay dialog is still open (not submitted).
    expect(await page.locator('.modal.show').count()).toBeGreaterThan(0);
  });

  test('TC-TERM-11: second attempt while one is in progress is refused', async ({ page }) => {
    const { fired, raw } = await payOnMachine(page, F.inv_active, 'PineLabs - Card');
    expect(fired).toBe('fired');
    expect(raw.toLowerCase()).toContain('in progress');
  });

  test('TC-TERM-01(init): Pay on Machine with Card creates a transaction', async ({ page }) => {
    // Verifies the UI -> server -> transaction flow. The gateway's accept/decline
    // outcome depends on the terminal/simulator (may be offline), so we assert a
    // transaction was CREATED for the invoice, not a specific status.
    const { fired } = await payOnMachine(page, F.inv_clean, 'PineLabs - Card');
    expect(fired).toBe('fired');
    const txns = await page.evaluate(async ({ dt, inv }) => {
      const r = await fetch(`/api/resource/${dt}?filters=[["reference_name","=","${inv}"],["flow_type","=","Terminal"]]&fields=["name","status"]`);
      const j = await r.json();
      return j.data || [];
    }, { dt: DT, inv: F.inv_clean });
    expect(txns.length).toBeGreaterThan(0);
  });
});

// ── Payment Link (Plural) ──────────────────────────────────────────────
test.describe('Payment Link', () => {
  test('TC-PLURAL-06: Send Payment Link on a draft is rejected (must be submitted)', async ({ page }) => {
    let raw = '';
    const onResp = async (r) => {
      if (r.url().includes('initiate_payment_link')) { try { raw = await r.text(); } catch { /* ignore */ } }
    };
    page.on('response', onResp);
    await openForm(page, `sales-invoice/${F.inv_draft}`); // draft, button visible (show_on_docstatus=0,1)
    await page.locator('button:has-text("Send Payment Link")').first().click();
    await page.waitForTimeout(4000);
    page.off('response', onResp);
    expect(raw.toLowerCase()).toContain('must be submitted');
  });
});

// ── Conflict guard ─────────────────────────────────────────────────────
test.describe('Conflict guard', () => {
  test('TC-CG-01: manual Payment Entry is blocked while a Pine Labs payment is active', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_active}`); // has an active Pinelabs Transaction
    // Build a Payment Entry against this invoice and try to save it — the
    // validate hook (conflict_guard) must refuse it.
    const result = await page.evaluate(async (inv) => {
      const r = await frappe.call({
        method: 'erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry',
        args: { dt: 'Sales Invoice', dn: inv },
      });
      const pe = r.message;
      pe.mode_of_payment = 'Cash';
      try {
        const doc = frappe.model.sync(pe)[0];
        await frappe.call({ method: 'frappe.client.save', args: { doc } });
        return { blocked: false };
      } catch (e) {
        const msgs = (frappe.last_response && frappe.last_response._server_messages) || '';
        return { blocked: true, msgs: String(msgs) };
      }
    }, F.inv_active);
    expect(result.blocked).toBe(true);
    expect(result.msgs.toLowerCase()).toContain('in progress');
  });
});
