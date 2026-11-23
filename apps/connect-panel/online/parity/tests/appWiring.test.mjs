import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../../js/app.js', import.meta.url), 'utf8');

test('默认传送实现必须在 ...services 之后注入，否则会被 undefined 覆盖', () => {
  const spread = source.indexOf('...services');
  const transfer = source.indexOf('transferToLibrary: services.transferToLibrary ?? transferApprovedNote');
  assert.ok(spread > -1, '应存在 ...services 展开');
  assert.ok(transfer > -1, '应注入 transferToLibrary 默认实现');
  assert.ok(transfer > spread, '默认实现必须在展开之后，否则未传该键时会被覆盖为 undefined');
});

test('传送模块被显式导入', () => {
  assert.match(source, /import \{ transferApprovedNote \} from '\.\/transfer\.js(\?v=[^']*)?'/u);
});

test('默认实现只注入一次', () => {
  const matches = source.match(/transferToLibrary: services\.transferToLibrary/gu) ?? [];
  assert.equal(matches.length, 1);
});
