import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PYTHON_CASEFOLD, pythonCasefold } from '../../js/pythonCasefold.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', '..');
const python = path.join(repositoryRoot, '.venv', 'Scripts', 'python.exe');
const source = String.raw`import json,sys
print(json.dumps({str(i): chr(i).casefold() for i in range(0x110000) if not 0xD800 <= i <= 0xDFFF}, ensure_ascii=False))`;
const output = execFileSync(python, ['-c', source], { cwd: repositoryRoot, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
const expected = JSON.parse(output);

test('全部 Unicode 码点 casefold 与 Python 参照相等', () => {
  assert.equal(PYTHON_CASEFOLD.size, 352);
  for (let codePoint = 0; codePoint <= 0x10ffff; codePoint += 1) {
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) continue;
    const character = String.fromCodePoint(codePoint);
    assert.equal(pythonCasefold(character), expected[String(codePoint)], `U+${codePoint.toString(16).toUpperCase()}`);
  }
});

test('复合路径大小写折叠、NFC 与默认字符', () => {
  assert.equal(pythonCasefold('Straße İ ﬃ ᾈ'), 'strasse i\u0307 ffi ἀι');
  assert.equal(pythonCasefold('2-Permanent/中文.md'), '2-permanent/中文.md');
});
