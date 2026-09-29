/* ============================================================================
   知识库阅读器 —— 应用外壳

   视图：首页 / 浏览（三栏工作区）/ 搜索 / 收藏 / 任务 / 设置

   全部请求走 API.get/post（它自动处理平台前缀与鉴权头，见 app.js 的 detectBase）。
   路径参数一律 encodeURIComponent —— parse_qs 会把 + 当成空格，
   文件名里出现 +、#、& 时不编码就会找错文件。

   所有用户可见的动态文本都走 U.esc 或 textContent，绝不裸拼 innerHTML；
   唯一的例外是服务端**已转义**的渲染结果（api/file/html、片段里的 <mark>）。
   ============================================================================ */

const State = {
  kbs: [],
  currentKb: null,
  allowedRoots: [],
  settings: {},
  tabs: [],            // 浏览视图打开的文件（每个是一个「标签」）
  activeTab: -1,
  favorites: new Set(),
  viewerHandle: null,
};

const THEME_BY_ID = { system: 'system', light: 'light', dark: 'dark' };

function applyTheme(theme) {
  const value = THEME_BY_ID[theme] || 'system';
  if (value === 'system') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', value);
  }
}

function contentUrl(meta, extra) {
  const params = ['path=' + encodeURIComponent(meta.path)];
  if (meta.mtime_ns) params.push('v=' + meta.mtime_ns);
  if (extra) params.push(extra);
  return API.url('api/file/content?' + params.join('&'));
}

function fileIcon(meta) {
  if (meta && meta.icon && meta.icon.value) {
    if (meta.icon.type === 'upload') return { upload: meta.icon.value };
    return { name: meta.icon.value };
  }
  return { name: 'file' };
}

function iconHtml(meta, size) {
  const icon = fileIcon(meta);
  if (icon.upload) {
    return '<img src="' + API.url('api/icon/file/' + encodeURIComponent(icon.upload))
      + '" alt="" style="width:' + (size || 15) + 'px;height:' + (size || 15)
      + 'px;border-radius:3px;object-fit:cover">';
  }
  return Icons.svg(icon.name, { size: size || 15 });
}

/* ---------------------------------------------------------------- 首页 */

async function renderHome(main) {
  main.appendChild(UI.empty('refresh', T('正在读取知识库…'), ''));

  let data;
  try {
    data = await API.get('api/home');
  } catch (error) {
    main.innerHTML = '';
    main.appendChild(UI.empty('alert', T('无法读取知识库'), U.esc(error.message || String(error))));
    return;
  }
  State.kbs = data.knowledge_bases || [];
  State.allowedRoots = data.allowed_roots || [];
  State.favorites = new Set((data.favorites || []).map((item) => item.path));

  main.innerHTML = '';

  const greeting = U.el('div', { style: 'margin-bottom:18px' }, [
    U.el('h1', { style: 'margin:0 0 4px;font-size:22px', text: greetingText() }),
    U.el('div', { style: 'color:var(--text-soft);font-size:13.5px',
                  text: T('继续探索你的知识') }),
  ]);
  main.appendChild(greeting);

  // --- 我的知识库 ---
  const head = U.el('div', { style: 'display:flex;align-items:center;gap:10px;margin:0 0 10px' }, [
    U.el('h2', { style: 'margin:0;font-size:15px', text: T('我的知识库') }),
    U.el('span', { class: 'spacer', style: 'flex:1' }),
  ]);
  const addBtn = U.el('button', { class: 'btn', text: T('＋ 新建知识库') });
  addBtn.addEventListener('click', newKnowledgeBase);
  head.appendChild(addBtn);
  main.appendChild(head);

  if (!State.kbs.length) {
    const empty = UI.empty('book', T('还没有知识库'),
      T('知识库把一个已有的文件夹变成只读、可搜索的阅读空间。')
      + T('<br>原文件不会被修改、移动或删除。'),
      addBtn.cloneNode(true));
    empty.querySelector('button').addEventListener('click', newKnowledgeBase);
    main.appendChild(empty);
  } else {
    const grid = U.el('div', { class: 'kb-cards' });
    State.kbs.forEach((kb) => {
      const card = U.el('div', { class: 'kb-card' });
      const iconHtmlValue = kb.icon_type === 'upload'
        ? '<img src="' + API.url('api/icon/file/' + encodeURIComponent(kb.icon_value)) + '" alt="">'
        : Icons.svg(kb.icon_value || 'book', { size: 22 });
      card.innerHTML =
        '<div class="ico">' + iconHtmlValue + '</div>'
        + '<div class="kb-name"></div>'
        + '<div class="kb-path"></div>'
        + '<div class="kb-stats"></div>';
      card.querySelector('.kb-name').textContent = kb.name;
      card.querySelector('.kb-path').textContent = kb.root_path;
      card.querySelector('.kb-path').title = kb.root_path;
      card.querySelector('.kb-stats').appendChild(
        U.el('span', { text: U.num(kb.live_file_count || 0) + T(' 个文件') }));
      card.querySelector('.kb-stats').appendChild(
        U.el('span', { text: U.size(kb.total_bytes || 0) }));
      if (!kb.root_exists) {
        card.appendChild(U.el('span', { class: 'kb-flag', text: T('目录不可访问') }));
      }
      if (kb.scan_state === 'scanning') {
        card.appendChild(U.el('span', { class: 'kb-flag', text: T('正在扫描') }));
      }
      card.addEventListener('click', () => {
        State.currentKb = kb;
        Shell.show('browse');
      });
      grid.appendChild(card);
    });
    main.appendChild(grid);
  }

  // --- 最近阅读 ---
  const recents = U.el('div', { class: 'card', style: 'margin-top:20px' }, [
    U.el('h2', { html: Icons.svg('clock') + T(' 最近阅读') }),
  ]);
  if (!data.recents.length) {
    recents.appendChild(U.el('div', { class: 'card-hint', text: T('还没有打开过任何文件。') }));
  } else {
    recents.appendChild(buildFileList(data.recents, (item) => openInBrowser(item.path)));
  }
  main.appendChild(recents);

  // --- 收藏 ---
  if (data.favorites.length) {
    const favs = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
      U.el('h2', { html: Icons.svg('tag') + T(' 收藏') }),
    ]);
    favs.appendChild(buildFileList(data.favorites, (item) => openInBrowser(item.path)));
    main.appendChild(favs);
  }

  // --- 最近更新 ---
  if (data.recently_updated.length) {
    const newer = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
      U.el('h2', { html: Icons.svg('refresh') + T(' 最近更新') }),
    ]);
    newer.appendChild(buildFileList(data.recently_updated, (item) => openInBrowser(item.path), true));
    main.appendChild(newer);
  }

  if (data.counts && data.counts.pending) {
    main.appendChild(U.el('div', { class: 'info-note', style: 'margin-top:16px',
      text: T('还有 ') + U.num(data.counts.pending) + T(' 个文件等待建立索引。')
        + T('索引完成后它们才能被全文检索命中。') }));
  }
  if (!State.allowedRoots.length) {
    main.appendChild(U.el('div', { class: 'info-note warn', style: 'margin-top:16px',
      text: T('还没有配置可访问目录。新建知识库时会自动把所选目录加入白名单；')
        + T('也可以到「设置」里手动管理。') }));
  }
}

function greetingText() {
  const hour = new Date().getHours();
  if (hour < 6) return T('夜深了');
  if (hour < 12) return T('早上好');
  if (hour < 18) return T('下午好');
  return T('晚上好');
}

function buildFileList(items, onPick, showKind) {
  const ul = U.el('ul', { class: 'rec-list' });
  items.forEach((item) => {
    const li = U.el('li', {});
    li.innerHTML = iconHtml(item, 15);
    li.appendChild(U.el('span', { class: 'n', text: item.name }));
    li.appendChild(U.el('span', {
      class: 'r',
      text: (item.missing ? T('已不在索引中 · ') : '')
        + (showKind ? (item.rel_path || '') : U.time(item.mtime_ns / 1e9)),
    }));
    if (item.missing) li.classList.add('missing');
    li.addEventListener('click', () => onPick(item));
    ul.appendChild(li);
  });
  return ul;
}

/* ---------------------------------------------------------------- 浏览（三栏） */

async function renderBrowse(main) {
  document.body.classList.add('kb-fullbleed');
  main.innerHTML = '';

  if (!State.kbs.length) {
    try {
      const data = await API.get('api/kb/list');
      State.kbs = data.knowledge_bases || [];
    } catch (error) { /* 下面统一给空状态 */ }
  }
  if (!State.currentKb && State.kbs.length) State.currentKb = State.kbs[0];
  if (!State.currentKb) {
    document.body.classList.remove('kb-fullbleed');
    main.appendChild(UI.empty('book', T('还没有知识库'), T('先到首页新建一个知识库。')));
    return;
  }

  const kb = State.currentKb;
  const shell = U.el('div', { class: 'kb-shell' });
  const tree = U.el('div', { class: 'kb-pane kb-tree' });
  const splitter1 = U.el('div', { class: 'kb-splitter' });
  const viewer = U.el('div', { class: 'kb-pane kb-viewer' });
  const splitter2 = U.el('div', { class: 'kb-splitter' });
  const info = U.el('div', { class: 'kb-pane kb-info' });
  shell.appendChild(tree);
  shell.appendChild(splitter1);
  shell.appendChild(viewer);
  shell.appendChild(splitter2);
  shell.appendChild(info);
  main.appendChild(shell);

  // 记忆栏宽（localStorage 只放 UI 偏好，不放任何业务数据）
  try {
    const saved = JSON.parse(localStorage.getItem('kb.panes') || '{}');
    if (saved.tree) shell.style.setProperty('--tree-w', saved.tree + 'px');
    if (saved.info) shell.style.setProperty('--info-w', saved.info + 'px');
  } catch (error) { /* 隐私模式下 localStorage 会抛，忽略即可 */ }

  function savePanes() {
    try {
      const style = getComputedStyle(shell);
      localStorage.setItem('kb.panes', JSON.stringify({
        tree: parseInt(style.getPropertyValue('--tree-w'), 10) || 288,
        info: parseInt(style.getPropertyValue('--info-w'), 10) || 320,
      }));
    } catch (error) { /* 忽略 */ }
  }

  makeSplitter(splitter1, shell, '--tree-w', 180, 560, savePanes);
  makeSplitter(splitter2, shell, '--info-w', 200, 620, savePanes);

  // ---- 查看器头部 ----
  const vhead = U.el('div', { class: 'kb-viewer-head' });
  const vbody = U.el('div', { class: 'kb-viewer-body' });
  viewer.appendChild(vhead);
  viewer.appendChild(vbody);

  const vname = U.el('span', { class: 'name', text: T('未选择文件') });
  const vbadge = U.el('span', { class: 'badge', text: '' });
  const vspacer = U.el('span', { class: 'spacer' });
  const vactions = U.el('span', { style: 'display:flex;gap:2px;align-items:center' });
  vhead.appendChild(vname);
  vhead.appendChild(vbadge);
  vhead.appendChild(vspacer);
  vhead.appendChild(vactions);

  // ---- 信息栏 ----
  const infoBody = U.el('div');
  info.appendChild(infoBody);

  function setInfo(meta) {
    infoBody.innerHTML = '';
    if (!meta) {
      infoBody.appendChild(U.el('div', { class: 'info-block' }, [
        U.el('div', { class: 'info-note', text: T('选中文件后这里会显示它的信息、')
          + T('相关文件与可定位的大纲。') }),
      ]));
      return;
    }
    const block = U.el('div', { class: 'info-block' }, [U.el('h3', { text: T('文件信息') })]);
    const dl = U.el('dl', { class: 'info-kv' });
    const rows = [
      [T('名称'), meta.name],
      [T('类型'), kindLabel(meta.kind) + (meta.ext ? '（' + meta.ext + '）' : '')],
      [T('大小'), meta.is_dir ? T('目录') : U.size(meta.size)],
      [T('修改时间'), U.time(meta.mtime_ns / 1e9)],
      [T('知识库'), meta.kb_name],
      [T('相对路径'), meta.rel_path || '/'],
    ];
    if (meta.image_w && meta.image_h) rows.push([T('尺寸'), meta.image_w + ' × ' + meta.image_h]);
    if (meta.encoding) rows.push([T('文本编码'), meta.encoding]);
    if (meta.text_chars) rows.push([T('已索引字符'), U.num(meta.text_chars)]);
    rows.forEach((pair) => {
      dl.appendChild(U.el('dt', { text: pair[0] }));
      dl.appendChild(U.el('dd', { text: String(pair[1] == null ? '—' : pair[1]) }));
    });
    block.appendChild(dl);
    infoBody.appendChild(block);

    if (meta.note) {
      infoBody.appendChild(U.el('div', { class: 'info-block' }, [
        U.el('h3', { text: T('索引状态') }),
        U.el('div', { class: 'info-note' + (meta.text_state === 'failed' ? ' warn' : ''),
                      text: meta.note }),
      ]));
    }

    const outlineBlock = U.el('div', { class: 'info-block', style: 'display:none' }, [
      U.el('h3', { text: T('大纲') }),
    ]);
    const outlineList = U.el('ul', { class: 'info-list' });
    outlineBlock.appendChild(outlineList);
    infoBody.appendChild(outlineBlock);

    const relatedBlock = U.el('div', { class: 'info-block' }, [
      U.el('h3', { text: T('相关文件') }),
      U.el('div', { class: 'info-note', text: T('正在查找…') }),
    ]);
    infoBody.appendChild(relatedBlock);

    const actions = U.el('div', { class: 'info-block' }, [U.el('h3', { text: T('操作') })]);
    const row = U.el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px' });
    const favBtn = U.el('button', { class: 'icon-btn',
      html: Icons.svg('tag') + ' ' + (meta.favorited ? T('取消收藏') : T('收藏')) });
    favBtn.addEventListener('click', async () => {
      const result = await API.post('api/fav/toggle', { path: meta.path });
      if (result.favorited) State.favorites.add(meta.path);
      else State.favorites.delete(meta.path);
      favBtn.innerHTML = Icons.svg('tag') + ' ' + (result.favorited ? T('取消收藏') : T('收藏'));
      UI.ok(result.favorited ? T('已加入收藏') : T('已取消收藏'));
    });
    row.appendChild(favBtn);
    row.appendChild(U.el('a', { class: 'icon-btn', href: contentUrl(meta, 'download=1'),
      style: 'text-decoration:none', html: Icons.svg('download') + T(' 下载') }));
    const iconBtn = U.el('button', { class: 'icon-btn', html: Icons.svg('image') + T(' 更改图标') });
    iconBtn.addEventListener('click', () => chooseIcon(meta, () => reloadTree()));
    row.appendChild(iconBtn);

    // 图标上传（只接受 SVG/图片，服务端按内容嗅探并清洗）
    const uploadLabel = U.el('label', { class: 'icon-btn', style: 'cursor:pointer',
      html: Icons.svg('upload') + T(' 上传图标') });
    const fileInput = U.el('input', { type: 'file', accept: 'image/*,.svg', style: 'display:none' });
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      const buffer = await file.arrayBuffer();
      let binary = '';
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
      try {
        const result = await API.post('api/icon/upload', {
          target_path: meta.path, data_base64: btoa(binary),
        });
        if (result.ok) { UI.ok(T('图标已更新')); reloadTree(); setInfo(meta); }
        else UI.err(result.error || T('图标无法保存'));
      } catch (error) {
        UI.err(error.message || String(error));
      }
      fileInput.value = '';
    });
    uploadLabel.appendChild(fileInput);
    row.appendChild(uploadLabel);
    actions.appendChild(row);
    infoBody.appendChild(actions);

    const infoApi = {
      block(title, node) {
        infoBody.appendChild(U.el('div', { class: 'info-block' }, [
          U.el('h3', { text: title }), node,
        ]));
      },
      outline(items) {
        if (!items || !items.length) return;
        outlineBlock.style.display = '';
        outlineList.innerHTML = '';
        items.forEach((item) => {
          const li = U.el('li', {}, [
            U.el('span', { html: Icons.svg('hash', { size: 14 }) }),
            U.el('span', { class: 'n', text: item.label }),
          ]);
          li.addEventListener('click', () => {
            if (item.onclick) { item.onclick(); return; }
            if (item.anchor) {
              const node = document.getElementById(item.anchor);
              if (node) node.scrollIntoView({ block: 'start', behavior: 'smooth' });
            } else if (item.page) {
              const handle = State.viewerHandle;
              if (handle && handle.gotoPage) handle.gotoPage(item.page);
            }
          });
          outlineList.appendChild(li);
        });
      },
    };

    // 相关文件（异步，不阻塞查看器）
    API.get('api/file/related?path=' + encodeURIComponent(meta.path) + '&limit=10')
      .then((data) => {
        const host = relatedBlock.querySelector('.info-note');
        if (!host) return;
        host.remove();
        if (!data.related || !data.related.length) {
          relatedBlock.appendChild(U.el('div', { class: 'info-note',
            text: T('没有找到明显相关的文件。') }));
          return;
        }
        const ul = U.el('ul', { class: 'info-list' });
        data.related.forEach((item) => {
          const li = U.el('li', {});
          li.innerHTML = iconHtml(item, 14);
          li.appendChild(U.el('span', { class: 'n', text: item.name }));
          li.appendChild(U.el('span', { class: 'r', text: item.reason }));
          li.addEventListener('click', () => openInBrowser(item.path));
          ul.appendChild(li);
        });
        relatedBlock.appendChild(ul);
        relatedBlock.appendChild(U.el('div', {
          class: 'info-note', style: 'margin-top:8px',
          text: T('相关文件基于所在目录与文件名计算，不是 AI 推荐。'),
        }));
      })
      .catch(() => { /* 相关文件失败不影响主流程 */ });

    return infoApi;
  }

  setInfo(null);

  // ---- 打开一个文件 ----
  async function openFile(path, options) {
    let meta;
    try {
      meta = await API.get('api/file/meta?path=' + encodeURIComponent(path)
        + (State.currentKb ? '&kb=' + State.currentKb.id : ''));
    } catch (error) {
      UI.err(error.message || String(error));
      return;
    }
    State.activeTab = State.tabs.findIndex((tab) => tab.path === meta.path);
    if (State.activeTab < 0) {
      State.tabs.push({ path: meta.path, name: meta.name });
      State.activeTab = State.tabs.length - 1;
    }
    vname.textContent = meta.name;
    vbadge.textContent = kindLabel(meta.kind);
    vbadge.className = 'badge';
    vactions.innerHTML = '';
    document.body.classList.remove('reader-wide', 'reader-full');

    const infoApi = setInfo(meta);
    const controls = [];
    function ctxControls(list) {
      list.forEach((item) => {
        const button = U.el('button', { class: 'icon-btn', html: Icons.svg(item.icon),
                                        title: item.title });
        button.addEventListener('click', item.onclick);
        vactions.appendChild(button);
      });
    }
    // 上一个文件遗留的控件清掉
    State.viewerHandle = null;

    State.viewerHandle = await Viewers.show(vbody, meta, {
      toast: (message, kind) => UI.toast(message, kind),
      controls: ctxControls,
      info: (key, value) => infoApi.block(key, U.el('div', { class: 'info-note', text: value })),
      note: (text, kind) => {
        const node = U.el('div', { class: 'info-note' + (kind ? ' ' + kind : ''), text: text });
        node.style.margin = '10px 14px';
        vbody.insertBefore(node, vbody.firstChild);
        if (meta.text_state === 'unsupported') {
          vbadge.className = 'badge warn';
          vbadge.textContent = T('无文本层');
        }
      },
      outline: (items) => infoApi.outline(items),
      open: (targetPath) => openFile(targetPath),
    });

    // 记录「最近阅读」（失败不影响阅读）
    API.post('api/file/touch', { path: meta.path }).catch(() => {});
    markTreeActive();
    if (options && options.focus) shell.classList.add('focus');
  }

  function markTreeActive() {
    const active = State.tabs[State.activeTab];
    tree.querySelectorAll('.tree-row').forEach((row) => {
      row.classList.toggle('active', !!active && row.dataset.path === active.path);
    });
  }

  State.openFile = openFile;

  // ---- 文件树 ----
  const treeHead = U.el('div', { class: 'kb-tree-head' });
  const kbSelect = U.el('select');
  State.kbs.forEach((item) => {
    kbSelect.appendChild(U.el('option', { value: String(item.id), text: item.name }));
  });
  kbSelect.value = String(kb.id);
  kbSelect.addEventListener('change', () => {
    State.currentKb = State.kbs.find((item) => String(item.id) === kbSelect.value);
    State.tabs = [];
    State.activeTab = -1;
    Shell.show('browse');
  });
  treeHead.appendChild(kbSelect);
  treeHead.appendChild(U.el('span', { class: 'spacer' }));

  const scanBtn = U.el('button', { class: 'icon-btn', html: Icons.svg('scan'), title: T('重新扫描') });
  scanBtn.addEventListener('click', async () => {
    try {
      await Jobs.submit('scan', { kb: State.currentKb.id },
                        T('扫描「') + State.currentKb.name + '」');
      UI.ok(T('已开始扫描，可在「任务」里查看进度'));
      Shell.show('jobs');
    } catch (error) {
      UI.err(error.message || String(error));
    }
  });
  const filterBtn = U.el('button', { class: 'icon-btn', html: Icons.svg('panel'),
                                     title: T('隐藏 / 显示信息栏') });
  filterBtn.addEventListener('click', () => {
    shell.classList.toggle('no-info');
    splitter2.style.display = shell.classList.contains('no-info') ? 'none' : '';
    savePanes();
  });
  const focusBtn = U.el('button', { class: 'icon-btn', html: Icons.svg('eye'),
                                    title: T('专注阅读（隐藏两侧）') });
  focusBtn.addEventListener('click', () => {
    shell.classList.toggle('focus');
    focusBtn.classList.toggle('on', shell.classList.contains('focus'));
  });
  treeHead.appendChild(scanBtn);
  treeHead.appendChild(filterBtn);
  treeHead.appendChild(focusBtn);

  const treeBody = U.el('div', { class: 'kb-tree-body' });
  tree.appendChild(treeHead);
  tree.appendChild(treeBody);

  const expanded = new Set(['']);

  async function loadLevel(parentRel, container, depth) {
    container.innerHTML = '';
    container.appendChild(U.el('div', { class: 'tree-empty', text: T('正在读取…') }));
    let data;
    try {
      data = await API.get('api/kb/' + kb.id + '/tree?parent=' + encodeURIComponent(parentRel));
    } catch (error) {
      container.innerHTML = '';
      container.appendChild(U.el('div', { class: 'tree-empty',
        text: T('无法读取：') + (error.message || error) }));
      return;
    }
    container.innerHTML = '';
    if (!data.entries.length) {
      container.appendChild(U.el('div', { class: 'tree-empty', text: T('（空）') }));
      return;
    }
    if (data.total > data.entries.length) {
      container.appendChild(U.el('div', { class: 'tree-empty',
        text: T('只显示前 ') + data.entries.length + T(' 项（共 ') + U.num(data.total) + T(' 项）。')
          + T('可用搜索定位其余文件。') }));
    }
    data.entries.forEach((entry) => {
      const row = U.el('div', { class: 'tree-row', dataset: { path: entry.path } });
      row.style.paddingLeft = (6 + depth * 12) + 'px';
      const twig = U.el('span', { class: 'twig' });
      if (entry.is_dir) {
        twig.innerHTML = Icons.svg(expanded.has(entry.rel_path) ? 'chevronDown' : 'chevronRight',
                                   { size: 12 });
      }
      row.appendChild(twig);
      const iconSpan = U.el('span', { html: iconHtml(entry, 15) });
      row.appendChild(iconSpan);
      row.appendChild(U.el('span', { class: 'label', text: entry.name }));
      if (!entry.is_dir) {
        row.appendChild(U.el('span', { class: 'size', text: U.size(entry.size) }));
      }
      if (entry.is_symlink) row.title = T('这是一个符号链接');
      if (entry.text_state === 'unsupported') row.title = T('该文件没有可提取的文本层');

      const children = U.el('div', { class: 'tree-children' });
      let loaded = false;

      async function toggle() {
        if (!entry.is_dir) return;
        if (expanded.has(entry.rel_path)) {
          expanded.delete(entry.rel_path);
          children.innerHTML = '';
          loaded = false;
          twig.innerHTML = Icons.svg('chevronRight', { size: 12 });
          return;
        }
        expanded.add(entry.rel_path);
        twig.innerHTML = Icons.svg('chevronDown', { size: 12 });
        if (!loaded) { await loadLevel(entry.rel_path, children, depth + 1); loaded = true; }
      }

      row.addEventListener('click', (event) => {
        if (entry.is_dir) {
          toggle();
        } else {
          openFile(entry.path, { focus: event.detail === 2 });
        }
      });
      row.addEventListener('dblclick', () => {
        if (!entry.is_dir) openFile(entry.path, { focus: true });
      });
      row.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        entryMenu(entry);
      });
      container.appendChild(row);
      container.appendChild(children);

      if (entry.is_dir && expanded.has(entry.rel_path)) {
        loadLevel(entry.rel_path, children, depth + 1).then(() => { loaded = true; });
      }
    });
  }

  function reloadTree() {
    return loadLevel('', treeBody, 0);
  }

  function entryMenu(entry) {
    const buttons = [
      { text: T('设为收藏 / 取消收藏'), kind: '', onclick: async (close) => {
        await API.post('api/fav/toggle', { path: entry.path });
        UI.ok(T('已更新收藏'));
        close();
      } },
      { text: T('更改图标'), kind: '', onclick: (close) => { close(); chooseIcon(entry, reloadTree); } },
      { text: T('复制路径'), kind: '', onclick: async (close) => {
        try { await navigator.clipboard.writeText(entry.path); UI.ok(T('路径已复制')); }
        catch (error) { UI.warn(T('浏览器未允许写入剪贴板')); }
        close();
      } },
      { text: T('下载'), kind: '', onclick: (close) => {
        window.open(contentUrl(entry, 'download=1'), '_blank', 'noopener');
        close();
      } },
      { text: T('取消'), kind: '', onclick: (close) => close() },
    ];
    UI.modal({
      title: entry.name,
      bodyHtml: '<div class="prewrap">' + U.esc(entry.rel_path || '/') + '</div>',
      buttons: buttons,
    });
  }

  await reloadTree();

  // 恢复上次打开的文件（同一个知识库内）
  const active = State.tabs[State.activeTab];
  if (active) openFile(active.path);
}

function makeSplitter(node, shell, variable, min, max, onDone) {
  let startX = 0;
  let startValue = 0;
  node.addEventListener('pointerdown', (event) => {
    const style = getComputedStyle(shell);
    startValue = parseInt(style.getPropertyValue(variable), 10)
      || (variable === '--tree-w' ? 288 : 320);
    startX = event.clientX;
    node.classList.add('dragging');
    document.body.classList.add('kb-resizing');
    node.setPointerCapture(event.pointerId);
  });
  node.addEventListener('pointermove', (event) => {
    if (!node.classList.contains('dragging')) return;
    const delta = variable === '--tree-w'
      ? event.clientX - startX
      : startX - event.clientX;
    const next = Math.max(min, Math.min(max, startValue + delta));
    shell.style.setProperty(variable, next + 'px');
  });
  node.addEventListener('pointerup', (event) => {
    node.classList.remove('dragging');
    document.body.classList.remove('kb-resizing');
    try { node.releasePointerCapture(event.pointerId); } catch (error) { /* 忽略 */ }
    if (onDone) onDone();
  });
}

function kindLabel(kind) {
  return {
    pdf: 'PDF', markdown: 'Markdown', html: T('网页'), text: T('文本'), code: T('代码'),
    json: 'JSON', xml: 'XML', yaml: 'YAML', csv: T('表格'), xlsx: 'Excel',
    docx: 'Word', pptx: T('演示文稿'), image: T('图片'), audio: T('音频'), video: T('视频'),
    dir: T('目录'), other: T('其它'),
  }[kind] || T('文件');
}

function chooseIcon(entry, done) {
  let icons = [];
  API.get('api/icon/builtin').then((data) => {
    icons = data.icons || [];
    const grid = U.el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px' });
    icons.forEach((icon) => {
      const button = U.el('button', {
        class: 'icon-btn', title: icon.label,
        html: Icons.svg(icon.value, { size: 20 }),
      });
      button.addEventListener('click', async () => {
        await API.post('api/icon/set', {
          target_path: entry.path,
          target_type: entry.is_dir ? 'dir' : 'file',
          icon_type: 'builtin', icon_value: icon.value,
        });
        UI.ok(T('图标已更新'));
        close();
        if (done) done();
      });
      grid.appendChild(button);
    });
    const clearBtn = U.el('button', { class: 'btn', text: T('恢复默认图标') });
    clearBtn.addEventListener('click', async () => {
      await API.post('api/icon/clear', { target_path: entry.path });
      UI.ok(T('已恢复默认'));
      close();
      if (done) done();
    });
    const dialog = UI.modal({
      title: T('为「') + entry.name + T('」选择图标'),
      bodyHtml: '',
      wide: true,
      buttons: [{ text: T('关闭'), kind: '', onclick: (close) => close() }],
    });
    dialog.body.appendChild(grid);
    dialog.body.appendChild(U.el('div', { style: 'margin-top:12px' }, [clearBtn]));
  });
}

function openInBrowser(path) {
  if (!State.openFile) { Shell.show('browse'); }
  const go = () => {
    if (State.openFile) State.openFile(path);
    else setTimeout(() => { if (State.openFile) State.openFile(path); }, 350);
  };
  if (Shell.current() !== 'browse') {
    Shell.show('browse');
    setTimeout(go, 320);
  } else {
    go();
  }
}

/* ---------------------------------------------------------------- 搜索 */

function renderSearch(main) {
  document.body.classList.remove('kb-fullbleed');
  main.innerHTML = '';
  const box = U.el('div', { class: 'search-box' });
  const input = U.el('input', { type: 'search', placeholder: T('搜索知识库（文件名与全文内容）…') });
  const button = U.el('button', { class: 'btn', text: T('搜索') });
  box.appendChild(input);
  box.appendChild(button);
  main.appendChild(box);

  const filters = U.el('div', { class: 'filters' });
  const kbSelect = U.el('select');
  kbSelect.appendChild(U.el('option', { value: '', text: T('全部知识库') }));
  State.kbs.forEach((kb) => {
    kbSelect.appendChild(U.el('option', { value: String(kb.id), text: kb.name }));
  });
  const kindSelect = U.el('select');
  [['', T('全部类型')], ['pdf', 'PDF'], ['docx', 'Word'], ['xlsx', 'Excel'],
   ['pptx', T('演示文稿')], ['markdown', 'Markdown'], ['text', T('文本')],
   ['code', T('代码')], ['csv', T('表格')], ['image', T('图片')],
   ['audio', T('音频')], ['video', T('视频')]].forEach((pair) => {
    kindSelect.appendChild(U.el('option', { value: pair[0], text: pair[1] }));
  });
  const sortSelect = U.el('select');
  [['relevance', T('相关度')], ['time', T('时间')], ['name', T('名称')]].forEach((pair) => {
    sortSelect.appendChild(U.el('option', { value: pair[0], text: pair[1] }));
  });
  filters.appendChild(kbSelect);
  filters.appendChild(kindSelect);
  filters.appendChild(U.el('span', { class: 'meta', text: T('排序') }));
  filters.appendChild(sortSelect);
  if (State.currentKb) kbSelect.value = String(State.currentKb.id);
  main.appendChild(filters);

  const summary = U.el('div', { class: 'card-hint' });
  main.appendChild(summary);
  const results = U.el('div');
  main.appendChild(results);

  async function run() {
    const query = input.value.trim();
    results.innerHTML = '';
    summary.textContent = '';
    if (!query) return;
    summary.textContent = T('搜索中…');
    const params = ['q=' + encodeURIComponent(query), 'limit=60'];
    if (kbSelect.value) params.push('kb=' + kbSelect.value);
    if (kindSelect.value) params.push('kind=' + kindSelect.value);
    if (sortSelect.value) params.push('sort=' + sortSelect.value);
    let data;
    try {
      data = await API.get('api/search?' + params.join('&'));
    } catch (error) {
      summary.textContent = T('搜索失败：') + (error.message || error);
      return;
    }
    summary.innerHTML = '';
    summary.appendChild(U.el('span', {
      text: T('找到 ') + U.num(data.total) + T(' 个结果（用时取决于索引规模）') }));
    if (data.degraded && data.hint) {
      // 降级必须让用户看见，绝不假装全文检索成功了
      summary.appendChild(U.el('span', {
        class: 'badge degraded', style: 'margin-left:8px', text: T('模糊匹配') }));
      summary.appendChild(U.el('div', { class: 'info-note warn',
        style: 'margin-top:8px', text: data.hint }));
    }
    if (!data.results.length) {
      results.appendChild(UI.empty('search', T('没有找到匹配的文件'),
        T('可以换一个关键词，或先把知识库扫描完整。')));
      return;
    }
    data.results.forEach((item) => {
      const node = U.el('div', { class: 'result-item' });
      const top = U.el('div', { class: 'top' });
      top.innerHTML = iconHtml(item, 15);
      top.appendChild(U.el('span', { class: 'nm', text: item.name }));
      if (item.unit && item.unit.label) {
        top.appendChild(U.el('span', { class: 'u', text: item.unit.label }));
      }
      node.appendChild(top);
      node.appendChild(U.el('div', { class: 'path', text: item.rel_path }));
      if (item.snippet_html) {
        // snippet_html 是服务端转义后再插入 <mark> 的，可以安全渲染
        node.appendChild(U.el('div', { class: 'snip', html: item.snippet_html }));
      }
      node.addEventListener('click', () => {
        openInBrowser(item.path);
        if (item.unit && item.unit.type === 'page') {
          setTimeout(() => {
            const handle = State.viewerHandle;
            if (handle && handle.gotoPage) handle.gotoPage(item.unit.no);
          }, 700);
        }
      });
      results.appendChild(node);
    });
  }

  button.addEventListener('click', run);
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') run(); });
  kbSelect.addEventListener('change', run);
  kindSelect.addEventListener('change', run);
  sortSelect.addEventListener('change', run);
  setTimeout(() => input.focus(), 60);
  return { run, setQuery: (value) => { input.value = value; run(); } };
}

/* ---------------------------------------------------------------- 收藏 */

async function renderFavorites(main) {
  document.body.classList.remove('kb-fullbleed');
  main.innerHTML = '';
  main.appendChild(U.el('h2', { style: 'margin-top:0', text: T('收藏') }));
  let data;
  try {
    data = await API.get('api/fav/list?limit=500');
  } catch (error) {
    main.appendChild(UI.empty('alert', T('无法读取收藏'), U.esc(error.message || String(error))));
    return;
  }
  if (!data.favorites.length) {
    main.appendChild(UI.empty('tag', T('还没有收藏'),
      T('在文件树或信息栏点「收藏」，常用的文件会集中到这里。')));
    return;
  }
  const card = U.el('div', { class: 'card' });
  card.appendChild(buildFileList(data.favorites, (item) => openInBrowser(item.path)));
  main.appendChild(card);

  const recent = await API.get('api/recent/list?limit=40');
  if (recent.recents && recent.recents.length) {
    const recentCard = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
      U.el('h2', { html: Icons.svg('clock') + T(' 最近阅读') }),
      buildFileList(recent.recents, (item) => openInBrowser(item.path)),
    ]);
    const clear = U.el('button', { class: 'btn', text: T('清空最近阅读记录') });
    clear.addEventListener('click', async () => {
      const confirmed = await UI.confirm({
        title: T('清空最近阅读记录？'), danger: true,
        body: T('只会清除本应用记录的「最近打开」列表，不会影响任何文件。'),
        confirmText: T('清空'),
      });
      if (!confirmed) return;
      await API.post('api/recent/clear', {});
      UI.ok(T('已清空'));
      Shell.show('favorites');
    });
    recentCard.appendChild(U.el('div', { style: 'margin-top:10px' }, [clear]));
    main.appendChild(recentCard);
  }
}

/* ---------------------------------------------------------------- 任务 */

function renderJobs(main) {
  document.body.classList.remove('kb-fullbleed');
  main.innerHTML = '';
  main.appendChild(U.el('h2', { style: 'margin-top:0', text: T('任务') }));
  main.appendChild(U.el('div', { class: 'card-hint',
    text: T('扫描与建立索引都在后台进行，可以随时取消或暂停。')
      + T('任务中断后重新启动应用会自动续跑。') }));
  const body = U.el('div', { class: 'card' });
  main.appendChild(body);

  async function refresh() {
    let data;
    try {
      data = await API.get('api/jobs?limit=100');
    } catch (error) {
      body.innerHTML = '';
      body.appendChild(UI.empty('alert', T('无法读取任务'), U.esc(error.message || String(error))));
      return;
    }
    body.innerHTML = '';
    Jobs.renderTable(body, data.jobs || [], { emptyHint: T('还没有任务') });
  }
  Jobs.reload = refresh;
  refresh();
}

/* ---------------------------------------------------------------- 设置 */

async function renderSettings(main) {
  document.body.classList.remove('kb-fullbleed');
  main.innerHTML = '';
    // 界面语言（放最前：非中文用户进来第一眼就该看到它）
    // UI.langSelect() 内部已处理「落 localStorage + 套用 + 同步到后端 settings.ui_language」。
    %(host)s.appendChild(U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('globe', { size: 17 }) }),
        U.el('span', { text: T('界面语言') }),
      ]),
      U.el('div', { class: 'card-hint',
        text: T('选择本应用界面的语言。首次打开时会跟随浏览器语言。') }),
      UI.langSelect(),
    ]));
  main.appendChild(U.el('h2', { style: 'margin-top:0', text: T('设置') }));

  let settings = {};
  let status = {};
  try {
    const [settingsData, statusData] = await Promise.all([
      API.get('api/settings'), API.get('api/status'),
    ]);
    settings = settingsData.settings || {};
    status = statusData;
    State.settings = settings;
    State.allowedRoots = settings.allowed_roots || [];
  } catch (error) {
    main.appendChild(UI.empty('alert', T('无法读取设置'), U.esc(error.message || String(error))));
    return;
  }

  // --- 外观 ---
  const themeCard = U.el('div', { class: 'card' }, [
    U.el('h2', { html: Icons.svg('eye') + T(' 外观') }),
  ]);
  const themeRow = U.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' });
  [['system', T('跟随系统')], ['light', T('浅色')], ['dark', T('深色')]].forEach((pair) => {
    const button = U.el('button', {
      class: 'btn' + ((settings.theme || 'system') === pair[0] ? '' : ' ghost'),
      text: pair[1],
    });
    button.addEventListener('click', async () => {
      applyTheme(pair[0]);
      await API.post('api/settings', { theme: pair[0] });
      themeRow.querySelectorAll('button').forEach((node) => node.classList.add('ghost'));
      button.classList.remove('ghost');
    });
    themeRow.appendChild(button);
  });
  themeCard.appendChild(themeRow);
  main.appendChild(themeCard);

  // --- 可访问目录 ---
  const rootsCard = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
    U.el('h2', { html: Icons.svg('folderOpen') + T(' 可访问目录') }),
    U.el('div', { class: 'card-hint',
      text: T('只有这里的目录能被本应用读取。知识库必须建立在这些目录之内。') }),
  ]);
  const rootList = U.el('ul', { class: 'info-list' });
  (settings.allowed_roots || []).forEach((root) => {
    const li = U.el('li', {});
    li.innerHTML = Icons.svg('folder', { size: 15 });
    li.appendChild(U.el('span', { class: 'n', text: root }));
    const remove = U.el('button', { class: 'icon-btn', html: Icons.svg('x'), title: T('移出白名单') });
    remove.addEventListener('click', async (event) => {
      event.stopPropagation();
      const next = (settings.allowed_roots || []).filter((item) => item !== root);
      await API.post('api/settings', { allowed_roots: next });
      UI.ok(T('已移出白名单（不会删除任何文件）'));
      Shell.show('settings');
    });
    li.appendChild(remove);
    rootList.appendChild(li);
  });
  if (!(settings.allowed_roots || []).length) {
    rootList.appendChild(U.el('li', { text: T('（还没有添加任何目录）') }));
  }
  rootsCard.appendChild(rootList);
  const addRoot = U.el('button', { class: 'btn', text: T('＋ 添加目录'), style: 'margin-top:10px' });
  addRoot.addEventListener('click', () => {
    UI.pickDir({
      title: T('选择允许本应用读取的目录'),
      onPick: async (path) => {
        if (!path) return;
        const next = (settings.allowed_roots || []).concat([path]);
        await API.post('api/settings', { allowed_roots: next });
        UI.ok(T('已添加：') + path);
        Shell.show('settings');
      },
    });
  });
  rootsCard.appendChild(addRoot);
  main.appendChild(rootsCard);

  // --- 索引 ---
  const indexCard = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
    U.el('h2', { html: Icons.svg('scan') + T(' 索引') }),
  ]);
  const indexInfo = U.el('dl', { class: 'info-kv' });
  const counts = status.counts || {};
  [
    [T('已收录文件'), U.num(counts.files || 0)],
    [T('已建立索引'), U.num(counts.indexed || 0)],
    [T('等待索引'), U.num(counts.pending || 0)],
    [T('无文本层'), U.num(counts.unsupported || 0)],
    [T('索引失败'), U.num(counts.failed || 0)],
    [T('索引数据占用'), status.db_bytes_text || '—'],
  ].forEach((pair) => {
    indexInfo.appendChild(U.el('dt', { text: pair[0] }));
    indexInfo.appendChild(U.el('dd', { text: String(pair[1]) }));
  });
  indexCard.appendChild(indexInfo);
  const reindex = U.el('button', { class: 'btn', style: 'margin-top:10px',
                                   text: T('重建全部全文索引') });
  reindex.addEventListener('click', async () => {
    const confirmed = await UI.confirm({
      title: T('重建全文索引？'),
      body: T('会重新读取所有已收录文件的文本内容。源文件不会被修改，')
        + T('只是重新建立检索索引，可能需要较长时间。'),
      confirmText: T('开始重建'),
    });
    if (!confirmed) return;
    await API.post('api/search/reindex', {});
    UI.ok(T('已加入任务队列'));
    Shell.show('jobs');
  });
  indexCard.appendChild(reindex);
  main.appendChild(indexCard);

  // --- 能力与诊断（售后定位用） ---
  const diagCard = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
    U.el('h2', { html: Icons.svg('activity') + T(' 运行状态') }),
  ]);
  const diag = U.el('dl', { class: 'info-kv' });
  [
    [T('版本'), status.version || '—'],
    ['SQLite', status.sqlite_version || '—'],
    [T('全文检索 (FTS5)'), status.fts5 ? T('可用') : T('不可用（已降级为模糊匹配）')],
    [T('子串索引 (trigram)'), status.trigram ? T('可用') : T('不可用')],
    [T('数据目录'), (status.paths && status.paths.data_dir) || '—'],
  ].forEach((pair) => {
    diag.appendChild(U.el('dt', { text: pair[0] }));
    diag.appendChild(U.el('dd', { text: String(pair[1]) }));
  });
  diagCard.appendChild(diag);
  (status.degraded_features || []).forEach((item) => {
    diagCard.appendChild(U.el('div', { class: 'info-note warn', style: 'margin-top:8px',
      text: item.reason }));
  });
  main.appendChild(diagCard);

  // --- 关于 ---
  const about = U.el('div', { class: 'card', style: 'margin-top:14px' }, [
    U.el('h2', { html: Icons.svg('info') + T(' 关于') }),
    U.el('div', { class: 'info-note' }, [
      U.el('div', { text: T('本应用对知识库中的源文件**严格只读**：')
        + T('不修改、不删除、不移动、不改名任何文件。') }),
      U.el('div', { style: 'margin-top:6px',
        text: T('索引数据只保存在应用自己的数据目录里，注销知识库即可清除。') }),
      U.el('div', { style: 'margin-top:6px',
        text: T('全部功能离线可用，应用不发起任何对外网络请求。') }),
    ]),
  ]);
  main.appendChild(about);
}

/* ---------------------------------------------------------------- 新建知识库 */

function newKnowledgeBase() {
  const nameInput = U.el('input', {
    type: 'text', placeholder: T('例如：产品文档'),
    style: 'width:100%;padding:8px 10px;border:1px solid var(--border);'
      + 'border-radius:var(--radius-sm);background:var(--surface-2);color:var(--text);'
      + 'font-family:inherit;font-size:13.5px',
  });
  const pathBox = U.el('div', {
    style: 'margin-top:8px;font-family:var(--mono);font-size:12.5px;'
      + 'background:var(--surface-2);border:1px solid var(--border);'
      + 'border-radius:var(--radius-sm);padding:7px 10px;color:var(--text-faint)',
    text: T('还没有选择目录'),
  });
  let chosenPath = '';

  const pickBtn = U.el('button', { class: 'btn', text: T('选择文件夹…') });
  pickBtn.addEventListener('click', () => {
    UI.pickDir({
      title: T('选择要变成知识库的文件夹'),
      onPick: (path) => {
        if (!path) return;
        chosenPath = path;
        pathBox.textContent = path;
        pathBox.style.color = 'var(--text)';
      },
    });
  });

  const dialog = UI.modal({
    title: T('新建知识库'),
    bodyHtml: '',
    buttons: [
      { text: T('取消'), kind: 'ghost', onclick: (close) => close() },
      { text: T('创建并扫描'), kind: '', onclick: async (close) => {
        const name = nameInput.value.trim();
        if (!name) { UI.warn(T('请填一个名称')); return; }
        if (!chosenPath) { UI.warn(T('请选择文件夹')); return; }
        try {
          const result = await API.post('api/kb/create', {
            name: name, root_path: chosenPath,
          });
          close();
          UI.ok(T('知识库已创建，正在扫描'));
          State.currentKb = result.kb;
          Shell.show('jobs');
        } catch (error) {
          UI.err(error.message || String(error));
        }
      } },
    ],
  });
  dialog.body.appendChild(U.el('div', { class: 'card-hint',
    text: T('给这个知识库起个名字，然后选择它对应的文件夹。') }));
  dialog.body.appendChild(nameInput);
  dialog.body.appendChild(U.el('div', { style: 'margin-top:12px;display:flex;gap:8px;'
    + 'align-items:center' }, [pickBtn]));
  dialog.body.appendChild(pathBox);
  dialog.body.appendChild(U.el('div', { class: 'info-note', style: 'margin-top:12px',
    text: T('文件夹只会被**读取**。原文件不会被修改、移动或删除。') }));
  setTimeout(() => nameInput.focus(), 60);
}

/* ---------------------------------------------------------------- 快速打开 */

const QuickOpen = (() => {
  let mask = null;
  let input = null;
  let list = null;
  let items = [];
  let selected = 0;

  function close() {
    if (mask) { mask.remove(); mask = null; }
  }

  function draw() {
    list.innerHTML = '';
    if (!items.length) {
      list.appendChild(U.el('div', { class: 'qp-row', text: T('没有匹配的文件') }));
      return;
    }
    items.forEach((item, index) => {
      const row = U.el('div', { class: 'qp-row' + (index === selected ? ' sel' : '') });
      row.innerHTML = iconHtml(item, 15);
      row.appendChild(U.el('span', { text: item.name }));
      row.appendChild(U.el('span', { class: 'p', text: item.rel_path || '' }));
      row.addEventListener('click', () => { pick(index); });
      list.appendChild(row);
    });
  }

  function pick(index) {
    const item = items[index];
    close();
    if (item) openInBrowser(item.path);
  }

  async function search(query) {
    try {
      const data = await API.get('api/search/suggest?limit=30&q=' + encodeURIComponent(query));
      items = data.results || [];
    } catch (error) {
      items = [];
    }
    selected = 0;
    draw();
  }

  function open(initial) {
    if (mask) { input.focus(); return; }
    input = U.el('input', { type: 'text', placeholder: T('输入文件名…（Enter 打开，Esc 关闭）') });
    list = U.el('div', { class: 'qp-list' });
    const box = U.el('div', { class: 'qp-box' }, [input, list]);
    mask = U.el('div', { class: 'qp-mask' }, [box]);
    mask.addEventListener('click', (event) => { if (event.target === mask) close(); });
    document.body.appendChild(mask);

    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => search(input.value), 140);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { close(); }
      else if (event.key === 'ArrowDown') {
        event.preventDefault();
        selected = Math.min(items.length - 1, selected + 1); draw();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        selected = Math.max(0, selected - 1); draw();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        pick(selected);
      }
    });
    if (initial) input.value = initial;
    search(initial || '');
    setTimeout(() => input.focus(), 40);
  }

  return { open, close };
})();

/* ---------------------------------------------------------------- 键盘快捷键 */

function bindShortcuts() {
  document.addEventListener('keydown', (event) => {
    const meta = event.ctrlKey || event.metaKey;
    const tag = (event.target && event.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

    if (meta && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      Shell.show('search');
      const view = Shell.view('search');
      if (view && view.setQuery) view.setQuery('');
      return;
    }
    if (meta && event.key.toLowerCase() === 'p') {
      event.preventDefault();
      QuickOpen.open('');
      return;
    }
    if (typing) return;

    if (!meta && event.key === 'f') {
      const node = document.querySelector('.kb-shell');
      if (node) { node.classList.toggle('focus'); event.preventDefault(); }
      return;
    }
    if (event.key === 'Escape') {
      const node = document.querySelector('.kb-shell');
      if (node && node.classList.contains('focus')) { node.classList.remove('focus'); }
      return;
    }
    if (meta && event.key.toLowerCase() === 'b') {
      const node = document.querySelector('.kb-shell');
      if (!node) return;
      event.preventDefault();
      if (event.shiftKey) node.classList.toggle('no-info');
      else node.classList.toggle('no-tree');
    }
  });
}

/* ---------------------------------------------------------------- 启动 */

(async function boot() {
  let views = {};
  let settings = {};

  try {
    settings = (await API.get('api/settings')).settings || {};
  } catch (error) { /* 用默认主题 */ }
  applyTheme(settings.theme || 'system');

  const Shell = window.Shell = (function initShell() {
    const viewState = {};
    const shell = (function () {
      const views = {
        home: {
          label: T('首页'), icon: 'home',
          render: (main) => { clearFullbleed(); renderHome(main); },
        },
        browse: {
          label: T('浏览'), icon: 'book',
          render: (main) => { renderBrowse(main); },
        },
        search: {
          label: T('搜索'), icon: 'search',
          render: (main) => { viewState.search = renderSearch(main); },
        },
        favorites: {
          label: T('收藏'), icon: 'tag',
          render: (main) => { clearFullbleed(); renderFavorites(main); },
        },
        jobs: {
          label: T('任务'), icon: 'activity',
          render: (main) => { clearFullbleed(); renderJobs(main); },
        },
        settings: {
          label: T('设置'), icon: 'settings',
          render: (main) => { clearFullbleed(); renderSettings(main); },
        },
      };
      const base = ShellInit(views, { defaultView: 'home' });
      // 切语言后重渲染当前视图 —— 框架只换静态文案，动态渲染的部分要靠这个事件
      Shell.bindLanguage(base);
      base.view = (key) => viewState[key];
      return base;
    })();
    return shell;
  })();

  function clearFullbleed() { document.body.classList.remove('kb-fullbleed'); }

  // 顶栏按钮
  const quickBtn = U.byId('btn-quick');
  if (quickBtn) {
    U.byId('btn-quick-icon').innerHTML = Icons.svg('search', { size: 14 });
    quickBtn.addEventListener('click', () => QuickOpen.open(''));
  }

  await Shell.loadAppInfo();
  Jobs.mountTaskbar(U.byId('taskbar'));
  Jobs.start(2500);
  bindShortcuts();

  try {
    const data = await API.get('api/kb/list');
    State.kbs = data.knowledge_bases || [];
    if (State.kbs.length === 1) State.currentKb = State.kbs[0];
    const meta = U.byId('kb-meta');
    if (meta) {
      meta.textContent = State.kbs.length
        ? (State.kbs.length + T(' 个知识库'))
        : '';
    }
  } catch (error) { /* 首页会再取一次 */ }
})();

/* Shell.init 在 app.js 里，这里包一层是为了在切换视图时先清掉满屏类 */
function ShellInit(views, options) {
  const wrapped = {};
  Object.keys(views).forEach((key) => {
    wrapped[key] = {
      label: views[key].label,
      icon: views[key].icon,
      render(main) {
        if (key !== 'browse') document.body.classList.remove('kb-fullbleed');
        views[key].render(main);
      },
    };
  });
  return Shell.init(wrapped, options);
}
