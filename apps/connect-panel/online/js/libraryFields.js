/**
 * 四库字段定义：标题、必填、默认值、获取指引。
 * 设计原则——默认值已经能连上，用户只在需要时改。
 */

export const FIELD_HELP = {
  m: [
    {
      key: 'space_id',
      label: '乐享知识库',
      placeholder: '选择或粘贴知识库 ID',
      required: true,
      // 默认值 = 已验证可用的空间，先能跑通再改
      defaultValue: 'bda1340e5541477d9698d82dc9a4b451',
      helpText: '已默认填好你的「M库范式」知识库；换库时在乐享打开目标库，地址栏 /space/<这串 ID> 就是它。',
      helpLink: { label: '打开乐享知识库', url: 'https://m.lexiang-app.com/' },
    },
    {
      key: 'path',
      label: '放进哪个文件夹',
      placeholder: '留空 = 知识库根目录',
      required: false,
      defaultValue: '',
      helpText: '填相对路径即可，代理会自动逐级建文件夹。',
      helpLink: null,
    },
  ],
  t: [
    {
      key: 'mode',
      label: '接入方式',
      type: 'select',
      options: [
        { value: 'local', label: '本地文件夹（浏览器直接读取）' },
        { value: 'mcp', label: 'MCP 服务（远端库）' },
      ],
      required: true,
      defaultValue: 'local',
      helpText: '本地模式在本机授权 Obsidian 库；MCP 模式读取部署好的远端服务。',
      helpLink: null,
    },
    {
      key: 'vault_root',
      label: '只扫描这个文件夹',
      placeholder: '留空 = 整个库',
      required: false,
      defaultValue: '',
      helpText: '本地模式填库内相对路径；留空就是整库。',
      helpLink: null,
      onlyMode: 'local',
    },
    {
      key: 'mcp_endpoint',
      label: 'MCP 端点地址',
      placeholder: 'https://你的-obsidian-mcp/mcp',
      required: false,
      defaultValue: '',
      helpText: 'MCP 模式必填，填写提供 Obsidian 读取能力的 MCP 服务地址。',
      helpLink: null,
      onlyMode: 'mcp',
    },
  ],
  r: [
    {
      key: 'mode',
      label: '接入方式',
      type: 'select',
      options: [
        { value: 'local', label: '本地文件夹（浏览器直接读取）' },
        { value: 'mcp', label: 'MCP 服务（远端库）' },
      ],
      required: true,
      defaultValue: 'local',
      helpText: '通常与 T 库相同；R 库只按标签区分口径。',
      helpLink: null,
    },
    {
      key: 'vault_root',
      label: '只扫描这个文件夹',
      placeholder: '留空 = 整个库',
      required: false,
      defaultValue: '',
      helpText: '本地模式填库内相对路径；留空就是整库。',
      helpLink: null,
      onlyMode: 'local',
    },
    {
      key: 'mcp_endpoint',
      label: 'MCP 端点地址',
      placeholder: 'https://你的-obsidian-mcp/mcp',
      required: false,
      defaultValue: '',
      helpText: 'MCP 模式必填，填写提供 Obsidian 读取能力的 MCP 服务地址。',
      helpLink: null,
      onlyMode: 'mcp',
    },
  ],
  p: [
    {
      key: 'repo',
      label: 'GitHub 仓库',
      placeholder: 'owner/repo，例如 13Asix21/MTR-P-hub',
      required: true,
      defaultValue: '13Asix21/MTR-P-hub',
      helpText: '直接复制浏览器地址栏 github.com/ 后面那两段。',
      helpLink: { label: '打开 GitHub 仓库列表', url: 'https://github.com/13Asix21?tab=repositories' },
    },
    {
      key: 'branch',
      label: '分支',
      placeholder: 'main',
      required: false,
      defaultValue: 'main',
      helpText: '一般就是 main，不用改。',
      helpLink: null,
    },
  ],
};

/** M 库的代理是部署好的固定服务，用户一般不需要改，所以放在高级里。 */
export const ADVANCED_FIELDS = {
  m: [
    {
      key: 'proxy_url',
      label: '乐享代理地址',
      placeholder: 'https://mtrp-lexiang-proxy.app.workbuddy.host',
      defaultValue: 'https://mtrp-lexiang-proxy.app.workbuddy.host',
      helpText: '已部署好的独立代理，保持默认即可。',
      helpLink: { label: '检查代理状态', url: 'https://mtrp-lexiang-proxy.app.workbuddy.host/mtrp/health?sharecode=QOPTB45RQL' },
    },
    {
      key: 'sharecode',
      label: '代理访问码',
      placeholder: 'QOPTB45RQL',
      defaultValue: 'QOPTB45RQL',
      helpText: '代理分享链接里 sharecode= 后面那串，保持默认即可。',
      helpLink: { label: '打开代理链接', url: 'https://mtrp-lexiang-proxy.app.workbuddy.host/?sharecode=QOPTB45RQL' },
    },
  ],
  p: [
    {
      key: 'github_token',
      label: 'GitHub Token（写入需要）',
      type: 'password',
      placeholder: 'ghp_... 或 github_pat_...',
      defaultValue: '',
      helpText: '只保存在本机浏览器，不写入云端。用于读取私有仓库或提交内容；只读用途可留空。',
      helpLink: { label: '创建 Token', url: 'https://github.com/settings/tokens/new?scopes=repo&description=MTR-P' },
      localOnly: true,
    },
    {
      key: 'proxy_url',
      label: 'GitHub 代理地址（可选）',
      placeholder: '私有仓库或限流时填写',
      defaultValue: '',
      helpText: '公开仓库留空即可，浏览器可直连。',
      helpLink: null,
    },
  ],
  t: [],
  r: [],
};

/** 组装成保存时需要的扁平配置。 */
export function buildConfig(library, values) {
  if (library === 'm') {
    return {
      space_id: (values.space_id ?? '').trim(),
      path: (values.path ?? '').trim(),
      proxy_url: (values.proxy_url ?? '').trim(),
      sharecode: (values.sharecode ?? '').trim(),
    };
  }
  if (library === 't' || library === 'r') {
    return {
      mode: (values.mode ?? 'local').trim() || 'local',
      vault_root: (values.vault_root ?? '').trim(),
      mcp_endpoint: (values.mcp_endpoint ?? '').trim(),
      managed_roots: [],
      tags: [],
    };
  }
  // P 库：github_token 是本机凭据，不进云端配置（由 localStorage 单独保管）。
  // 把 owner/repo 拆开保存，界面上是一个输入框。
  const repo = (values.repo ?? '').trim().replace(/^https?:\/\/github\.com\//u, '').replace(/\.git$/u, '');
  const [owner, name] = repo.split('/');
  return {
    owner: (owner ?? '').trim(),
    repo: (name ?? '').trim(),
    branch: (values.branch ?? '').trim() || 'main',
    proxy_url: (values.proxy_url ?? '').trim(),
  };
}

/** 把已保存配置转成界面显示值（P 库的 owner/repo 合并为一格）。 */
export function toFormValues(library, config = {}) {
  if (library === 'm') {
    return {
      space_id: config.space_id ?? '',
      path: config.path ?? '',
      proxy_url: config.proxy_url ?? '',
      sharecode: config.sharecode ?? '',
    };
  }
  if (library === 't' || library === 'r') return { mode: config.mode ?? 'local', vault_root: config.vault_root ?? '', mcp_endpoint: config.mcp_endpoint ?? '' };
  const owner = config.owner ?? '';
  const repo = config.repo ?? '';
  return {
    repo: owner && repo ? `${owner}/${repo}` : '',
    branch: config.branch ?? 'main',
    proxy_url: config.proxy_url ?? '',
  };
}

/** 首次打开时用默认值预填，减少用户手填。 */
export function defaultsFor(library) {
  const values = {};
  for (const field of FIELD_HELP[library] ?? []) {
    if (field.defaultValue) values[field.key] = field.defaultValue;
  }
  for (const field of ADVANCED_FIELDS[library] ?? []) {
    if (field.defaultValue) values[field.key] = field.defaultValue;
  }
  return values;
}
