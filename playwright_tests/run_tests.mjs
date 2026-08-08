// Playwright: drive the Pinelabs app in the Frappe Desk like a human user.
// Records OBSERVED behavior per test case to results.json. Does NOT modify app code.
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = 'http://127.0.0.1:8015';
const SID = process.env.PL_SID;
if (!SID) { console.error('PL_SID env var (session cookie) required'); process.exit(1); }

const results = {};
const shots = [];
function rec(tc, actual, verdict) {
  results[tc] = { actual, verdict };
  console.log(`\n[${tc}] ${verdict}\n   ${actual.replace(/\n/g, '\n   ')}`);
}

// Open a Desk form and wait until it is genuinely loaded (title + settled).
async function openForm(page, route) {
  await page.goto(`${BASE}/app/${route}`, { waitUntil: 'domcontentloaded' });
  // Wait for Frappe form container / page title to appear.
  await page.waitForSelector('.page-head, .title-area, .layout-main', { timeout: 30000 });
  // Let the refresh event + custom-button injection run.
  await page.waitForTimeout(3500);
}

async function hasButton(page, label) {
  // Custom buttons may be inline or under the "..." / actions menu.
  const inline = await page.locator(`button:has-text("${label}"), .btn:has-text("${label}")`).count();
  if (inline > 0) return true;
  // Try opening any collapsed custom-button group.
  const groups = page.locator('.custom-btn-group button, .inner-group-button button');
  if (await groups.count()) {
    try { await groups.first().click({ timeout: 1500 }); } catch {}
    await page.waitForTimeout(500);
    const after = await page.locator(`a:has-text("${label}"), button:has-text("${label}")`).count();
    if (after > 0) return true;
  }
  return false;
}

async function shot(page, name) {
  const p = `shot_${name}.png`;
  try { await page.screenshot({ path: p, fullPage: false }); shots.push(p); } catch {}
}

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1500, height: 900 } });
await ctx.addCookies([
  { name: 'sid', value: SID, domain: '127.0.0.1', path: '/' },
  { name: 'user_id', value: 'Administrator', domain: '127.0.0.1', path: '/' },
]);
const page = await ctx.newPage();
page.on('dialog', d => d.accept().catch(() => {}));

// Sanity: confirm we're logged in (not bounced to /login).
await page.goto(`${BASE}/app/home`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2000);
if (page.url().includes('/login')) {
  rec('AUTH', 'Redirected to /login — session cookie not accepted.', 'BLOCKED');
  fs.writeFileSync('results.json', JSON.stringify(results, null, 2));
  await browser.close(); process.exit(0);
}
console.log('Logged in OK, url =', page.url());

// ── TC-01: submitted UNPAID invoice → both buttons present (positive control)
try {
  await openForm(page, 'sales-invoice/ACC-SINV-2026-00003');
  const pom = await hasButton(page, 'Pay on Machine');
  const spl = await hasButton(page, 'Send Payment Link');
  await shot(page, 'tc01_submitted_unpaid');
  rec('TC-01',
    `Submitted unpaid invoice (outstanding 1000). 'Pay on Machine' present=${pom}; 'Send Payment Link' present=${spl}.`,
    (pom && spl) ? 'PASS (both buttons render)' : 'UNEXPECTED (a button is missing)');
} catch (e) { rec('TC-01', 'Error: ' + e.message, 'ERROR'); }

// ── TC-02: PAID invoice → buttons hidden
try {
  await openForm(page, 'sales-invoice/ACC-SINV-2026-00001');
  const pom = await hasButton(page, 'Pay on Machine');
  const spl = await hasButton(page, 'Send Payment Link');
  await shot(page, 'tc02_paid');
  rec('TC-02',
    `Paid invoice (outstanding 0). 'Pay on Machine' present=${pom}; 'Send Payment Link' present=${spl}.`,
    (!pom && !spl) ? 'PASS (correctly hidden)' : 'UNEXPECTED (buttons shown on paid doc)');
} catch (e) { rec('TC-02', 'Error: ' + e.message, 'ERROR'); }

// ── TC-03: DRAFT invoice → the BUG-13 claim (should buttons be absent?)
try {
  await openForm(page, 'sales-invoice/ACC-SINV-2026-00002');
  const docstatusText = await page.locator('.indicator-pill, .title-area').first().innerText().catch(() => '');
  const pom = await hasButton(page, 'Pay on Machine');
  const spl = await hasButton(page, 'Send Payment Link');
  await shot(page, 'tc03_draft');
  const appear = pom || spl;
  rec('TC-03',
    `Draft invoice (status pill: "${docstatusText.replace(/\n/g,' ').slice(0,40)}"). 'Pay on Machine' present=${pom}; 'Send Payment Link' present=${spl}.`,
    appear
      ? 'BUG-13 REFUTED — buttons DO render on the draft (outstanding_amount is populated, not 0)'
      : 'BUG-13 CONFIRMED — buttons absent on the draft');
} catch (e) { rec('TC-03', 'Error: ' + e.message, 'ERROR'); }

// ── TC-04: manual PENDING → FAILED on a Pinelabs Transaction (BUG-03)
async function tryStatusChange(tc, target) {
  try {
    await openForm(page, 'pinelabs-transaction/PLT-2026-00002');
    // Read current status.
    const before = await page.locator('[data-fieldname="status"] select, [data-fieldname="status"] input').first().inputValue().catch(() => '?');
    // Change the Select.
    const sel = page.locator('[data-fieldname="status"] select').first();
    await sel.selectOption({ label: target }).catch(async () => {
      await sel.selectOption(target).catch(() => {});
    });
    await page.waitForTimeout(400);
    // Save with Ctrl+S.
    await page.keyboard.press('Control+s');
    await page.waitForTimeout(2500);
    // Detect an error modal (frappe.throw) vs a clean save.
    const modal = page.locator('.modal.show .modal-body, .msgprint, .modal-title:has-text("Error")');
    let errText = '';
    if (await modal.count()) errText = (await modal.first().innerText().catch(() => '')).slice(0, 260);
    const savedIndicator = await page.locator('.indicator-pill:has-text("Saved"), .indicator-pill:has-text("Not Saved")').first().innerText().catch(() => '');
    // Re-read persisted status via API to be certain.
    const persisted = await page.evaluate(async () => {
      const r = await fetch('/api/resource/Pinelabs Transaction/PLT-2026-00002?fields=["status"]', { headers: { 'X-Frappe-CSRF-Token': (window.frappe && frappe.csrf_token) || '' } });
      const j = await r.json(); return (j.data && j.data.status) || '?';
    }).catch(() => '?');
    await shot(page, `${tc}_${target}`);
    const rejected = /Illegal status transition|may only be set|Cannot|not allowed/i.test(errText) || persisted !== target;
    return { before, target, errText, persisted, rejected };
  } catch (e) { return { error: e.message }; }
}
try {
  const r = await tryStatusChange('TC-04', 'FAILED');
  if (r.error) rec('TC-04', 'Error: ' + r.error, 'ERROR');
  else rec('TC-04',
    `Changed status PENDING→FAILED and saved. Error shown: ${r.errText ? '"'+r.errText.replace(/\n/g,' ')+'"' : 'NONE'}. Persisted status now = ${r.persisted}.`,
    r.rejected
      ? 'BUG-03 not reproduced here — the save was rejected'
      : 'BUG-03 CONFIRMED — PENDING→FAILED accepted with no internal flag (persisted=FAILED)');
} catch (e) { rec('TC-04', 'Error: ' + e.message, 'ERROR'); }

// ── TC-05: manual → SUCCESS control (expected to be blocked)
// Reset the row to PENDING first (via API) so we test SUCCESS from an active state.
try {
  await page.evaluate(async () => {
    await fetch('/api/method/frappe.client.set_value', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': (window.frappe && frappe.csrf_token) || '' },
      body: JSON.stringify({ doctype: 'Pinelabs Transaction', name: 'PLT-2026-00002', fieldname: 'status', value: 'PENDING' })
    });
  }).catch(() => {});
  await page.waitForTimeout(1000);
  const r = await tryStatusChange('TC-05', 'SUCCESS');
  if (r.error) rec('TC-05', 'Error: ' + r.error, 'ERROR');
  else rec('TC-05',
    `Changed status →SUCCESS and saved. Error shown: ${r.errText ? '"'+r.errText.replace(/\n/g,' ')+'"' : 'NONE'}. Persisted status now = ${r.persisted}.`,
    r.rejected
      ? 'PASS — SUCCESS correctly blocked for manual edit (guard works)'
      : 'UNEXPECTED — manual SUCCESS was accepted');
} catch (e) { rec('TC-05', 'Error: ' + e.message, 'ERROR'); }

// ── TC-06: Pay on Machine pre-flight error (no account mapped) — fail-fast
try {
  await openForm(page, 'sales-invoice/ACC-SINV-2026-00003');
  // Click Pay on Machine.
  let clicked = false;
  const b = page.locator('button:has-text("Pay on Machine"), .btn:has-text("Pay on Machine")').first();
  if (await b.count()) { await b.click().catch(() => {}); clicked = true; }
  await page.waitForTimeout(1500);
  // A dialog with mode-of-payment select + Start Payment button should appear.
  let dialogSeen = await page.locator('.modal.show').count() > 0;
  // Pick PineLabs - Card if a select is present.
  const modeSel = page.locator('.modal.show select').first();
  if (await modeSel.count()) {
    await modeSel.selectOption({ label: 'PineLabs - Card' }).catch(async () => {
      await modeSel.selectOption({ index: 1 }).catch(() => {});
    });
  }
  // Click Start Payment.
  const start = page.locator('.modal.show button:has-text("Start Payment"), .modal.show .btn-primary').first();
  if (await start.count()) await start.click().catch(() => {});
  await page.waitForTimeout(3500);
  // Capture any error text now shown.
  let err = '';
  const errLoc = page.locator('.modal.show .modal-body, .msgprint, .desk-alert, .form-message');
  if (await errLoc.count()) err = (await errLoc.first().innerText().catch(() => '')).slice(0, 300);
  await shot(page, 'tc06_pay_on_machine');
  const preflight = /default account|no default account|account configured|mapped/i.test(err);
  rec('TC-06',
    `Clicked Pay on Machine (dialog opened=${dialogSeen}, button clicked=${clicked}). Message after Start Payment: ${err ? '"'+err.replace(/\n/g,' ')+'"' : 'NONE captured'}.`,
    preflight
      ? 'PASS — fail-fast account pre-flight fired before any gateway call'
      : 'OBSERVED — see captured message (may be a different guard/state)');
} catch (e) { rec('TC-06', 'Error: ' + e.message, 'ERROR'); }

fs.writeFileSync('results.json', JSON.stringify(results, null, 2));
console.log('\nScreenshots:', shots.join(', '));
console.log('Wrote results.json');
await browser.close();
