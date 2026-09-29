import assert from 'node:assert/strict';
import { copyFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { checkSetup } from './check-setup.mjs';

function fixture(t, installed = { '@supabase/supabase-js': '2.117.2', vite: '7.3.6' }) {
  const dir = mkdtempSync(join(tmpdir(), 'savflux-setup-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const write = (path, value) => writeFileSync(join(dir, path), JSON.stringify(value));
  write('package.json', {
    dependencies: { '@supabase/supabase-js': '^2.117.2' },
    devDependencies: { vite: '^7.3.6' },
  });
  write('package-lock.json', { packages: {
    'node_modules/@supabase/supabase-js': { version: '2.117.2' },
    'node_modules/vite': { version: '7.3.6' },
  } });
  for (const [name, version] of Object.entries(installed)) {
    mkdirSync(join(dir, 'node_modules', name), { recursive: true });
    write(`node_modules/${name}/package.json`, { version });
  }
  return dir;
}

test('accepts the locked install on supported Node versions', (t) => {
  const dir = fixture(t);
  for (const version of ['22.12.0', '22.22.3', '24.0.0']) {
    assert.deepEqual(checkSetup(dir, version), []);
  }
});

test('detects the reported missing Supabase and stale Vite installation', (t) => {
  const problems = checkSetup(fixture(t, { vite: '5.4.21' }), '22.12.0');
  assert.equal(problems.length, 2);
  assert.match(problems[0], /@supabase\/supabase-js: missing/);
  assert.match(problems[1], /vite: installed 5.4.21, lockfile requires 7.3.6/);
});

test('works without any node_modules', (t) => {
  assert.equal(checkSetup(fixture(t, {}), '22.12.0').length, 2);
});

test('rejects old Node before reading dependencies', () => {
  for (const version of ['18.20.0', '20.19.0', '22.11.0']) {
    assert.match(checkSetup('/not-needed', version)[0], /Node .* is unsupported/);
  }
});

test('reports direct dependencies absent from the lockfile', (t) => {
  const dir = fixture(t);
  writeFileSync(join(dir, 'package-lock.json'), JSON.stringify({ packages: {} }));
  assert.match(checkSetup(dir, '22.12.0')[0], /missing from package-lock.json/);
});

test('CLI fails before Vite with actionable repair instructions', (t) => {
  const dir = fixture(t, {});
  // Run an isolated copy so the test does not change the real node_modules.
  mkdirSync(join(dir, 'scripts'));
  const script = join(dir, 'scripts/check-setup.mjs');
  copyFileSync(new URL('./check-setup.mjs', import.meta.url), script);
  const result = spawnSync(process.execPath, [script], { encoding: 'utf8', cwd: tmpdir() });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /@supabase\/supabase-js: missing/);
  assert.match(result.stderr, /From frontend\/, run npm ci, then npm run dev/);
});
