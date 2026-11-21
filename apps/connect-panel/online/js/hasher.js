/**
 * 原始字节 SHA-256；禁止在调用前解码、规范化换行或剥离 BOM。
 * @param {Uint8Array|ArrayBuffer} input
 * @returns {Promise<string>} 64 位小写十六进制摘要
 */
export async function sha256Bytes(input) {
  const bytes = asBytes(input);
  if (!globalThis.crypto?.subtle) {
    throw new Error('SHA256_UNAVAILABLE');
  }
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** @param {Uint8Array|ArrayBuffer} input */
export function asBytes(input) {
  if (input instanceof Uint8Array) {
    return input;
  }
  if (input instanceof ArrayBuffer) {
    return new Uint8Array(input);
  }
  throw new TypeError('SHA256_INPUT_MUST_BE_BYTES');
}

/** @param {string} text 显式 UTF-8 编码，不修改任何字符或换行。 */
export function utf8Bytes(text) {
  if (typeof text !== 'string') {
    throw new TypeError('UTF8_INPUT_MUST_BE_STRING');
  }
  return new TextEncoder().encode(text);
}
