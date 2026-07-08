import { chromium } from '/opt/node22/lib/node_modules/playwright/index.mjs';

const base = '/tmp/claude-0/-home-user-receitas/d7bf97ce-f0c4-5682-9c27-fff6cfa2a810/scratchpad';
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
await p.goto('file://' + base + '/crono.html');
await p.waitForTimeout(400);
// mede a largura dos cabeçalhos de dia e do body
const m = await p.evaluate(() => {
  const ths = Array.from(document.querySelectorAll('.crono thead th')).filter(
    t => !t.classList.contains('rec-col') && !t.classList.contains('tot-col'));
  const widths = ths.slice(0, 5).map(t => Math.round(t.getBoundingClientRect().width));
  const body = document.body;
  return {
    dia_widths: widths,
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
});
console.log(JSON.stringify(m));
const el = await p.$('.crono-wrap');
await el.scrollIntoViewIfNeeded();
await el.screenshot({ path: base + '/crono_shot.png' });
await b.close();
