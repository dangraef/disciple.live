import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('landing page contains core content and accessible conversion form', async () => {
  const html = await readFile('index.html', 'utf8');
  assert.match(html, /Faith isn’t a finish line/);
  assert.match(html, /label class="sr-only" for="email">Email address/);
  assert.match(html, /input id="email" type="email"[^>]*required/);
  assert.match(html, /id="path"/);
  assert.match(html, /id="circles"/);
});
