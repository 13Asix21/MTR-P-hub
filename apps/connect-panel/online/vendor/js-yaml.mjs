/* js-yaml 4.1.0 官方 UMD 的 ES module 适配层；不修改发行文件，不访问网络。 */
import './js-yaml.min.js';

const yaml = globalThis.jsyaml;
if (!yaml || typeof yaml.load !== 'function') {
  throw new Error('vendored js-yaml 4.1.0 初始化失败。');
}

export const loadYaml = yaml.load;
export default yaml;
