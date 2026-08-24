import { chromium } from 'playwright';
import fs from 'fs';
const BASE = 'http://127.0.0.1:8015';
const SID = process.env.PL_SID;

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1500, height: 900 } });
await ctx.addCookies([
  { name: 'sid', value: SID, domain: '127.0.0.1', path: '/' },
  { name: 'user_id', value: 'Administrator', domain: '127.0.0.1', path: '/' },
]);
const page = await ctx.newPage();
page.on('dialog', d => d.accept().catch(() => {}));

// Capture the server response to initiate_terminal_payment directly.
let serverErr = null;
page.on('response', async (resp) => {
  if (resp.url().includes('initiate_terminal_payment')) {
    try {
      const status = resp.status();
      const body = await resp.text();
      serverErr = { status, body: body.slice(0, 1200) };
    } catch {}
  }
});

await page.goto(`${BASE}/app/sales-invoice/ACC-SINV-2026-00003`, { waitUntil: 'domcontentloaded' });
await page.waitForSelector('.page-head, .layout-main', { timeout: 30000 });
await page.waitForTimeout(3500);

await page.locator('button:has-text("Pay on Machine")').first().click();
await page.waitForTimeout(1200);

// Human-style: type into the Link field and pick the item from the dropdown.
const modeInput = page.locator('.modal.show [data-fieldname="mode_of_payment"] input').first();
await modeInput.click();
await modeInput.fill('PineLabs - Card');
await page.waitForTimeout(1500);  // wait for the link query + awesomplete list
await page.locator('.modal.show [data-fieldname="mode_of_payment"] .awesomplete li:has-text("PineLabs - Card"), ul[role="listbox"] li:has-text("PineLabs - Card")')
  .first().click().catch(() => {});
await page.waitForTimeout(600);
const info = await page.evaluate(() => ({
  value: cur_dialog ? cur_dialog.get_value('mode_of_payment') : '(dialog gone)',
  buttons: cur_dialog ? [...cur_dialog.$wrapper.find('.modal-footer button')].map(b => b.innerText.trim()).filter(Boolean) : [],
}));
// Fire Start Payment via the dialog's own primary action with current values
// (equivalent to clicking the button; robust vs overlay/focus quirks).
const fired = await page.evaluate(() => {
  const d = cur_dialog;
  const vals = d.get_values();          // null if a required field is unset
  if (!vals) return 'get_values null (mode not set?)';
  d.primary_action(vals);               // hides dialog + calls initiate_terminal_payment
  return 'fired with ' + JSON.stringify(vals);
});
info.fired = fired;
await page.waitForTimeout(6000);

// Search the entire page for the account pre-flight error.
const pageText = await page.evaluate(() => document.body.innerText);
const m = pageText.match(/[^\n]*default account[^\n]*/i)
       || pageText.match(/[^\n]*Party[^\n]*mandatory[^\n]*/i)
       || pageText.match(/[^\n]*already in progress[^\n]*/i);
const txn = await page.evaluate(async () => {
  const r = await fetch('/api/resource/Pinelabs Transaction?filters=[["reference_name","=","ACC-SINV-2026-00003"]]&fields=["name","status","error_message"]');
  const j = await r.json(); return j.data || [];
}).catch(() => []);

await page.screenshot({ path: 'shot_tc06_final.png' }).catch(() => {});
const out = {
  mode_value_committed: info.value,
  dialog_buttons: info.buttons,
  server_response: serverErr,
  page_error_line: m ? m[0].trim().slice(0, 300) : null,
  txn_created: txn,
};
fs.writeFileSync('results_tc06.json', JSON.stringify(out, null, 2));
console.log(JSON.stringify(out, null, 2));
await browser.close();
