import { readFile, readdir, stat, lstat, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sha256Bytes } from '../js/hasher.js';
import { loadFileDetail, walkCollect } from '../js/scanner.js';
import {
  ReviewError,
  buildExportItems,
  computeGlobal,
  computeProgress,
  coverageByDirectory,
  decide,
  emptyState,
  reset,
  reviewStatusFor,
  validateState,
} from '../js/reviewDomain.js';

const parityDir = path.dirname(fileURLToPath(import.meta.url));

function errorDict(error) {
  if (!(error instanceof ReviewError)) {
    throw error;
  }
  return { code: error.code, message: error.message, http_status: error.httpStatus };
}

function domainReport(script) {
  let state = emptyState();
  const captures = {};
  for (const [index, step] of script.steps.entries()) {
    const name = step.name ?? `${String(index).padStart(2, '0')}-${step.op}`;
    switch (step.op) {
      case 'decide':
        state = decide(state, step.path, step.action, step.reason, step.sha, step.at);
        break;
      case 'capture':
        captures[name] = structuredClone(state);
        break;
      case 'progress':
        captures[name] = computeProgress(script.listing, state);
        break;
      case 'coverage':
        captures[name] = coverageByDirectory(script.listing, state);
        break;
      case 'global': {
        const other = decide(emptyState(), '2-Permanent/a.md', 'rejected', 'other', script.sha, '2026-01-01T00:01:00+00:00');
        captures[name] = computeGlobal(script.listing, { 'fixture-user': state, 'other-user': other });
        break;
      }
      case 'export':
        captures[name] = buildExportItems(script.listing, state, step.include, (pathName) => step.fresh[pathName], 'fixture-user');
        break;
      case 'status':
        captures[name] = reviewStatusFor(state.items[step.path], step.current_sha);
        break;
      case 'reset':
        state = reset(state, step.path, step.mode, step.at);
        break;
      case 'reset_error':
      case 'decide_error':
        try {
          if (step.op === 'reset_error') {
            reset(state, step.path, step.mode, step.at);
          } else {
            decide(state, step.path, step.action, step.reason, step.sha, step.at);
          }
        } catch (error) {
          captures[name] = errorDict(error);
          break;
        }
        throw new Error(`预期失败但成功：${name}`);
      case 'history_limit': {
        let limited = emptyState();
        for (let eventIndex = 0; eventIndex < 205; eventIndex += 1) {
          limited = decide(limited, '2-Permanent/a.md', 'approved', 'x', script.sha, `2026-01-02T00:${String(Math.floor(eventIndex / 60)).padStart(2, '0')}:${String(eventIndex % 60).padStart(2, '0')}+00:00`);
        }
        captures[name] = { history_length: limited.history.length, first_at: limited.history[0].at, last_at: limited.history.at(-1).at };
        break;
      }
      case 'invalid_states': {
        const valid = structuredClone(state);
        const invalidCases = {
          top_level: [],
          unknown_field: { ...valid, extra: true },
          record_field: { ...valid, items: { '2-Permanent/a.md': {} } },
          nfd_path: { ...valid, items: { '2-Permanent/cafe\u0301.md': {} } },
          bad_history: { ...valid, history: [{ path: '2-Permanent/a.md', op: 'UNDO', from: null, to: null, reason: null, content_sha256: null, at: '2026-01-01T00:00:00+00:00' }] },
          bad_sha: { ...valid, history: [{ path: '2-Permanent/a.md', op: 'DECIDE', from: null, to: 'approved', reason: null, content_sha256: 'ABC', at: '2026-01-01T00:00:00+00:00' }] },
          long_reason: { ...valid, history: [{ path: '2-Permanent/a.md', op: 'DECIDE', from: null, to: 'approved', reason: 'x'.repeat(501), content_sha256: null, at: '2026-01-01T00:00:00+00:00' }] },
        };
        const results = {};
        for (const [caseName, raw] of Object.entries(invalidCases)) {
          try {
            validateState(raw);
          } catch (error) {
            results[caseName] = errorDict(error);
            continue;
          }
          throw new Error(`非法状态被接受：${caseName}`);
        }
        captures[name] = results;
        break;
      }
      default:
        throw new Error(`未知 domain op：${step.op}`);
    }
  }
  return captures;
}

async function mtimeIso(filePath) {
  const metadata = await stat(filePath);
  const milliseconds = Math.floor(metadata.mtimeMs * 1000);
  const date = new Date(milliseconds / 1000);
  return `${date.toISOString().slice(0, -1)}+00:00`;
}

function nodeAdapter(vault) {
  return {
    async statDirectory(parts) {
      try {
        const metadata = await lstat(path.join(vault, ...parts));
        if (metadata.isSymbolicLink()) return { kind: 'link' };
        return { kind: metadata.isDirectory() ? 'directory' : 'missing' };
      } catch {
        return { kind: 'missing' };
      }
    },
    async listDirectory(parts) {
      const directory = path.join(vault, ...parts);
      const entries = await readdir(directory, { withFileTypes: true });
      return Promise.all(entries.map(async (entry) => {
        const filePath = path.join(directory, entry.name);
        const metadata = await lstat(filePath);
        let kind = 'other';
        if (metadata.isSymbolicLink()) kind = entry.isDirectory() ? 'link-directory' : 'link';
        else if (entry.isDirectory()) kind = 'directory';
        else if (entry.isFile()) kind = 'file';
        return { name: entry.name, kind, size: metadata.size, mtime: await mtimeIso(filePath) };
      }));
    },
    async readBytes(parts) {
      return new Uint8Array(await readFile(path.join(vault, ...parts)));
    },
  };
}

async function nistVectors() {
  const cases = [
    ['empty', new Uint8Array()],
    ['abc', new TextEncoder().encode('abc')],
    ['one_mib_zero', new Uint8Array(1_048_576)],
    ['crlf_bom', new Uint8Array([0xef, 0xbb, 0xbf, 0x41, 0x0d, 0x0a, 0x42, 0x0a])],
  ];
  return Promise.all(cases.map(async ([name, bytes]) => ({ name, size_bytes: bytes.length, sha256: await sha256Bytes(bytes) })));
}

export async function run(vault) {
  const listing = await walkCollect(nodeAdapter(vault), { path: vault });
  const sourcePaths = listing._source_paths;
  delete listing._source_paths;
  const details = [];
  for (const item of listing.items) {
    if (item.parse_status === 'FILE_TOO_LARGE') {
      details.push({ path: item.vault_relative_path, error: { code: 'FILE_TOO_LARGE', message: '文件超过单文件读取上限，未读取正文。', http_status: 413 } });
      continue;
    }
    const sourcePath = sourcePaths[item.vault_relative_path] ?? item.vault_relative_path;
    const raw = new Uint8Array(await readFile(path.join(vault, ...sourcePath.split('/'))));
    const detail = await loadFileDetail(item, raw);
    details.push({ path: item.vault_relative_path, detail });
  }
  const script = JSON.parse(await readFile(path.join(parityDir, 'domain_cases.json'), 'utf8'));
  return {
    listing,
    details,
    domain_case: domainReport(script),
    nist_vectors: await nistVectors(),
    real_vault: false,
  };
}

function parseArguments(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    if (!['--vault', '--out'].includes(argv[index]) || argv[index + 1] === undefined) {
      throw new Error('用法：node run_js_engine.mjs --vault <fixture> --out <report>');
    }
    result[argv[index].slice(2)] = argv[index + 1];
  }
  if (!result.vault || !result.out) throw new Error('--vault 与 --out 均必填');
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = parseArguments(process.argv.slice(2));
  const startedAt = new Date().toISOString();
  const vault = path.resolve(args.vault);
  const report = await run(vault);
  report.run_metadata = { started_at: startedAt, finished_at: new Date().toISOString() };
  await mkdir(path.dirname(path.resolve(args.out)), { recursive: true });
  await writeFile(args.out, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  console.log(JSON.stringify({ engine: 'javascript-online', total: report.listing.total, out: args.out }));
}
