/**
 * WCAG 2.2 AA audit of the exported documentation site.
 *
 * Serves website/out, then runs axe-core over every page in both the light and
 * the dark theme and reports violations grouped by rule. Exits non-zero if any
 * violation is found, so it can gate CI.
 *
 * Usage:  npm run build && npm run a11y
 */
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const AXE = await readFile(require.resolve('axe-core/axe.min.js'), 'utf8');

const OUT = path.resolve(process.cwd(), 'out');
const PORT = Number(process.env.A11Y_PORT || 8099);
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
  '.png': 'image/png', '.ico': 'image/x-icon' };

const server = createServer(async (req, res) => {
  try {
    let p = path.join(OUT, decodeURIComponent(req.url.split('?')[0]));
    if ((await stat(p).catch(() => null))?.isDirectory()) p = path.join(p, 'index.html');
    const body = await readFile(p);
    res.writeHead(200, { 'content-type': MIME[path.extname(p)] || 'application/octet-stream' });
    res.end(body);
  } catch {
    res.writeHead(404).end('not found');
  }
});
await new Promise(r => server.listen(PORT, r));

const pages = (await readFile(path.join(OUT, 'docs.html'), 'utf8'))
  ? ['/docs.html', '/docs/quick-start.html', '/docs/usage.html', '/docs/architecture.html',
     '/docs/benchmarks.html', '/docs/mcp.html', '/docs/language-support.html',
     '/docs/verify.html', '/docs/contributing.html', '/404.html']
  : [];

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ['--no-sandbox', '--disable-gpu'],
});

const byRule = {}, byImpact = {};
let total = 0;

for (const dark of [false, true]) {
  for (const route of pages) {
    const ctx = await browser.newContext({
      colorScheme: dark ? 'dark' : 'light',
      viewport: { width: 1280, height: 900 },
    });
    const page = await ctx.newPage();
    await page.goto(`http://127.0.0.1:${PORT}${route}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    await page.addScriptTag({ content: AXE });
    const violations = await page.evaluate(async tags => {
      const r = await window.axe.run(document, { runOnly: { type: 'tag', values: tags } });
      return r.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.length }));
    }, TAGS);

    const n = violations.reduce((a, v) => a + v.nodes, 0);
    total += n;
    for (const v of violations) {
      byRule[v.id] = (byRule[v.id] || 0) + v.nodes;
      byImpact[v.impact || 'unknown'] = (byImpact[v.impact || 'unknown'] || 0) + v.nodes;
    }
    console.log(`${dark ? 'dark ' : 'light'}  ${route.padEnd(32)} ${n}`);
    await ctx.close();
  }
}

await browser.close();
server.close();

console.log(`\nviolation instances: ${total}`);
if (total) {
  console.log('by impact:', byImpact);
  console.log('by rule:', byRule);
  process.exit(1);
}
console.log('WCAG 2.2 AA: no axe-core violations.');
