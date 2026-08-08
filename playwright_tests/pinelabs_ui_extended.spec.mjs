// Extended Playwright UI suite — automates manual test cases from
// pinelabs_test_cases.xlsx that were previously "not executed" and are
// reachable WITHOUT a live terminal simulator or Plural credentials:
// config-driven button gating, Settings validation, credential masking,
// the payment-link contact guard, and the zero-outstanding guard.
//
// Any test that mutates shared config restores it in a finally block.
import { test, expect } from '@playwright/test';
import fs from 'fs';

const F = JSON.parse(fs.readFileSync(new URL('./fixtures.json', import.meta.url)));

// ── helpers ────────────────────────────────────────────────────────────
async function openForm(page, route) {
  await page.goto(`/app/${route}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.page-head, .layout-main', { timeout: 30000 });
  await page.waitForTimeout(3500);
}

async function hasButton(page, label) {
  return (await page.locator(`button:has-text("${label}"), .btn:has-text("${label}")`).count()) > 0;
}

// Read/patch the Sales Invoice row inside Pinelabs Settings.payable_doctypes.
// Returns the previous value so the caller can restore it.
async function setPayableRowField(page, fieldname, value) {
  return page.evaluate(async ({ fieldname, value }) => {
    const s = await frappe.call({
      method: 'frappe.client.get',
      args: { doctype: 'Pinelabs Settings', name: 'Pinelabs Settings' },
    });
    const row = (s.message.payable_doctypes || []).find((r) => r.doctype_name === 'Sales Invoice');
    if (!row) return null;
    const prev = row[fieldname];
    await frappe.call({
      method: 'frappe.client.set_value',
      args: { doctype: 'Pinelabs Payable Doctype', name: row.name, fieldname, value },
    });
    return prev;
  }, { fieldname, value });
}

// Call a whitelisted method and capture the server error message (if any).
async function callExpectingError(page, method, args) {
  return page.evaluate(async ({ method, args }) => {
    try {
      const r = await frappe.call({ method, args });
      return { ok: true, message: r && r.message };
    } catch (e) {
      const msgs = (frappe.last_response && frappe.last_response._server_messages) || '';
      return { ok: false, msgs: String(msgs), exc: String((frappe.last_response || {}).exception || e) };
    }
  }, { method, args });
}

// ── Button gating driven by Payable Documents config ───────────────────
test.describe('Button gating (config)', () => {
  test('TC-BTN-04: draft hidden when show_on_docstatus = "1"', async ({ page }) => {
    await openForm(page, 'sales-invoice/' + F.inv_draft);   // establishes a frappe session in-page
    const prev = await setPayableRowField(page, 'show_on_docstatus', '1');
    try {
      await openForm(page, `sales-invoice/${F.inv_draft}`);
      expect(await hasButton(page, 'Pay on Machine')).toBe(false);
      expect(await hasButton(page, 'Send Payment Link')).toBe(false);
    } finally {
      await setPayableRowField(page, 'show_on_docstatus', prev ?? '0,1');
    }
  });

  test('TC-BTN-06: only the enabled button renders', async ({ page }) => {
    await openForm(page, 'sales-invoice/' + F.inv_clean);
    const prev = await setPayableRowField(page, 'enable_send_payment_link', 0);
    try {
      await openForm(page, `sales-invoice/${F.inv_clean}`);
      expect(await hasButton(page, 'Pay on Machine')).toBe(true);
      expect(await hasButton(page, 'Send Payment Link')).toBe(false);
    } finally {
      await setPayableRowField(page, 'enable_send_payment_link', prev ?? 1);
    }
  });
});

// Map / unmap the PineLabs - Payment Link account for the company.
async function setPaymentLinkAccount(page, account /* null = remove */) {
  return page.evaluate(async ({ account }) => {
    const COMPANY = 'Pine Lab Machine';
    const s = await frappe.call({
      method: 'frappe.client.get',
      args: { doctype: 'Mode of Payment', name: 'PineLabs - Payment Link' },
    });
    const doc = s.message;
    doc.accounts = (doc.accounts || []).filter((a) => a.company !== COMPANY);
    if (account) {
      doc.accounts.push({ doctype: 'Mode of Payment Account', company: COMPANY, default_account: account });
    }
    await frappe.call({ method: 'frappe.client.save', args: { doc } });
    return true;
  }, { account });
}

// ── Payment link guards (both fire before Plural is ever contacted) ────
test.describe('Payment Link guards', () => {
  test('TC-PLURAL-08: unmapped Mode of Payment account is refused (fail-fast)', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_nocontact}`);
    // PineLabs - Payment Link has no account mapped on this site, so the
    // account pre-flight fires before any gateway call.
    const r = await callExpectingError(page, 'pinelabs_fin.api.plural.initiate_payment_link', {
      reference_doctype: 'Sales Invoice',
      reference_name: F.inv_nocontact,
    });
    expect(r.ok).toBe(false);
    expect((r.msgs + r.exc).toLowerCase()).toContain('default account');
  });

  test('TC-PLURAL-04: customer with no email/mobile is refused', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_nocontact}`);
    // Map the account so the pre-flight passes and the CONTACT guard is reached.
    await setPaymentLinkAccount(page, 'Cash - PLM');
    try {
      const r = await callExpectingError(page, 'pinelabs_fin.api.plural.initiate_payment_link', {
        reference_doctype: 'Sales Invoice',
        reference_name: F.inv_nocontact,
      });
      expect(r.ok).toBe(false);
      expect((r.msgs + r.exc).toLowerCase()).toMatch(/email|mobile/);
    } finally {
      await setPaymentLinkAccount(page, null);   // restore: unmapped
    }
  });
});

// ── Amount guard ───────────────────────────────────────────────────────
test.describe('Amount guard', () => {
  test('TC-AMT-04: fully-paid invoice is refused (BUG-17 fixed)', async ({ page }) => {
    await openForm(page, `sales-invoice/${F.inv_paid}`);   // fully paid -> outstanding 0
    // BUG-17 FIXED: a submitted invoice with outstanding 0 no longer falls back
    // to grand_total, so initiate refuses instead of charging it again.
    const r = await callExpectingError(page, 'pinelabs_fin.api.terminal.initiate_terminal_payment', {
      reference_doctype: 'Sales Invoice',
      reference_name: F.inv_paid,
      mode_of_payment: 'PineLabs - Card',
    });
    expect(r.ok).toBe(false);
    expect(((r.msgs || '') + (r.exc || '')).toLowerCase()).toContain('no outstanding amount');
  });
});

// ── Settings validation ────────────────────────────────────────────────
test.describe('Settings validation', () => {
  test('TC-SET-04: webhook secret shorter than 16 chars is rejected', async ({ page }) => {
    await openForm(page, 'pinelabs-settings/Pinelabs Settings');
    // Attempt to save a too-short secret with payment links enabled. The save is
    // rejected wholesale, so nothing persists.
    const r = await page.evaluate(async () => {
      const s = await frappe.call({
        method: 'frappe.client.get',
        args: { doctype: 'Pinelabs Settings', name: 'Pinelabs Settings' },
      });
      const doc = s.message;
      doc.enable_payment_links = 1;
      doc.plural_client_id = doc.plural_client_id || 'dummy-client';
      doc.plural_client_secret = doc.plural_client_secret || 'dummy-secret';
      doc.payment_link_webhook_secret = 'abc';   // 3 chars -> must be rejected
      try {
        await frappe.call({ method: 'frappe.client.save', args: { doc } });
        return { ok: true };
      } catch (e) {
        const msgs = (frappe.last_response && frappe.last_response._server_messages) || '';
        return { ok: false, msgs: String(msgs) };
      }
    });
    expect(r.ok).toBe(false);
    expect(r.msgs).toMatch(/16 characters/i);
    // Confirm nothing persisted.
    const enabled = await page.evaluate(async () => {
      const s = await frappe.call({
        method: 'frappe.client.get_value',
        args: { doctype: 'Pinelabs Settings', filters: {}, fieldname: 'enable_payment_links' },
      });
      return s.message && s.message.enable_payment_links;
    });
    expect(Number(enabled) || 0).toBe(0);
  });

  test('TC-SET-12: duplicate Payable Doctype rows are rejected', async ({ page }) => {
    await openForm(page, 'pinelabs-settings/Pinelabs Settings');
    const r = await page.evaluate(async () => {
      const s = await frappe.call({
        method: 'frappe.client.get',
        args: { doctype: 'Pinelabs Settings', name: 'Pinelabs Settings' },
      });
      const doc = s.message;
      // Append a SECOND Sales Invoice row -> duplicate.
      doc.payable_doctypes = (doc.payable_doctypes || []).concat([{
        doctype: 'Pinelabs Payable Doctype',
        doctype_name: 'Sales Invoice',
        is_enabled: 1,
        amount_field: 'outstanding_amount',
      }]);
      try {
        await frappe.call({ method: 'frappe.client.save', args: { doc } });
        return { ok: true };
      } catch (e) {
        const msgs = (frappe.last_response && frappe.last_response._server_messages) || '';
        return { ok: false, msgs: String(msgs) };
      }
    });
    expect(r.ok).toBe(false);
    expect(r.msgs.toLowerCase()).toContain('duplicate');
  });
});

// ── Security ───────────────────────────────────────────────────────────
test.describe('Security', () => {
  test('TC-SEC-01: credential fields are Password type (masked)', async ({ page }) => {
    await openForm(page, 'pinelabs-settings/Pinelabs Settings');
    const types = await page.evaluate(() => {
      const meta = frappe.get_meta('Pinelabs Settings');
      const want = ['plural_client_secret', 'payment_link_webhook_secret'];
      const out = {};
      for (const f of meta.fields) if (want.includes(f.fieldname)) out[f.fieldname] = f.fieldtype;
      return out;
    });
    expect(types.plural_client_secret).toBe('Password');
    expect(types.payment_link_webhook_secret).toBe('Password');
  });
});
