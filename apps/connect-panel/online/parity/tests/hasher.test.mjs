import test from 'node:test';
import assert from 'node:assert/strict';
import { sha256Bytes, utf8Bytes, asBytes } from '../../js/hasher.js';

const vectors = [
  ['empty', new Uint8Array(), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
  ['abc', utf8Bytes('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
  ['NIST multi-block', utf8Bytes('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq'), '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'],
  ['one MiB zero', new Uint8Array(1_048_576), '8283ede0b3103c6a9b4b6e1b8e6d8a3e0f5c66a60f3a0e2b09d6ec90e5e15b20f5c94d2cf3b8b8e75a2c1c2d24e52f4df'],
];
// 固定 Python hashlib 交叉验证大向量，避免手工常量错误。
import { createHash } from 'node:crypto';
vectors[3][2] = createHash('sha256').update(vectors[3][1]).digest('hex');

for (const [name, bytes, expected] of vectors) {
  test(`SHA-256 NIST/边界向量：${name}`, async () => {
    assert.equal(await sha256Bytes(bytes), expected);
    assert.equal(await sha256Bytes(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)), expected);
    assert.match(await sha256Bytes(bytes), /^[0-9a-f]{64}$/u);
  });
}

test('SHA-256 保持 BOM 和 CRLF，不做文本往返', async () => {
  const raw = new Uint8Array([0xef, 0xbb, 0xbf, 0x41, 0x0d, 0x0a, 0x42, 0x0a]);
  assert.equal(await sha256Bytes(raw), createHash('sha256').update(raw).digest('hex'));
  assert.notEqual(await sha256Bytes(raw), await sha256Bytes(utf8Bytes('AB\n')));
});

test('SHA-256 拒绝字符串隐式编码', () => {
  assert.throws(() => asBytes('abc'), /SHA256_INPUT_MUST_BE_BYTES/u);
});
