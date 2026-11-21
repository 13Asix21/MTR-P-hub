import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const html = await readFile(new URL('../../index.html', import.meta.url), 'utf8');
const app = await readFile(new URL('../../js/app.js', import.meta.url), 'utf8');
const access = await readFile(new URL('../../js/fsAccess.js', import.meta.url), 'utf8');

test('站点只呈现真实只读扫描与云未连接状态，无伪造同步或外链', () => {
  assert.match(html, /云台账未连接/u);
  assert.match(html, /Vault 只读/u);
  assert.doesNotMatch(html, /showDirectoryPicker\s*\(|https?:\/\/|innerHTML|eval\s*\(/u);
  assert.doesNotMatch(app, /showDirectoryPicker\s*\(|fetch\s*\(|localStorage|indexedDB/u);
  assert.match(access, /showDirectoryPicker\(\{ mode: 'read' \}\)/u);
  assert.doesNotMatch(access, /readwrite|createWritable|removeEntry/u);
});

test('vendored YAML 固定版本与 LICENSE 同源交付', async () => {
  const baseline = JSON.parse(await readFile(new URL('../../vendor/BASELINE.json', import.meta.url), 'utf8'));
  const vendor = await readFile(new URL('../../vendor/js-yaml.min.js', import.meta.url), 'utf8');
  const license = await readFile(new URL('../../vendor/js-yaml.LICENSE', import.meta.url), 'utf8');
  assert.equal(baseline.version, '4.1.0');
  assert.match(vendor, /js-yaml 4\.1\.0/u);
  assert.match(license, /MIT License/u);
});
