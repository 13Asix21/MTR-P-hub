/**
 * 在线设置面板：登录 + 按用户编辑四库连接。
 * 未登录时只展示说明，不读写任何用户数据。
 */
import { LIBRARIES, cloudAvailable, clearCallbackParams, currentSession, getCloud } from './cloudClient.js';
import {
  LibraryConfigError,
  deleteConnection,
  describeLibrary,
  emptyConfig,
  loadConnections,
  normalizeConfig,
  probeGithub,
  saveConnection,
} from './librarySettings.js';

function el(document, tag, text = '', className = '') {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(document, text, className, onClick) {
  const node = el(document, 'button', text, className);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

const FIELD_SCHEMA = {
  m: [
    { key: 'space_id', label: '乐享空间 ID', placeholder: '例如 c3f157b3d03d4c7fa46a8fb8e70b1ae7' },
    { key: 'path', label: '目标路径（相对知识库根）', placeholder: '例如 研发/工业视觉，留空表示根目录' },
    { key: 'proxy_url', label: '乐享代理地址（服务端）', placeholder: 'https://your-gateway/mtrp/lexiang，留空表示未接入' },
  ],
  t: [
    { key: 'vault_root', label: '库内起始目录（相对路径）', placeholder: '留空 = 整个库；或填 2-Permanent' },
    { key: 'managed_roots', label: '附加目录（逗号分隔，可空）', placeholder: 'Inbox, 3-Knowledge-Graph', list: true },
    { key: 'tags', label: '关注标签（逗号分隔，可空）', placeholder: 'mtr/trial', list: true },
  ],
  r: [
    { key: 'vault_root', label: '库内起始目录（相对路径）', placeholder: '留空 = 整个库；或填 2-Permanent' },
    { key: 'managed_roots', label: '附加目录（逗号分隔，可空）', placeholder: 'Archive', list: true },
    { key: 'tags', label: '关注标签（逗号分隔，可空）', placeholder: 'mtr/rarely', list: true },
  ],
  p: [
    { key: 'owner', label: 'GitHub 所有者', placeholder: '13Asix21' },
    { key: 'repo', label: '仓库名', placeholder: 'MTR-P-hub' },
    { key: 'branch', label: '分支', placeholder: 'main' },
    { key: 'proxy_url', label: 'GitHub 代理地址（可选）', placeholder: '私有仓库或限流时填写' },
  ],
};

export class LibrarySettingsPanel {
  constructor(documentRef, options = {}) {
    this.document = documentRef;
    this.root = null;
    this.session = null;
    this.entries = {};
    this.drafts = {};
    this.busy = false;
    this.notice = null;
    this.onClose = options.onClose ?? (() => {});
  }

  async open(container) {
    this.root = container;
    container.replaceChildren(el(this.document, 'p', '正在连接云服务…', 'notice'));
    if (!cloudAvailable()) {
      this.renderUnavailable('本页面不是安全上下文（HTTPS 或 127.0.0.1），云服务不可用。请通过正式分享链接打开。');
      return;
    }
    try {
      const cloud = await getCloud();
      const callback = await this.consumeCallback(cloud);
      if (callback.error) {
        this.renderLogin(callback.error.message);
        return;
      }
      const { session, error } = await currentSession();
      if (error) {
        this.renderLogin(`读取登录状态失败：${error.message ?? error.kind ?? '未知错误'}`);
        return;
      }
      if (!session) {
        this.renderLogin();
        return;
      }
      this.session = session;
      await this.renderSettings();
    } catch (error) {
      this.renderLogin(error?.message ?? '云服务初始化失败。');
    }
  }

  async consumeCallback(cloud) {
    const url = new URL(globalThis.location.href);
    if (!url.searchParams.has('code') && !url.searchParams.has('error')) return { error: null };
    const provider = url.searchParams.get('provider');
    if (provider !== 'workbuddy' && provider !== 'wechat') {
      return { error: { message: '授权回调缺少有效的登录方式，请重新登录。' } };
    }
    const handler = provider === 'workbuddy'
      ? cloud.auth.handleWorkBuddyCallback
      : cloud.auth.handleWechatWebCallback;
    if (typeof handler !== 'function') {
      return { error: { message: '当前云 SDK 版本不支持该登录方式。' } };
    }
    const { data, error } = await handler.call(cloud.auth);
    if (error) return { error };
    if (data) clearCallbackParams();
    return { error: null };
  }

  renderUnavailable(message) {
    const box = el(this.document, 'div', '', 'settings-panel');
    box.append(el(this.document, 'h2', '在线设置不可用'), el(this.document, 'p', message, 'notice'));
    this.root.replaceChildren(box);
  }

  renderLogin(message = '') {
    const box = el(this.document, 'div', '', 'settings-panel');
    box.append(
      el(this.document, 'h2', '登录后配置你的四库连接'),
      el(this.document, 'p', '连接配置按账号保存在云端，换设备登录同一账号即可恢复。Obsidian 目录授权仍然只能在每台机器上单独授予。', 'settings-hint'),
    );
    if (message) box.append(el(this.document, 'p', message, 'settings-error'));

    // 手机号 + 短信验证码（主流方式，不依赖第三方账号）
    const phoneForm = el(this.document, 'div', '', 'login-block');
    phoneForm.append(el(this.document, 'h3', '手机号验证码登录'));
    const phoneLabel = el(this.document, 'label', '手机号', 'settings-label');
    phoneLabel.setAttribute('for', 'login-phone');
    const phone = el(this.document, 'input', '', 'text-input');
    phone.id = 'login-phone';
    phone.type = 'tel';
    phone.inputMode = 'numeric';
    phone.autocomplete = 'tel';
    phone.placeholder = '请输入 11 位手机号';
    phone.maxLength = 11;
    const phoneError = el(this.document, 'p', '', 'settings-error');
    phoneError.hidden = true;
    const sendCode = button(this.document, '获取验证码', 'secondary-button', () => this.sendPhoneCode(phone.value));
    sendCode.id = 'send-phone-code';
    const codeRow = el(this.document, 'div', '', 'settings-actions');
    codeRow.append(phone, sendCode);
    const codeLabel = el(this.document, 'label', '短信验证码', 'settings-label');
    codeLabel.setAttribute('for', 'login-code');
    const code = el(this.document, 'input', '', 'text-input');
    code.id = 'login-code';
    code.inputMode = 'numeric';
    code.autocomplete = 'one-time-code';
    code.placeholder = '6 位验证码';
    const submit = button(this.document, '登录 / 注册', 'primary-button', () => this.submitPhoneLogin(phone.value, code.value));
    submit.id = 'submit-phone-login';
    phone.addEventListener('input', () => { phoneError.hidden = true; phone.classList.remove('input-invalid'); });
    phoneForm.append(phoneLabel, codeRow, phoneError, codeLabel, code, submit);
    box.append(phoneForm);

    // 微信扫码
    const wechat = button(this.document, '微信扫码登录', 'secondary-button', () => this.startLogin('wechat'));
    wechat.id = 'login-wechat';
    box.append(el(this.document, 'h3', '其他方式'), wechat);

    this.notice = el(this.document, 'p', '', 'notice');
    this.notice.id = 'settings-notice';
    this.notice.setAttribute('role', 'status');
    box.append(this.notice);
    this.root.replaceChildren(box);
    this.pendingOtp = null;
  }

  /** 校验失败不发请求；挑战保存在实例上，重试不会重发验证码。 */
  async sendPhoneCode(phoneValue) {
    const phone = String(phoneValue ?? '').trim();
    if (!/^1\d{10}$/u.test(phone)) {
      this.showPhoneError('请输入正确的 11 位手机号');
      return;
    }
    this.setNotice('正在发送验证码…');
    try {
      const cloud = await getCloud();
      const sent = await cloud.auth.sendOtp({ phone });
      if (sent.error) {
        this.setNotice(sent.error.message ?? '验证码发送失败。');
        return;
      }
      this.pendingOtp = { phone, verificationId: sent.data.verificationId, isExistingUser: sent.data.isExistingUser };
      this.setNotice('验证码已发送，请查收短信（5 分钟内有效）。');
    } catch (error) {
      this.setNotice(error?.message ?? '验证码发送失败。');
    }
  }

  async submitPhoneLogin(phoneValue, codeValue) {
    const phone = String(phoneValue ?? '').trim();
    const token = String(codeValue ?? '').trim();
    const pending = this.pendingOtp;
    if (!pending || pending.phone !== phone) {
      this.showPhoneError('请先获取当前手机号的验证码');
      return;
    }
    if (!token) {
      this.setNotice('请输入短信验证码。');
      return;
    }
    this.setNotice('正在登录…');
    try {
      const cloud = await getCloud();
      const completed = await cloud.auth.verifyOtp({
        phone: pending.phone,
        verificationId: pending.verificationId,
        isExistingUser: pending.isExistingUser,
        token,
      });
      if (completed.error) {
        this.setNotice(completed.error.message ?? '验证码不正确或已过期。');
        return;
      }
      this.pendingOtp = null;
      this.session = completed.data;
      await this.renderSettings();
    } catch (error) {
      this.setNotice(error?.message ?? '登录失败。');
    }
  }

  showPhoneError(text) {
    const node = this.root.querySelector('.settings-panel .settings-error');
    const phone = this.root.querySelector('#login-phone');
    if (node) { node.textContent = text; node.hidden = false; }
    phone?.classList.add('input-invalid');
  }

  async startLogin(provider) {
    try {
      this.setNotice('正在跳转授权…');
      const cloud = await getCloud();
      const back = new URL(globalThis.location.href);
      // 站点根目录 + 当前业务参数；回调落在根路径，避免静态站点 404 丢 code。
      back.pathname = '/';
      for (const key of ['code', 'state', 'provider']) back.searchParams.delete(key);
      back.searchParams.set('settings', '1');
      const { data, error } = await cloud.auth.signInWithOAuth({
        provider,
        redirectTo: back.toString(),
      });
      if (error) {
        this.setNotice(error.message ?? '发起登录失败。');
        return;
      }
      globalThis.location.assign(data.url);
    } catch (error) {
      this.setNotice(error?.message ?? '发起登录失败。');
    }
  }

  setNotice(text) {
    if (this.notice) this.notice.textContent = text;
  }

  async renderSettings(message = '') {
    const cloud = await getCloud();
    this.entries = await loadConnections(cloud);
    this.drafts = {};
    for (const [id, entry] of Object.entries(this.entries)) {
      this.drafts[id] = { ...entry.config };
    }
    const box = el(this.document, 'div', '', 'settings-panel');
    const user = this.session?.user ?? {};
    box.append(
      el(this.document, 'h2', '在线设置 · 我的库连接'),
      el(this.document, 'p', `当前账号：${user.name || user.email || user.phone || user.id || '已登录'}　配置保存在云端，仅本账号可见。`, 'settings-hint'),
      this.renderPrivacyNote(),
    );
    if (message) box.append(el(this.document, 'p', message, 'notice'));
    for (const library of LIBRARIES) box.append(this.renderCard(library));
    box.append(this.renderFooter());
    this.root.replaceChildren(box);
    this.notice = box.querySelector('#settings-notice');
  }

  renderPrivacyNote() {
    const wrap = el(this.document, 'div', '', 'settings-warning');
    wrap.append(
      el(this.document, 'strong', '凭据不会存在这里：'),
      this.document.createTextNode('本面板只保存库地址与标识（空间 ID、仓库名、目录、标签）。乐享 token、GitHub PAT 一律留在服务端代理或本机环境变量；填写 token 会被拒绝。'),
    );
    return wrap;
  }

  renderCard(library) {
    const entry = this.entries[library.id];
    const draft = this.drafts[library.id];
    const card = el(this.document, 'section', '', 'settings-card');
    card.dataset.library = library.id;

    const head = el(this.document, 'div', '', 'settings-card-head');
    head.append(
      el(this.document, 'h3', library.name),
      el(this.document, 'span', entry.saved ? (entry.enabled ? '已配置' : '已停用') : '未配置', `settings-badge${entry.saved ? ' is-on' : ''}`),
    );
    card.append(head, el(this.document, 'p', library.hint, 'settings-hint'));

    const status = el(this.document, 'p', this.describeState(library.id, entry), 'settings-status');
    if (library.id === 'm') status.dataset.fallback = 'proxy-required';
    if (library.id === 'p') status.dataset.fallback = 'direct-ok';
    card.append(status);

    const form = el(this.document, 'div', '', 'settings-fields');
    for (const field of FIELD_SCHEMA[library.id]) {
      const id = `lib-${library.id}-${field.key}`;
      const label = el(this.document, 'label', field.label, 'settings-label');
      label.setAttribute('for', id);
      const input = el(this.document, 'input', '', 'text-input');
      input.id = id;
      input.spellcheck = false;
      input.placeholder = field.placeholder;
      input.value = field.list
        ? (Array.isArray(draft[field.key]) ? draft[field.key].join(', ') : '')
        : (draft[field.key] ?? '');
      input.addEventListener('input', () => {
        draft[field.key] = field.list
          ? input.value.split(/[,，]/u).map((part) => part.trim()).filter(Boolean)
          : input.value;
      });
      form.append(label, input);
    }
    const labelId = `lib-${library.id}-label`;
    const labelNode = el(this.document, 'label', '显示名称（可选）', 'settings-label');
    labelNode.setAttribute('for', labelId);
    const labelInput = el(this.document, 'input', '', 'text-input');
    labelInput.id = labelId;
    labelInput.value = entry.label ?? '';
    labelInput.addEventListener('input', () => { draft.__label = labelInput.value; });
    form.append(labelNode, labelInput);
    card.append(form);

    const actions = el(this.document, 'div', '', 'settings-actions');
    actions.append(button(this.document, '保存到云端', 'primary-button', () => this.save(library.id)));
    if (library.id === 'p') {
      actions.append(button(this.document, '测试连接', 'secondary-button', () => this.probe(library.id)));
    }
    if (entry.saved) {
      actions.append(button(this.document, '删除配置', 'danger-button', () => this.remove(library.id)));
    }
    card.append(actions);

    const result = el(this.document, 'p', '', 'settings-status');
    result.dataset.role = 'result';
    card.append(result);
    return card;
  }

  describeState(library, entry) {
    if (!entry.saved) return library === 'm' ? '未配置。乐享必须经服务端代理访问。' : '未配置。';
    if (!entry.enabled) return '已停用，不会参与查询。';
    if (library === 'm') return entry.config.proxy_url ? `已配置代理 · 目标路径：${entry.config.path || '知识库根目录'}；可在审核页通过后点「传送到 M 库」。` : '缺少代理地址，乐享暂不可用。';
    if (library === 't' || library === 'r') return `目录：${entry.config.vault_root || '整个库'}；文件授权需在各机器本地授予。`;
    if (library === 'p') return entry.config.owner && entry.config.repo ? `仓库：${entry.config.owner}/${entry.config.repo}` : '缺少仓库信息。';
    return '';
  }

  cardNode(library) {
    return this.root.querySelector(`[data-library="${library}"]`);
  }

  setCardMessage(library, text, isError = false) {
    const node = this.cardNode(library)?.querySelector('[data-role="result"]');
    if (!node) return;
    node.textContent = text;
    node.classList.toggle('settings-error', isError);
  }

  async save(library) {
    if (this.busy) return;
    this.busy = true;
    this.setCardMessage(library, '保存中…');
    try {
      const cloud = await getCloud();
      const draft = { ...this.drafts[library] };
      const label = draft.__label ?? this.entries[library]?.label ?? '';
      delete draft.__label;
      normalizeConfig(library, draft);
      const saved = await saveConnection(cloud, library, {
        config: draft,
        label,
        enabled: true,
        ownerName: this.session?.user?.name ?? null,
      });
      this.entries[library] = { ...saved, saved: true };
      this.setCardMessage(library, '已保存到云端。');
      await this.refreshBadge(library);
    } catch (error) {
      const message = error instanceof LibraryConfigError ? error.message : (error?.message ?? '保存失败。');
      this.setCardMessage(library, message, true);
    } finally {
      this.busy = false;
    }
  }

  async refreshBadge(library) {
    const card = this.cardNode(library);
    const badge = card?.querySelector('.settings-badge');
    const status = card?.querySelector('.settings-status');
    const entry = this.entries[library];
    if (badge) {
      badge.textContent = entry.enabled ? '已配置' : '已停用';
      badge.classList.toggle('is-on', true);
    }
    if (status) status.textContent = this.describeState(library, entry);
  }

  async probe(library) {
    if (this.busy) return;
    this.busy = true;
    this.setCardMessage(library, '测试中…');
    try {
      const draft = { ...this.drafts[library] };
      delete draft.__label;
      const info = await probeGithub(normalizeConfig(library, draft));
      this.setCardMessage(library, `连接正常：${info.full_name}（默认分支 ${info.default_branch || '未知'}${info.private ? '，私有' : '，公开'}）。`);
    } catch (error) {
      this.setCardMessage(library, error?.message ?? '测试失败。', true);
    } finally {
      this.busy = false;
    }
  }

  async remove(library) {
    if (this.busy) return;
    if (!globalThis.confirm(`确定删除「${describeLibrary(library).name}」的云端配置？Obsidian 本地授权不受影响。`)) return;
    this.busy = true;
    this.setCardMessage(library, '删除中…');
    try {
      const cloud = await getCloud();
      const removed = await deleteConnection(cloud, library);
      if (!removed) {
        this.setCardMessage(library, '没有可删除的配置（可能已被删除或不属于当前账号）。', true);
        return;
      }
      this.entries[library] = { library, label: '', enabled: false, config: emptyConfig(library), updatedAt: null, saved: false };
      this.drafts[library] = emptyConfig(library);
      await this.renderSettings(`已删除「${describeLibrary(library).name}」的云端配置。`);
    } catch (error) {
      this.setCardMessage(library, error?.message ?? '删除失败。', true);
    } finally {
      this.busy = false;
    }
  }

  renderFooter() {
    const wrap = el(this.document, 'div', '', 'settings-footer');
    wrap.append(
      el(this.document, 'p', 'Obsidian 目录读取授权保存在本机浏览器，不会随账号同步；换机器需要重新选择文件夹。', 'settings-hint'),
      button(this.document, '退出登录', 'secondary-button', async () => {
        const cloud = await getCloud();
        await cloud.auth.signOut();
        this.session = null;
        this.renderLogin('已退出登录。');
      }),
      button(this.document, '关闭设置', 'secondary-button', () => this.onClose()),
    );
    const notice = el(this.document, 'p', '', 'notice');
    notice.id = 'settings-notice';
    wrap.append(notice);
    return wrap;
  }
}
