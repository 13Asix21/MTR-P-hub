import { computeProgress, emptyState, REVIEW_BATCH_MAX_ITEMS, reviewStatusFor } from './reviewDomain.js?v=20261123e';
import { applyBatch, localProfileStatus } from './reviewWorkflow.js?v=20261123e';
import { downloadExportPair } from './export.js?v=20261123e';
import { renderNotePreview } from './markdownPreview.js?v=20261123e';
import { transferApprovedNote } from './transfer.js?v=20261123e';

/** 创建安全 DOM 节点；所有外部动态值只能走 textContent。 */
export function element(documentRef, tag, text = '', className = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = String(text);
  return node;
}

export function formatPercent(progress) {
  if (progress.pass_rate === null) return '暂无通过率';
  return `通过率 ${(progress.pass_rate * 100).toFixed(1)}%`;
}

function button(documentRef, text, className = 'secondary-button', handler = null) {
  const control = element(documentRef, 'button', text, className);
  control.type = 'button';
  if (handler) control.addEventListener('click', handler);
  return control;
}

function errorMessage(error) {
  const storageErrors = ['OFFLINE_UNAVAILABLE', 'OFFLINE_BLOCKED', 'OFFLINE_STORAGE_FAILED'];
  if (storageErrors.includes(error?.code) || ['QuotaExceededError', 'SecurityError', 'InvalidStateError'].includes(error?.name)) {
    return '本机离线台账无法写入（可能被隐私模式、浏览器策略或存储空间阻止）。本次决定未生效，请先导出备份或换用可用浏览器。';
  }
  return error?.message || '操作未完成，请重试。';
}

/** 真实 UI 服务控制器；没有登录、网络台账或 Vault 写入依赖。 */
export class WorkbenchUi {
  constructor(documentRef, services = {}) {
    this.document = documentRef;
    this.services = services;
    this.root = null;
    this.adapter = null;
    this.listing = null;
    this.state = emptyState();
    this.filter = '';
    this.statusFilter = 'all';
    this.abortController = null;
    this.selectedPath = null;
    this.selectedPaths = new Set();
    this.detail = null;
    this.detailSequence = 0;
    this.detailPromise = null;
    this.vaultId = null;
    this.offlineAvailable = false;
    this.pendingCount = 0;
    this.actionService = null;
    this.actionBusy = false;
    this.actionQueue = Promise.resolve();
    this.restoreHandle = null;
    this.storageWarning = '';
    this.restorePromise = null;
    this.confirm = services.confirm ?? ((message) => globalThis.confirm(message));
    this.online = services.online ?? (() => globalThis.navigator?.onLine !== false);
    this.onKeyDown = (event) => this.handleKey(event);
  }

  mount(root) {
    this.root = root;
    this.renderEmpty();
    this.restorePromise = this.prepareRestore();
    return this;
  }

  destroy() {
    this.document.removeEventListener('keydown', this.onKeyDown);
    this.abortController?.abort();
    this.abortController = null;
  }

  clear() {
    this.abortController?.abort();
    this.abortController = null;
    this.document.removeEventListener('keydown', this.onKeyDown);
    this.root.replaceChildren();
  }

  setOfflineError(error) {
    this.offlineAvailable = false;
    const warning = this.root.querySelector('#storage-warning');
    if (warning) warning.textContent = errorMessage(error);
  }

  showStorageError(error) {
    this.storageWarning = errorMessage(error);
    const warning = this.root?.querySelector('#storage-warning');
    if (warning) warning.textContent = this.storageWarning;
    else this.setNotice(this.storageWarning);
  }

  async prepareRestore() {
    try {
      if (!this.services.offlineBuffer) throw new Error('本机离线台账未配置。');
      await this.services.offlineBuffer.open();
      this.offlineAvailable = true;
      this.restoreHandle = await this.services.loadDirectoryHandle(this.services.offlineBuffer);
      const restore = this.root.querySelector('#restore-vault');
      if (restore && this.restoreHandle) {
        restore.hidden = false;
        restore.textContent = `继续使用上次目录：${this.restoreHandle.name}`;
      }
      const warning = this.root.querySelector('#storage-warning');
      if (warning) warning.textContent = '本机 IndexedDB 可用；台账未加密，请勿在共用浏览器保存敏感理由。';
    } catch (error) {
    this.setOfflineError(error);
    this.showStorageError(error);
  }
}

  renderUnsupported() {
    this.clear();
    const main = element(this.document, 'main', '', 'stage-shell');
    main.append(
      element(this.document, 'p', '审核工作台 / 浏览器要求', 'eyebrow'),
      element(this.document, 'h1', '需要 Chromium 的目录读取能力。'),
      element(this.document, 'p', '本工具依赖 Chromium 的 File System Access API，请使用 Chrome 或 Edge 最新版打开（最低 Chrome 108 / Edge 108）。', 'lead'),
      element(this.document, 'p', '不提供上传压缩包、只读降级或其他目录访问替代路径；页面不会请求本地文件。', 'notice'),
    );
    this.root.append(main);
  }

  renderEmpty(message = '未选择目录') {
    this.clear();
    const main = element(this.document, 'main', '', 'stage-shell');
    const select = button(this.document, '选择 Obsidian 库文件夹', 'primary-button', async () => {
      select.disabled = true;
      this.setNotice('等待你选择 Obsidian 库的根目录。');
      try {
        if (this.restorePromise) await this.restorePromise;
        if (!this.offlineAvailable) throw new Error('本机离线台账不可用，无法开始审核。请检查浏览器存储权限。');
        const handle = await this.services.pickDirectory();
        if (handle) await this.authorizeAndScan(handle);
        else this.setNotice('未选择目录');
      } catch (error) {
        this.setNotice(error?.message || '目录授权失败，请重新选择。');
      } finally { select.disabled = false; }
    });
    select.id = 'select-vault';
    const restore = button(this.document, '继续使用上次目录', 'secondary-button', async () => {
      restore.disabled = true;
      try {
        if (this.restorePromise) await this.restorePromise;
        if (!this.restoreHandle) throw new Error('没有可恢复的本机目录授权。');
        if (await this.services.requestReadPermission(this.restoreHandle) !== 'granted') {
          throw new Error('目录只读授权未获允许，请重新选择文件夹。');
        }
        await this.authorizeAndScan(this.restoreHandle);
      } catch (error) {
        this.setNotice(errorMessage(error));
      } finally { restore.disabled = false; }
    });
    restore.id = 'restore-vault';
    restore.hidden = true;
    this.notice = element(this.document, 'p', message, 'notice');
    this.notice.id = 'app-notice';
    this.notice.setAttribute('role', 'status');
    main.append(
      element(this.document, 'p', '审核工作台 / 在线版', 'eyebrow'),
      element(this.document, 'h1', '从你的 Obsidian 库开始，继续审核。'),
      element(this.document, 'p', '身份：本地浏览器档案（不是账号登录）。手动选择 Obsidian 库后仅读取笔记，绝不写入库内文件。', 'lead'),
      element(this.document, 'p', '适用于 PARA、Zettelkasten、扁平等多层文件夹结构：会递归遍历库内所有子文件夹下的 Markdown，自动跳过 .obsidian/.trash/.git 等隐藏目录。', 'privacy-note'),
      select,
      restore,
      element(this.document, 'p', '审核动作和理由仅保存于当前浏览器的 IndexedDB，不是云同步；笔记正文不进入台账，也不会发起云请求。', 'privacy-note'),
      element(this.document, 'p', '本机台账未加密；浏览器配置、操作系统磁盘加密与本机访问控制不属于本应用加密。请勿将 Base64 或 SHA-256 误认为加密。', 'privacy-note'),
      element(this.document, 'p', '清理浏览器数据、隐私模式或存储空间不足可能导致台账不可用或丢失，请定期下载 JSON/CSV 导出。', 'privacy-note'),
      this.renderSettingsEntry(),
      element(this.document, 'p', '', 'storage-warning'),
      this.notice,
    );
    main.querySelector('.storage-warning').id = 'storage-warning';
    main.querySelector('.storage-warning').setAttribute('role', 'alert');
    this.root.append(main);
  }

  renderSettingsEntry() {
    const wrap = element(this.document, 'div', '', 'settings-entry');
    wrap.setAttribute('aria-label', '在线设置');
    const open = button(this.document, '在线设置 · 我的库连接', 'secondary-button', async () => {
      const { LibrarySettingsPanel } = await import('./librarySettingsPanel.js?v=20261123e');
      if (!this.settingsPanel) {
        this.settingsPanel = new LibrarySettingsPanel(this.document, { onClose: () => this.closeSettings() });
      }
      this.settingsHost = element(this.document, 'div', '', 'settings-host');
      wrap.append(this.settingsHost);
      await this.settingsPanel.open(this.settingsHost);
    });
    open.id = 'open-settings';
    const explain = button(this.document, '看看流转说明', 'secondary-button', async () => {
      const { FlowExplainer } = await import('./flowExplainer.js?v=20261123e');
      this.flowHost = element(this.document, 'div', '', 'settings-host');
      wrap.append(this.flowHost);
      const mConfig = await this.loadMLibraryConfig();
      const counts = { approved: 0, rejected: 0 };
      for (const record of Object.values(this.state?.items ?? {})) {
        if (record?.action === 'approved') counts.approved += 1;
        if (record?.action === 'rejected') counts.rejected += 1;
      }
      if (!this.flowExplainer) {
        this.flowExplainer = new FlowExplainer(this.document, { onClose: () => this.closeFlow() });
      }
      this.flowExplainer.render(this.flowHost, { mConfig, counts });
    });
    explain.id = 'open-flow-guide';
    wrap.append(open, explain);
    return wrap;
  }

  /** 读取云端 M 库配置（未登录或失败时返回 null，不阻塞页面）。 */
  async loadMLibraryConfig() {
    try {
      const { getCloud } = await import('./cloudClient.js?v=20261123e');
      const { loadConnections } = await import('./librarySettings.js?v=20261123e');
      const { currentSession } = await import('./cloudClient.js?v=20261123e');
      const { session } = await currentSession();
      if (!session) return null;
      this.session = session;
      const map = await loadConnections(await getCloud());
      return map.m?.config ?? null;
    } catch {
      return null;
    }
  }

  closeFlow() {
    this.flowHost?.replaceChildren();
    this.flowHost?.remove();
    this.flowHost = null;
  }

  closeSettings() {
    this.settingsHost?.replaceChildren();
    this.settingsHost?.remove();
    this.settingsHost = null;
  }

  setNotice(message) {
    if (this.notice) this.notice.textContent = message;
    else {
      const notice = this.root.querySelector('#app-notice');
      if (notice) notice.textContent = message;
    }
  }

  async authorizeAndScan(handle) {
    if (!this.offlineAvailable) await this.prepareRestore();
    if (!this.offlineAvailable) throw new Error('本机离线台账不可用，无法开始审核。请检查浏览器存储权限。');
    this.adapter = await this.services.createAdapter(handle);
    const main = this.root.querySelector('main');
    main?.replaceChildren();
    const progress = element(this.document, 'progress', '', 'scan-progress');
    progress.max = 1;
    progress.value = 0;
    this.notice = element(this.document, 'p', '正在遍历 Obsidian 库…', 'notice');
    this.notice.id = 'app-notice';
    const cancel = button(this.document, '中断扫描', 'secondary-button', () => this.abortController.abort());
    this.abortController = new AbortController();
    main?.append(element(this.document, 'h1', `已授权：${handle.name}`), progress, this.notice, cancel);
    try {
      this.listing = await this.services.scan(handle, {
        signal: this.abortController.signal,
        onProgress: ({ scanned, currentDirectory }) => { this.setNotice(`已扫描 ${scanned} 篇 / 当前目录 ${currentDirectory || '库根目录'}`); },
      });
      this.vaultId = await this.services.vaultId(handle.name, this.listing);
      this.state = await this.services.offlineBuffer.replayPending({ userId: 'local-browser', vaultId: this.vaultId });
      this.actionService = this.services.createActionService({
        adapter: this.adapter,
        offlineBuffer: this.services.offlineBuffer,
        userId: 'local-browser',
        vaultId: this.vaultId,
        sourcePaths: this.listing._source_paths ?? {},
      });
      await this.services.saveDirectoryHandle(this.services.offlineBuffer, handle);
      await this.refreshPending();
      this.renderWorkbench();
    } catch (error) {
      if (error?.name === 'AbortError') this.renderEmpty('扫描已中断，未选择目录。');
      else this.renderEmpty(errorMessage(error));
    }
  }

  async refreshPending() {
    const status = await this.services.offlineBuffer.getStatus('local-browser');
    this.pendingCount = status.pending;
    return status;
  }

  visibleItems() {
    const query = this.filter.toLocaleLowerCase();
    return this.listing.items.filter((item) => {
      const status = reviewStatusFor(this.state.items[item.vault_relative_path], item.sha256);
      return (!query || item.vault_relative_path.toLocaleLowerCase().includes(query))
        && (this.statusFilter === 'all' || status === this.statusFilter || item.mtr_state === this.statusFilter);
    });
  }

  renderWorkbench() {
    this.clear();
    const progress = computeProgress(this.listing, this.state);
    const main = element(this.document, 'main', '', 'workbench');
    const header = element(this.document, 'section', '', 'summary-panel');
    header.append(
      element(this.document, 'h1', `审核队列 ${this.listing.total} 篇`),
      element(this.document, 'p', `已审 ${progress.reviewed} / ${this.listing.total}　通过 ${progress.approved}　拒绝 ${progress.rejected}　跳过 ${progress.skipped}　未审 ${progress.pending}　格式异常 ${progress.invalid}　内容已变更 ${progress.stale}　${formatPercent(progress)}`),
    );
    const identity = localProfileStatus(this.state, this.pendingCount, this.online());
    const privacy = element(this.document, 'section', '', 'privacy-panel');
    privacy.append(
      element(this.document, 'p', `${identity.identity}（非登录） · ${identity.sync} · ${identity.connectivity}`),
      element(this.document, 'p', `本机待保存动作 ${this.pendingCount} 条。台账仅存路径、理由、决定与哈希，不存正文且未加密。`),
      element(this.document, 'p', this.storageWarning, 'storage-warning'),
    );
    privacy.querySelector('.storage-warning').id = 'storage-warning';
    privacy.querySelector('.storage-warning').setAttribute('role', 'alert');
    const toolbar = element(this.document, 'section', '', 'toolbar');
    const search = element(this.document, 'input');
    search.type = 'search';
    search.placeholder = '搜索相对路径';
    search.setAttribute('aria-label', '搜索相对路径');
    search.value = this.filter;
    search.addEventListener('input', () => { this.filter = search.value; this.renderItems(); });
    const filter = element(this.document, 'select');
    filter.setAttribute('aria-label', '按审核状态筛选');
    for (const [value, label] of [['all', '全部状态'], ['pending', '未审'], ['approved', '通过'], ['rejected', '拒绝'], ['skipped', '跳过'], ['stale', '内容已变更'], ['invalid', '格式异常'], ['trial', 'mtr/trial'], ['rarely', 'mtr/rarely']]) {
      const option = element(this.document, 'option', label);
      option.value = value;
      filter.append(option);
    }
    filter.value = this.statusFilter;
    filter.addEventListener('change', () => { this.statusFilter = filter.value; this.renderItems(); });
    const exportButton = button(this.document, '预览并导出已批准 JSON + CSV', 'primary-button', () => this.exportApproved(exportButton));
    exportButton.id = 'export-approved';
    exportButton.classList.remove('primary-button');
    exportButton.classList.add('secondary-button');
    const rescan = button(this.document, '重新选择并扫描', 'secondary-button', () => this.renderEmpty('请重新授权目录；已读本机队列不会被清空。'));
    const clear = button(this.document, '清除本机台账…', 'danger-button', () => this.clearLocalData());
    clear.id = 'clear-local-data';
    toolbar.append(search, filter, exportButton, rescan, clear);
    const list = element(this.document, 'section', '', 'note-list');
    list.id = 'note-list';
    this.detailPanel = element(this.document, 'aside', '', 'detail-panel');
    this.detailPanel.id = 'review-detail';
    this.detailPanel.setAttribute('aria-label', '笔记详情与审核');
    this.notice = element(this.document, 'p', '点击路径或按 J/K 选择笔记。A 通过、R 拒绝、S 跳过、U 撤销。', 'notice');
    this.notice.id = 'app-notice';
    this.notice.setAttribute('role', 'status');
    this.notice.tabIndex = -1;
    const columns = element(this.document, 'div', '', 'review-columns');
    columns.append(list, this.detailPanel);
    main.append(header, privacy, toolbar, this.notice, columns);
    this.root.append(main);
    this.renderItems();
    this.renderDetailEmpty();
    this.document.addEventListener('keydown', this.onKeyDown);
    if (this.selectedPath) {
      const item = this.listing.items.find((candidate) => candidate.vault_relative_path === this.selectedPath);
      if (item) {
        if (this.detail?.item.vault_relative_path === item.vault_relative_path) this.renderDetail(item);
        else void this.openDetail(item);
      }
    }
  }

  renderItems() {
    const list = this.root.querySelector('#note-list');
    if (!list) return;
    list.replaceChildren();
    const items = this.visibleItems();
    if (this.statusFilter === 'invalid') {
      const invalid = this.listing.items.filter((item) => item.parse_status !== 'ok'
        && (!this.filter.toLocaleLowerCase() || item.vault_relative_path.toLocaleLowerCase().includes(this.filter.toLocaleLowerCase())));
      list.append(this.batchControls(invalid));
      this.renderRows(list, invalid);
      return;
    }
    list.append(this.batchControls(items));
    this.renderRows(list, items);
  }

  batchControls(items) {
    const bar = element(this.document, 'div', '', 'batch-bar');
    const selectable = new Set(items.filter((item) => item.parse_status !== 'FILE_TOO_LARGE').map((item) => item.vault_relative_path));
    const all = element(this.document, 'input');
    all.type = 'checkbox';
    all.id = 'select-visible';
    all.checked = selectable.size > 0 && [...selectable].every((path) => this.selectedPaths.has(path));
    all.setAttribute('aria-label', '选择当前筛选全部笔记');
    all.addEventListener('change', () => {
      if (all.checked) {
        if (selectable.size > REVIEW_BATCH_MAX_ITEMS) {
          all.checked = false;
          this.setNotice(`当前筛选 ${selectable.size} 篇，超过批量上限 ${REVIEW_BATCH_MAX_ITEMS}；请缩小筛选或分批勾选。`);
          return;
        }
        this.selectedPaths = new Set(selectable);
      } else {
        for (const path of selectable) this.selectedPaths.delete(path);
      }
      this.renderItems();
    });
    const count = element(this.document, 'span', `已选 ${this.selectedPaths.size} / 最多 ${REVIEW_BATCH_MAX_ITEMS} 篇`);
    for (const [action, label] of [['approved', '批量通过'], ['rejected', '批量拒绝'], ['skipped', '批量跳过'], ['undo', '批量撤销']]) {
      const control = button(this.document, label, 'secondary-button', () => this.batch(action));
      control.dataset.batchAction = action;
      bar.append(control);
    }
    bar.prepend(all, count);
    return bar;
  }

  renderRows(list, items) {
    if (!items.length) { list.append(element(this.document, 'p', '没有符合当前条件的笔记。', 'empty-note')); return; }
    const table = element(this.document, 'table');
    const head = element(this.document, 'thead');
    const header = element(this.document, 'tr');
    for (const label of ['选择', '相对路径', '审核', '分类', '解析状态', '出链 / 反链', '字节数']) header.append(element(this.document, 'th', label));
    head.append(header);
    const body = element(this.document, 'tbody');
    for (const item of items) {
      const path = item.vault_relative_path;
      const row = element(this.document, 'tr');
      row.dataset.path = path;
      if (path === this.selectedPath) row.classList.add('selected-row');
      const selectCell = element(this.document, 'td');
      const checkbox = element(this.document, 'input');
      checkbox.type = 'checkbox';
      checkbox.checked = this.selectedPaths.has(path);
      checkbox.setAttribute('aria-label', `选择 ${path}`);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked && this.selectedPaths.size >= REVIEW_BATCH_MAX_ITEMS) {
          checkbox.checked = false;
          this.setNotice(`批量选择最多 ${REVIEW_BATCH_MAX_ITEMS} 篇。`);
          return;
        }
        if (checkbox.checked) this.selectedPaths.add(path);
        else this.selectedPaths.delete(path);
        this.root.querySelector('.batch-bar span').textContent = `已选 ${this.selectedPaths.size} / 最多 ${REVIEW_BATCH_MAX_ITEMS} 篇`;
      });
      selectCell.append(checkbox);
      const pathCell = element(this.document, 'td', '', 'note-path');
      const open = button(this.document, path, 'path-button', () => this.openDetail(item));
      open.dataset.path = path;
      pathCell.append(open);
      const status = item.parse_status === 'FILE_TOO_LARGE' ? 'too_large' : reviewStatusFor(this.state.items[path], item.sha256);
      const statusText = { pending: '未审', approved: '通过', rejected: '拒绝', skipped: '跳过', stale: '内容已变更', too_large: '超限不可审' }[status];
      row.append(selectCell, pathCell, element(this.document, 'td', statusText), element(this.document, 'td', item.mtr_state), element(this.document, 'td', item.parse_status), element(this.document, 'td', `${item.links_out} / ${item.links_in}`), element(this.document, 'td', item.size_bytes));
      body.append(row);
    }
    table.append(head, body);
    list.append(table);
  }

  renderDetailEmpty() {
    this.detailPanel?.replaceChildren(element(this.document, 'p', '请选择一篇笔记查看原文。', 'empty-note'));
  }

  openDetail(item) {
    const path = item.vault_relative_path;
    const sequence = ++this.detailSequence;
    this.selectedPath = path;
    this.detail = null;
    this.detailPanel.replaceChildren(element(this.document, 'p', '正在重新读取原始字节并核对 SHA…', 'empty-note'));
    this.detailPromise = (async () => {
      try {
        const detail = await this.actionService.detail(item, this.state);
        if (sequence !== this.detailSequence || this.selectedPath !== path) return null;
        this.detail = detail;
        this.renderDetail(item);
        this.renderItems();
        return detail;
      } catch (error) {
        if (sequence !== this.detailSequence) return null;
        this.detail = null;
        this.detailPanel.replaceChildren(element(this.document, 'p', errorMessage(error), 'notice'));
        this.setNotice(errorMessage(error));
        return null;
      }
    })();
    return this.detailPromise;
  }

  renderDetail(item) {
    const detail = this.detail;
    this.detailPanel.replaceChildren();
    this.detailPanel.append(
      element(this.document, 'h2', item.vault_relative_path),
      element(this.document, 'p', `当前 SHA-256：${detail.item.sha256} · ${detail.item.parse_status}${detail.stale ? ' · 旧审核已失效' : ''}`),
      element(this.document, 'p', detail.item.parse_status === 'ok' ? '只读原文：解析结果仅供审核，不会修改文件。' : '格式或编码异常仍计入分母；以下为只读原文，可人工通过、拒绝或跳过。'),
    );
    const reason = element(this.document, 'textarea');
    reason.id = 'review-reason';
    reason.maxLength = 500;
    reason.rows = 3;
    reason.placeholder = '审核理由（可选，最多 500 字符；仅存本机浏览器）';
    reason.setAttribute('aria-label', '审核理由');
    reason.value = detail.decision?.reason ?? '';
    const actions = element(this.document, 'div', '', 'detail-actions');
    for (const [action, label, key] of [['approved', '通过 (A)', 'a'], ['rejected', '拒绝 (R)', 'r'], ['skipped', '跳过 (S)', 's'], ['undo', '撤销 (U)', 'u']]) {
      const control = button(this.document, label, 'secondary-button', () => this.performAction(action));
      control.dataset.action = action;
      control.dataset.key = key;
      if (action === 'undo') control.disabled = !detail.decision;
      actions.append(control);
    }
    this.detailPanel.append(reason, actions);

    // 审核结论与流转信息：拒绝留下可追溯标识，通过后才出现“传送到 M 库”。
    if (!this.transferConfigLoaded) this.ensureTransferConfig();
    this.detailPanel.append(this.renderDecisionFlow(item, detail));
    this.detailPanel.append(this.renderSourceViews(detail));
  }

  /** 结论区：拒绝标识 + 通过后的传送入口。 */
  renderDecisionFlow(item, detail) {
    const wrap = element(this.document, 'section', '', 'decision-flow');
    wrap.setAttribute('aria-label', '审核结论与流转');
    const decision = detail.decision;
    if (!decision) {
      wrap.append(element(this.document, 'p', '尚未审核。通过后可传送到 M 库；拒绝会在台账中留下待清理标识。', 'flow-hint'));
      return wrap;
    }
    const badge = element(this.document, 'span', decision.action === 'approved' ? '已通过' : decision.action === 'rejected' ? '已拒绝' : '已跳过', `flow-badge is-${decision.action}`);
    const head = element(this.document, 'div', '', 'flow-head');
    head.append(badge, element(this.document, 'span', `在 ${decision.decided_at ?? ''} 记录`, 'flow-time'));
    wrap.append(head);
    if (decision.reason) wrap.append(element(this.document, 'p', `理由：${decision.reason}`, 'flow-reason'));

    if (decision.action === 'rejected') {
      const box = element(this.document, 'div', '', 'reject-mark');
      box.append(
        element(this.document, 'strong', '已留下待清理标识'),
        element(this.document, 'p', `台账记录：路径 ${item.vault_relative_path}、拒绝时 SHA ${String(detail.item.sha256).slice(0, 12)}…、理由与时间。后续删除或修改时以此为准，本页面不会改动 Obsidian 原文件。`),
      );
      wrap.append(box);
    }

    if (decision.action === 'approved') {
      const row = element(this.document, 'div', '', 'flow-actions');
      const targetText = this.describeTransferTarget();
      row.append(element(this.document, 'span', targetText, 'flow-target'));
      const send = button(this.document, '传送到 M 库', 'primary-button', () => this.transferToLibrary());
      send.id = 'transfer-to-m';
      row.append(send);
      const hint = element(this.document, 'span', '', 'flow-hint');
      row.append(hint);
      const result = element(this.document, 'p', '', 'flow-result');
      result.dataset.role = 'transfer-result';
      wrap.append(row, result);
      // 配置可能晚于详情到达：渲染后统一刷新按钮可用性。
      this.syncTransferButton();
    }
    return wrap;
  }

  /** 按当前配置刷新传送按钮，避免渲染时序导致一直禁用。 */
  syncTransferButton() {
    const send = this.detailPanel?.querySelector('#transfer-to-m');
    if (!send) return;
    const config = this.mLibraryConfig;
    const ready = Boolean(config?.space_id && config?.proxy_url && config?.sharecode);
    const loaded = Boolean(this.transferConfigLoaded);
    send.disabled = !(loaded && ready);
    const hint = send.parentElement?.querySelector('.flow-hint');
    if (hint) {
      hint.textContent = !loaded
        ? '正在读取在线设置…'
        : ready
          ? ''
          : '需在「在线设置」填写乐享空间、代理地址与代理 sharecode 后才可传送。';
    }
  }

  /** 原文 / 预览双视图；预览只输出安全 HTML。 */
  renderSourceViews(detail) {
    const wrap = element(this.document, 'section', '', 'source-views');
    const bar = element(this.document, 'div', '', 'view-tabs');
    bar.setAttribute('role', 'tablist');
    const rawText = detail.raw_text ?? detail.body.text ?? '';
    const raw = element(this.document, 'pre', rawText, 'note-source');
    raw.id = 'note-source';
    raw.tabIndex = 0;
    raw.hidden = true;
    const preview = element(this.document, 'div', '', 'note-preview markdown-body');
    preview.id = 'note-preview';
    try {
      const { frontmatterText, html } = renderNotePreview(rawText);
      if (frontmatterText) {
        const fm = element(this.document, 'pre', frontmatterText, 'md-frontmatter');
        preview.append(fm);
      }
      // 内容来自本地已转义渲染器，不含用户原始 HTML。
      preview.insertAdjacentHTML('beforeend', html);
    } catch {
      preview.append(element(this.document, 'p', '预览渲染失败，请查看原文。', 'notice'));
    }
    const tabs = [
      ['preview', '预览', preview],
      ['raw', '原文', raw],
    ];
    for (const [id, label, panel] of tabs) {
      const tab = button(this.document, label, 'view-tab', () => {
        for (const [, , other] of tabs) other.hidden = other !== panel;
        for (const node of bar.querySelectorAll('.view-tab')) node.setAttribute('aria-selected', String(node.textContent === label));
      });
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(id === 'preview'));
      tab.dataset.view = id;
      bar.append(tab);
    }
    wrap.append(bar, preview, raw);
    return wrap;
  }

  describeTransferTarget() {
    const config = this.mLibraryConfig;
    if (!this.transferConfigLoaded) return '目标：正在读取在线设置中的 M 库配置…';
    if (!config || !config.space_id) return '目标：M 库（乐享知识库），尚未配置空间 ID';
    const path = config.path ? `/${String(config.path).replace(/^\/+/u, '')}` : '（知识库根目录）';
    return `目标：M 库（乐享知识库）· 空间 ${config.space_id} · 路径 ${path}`;
  }

  canTransfer() {
    const config = this.mLibraryConfig;
    // 会话在点击传送时再校验（transfer.js 内会拒绝未登录），避免详情先渲染、会话后到的时序问题。
    return Boolean(config?.space_id && config?.proxy_url && config?.sharecode && this.services.transferToLibrary);
  }

  /** 详情渲染前异步取一次云端 M 库配置；成功后重绘结论区，不需要用户手动刷新。 */
  ensureTransferConfig() {
    if (this.transferConfigPromise) return this.transferConfigPromise;
    this.transferConfigPromise = this.loadMLibraryConfig()
      .then((config) => {
        this.mLibraryConfig = config;
        this.transferConfigLoaded = true;
        this.refreshDecisionFlow();
        this.syncTransferButton();
      })
      .catch(() => {
        this.transferConfigLoaded = true;
        this.refreshDecisionFlow();
        this.syncTransferButton();
      });
    return this.transferConfigPromise;
  }

  /** 只替换结论区，不重绘整个详情，避免打断理由输入与原文选中。 */
  refreshDecisionFlow() {
    const panel = this.detailPanel;
    if (!panel || !this.detail || !this.selectedPath) return;
    const item = this.listing?.items?.find((candidate) => candidate.vault_relative_path === this.selectedPath);
    if (!item) return;
    const existing = panel.querySelector('.decision-flow');
    const next = this.renderDecisionFlow(item, this.detail);
    if (existing) existing.replaceWith(next);
    else panel.append(next);
  }

  async transferToLibrary() {
    const node = this.detailPanel.querySelector('[data-role="transfer-result"]');
    const path = this.selectedPath;
    if (!path) return;
    if (node) node.textContent = '正在传送…';
    try {
      if (!this.session) await this.loadMLibraryConfig();
      const transfer = this.services.transferToLibrary ?? transferApprovedNote;
      const result = await transfer({
        path,
        detail: this.detail,
        library: this.mLibraryConfig,
        session: this.session,
      });
      if (node) node.textContent = result.message;
      this.setNotice(`已传送：${path}`);
    } catch (error) {
      if (node) {
        node.textContent = error?.message ?? '传送失败。';
        node.classList.add('settings-error');
      }
    }
  }

  serialAction(operation) {
    const result = this.actionQueue.then(async () => {
      if (this.detailPromise) await this.detailPromise;
      return operation();
    });
    this.actionQueue = result.catch(() => {});
    return result;
  }

  performAction(action) {
    const path = this.selectedPath;
    const reason = this.root.querySelector('#review-reason')?.value ?? '';
    return this.serialAction(() => this.commitAction(action, path, reason));
  }

  async commitAction(action, path, reason) {
    this.actionBusy = true;
    try {
      const item = this.listing.items.find((candidate) => candidate.vault_relative_path === path);
      let detail = this.detail;
      if (item && (!detail || detail.item.vault_relative_path !== path)) {
        detail = await this.actionService.detail(item, this.state);
      }
      if (!item || !detail || detail.item.vault_relative_path !== path) return false;
      if (['FILE_READ_FAILED', 'FILE_TOO_LARGE', 'INVALID_ENCODING'].includes(detail.item.parse_status)) {
        this.setNotice('该篇没有可核验的有效原始字节，不能审核；请修复或重新扫描可读文件。');
        return false;
      }
      const result = action === 'undo'
        ? await this.actionService.reset(this.state, path, 'undo', detail.item.sha256, detail.item.sha256)
        : await this.actionService.decide(this.state, item, detail.item.sha256, action, reason);
      this.state = result.state;
      await this.refreshPending();
      if (this.selectedPath === path) this.detail = null;
      this.renderWorkbench();
      this.setNotice(`已写入当前浏览器台账：${path} · ${action}。未进行云同步。`);
      if (this.selectedPath === path) {
        const item = this.listing.items.find((candidate) => candidate.vault_relative_path === path);
        if (item) await this.openDetail(item);
      }
      return true;
    } catch (error) {
      this.setNotice(errorMessage(error));
      if (['OFFLINE_UNAVAILABLE', 'OFFLINE_BLOCKED', 'OFFLINE_STORAGE_FAILED'].includes(error?.code)) this.showStorageError(error);
      return false;
    } finally { this.actionBusy = false; }
  }

  async batch(action) {
    if (this.actionBusy) return;
    if (!this.selectedPaths.size) { this.setNotice('请先勾选要批量处理的笔记。'); return; }
    if (!['undo', 'approved'].includes(action) || !this.confirm(`确认对 ${this.selectedPaths.size} 篇执行“${action}”？只写本机台账，不改 Vault。`)) return;
    const paths = [...this.selectedPaths];
    return this.serialAction(async () => {
      this.actionBusy = true;
      try {
        const result = await applyBatch({
          listing: this.listing,
          state: this.state,
          paths,
          action,
          reason: '',
          decide: (...args) => this.actionService.decide(...args),
          reset: (...args) => this.actionService.reset(...args),
        });
        this.state = result.state;
        await this.refreshPending();
        this.selectedPaths = new Set(result.failures.map((failure) => failure.path));
        this.detail = null;
        this.renderWorkbench();
        this.setNotice(`批量完成 ${result.completed.length} 篇，未处理 ${result.failures.length} 篇。冲突及失败项仍保留，已读队列未清空。${result.failures.map((failure) => `${failure.path}: ${failure.code}`).join('；')}`);
      } catch (error) { this.setNotice(errorMessage(error)); }
      finally { this.actionBusy = false; }
    });
  }

  async exportApproved(control) {
    if (this.actionBusy) return;
    this.actionBusy = true;
    control.disabled = true;
    return this.serialAction(async () => {
      try {
      const result = await this.services.previewExport({
        listing: this.listing,
        state: this.state,
        include: 'approved',
        userId: 'local-browser',
        vaultRoot: this.listing.path,
        vaultId: this.vaultId,
        freshSha: (path) => this.actionService.freshSha(path),
      });
      const preview = result.preview;
      this.setNotice(`dry-run：可导出 ${preview.items_count} 篇，已剔除当前 hash 不一致 ${preview.excluded_stale} 篇。`);
      if (!this.confirm(`将下载 ${preview.items_count} 篇已批准 JSON 与 CSV（各一份）。stale 已剔除 ${preview.excluded_stale} 篇。继续？`)) return;
      await this.services.downloadExportPair(result, { documentRef: this.document });
      this.setNotice('已请求浏览器下载 JSON 与 CSV；未上传台账或笔记正文。');
      } catch (error) { this.setNotice(errorMessage(error)); }
      finally { this.actionBusy = false; control.disabled = false; }
    });
  }

  async clearLocalData() {
    const phrase = `只删除当前浏览器的审核动作、导出备份和目录句柄；不会删除或修改 Vault 中任何文件。此操作不可撤销，请先导出备份。`;
    if (!this.confirm(phrase) || !this.confirm('第二次确认：确定永久清除当前浏览器的本地台账？')) return;
    return this.serialAction(async () => {
      try {
      await this.services.offlineBuffer.clearLocalData();
      this.state = emptyState();
      this.selectedPath = null;
      this.selectedPaths.clear();
      this.detail = null;
      this.detailSequence += 1;
      this.restoreHandle = null;
      this.pendingCount = 0;
      this.renderWorkbench();
      this.setNotice('当前浏览器台账已清除；Vault 文件未改动。下次扫描将从空档案开始。');
      } catch (error) { this.setNotice(errorMessage(error)); }
    });
  }

  handleKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
    const target = event.target;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target?.tagName) || target?.isContentEditable) return;
    const key = event.key.toLowerCase();
    if (!['j', 'k', 'a', 'r', 's', 'u'].includes(key) || !this.listing) return;
    event.preventDefault();
    if (key === 'j' || key === 'k') {
      const items = this.visibleItems();
      if (!items.length) return;
      const current = items.findIndex((item) => item.vault_relative_path === this.selectedPath);
      const next = key === 'j' ? Math.min(items.length - 1, current + 1) : Math.max(0, current < 0 ? 0 : current - 1);
      void this.openDetail(items[next]);
      this.root.querySelector(`[data-path="${CSS.escape(items[next].vault_relative_path)}"]`)?.focus();
      return;
    }
    const action = { a: 'approved', r: 'rejected', s: 'skipped', u: 'undo' }[key];
    void this.performAction(action);
  }
}
