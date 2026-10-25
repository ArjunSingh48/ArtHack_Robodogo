// Dependency-free real-browser checks. Run: node --test web/tests/telegram.browser.test.mjs
// Set CHROME_BIN to Chrome/Chromium if no Playwright-cached browser is installed.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, readdir, access, mkdtemp, rm } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { resolve, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);
const root = fileURLToPath(new URL('../', import.meta.url));
async function browserPath() {
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN;
  const cache = join(homedir(), 'Library/Caches/ms-playwright');
  const dirs = await readdir(cache).catch(() => []);
  const candidates = dirs.filter((d) => d.startsWith('chromium_headless_shell-')).reverse()
    .map((d) => join(cache, d, 'chrome-headless-shell-mac-arm64/chrome-headless-shell'));
  candidates.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome');
  for (const p of candidates) { try { await access(p); return p; } catch {} }
}
const chrome = await browserPath();
let server, origin;
before(async () => {
  if (!chrome) return;
  server = createServer(async (req, res) => {
    try {
      if (new URL(req.url, 'http://localhost').pathname === '/app-under-test.js') {
        let app = await readFile(join(root, 'app.js'), 'utf8');
        app = app.replace("import { robot } from './robot.js';", `const robot = { on: false,
          pose: async (...args) => { window.robotCalls.push(args); }, walk: (...args) => window.robotCalls.push(args),
          walkEnd: async () => window.robotCalls.push('end'), stop: () => window.robotCalls.push('stop') };`)
          .replace("import('./sim3d.js')", "Promise.resolve({ start: () => ({ update() {} }) })");
        app += `\nexport const harness = { show, matchQuestion, send, toLobby, say,
          begin() { gen++; paused = false; clearTelegramPreview(); source = 'photos'; $('lobby').style.display = 'none'; return gen; },
          reveal(a, shot, q, g, photoSource = 'photos') { return revealMatch(a, shot, g, photoSource); },
          setPhotoControls() { controls('photo'); },
          currentGeneration() { return gen; }
        };`;
        res.writeHead(200, { 'Content-Type': 'text/javascript', 'Cache-Control': 'no-store' }).end(app);
        return;
      }
      const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
      if (!path.startsWith(root)) { res.writeHead(403).end(); return; }
      const data = await readFile(path);
      const type = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
        '.json': 'application/json', '.jpg': 'image/jpeg' }[extname(path)] || 'application/octet-stream';
      res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' }).end(data);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  origin = `http://127.0.0.1:${server.address().port}`;
});
after(async () => { if (server) await new Promise((resolve) => server.close(resolve)); });

for (const [name, size, reduced] of [
  ['projector', '1440,1000', false], ['narrow screen', '390,844', false], ['reduced motion', '1440,1000', true],
]) {
  test(`Telegram preview: ${name}`, { skip: !chrome && 'Set CHROME_BIN to run browser checks', timeout: 60000 }, async () => {
    const profile = await mkdtemp(join(tmpdir(), 'good-boy-browser-'));
    try {
      const { stdout } = await run(chrome, [
        '--headless', '--disable-gpu', '--no-sandbox', '--disable-background-networking',
        '--no-first-run', `--user-data-dir=${profile}`, `--window-size=${size}`,
        ...(reduced ? ['--force-prefers-reduced-motion'] : []),
        ...(process.env.SCREENSHOT_DIR ? [`--screenshot=${join(process.env.SCREENSHOT_DIR, `good-boy-${name.replaceAll(' ', '-')}.png`)}`] : []),
        '--virtual-time-budget=30000', '--dump-dom', `${origin}/tests/telegram.fixture.html`,
      ], { timeout: 50000, maxBuffer: 4 * 1024 * 1024 });
      const result = stdout.match(/<pre id="test-results"[^>]*>([^<]*)<\/pre>/);
      assert.ok(result, 'Browser did not finish the fixture: ' + stdout.slice(-1200));
      const parsed = JSON.parse(result[1].replaceAll('&quot;', '"').replaceAll('&amp;', '&'));
      assert.deepEqual(parsed.failures, [], JSON.stringify(parsed, null, 2));
      assert.equal(parsed.passed, 17, 'Fixture must exercise the chat lifecycle and app integration');
    } finally { await rm(profile, { recursive: true, force: true }); }
  });
}
