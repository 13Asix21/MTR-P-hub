/**
 * 在线设置面板：登录 + 按用户编辑四库连接。
 * 未登录时只展示说明，不读写任何用户数据。
 */
import { LIBRARIES, cloudAvailable, clearCallbackParams, currentSession, getCloud } from './cloudClient.js?v=20261123o';
import {
  LibraryConfigError,
  deleteConnection,
  describeLibrary,
  emptyConfig,
  loadConnections,
  normalizeConfig,
  saveConnection,
  testConnection,
} from './librarySettings.js?v=20261123o';
import { ADVANCED_FIELDS, FIELD_HELP, buildConfig, defaultsFor, toFormValues } from './libraryFields.js?v=20261123o';

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

export class LibrarySettingsPanel {
  constructor(documentRef, options = {}) {
    this.document = documentRef;
    this.root = null;
    this.session = null;
    this.entries = {};
    this.forms = {};
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
    this.forms = {};
    for (const library of LIBRARIES) {
      const saved = this.entries[library.id];
      this.forms[library.id] = saved.saved
        ? toFormValues(library.id, saved.config)
        : defaultsFor(library.id);
      // 本机凭据不来自云端，从本地读回填。
      if (library.id === 'p') this.forms.p.github_token = this.readLocalField('github_token');
    }
    const box = el(this.document, 'div', '', 'settings-panel');
    const user = this.session?.user ?? {};
    box.append(
      el(this.document, 'h2', '我的库连接'),
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

  renderField(library, field, advanced = false) {
    const id = `lib-${library}-${field.key}`;
    const row = el(this.document, 'div', '', advanced ? 'settings-field is-advanced' : 'settings-field');
    row.dataset.field = field.key;
    if (field.onlyMode) row.dataset.onlyMode = field.onlyMode;
    const head = el(this.document, 'div', '', 'settings-field-head');
    const label = el(this.document, 'label', field.label + (field.required ? ' *' : ''), 'settings-label');
    label.setAttribute('for', id);
    head.append(label);
    if (field.helpLink) {
      const link = el(this.document, 'a', '点此获取', 'help-link');
      link.href = field.helpLink.url;
      link.target = '_blank';
      link.rel = 'noreferrer noopener';
      link.title = field.helpLink.label;
      head.append(link);
    }
    let input;
    if (field.type === 'select') {
      input = el(this.document, 'select', '', 'text-input');
      for (const option of field.options ?? []) {
        const node = el(this.document, 'option', option.label);
        node.value = option.value;
        input.append(node);
      }
      input.value = this.forms[library][field.key] ?? field.defaultValue ?? '';
      input.addEventListener('change', () => {
        this.forms[library][field.key] = input.value;
        this.applyModeVisibility(library);
      });
    } else {
      input = el(this.document, 'input', '', 'text-input');
      input.spellcheck = false;
      input.type = field.type === 'password' ? 'password' : 'text';
      if (field.localOnly) input.autocomplete = 'off';
      input.placeholder = field.placeholder ?? '';
      input.value = this.forms[library][field.key] ?? '';
      input.addEventListener('input', () => {
        this.forms[library][field.key] = input.value;
        // 凭据类字段单独存本机，不进云端配置。
        if (field.localOnly) this.persistLocalField(field.key, input.value);
      });
    }
    input.id = id;
    row.append(head, input);
    if (field.helpText) row.append(el(this.document, 'p', field.helpText, 'field-help'));
    return row;
  }

  /** 本地 / MCP 二选一时，只显示当前模式相关的字段。 */
  applyModeVisibility(library) {
    const card = this.cardNode(library);
    if (!card) return;
    const mode = this.forms[library]?.mode ?? 'local';
    for (const row of card.querySelectorAll('[data-only-mode]')) {
      row.hidden = row.dataset.onlyMode !== mode;
    }
  }

  renderCard(library) {
    const entry = this.entries[library.id];
    const card = el(this.document, 'section', '', 'settings-card');
    card.dataset.library = library.id;

    const head = el(this.document, 'div', '', 'settings-card-head');
    head.append(
      el(this.document, 'h3', library.name),
      el(this.document, 'span', entry.saved ? (entry.enabled ? '已配置' : '已停用') : '未配置', `settings-badge${entry.saved ? ' is-on' : ''}`),
    );
    card.append(head, el(this.document, 'p', library.hint, 'settings-hint'));
    card.append(el(this.document, 'p', this.describeState(library.id, entry), 'settings-status'));

    const fields = el(this.document, 'div', '', 'settings-fields');
    for (const field of FIELD_HELP[library.id] ?? []) fields.append(this.renderField(library.id, field));
    card.append(fields);

    const advancedFields = ADVANCED_FIELDS[library.id] ?? [];
    if (advancedFields.length > 0) {
      const details = el(this.document, 'details', '', 'settings-advanced');
      details.append(el(this.document, 'summary', '高级设置（一般不用改）'));
      for (const field of advancedFields) details.append(this.renderField(library.id, field, true));
      card.append(details);
    }

    const actions = el(this.document, 'div', '', 'settings-actions');
    actions.append(button(this.document, '保存', 'primary-button', () => this.save(library.id)));
    actions.append(button(this.document, '测试连接', 'secondary-button', () => this.test(library.id)));
    if (entry.saved) actions.append(button(this.document, '删除', 'danger-button', () => this.remove(library.id)));
    card.append(actions);

    const result = el(this.document, 'p', '', 'settings-result');
    result.dataset.role = 'result';
    card.append(result);
    queueMicrotask(() => this.applyModeVisibility(library.id));
    return card;
  }

  describeState(library, entry) {
    if (!entry.saved) return library === 'm' || library === 'p' ? '未配置，填好点「测试连接」确认可用。' : '未配置；留空即扫描整个库。';
    if (!entry.enabled) return '已停用，不会参与查询。';
    if (library === 'm') {
      if (!entry.config.proxy_url || !entry.config.sharecode) return '配置不完整，无法传送。';
      return `目标：${entry.config.path || '知识库根目录'}；通过审核后可点「传送到 M 库」。`;
    }
    if (library === 't' || library === 'r') return `扫描范围：${entry.config.vault_root || '整个库'}；文件授权需在本机授予。`;
    if (library === 'p') return entry.config.owner && entry.config.repo ? `仓库：${entry.config.owner}/${entry.config.repo}（${entry.config.branch}）` : '仓库信息不完整。';
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
    node.classList.toggle('settings-ok', !isError && text.length > 0);
  }

  persistLocalField(key, value) {
    if (key !== 'github_token') return;
    try {
      if (value) globalThis.localStorage?.setItem('mtrp.githubToken', value);
      else globalThis.localStorage?.removeItem('mtrp.githubToken');
    } catch { /* 忽略隐私模式 */ }
  }

  readLocalField(key) {
    if (key !== 'github_token') return '';
    try {
      return globalThis.localStorage?.getItem('mtrp.githubToken') ?? '';
    } catch {
      return '';
    }
  }

  async save(library) {
    if (this.busy) return null;
    this.busy = true;
    this.setCardMessage(library, '保存中…');
    try {
      const cloud = await getCloud();
      const config = buildConfig(library, this.forms[library]);
      // 本机凭据字段不参与云端校验与保存。
      delete config.github_token;
      normalizeConfig(library, config);
      const saved = await saveConnection(cloud, library, {
        config,
        label: this.entries[library]?.label ?? '',
        enabled: true,
        ownerName: this.session?.user?.name ?? null,
      });
      this.entries[library] = { ...saved, saved: true };
      this.setCardMessage(library, '已保存到云端。');
      this.refreshBadge(library);
      return saved;
    } catch (error) {
      const message = error instanceof LibraryConfigError ? error.message : (error?.message ?? '保存失败。');
      this.setCardMessage(library, message, true);
      return null;
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

  /** 先保存再测，避免测的是没存进去的旧配置。 */
  async test(library) {
    if (this.busy) return;
    const saved = await this.save(library);
    if (!saved) return;
    this.busy = true;
    this.setCardMessage(library, '测试中…');
    try {
      const result = await testConnection(library, saved.config);
      this.setCardMessage(library, result.message);
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
      this.forms[library] = defaultsFor(library);
      await this.renderSettings(`已删除「${describeLibrary(library).name}」的云端配置。`);
    } catch (error) {
      this.setCardMessage(library, error?.message ?? '删除失败。', true);
    } finally {
      this.busy = false;
    }
  }

  /** 按固定步骤跑一遍联通性，每一步都给出真实回执或明确失败原因。 */
  async runSelfTest() {
    const host = this.root.querySelector('#connectivity-report');
    const btn = this.root.querySelector('#run-connectivity-test');
    if (!host || this.selfTesting) return;
    this.selfTesting = true;
    if (btn) btn.disabled = true;
    host.hidden = false;
    host.replaceChildren();
    const { WORKFLOW_STEPS, STEP_STATE, checkM, checkVault, checkGithub, summarize } = await import('./connectivityWorkflow.js?v=20261123o');
    const results = [];
    const render = () => {
      host.replaceChildren();
      for (const item of results) {
        const line = el(this.document, 'div', '', `wf-line is-${item.state}`);
        const mark = { ok: '✓', failed: '✗', skipped: '–', running: '…', pending: '·' }[item.state] ?? '·';
        line.append(el(this.document, 'span', mark, 'wf-mark'), el(this.document, 'span', `${item.label}：${item.message}`, 'wf-text'));
        host.append(line);
      }
      const sum = summarize(results);
      host.append(el(this.document, 'p', sum.allPassed ? `全部通过（${sum.ok} 项）` : `通过 ${sum.ok} · 失败 ${sum.failed} · 跳过 ${sum.skipped}`, sum.failed ? 'settings-error' : 'settings-ok'));
    };
    for (const step of WORKFLOW_STEPS) {
      const entry = { ...step, state: STEP_STATE.running, message: '检查中…' };
      results.push(entry);
      render();
      try {
        if (step.id === 'm') {
          entry.message = checkM(this.entries.m?.config).message;
          const { testConnection } = await import('./librarySettings.js?v=20261123o');
          const r = await testConnection('m', this.entries.m?.config ?? {});
          entry.state = STEP_STATE.ok;
          entry.message = r.message;
        } else if (step.id === 't' || step.id === 'r') {
          const cfg = this.entries[step.id]?.config;
          const chk = checkVault(cfg);
          entry.state = chk.state;
          entry.message = chk.message;
        } else if (step.id === 'p') {
          const cfg = this.entries.p?.config;
          const chk = checkGithub(cfg);
          if (chk.state === STEP_STATE.ok) {
            const { checkGithubAccess } = await import('./librarySettings.js?v=20261123o');
            const r = await checkGithubAccess(cfg);
            entry.message = r.message;
            if (!r.writable) entry.state = STEP_STATE.ok;
          } else {
            entry.message = chk.message;
          }
          entry.state = chk.state;
        } else {
          const { testAgent } = await import('./librarySettings.js?v=20261123o');
          const r = await testAgent(this.entries.m?.config ?? {});
          entry.state = STEP_STATE.ok;
          entry.message = r.message;
        }
      } catch (error) {
        entry.state = STEP_STATE.failed;
        entry.message = error?.message ?? '检查失败。';
      }
      render();
    }
    this.selfTesting = false;
    if (btn) btn.disabled = false;
  }

  renderFooter() {
    const wrap = el(this.document, 'div', '', 'settings-footer');
    const selfTest = button(this.document, '一键联通自检', 'primary-button', () => this.runSelfTest());
    selfTest.id = 'run-connectivity-test';
    const report = el(this.document, 'div', '', 'workflow-report');
    report.id = 'connectivity-report';
    report.hidden = true;
    wrap.append(selfTest, report);
    wrap.append(
      el(this.document, 'p', 'Obsidian 目录读取授权保存在本机浏览器，不会随账号同步；换机器需要重新选择文件夹。', 'settings-hint'),
      button(this.document, '退出登录', 'secondary-button', async () => {
        const cloud = await getCloud();
        await cloud.auth.signOut();
        this.session = null;
        this.renderLogin('已退出登录。');
      }),
      button(this.document, '关闭', 'secondary-button', () => this.onClose()),
    );
    const notice = el(this.document, 'p', '', 'notice');
    notice.id = 'settings-notice';
    wrap.append(notice);
    return wrap;
  }
}
