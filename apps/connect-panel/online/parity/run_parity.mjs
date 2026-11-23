import { spawnSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const parityDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(parityDir, '..', '..', '..', '..');
const reports = path.join(parityDir, 'reports');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const vault = process.argv[2] ? path.resolve(process.argv[2]) : path.join(parityDir, 'fixtures', 'vault');
const isDefault = vault === path.join(parityDir, 'fixtures', 'vault');
const manifest = JSON.parse(await readFile(path.join(parityDir, 'fixtures', 'fixtures_manifest.json'), 'utf8'));
const isStage2 = vault === path.join(root, 'tests', 'fixtures', 'stage2', 'vault');
const fixtureName = isDefault ? 'synthetic-extended' : isStage2 ? 'stage2-read-only' : 'custom-fixture';
// 整库扫描会额外纳入库根级的 outside-root.md；stage2 夹具没有该文件，因此不加。
const extraRootLevelNotes = 1;
const expected = isDefault
  ? manifest.expected_managed_markdown + extraRootLevelNotes
  : isStage2
    ? manifest.expected_stage2_managed_markdown
    : manifest.expected_combined_fixture_count;
if (!isDefault && !isStage2) throw new Error('第一关只允许 synthetic-extended 或 stage2 fixture，不接受真实 Vault 路径。');

function execute(command, args) {
  const started = new Date().toISOString();
  const result = spawnSync(command, args, { cwd: root, encoding: 'utf8' });
  const record = { command: [command, ...args].join(' '), started_at: started, exit_code: result.status, stdout: result.stdout.trim(), stderr: result.stderr.trim() };
  console.log(result.stdout.trim());
  if (result.stderr.trim()) console.error(result.stderr.trim());
  if (result.status !== 0) throw new Error(`命令失败：${record.command}`);
  return record;
}

const prefix = isStage2 ? 'stage2_' : 'extended_';
const reportNames = {
  python: `${prefix}py_report.json`,
  javascript: `${prefix}js_report.json`,
  differences: `${prefix}diff_report.json`,
  parity: `${prefix}parity_report.json`,
};
const commands = [
  execute(python, [path.join(parityDir, 'run_python_engine.py'), '--vault', vault, '--out', path.join(reports, reportNames.python)]),
  execute(process.execPath, [path.join(parityDir, 'run_js_engine.mjs'), '--vault', vault, '--out', path.join(reports, reportNames.javascript)]),
  execute(python, [path.join(parityDir, 'compare_reports.py'), path.join(reports, reportNames.python), path.join(reports, reportNames.javascript), '--expected-total', String(expected), '--out', path.join(reports, reportNames.differences)]),
];
const diff = JSON.parse(await readFile(path.join(reports, reportNames.differences), 'utf8'));
const summary = {
  schema_version: '1',
  stage: 'T01-T02 fixture gate',
  generated_at: new Date().toISOString(),
  fixture_name: fixtureName,
  fixture_count: expected,
  fixture_manifest: isStage2 ? path.relative(root, path.join(parityDir, 'fixtures', 'stage2_manifest.json')) : path.relative(root, path.join(parityDir, 'fixtures', 'fixtures_manifest.json')),
  fixture_tree_sha256: isDefault ? manifest.fixture_tree_sha256 : null,
  difference_count: diff.difference_count,
  nist_passed: diff.nist.passed,
  nist_vector_results: diff.nist.vector_results,
  real_vault_full_parity_performed: false,
  commands,
  reports: Object.values(reportNames).filter((name) => name !== reportNames.parity),
  passed: diff.passed,
};
const { writeFile } = await import('node:fs/promises');
await writeFile(path.join(reports, reportNames.parity), `${JSON.stringify(summary, null, 2)}\n`, 'utf8');
console.log(JSON.stringify(summary, null, 2));
if (isStage2) {
  execute(python, [path.join(parityDir, 'summarize_fixture_gate.py'), '--reports', reports, '--out', path.join(reports, 'parity_report.json')]);
}
