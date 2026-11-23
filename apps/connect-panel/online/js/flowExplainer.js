/**
 * 流转说明页：讲清「审核通过 → 传送到哪个库的哪条路径」，
 * 以及「拒绝了什么、留在哪里、后续怎么用」。
 */
import { describeLibrary } from './librarySettings.js?v=20261123o';

function el(document, tag, text = '', className = '') {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export const FLOW_STEPS = Object.freeze([
  {
    id: 'scan',
    title: '1. 只读扫描',
    body: '选择 Obsidian 库文件夹后递归遍历所有子文件夹的 Markdown，现算 SHA-256。整个过程不改动任何笔记。',
    where: '本机浏览器；正文不出浏览器',
  },
  {
    id: 'decide',
    title: '2. 人工审核',
    body: '逐篇或批量决定通过 / 拒绝 / 跳过。理由最多 500 字符，只存台账（路径、理由、决定、SHA），不存正文。',
    where: '本机 IndexedDB 台账，可按账号镜像到云端',
  },
  {
    id: 'approve',
    title: '3. 通过 → 传送到 M 库',
    body: '通过后出现「传送到 M 库」按钮，把这篇笔记送到你在「在线设置」里填写的乐享空间与目标路径。传送前会重新核对 SHA，内容变了会拒绝旧版本。',
    where: 'M 库（乐享知识库）指定空间 / 路径',
  },
  {
    id: 'reject',
    title: '4. 拒绝 → 留下待清理标识',
    body: '拒绝会写台账标识：路径、拒绝时 SHA、理由、时间。后续批量删除或修改时，以这份标识为第一手依据；页面不会自动改你的 Obsidian 文件。',
    where: '台账（rejected 状态）',
  },
  {
    id: 'export',
    title: '5. 导出放行清单',
    body: '导出 JSON + CSV，包含通过项与 stale 标记，供后续发布或归档使用。',
    where: '浏览器下载目录 / 数据目录 exports',
  },
]);

export function describeTarget(mConfig) {
  if (!mConfig) return 'M 库（乐享知识库）：尚未配置目标';
  const space = mConfig.space_id ? `空间 ${mConfig.space_id}` : '空间未填写';
  const path = mConfig.path ? `路径 /${String(mConfig.path).replace(/^\/+/u, '')}` : '路径：知识库根目录';
  const proxy = mConfig.proxy_url ? '代理已配置' : '代理未配置（无法传送）';
  return `M 库（乐享知识库）：${space} · ${path} · ${proxy}`;
}

export class FlowExplainer {
  constructor(documentRef, { onClose } = {}) {
    this.document = documentRef;
    this.onClose = onClose ?? (() => {});
    this.root = null;
  }

  render(container, { mConfig = null, counts = {} } = {}) {
    this.root = container;
    const box = el(this.document, 'div', '', 'flow-page');
    box.append(
      el(this.document, 'h2', '库的选择与流转说明'),
      el(this.document, 'p', '这篇页面只回答两件事：通过的文件去了哪里，拒绝的文件留下了什么。', 'settings-hint'),
    );

    const target = el(this.document, 'div', '', 'flow-target-card');
    target.append(
      el(this.document, 'strong', '当前传送目标'),
      el(this.document, 'p', describeTarget(mConfig)),
    );
    box.append(target);

    const list = el(this.document, 'ol', '', 'flow-steps');
    for (const step of FLOW_STEPS) {
      const item = el(this.document, 'li', '', 'flow-step');
      item.append(
        el(this.document, 'h3', step.title),
        el(this.document, 'p', step.body),
        el(this.document, 'p', `落在：${step.where}`, 'flow-where'),
      );
      list.append(item);
    }
    box.append(list);

    const summary = el(this.document, 'div', '', 'flow-summary');
    summary.append(el(this.document, 'h3', '当前队列'));
    summary.append(el(this.document, 'p', `已通过 ${counts.approved ?? 0} 篇 · 已拒绝 ${counts.rejected ?? 0} 篇 · 待清理标识 ${counts.rejected ?? 0} 条`));
    box.append(summary);

    box.append(
      el(this.document, 'p', '配置入口：返回审核页点击「在线设置 · 我的库连接」。传送必须登录并填好乐享空间、目标路径与代理地址。', 'settings-hint'),
    );
    const close = el(this.document, 'button', '返回审核', 'secondary-button');
    close.type = 'button';
    close.addEventListener('click', () => this.onClose());
    box.append(close);
    this.root.replaceChildren(box);
    return box;
  }
}

export { describeLibrary };
