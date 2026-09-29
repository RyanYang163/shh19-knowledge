/* ============================================================================
   Viewer Engine —— 插件式查看器

   设计文档 §9 的核心诉求：不要把「所有格式」写成一个巨大的 if-condition，
   而是统一成插件接口：

       { id, kinds, exts, canOpen(meta), open(ctx), destroy(), search?(q) }

   ctx = { host, meta, url(extra), toast, onOutline }

   **先渲染骨架再加载内容**（设计文档 §25 的核心原则）：open() 必须立刻把头部与
   占位结构放进 DOM，内容用 await 慢慢填。这样弱机上点开文件也不会「白屏一会儿」。

   关于 PDF：按用户决定走 **内置 PDF.js**（webui/pdfjs/，Apache-2.0）。
   只用它的**库**（pdf.min.mjs）而不用官方 viewer 界面 —— 官方 viewer 是一整套
   独立页面，嵌入后既要改写它的路径又要绕开它的样式，成本高于自己写一层外壳；
   而我们要的功能（翻页、缩放、文档内查找、跳到指定页）用库 API 都能做到。

   ⚠️ 包内**刻意不含** pdf.js 的 .wasm 与 cmaps/standard_fonts ——
   .wasm 是可执行的 WebAssembly 字节码，会正面撞上审核标准 §16.4 的一号一票否决
   （包内不得含二进制可执行文件，shh9 就是这么被驳的）；cmaps 与字体是 168 个
   二进制数据文件、约 1.5 MB。代价是「未嵌入 CID 字体」的中文 PDF 可能缺字形，
   已如实写进 README 的「不承诺的功能」。
   ============================================================================ */

const Viewers = (() => {
  const registry = [];
  let current = null;

  function register(spec) {
    if (!spec || !spec.id || typeof spec.open !== 'function') return;
    registry.push(spec);
  }

  function pick(meta) {
    const kind = (meta && meta.kind) || 'other';
    const ext = ((meta && meta.ext) || '').toLowerCase();
    return registry.find((viewer) => {
      try {
        return viewer.canOpen(meta, kind, ext);
      } catch (error) {
        return false;
      }
    }) || null;
  }

  /** 构造内容 URL。带 ?v=<mtime_ns> 做缓存失效 —— 框架固定发 no-store，
   *  HTTP 缓存指望不上，只能靠 URL 变化让浏览器重新取。 */
  function url(meta, extra) {
    const params = ['path=' + encodeURIComponent(meta.path)];
    if (meta.mtime_ns) params.push('v=' + meta.mtime_ns);
    if (extra) params.push(extra);
    return API.url('api/file/content?' + params.join('&'));
  }

  function apiUrl(endpoint, meta, extra) {
    const params = ['path=' + encodeURIComponent(meta.path)];
    if (extra) params.push(extra);
    return API.url(endpoint + '?' + params.join('&'));
  }

  function destroy() {
    if (current && typeof current.destroy === 'function') {
      try { current.destroy(); } catch (error) { /* 卸载失败不该影响换文件 */ }
    }
    current = null;
  }

  /**
   * 显示一个文件。
   * @param {HTMLElement} host 查看器主体容器
   * @param {object} meta /api/file/meta 的返回
   * @param {object} hooks
   *   查看器通过 ctx 回调外壳，外壳通过 hooks 提供实现：
   *   { controls(list), info(key, value), note(text, kind), outline(items),
   *     open(path, entry), toast(message, kind) }
   */
  async function show(host, meta, hooks) {
    destroy();
    host.innerHTML = '';
    const h = hooks || {};
    const viewer = pick(meta);
    if (!viewer) {
      host.appendChild(UI.empty('alert', T('暫不支持预览'),
        T('该格式没有内置查看器。可用「下载」在本地打开，')
        + T('全文检索仍可能命中它的文本内容。')));
      return null;
    }
    const ctx = {
      host: host,
      meta: meta,
      hook: h,
      url: (extra) => url(meta, extra),
      apiUrl: (endpoint, extra) => apiUrl(endpoint, meta, extra),
      toast: h.toast || ((message) => UI.toast(message)),
      controls: h.controls || function () {},
      info: h.info || function () {},
      note: h.note || function () {},
      outline: h.outline || function () {},
      open: h.open || function () {},
    };
    current = viewer;
    try {
      await viewer.open(ctx);
    } catch (error) {
      host.innerHTML = '';
      host.appendChild(UI.empty('alert', T('打开失败'),
        U.esc((error && error.message) || String(error))
        + T('<br>可尝试用「下载」在本地打开。')));
    }
    return viewer;
  }

  return { register, pick, show, destroy, url, apiUrl,
           list: () => registry.slice(), current: () => current };
})();


/* ---------------------------------------------------------------- 工具 */

/** 轻量语法高亮：**是分词器而不是解析器**，界面上也这么说。
 *  支持关键字 / 字符串 / 注释 / 数字四类，够读代码，不假装是 IDE。 */
const Tokens = (() => {
  const KEYWORDS = {
    python: 'def class return if elif else for while import from as with try except finally raise pass lambda yield global nonlocal assert del in is not and or None True False async await self',
    javascript: 'function const let var return if else for while do switch case break continue class extends new this typeof instanceof try catch finally throw import export from default async await null undefined true false',
    typescript: 'function const let var return if else for while switch case break continue class extends implements interface type enum new this try catch finally throw import export from default async await null undefined true false public private readonly',
    go: 'func package import return if else for range switch case break continue type struct interface map chan go defer select var const nil true false',
    rust: 'fn let mut pub use mod struct enum impl trait return if else match loop while for in break continue unsafe as where self Self Some None Ok Err true false const static',
    java: 'public private protected class interface extends implements return if else for while switch case break continue new this static final void int long double float boolean char String try catch finally throw import package null true false',
    c: 'int long short char void float double struct union enum typedef static const return if else for while switch case break continue sizeof unsigned signed goto NULL',
    cpp: 'int long short char void float double struct union enum class public private protected template typename namespace using return if else for while switch case break continue new delete this virtual const static nullptr true false auto',
    bash: 'if then else elif fi for while do done case esac function return local export readonly in echo set unset source exit',
    sql: 'select from where group by order having insert into values update set delete join left right inner outer on as and or not null limit offset create table index drop alter',
    ruby: 'def end class module if elsif else unless while until for in do return yield begin rescue ensure raise nil true false self require',
    php: 'function class return if else elseif for foreach while do switch case break continue new echo print public private protected static const use namespace try catch finally throw null true false array',
    css: '',
  };

  function escape(text) {
    return U.esc(text);
  }

  function highlight(text, lang) {
    const words = KEYWORDS[lang] || '';
    const keywordSet = new Set(words ? words.split(/\s+/) : []);
    let out = '';
    let index = 0;
    const length = text.length;
    while (index < length) {
      const ch = text[index];
      const rest = text.slice(index);

      // 注释
      let match = rest.match(/^(\/\/[^\n]*|#[^\n]*|--[^\n]*|\/\*[\s\S]*?\*\/)/);
      if (match && (lang !== 'css' || ch === '/' || ch === '#')) {
        out += '<span class="tok-com">' + escape(match[0]) + '</span>';
        index += match[0].length;
        continue;
      }
      // 字符串
      match = rest.match(/^("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)/);
      if (match) {
        out += '<span class="tok-str">' + escape(match[0]) + '</span>';
        index += match[0].length;
        continue;
      }
      // 数字
      match = rest.match(/^\b\d+(?:\.\d+)?\b/);
      if (match) {
        out += '<span class="tok-num">' + escape(match[0]) + '</span>';
        index += match[0].length;
        continue;
      }
      // 标识符 / 关键字
      match = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
      if (match) {
        const word = match[0];
        out += keywordSet.has(word)
          ? '<span class="tok-key">' + escape(word) + '</span>'
          : escape(word);
        index += word.length;
        continue;
      }
      out += escape(ch);
      index += 1;
    }
    return out;
  }

  return { highlight };
})();


/** 带行号的文本渲染。按**行**渲染而不是整块 <pre>，这样长文件也能虚拟化/查找。 */
function renderCode(text, lang, container) {
  const lines = text.split('\n');
  const table = U.el('table', { class: 'code-table' });
  const body = U.el('tbody');
  const limit = Math.min(lines.length, 20000);
  for (let i = 0; i < limit; i += 1) {
    body.appendChild(U.el('tr', {}, [
      U.el('td', { class: 'gutter', text: String(i + 1) }),
      U.el('td', { class: 'line', html: Tokens.highlight(lines[i], lang) }),
    ]));
  }
  table.appendChild(body);
  container.appendChild(table);
  if (lines.length > limit) {
    container.appendChild(U.el('div', {
      class: 'tree-empty',
      text: T('文件较长，只显示前 ') + limit + T(' 行。可用全文检索定位后面的内容。'),
    }));
  }
}


/* ---------------------------------------------------------------- 各查看器插件 */

/* --- 图片：缩放 / 拖动 / 旋转 / 适应窗口 --- */

Viewers.register({
  id: 'image',
  kinds: ['image'],
  canOpen: (meta, kind) => kind === 'image',
  open(ctx) {
    const state = { scale: 1, rotate: 0, fit: true, x: 0, y: 0 };
    const stage = U.el('div', { class: 'img-stage' });
    const img = U.el('img', { alt: ctx.meta.name, src: ctx.url() });
    stage.appendChild(img);
    ctx.host.appendChild(stage);

    function apply() {
      if (state.fit) {
        img.style.transform = 'rotate(' + state.rotate + 'deg)';
        img.style.maxWidth = '100%';
        img.style.maxHeight = '100%';
        img.style.width = '';
        img.style.height = '';
      } else {
        img.style.maxWidth = 'none';
        img.style.maxHeight = 'none';
        img.style.transform = 'translate(' + state.x + 'px,' + state.y + 'px) '
          + 'scale(' + state.scale + ') rotate(' + state.rotate + 'deg)';
      }
    }
    apply();

    stage.addEventListener('wheel', (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      state.fit = false;
      state.scale = Math.max(0.08, Math.min(12, state.scale * (event.deltaY < 0 ? 1.12 : 0.89)));
      apply();
    }, { passive: false });

    let dragging = null;
    stage.addEventListener('pointerdown', (event) => {
      if (state.fit) return;
      dragging = { x: event.clientX - state.x, y: event.clientY - state.y };
      img.classList.add('dragging');
    });
    window.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      state.x = event.clientX - dragging.x;
      state.y = event.clientY - dragging.y;
      apply();
    });
    window.addEventListener('pointerup', () => {
      dragging = null;
      img.classList.remove('dragging');
    });

    ctx.controls([
      { icon: 'plus', title: T('放大'), onclick: () => { state.fit = false; state.scale = Math.min(12, state.scale * 1.25); apply(); } },
      { icon: 'minus', title: T('缩小'), onclick: () => { state.fit = false; state.scale = Math.max(0.08, state.scale / 1.25); apply(); } },
      { icon: 'refresh', title: T('恢复适应窗口'), onclick: () => { state.fit = true; state.scale = 1; state.x = 0; state.y = 0; apply(); } },
      { icon: 'scan', title: T('旋转 90°'), onclick: () => { state.rotate = (state.rotate + 90) % 360; apply(); } },
      { icon: 'external', title: T('全屏'), onclick: () => { if (stage.requestFullscreen) stage.requestFullscreen(); } },
    ]);

    if (ctx.meta.image_w && ctx.meta.image_h) {
      ctx.info(T('尺寸'), ctx.meta.image_w + ' × ' + ctx.meta.image_h + T(' 像素'));
    }
    return { name: 'image' };
  },
  destroy() { /* 事件挂在 window 上，页面切换时旧节点被丢弃即可 */ },
});


/* --- PDF：内置 PDF.js（库 + 自绘外壳） --- */

Viewers.register({
  id: 'pdf',
  kinds: ['pdf'],
  canOpen: (meta, kind) => kind === 'pdf',
  async open(ctx) {
    const state = { doc: null, page: 1, scale: 1, rotation: 0, textCache: new Map(), task: null };

    const bar = U.el('div', { class: 'kb-toolbar' });
    const pageInput = U.el('input', { type: 'number', min: '1', value: '1',
                                       style: 'width:64px' });
    const pageLabel = U.el('span', { class: 'meta' });
    const searchInput = U.el('input', { type: 'search', placeholder: T('在文档内查找…') });
    const searchOut = U.el('span', { class: 'meta' });
    bar.appendChild(U.el('button', { class: 'icon-btn', html: Icons.svg('chevronUp'),
                                     title: T('上一页'), onclick: () => go(state.page - 1) }));
    bar.appendChild(U.el('button', { class: 'icon-btn', html: Icons.svg('chevronDown'),
                                     title: T('下一页'), onclick: () => go(state.page + 1) }));
    bar.appendChild(pageInput);
    bar.appendChild(pageLabel);
    bar.appendChild(U.el('span', { class: 'spacer' }));
    bar.appendChild(searchInput);
    bar.appendChild(searchOut);

    const canvasHost = U.el('div', {
      style: 'flex:1;overflow:auto;display:flex;justify-content:center;padding:16px 8px 40px',
    });
    const shell = U.el('div', { style: 'height:100%;display:flex;flex-direction:column' },
                       [bar, canvasHost]);
    ctx.host.appendChild(shell);

    const canvas = U.el('canvas', { style: 'box-shadow:var(--shadow-lg);background:#fff' });
    canvasHost.appendChild(canvas);

    ctx.controls([
      { icon: 'plus', title: T('放大'), onclick: () => { state.scale = Math.min(6, state.scale * 1.2); render(); } },
      { icon: 'minus', title: T('缩小'), onclick: () => { state.scale = Math.max(0.25, state.scale / 1.2); render(); } },
      { icon: 'refresh', title: T('实际大小'), onclick: () => { state.scale = 1; render(); } },
      { icon: 'scan', title: T('旋转'), onclick: () => { state.rotation = (state.rotation + 90) % 360; render(); } },
      { icon: 'panel', title: T('在文档内查找文本') , onclick: () => searchInput.focus() },
    ]);

    function go(number) {
      if (!state.doc) return;
      const target = Math.max(1, Math.min(state.doc.numPages, Number(number) || 1));
      state.page = target;
      render();
    }

    pageInput.addEventListener('change', () => go(pageInput.value));

    async function render() {
      if (!state.doc) return;
      if (state.task) { try { state.task.cancel(); } catch (error) { /* 忽略 */ } }
      const viewport0 = null;
      try {
        const page = await state.doc.getPage(state.page);
        const viewport = page.getViewport({ scale: state.scale, rotation: state.rotation });
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * ratio);
        canvas.height = Math.floor(viewport.height * ratio);
        canvas.style.width = Math.floor(viewport.width) + 'px';
        canvas.style.height = Math.floor(viewport.height) + 'px';
        const context = canvas.getContext('2d');
        state.task = page.render({
          canvasContext: context,
          viewport: viewport,
          transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null,
        });
        await state.task.promise;
      } catch (error) {
        if (error && error.name === 'RenderingCancelledException') return;
        canvasHost.innerHTML = '';
        canvasHost.appendChild(UI.empty('alert', T('这一页无法渲染'),
          U.esc(error && error.message ? error.message : String(error))));
        return;
      }
      pageLabel.textContent = '/ ' + state.doc.numPages;
      pageInput.value = String(state.page);
    }

    /** 取一页的文本（缓存）。查找用。 */
    async function pageText(number) {
      if (state.textCache.has(number)) return state.textCache.get(number);
      const page = await state.doc.getPage(number);
      const content = await page.getTextContent();
      const text = content.items.map((item) => item.str).join('');
      state.textCache.set(number, text);
      return text;
    }

    async function find(query) {
      const needle = (query || '').trim();
      if (!needle || !state.doc) { searchOut.textContent = ''; return; }
      searchOut.textContent = T('查找中…');
      const hits = [];
      for (let number = 1; number <= state.doc.numPages; number += 1) {
        const text = await pageText(number);
        if (text.toLowerCase().includes(needle.toLowerCase())) hits.push(number);
        if (hits.length >= 50) break;
      }
      if (!hits.length) {
        searchOut.textContent = T('未找到');
        return;
      }
      searchOut.textContent = T('第 ') + hits.slice(0, 12).join(' / ')
        + T(' 页命中') + (hits.length > 12 ? T(' 等') : '');
      go(hits[0]);
    }

    let searchTimer = null;
    searchInput.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => find(searchInput.value), 320);
    });
    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { clearTimeout(searchTimer); find(searchInput.value); }
    });

    // ---- 加载 ----
    let pdfjsLib = null;
    try {
      pdfjsLib = await import('./pdfjs/pdf.min.mjs');
      pdfjsLib.GlobalWorkerOptions.workerSrc = API.url('pdfjs/pdf.worker.min.mjs');
    } catch (error) {
      ctx.host.innerHTML = '';
      ctx.host.appendChild(UI.empty('alert', T('PDF 阅读器未能加载'),
        T('内置的渲染库没有成功载入，请重新打开本应用。')
        + T('<br>该 PDF 仍可「下载」后用本地阅读器打开。')));
      return { name: 'pdf' };
    }

    try {
      state.doc = await pdfjsLib.getDocument({ url: ctx.url(), withCredentials: true }).promise;
    } catch (error) {
      const message = String((error && error.message) || error);
      const encrypted = /password/i.test(message);
      ctx.host.innerHTML = '';
      ctx.host.appendChild(UI.empty('lock', encrypted ? T('这个 PDF 有密码') : T('无法打开这个 PDF'),
        encrypted
          ? T('文件已加密，请在本地阅读器里输入口令。<br>为避免把乱码写进索引，它的文本层不会被检索。')
          : U.esc(message) + T('<br>可尝试「下载」后用本地阅读器打开。')));
      return { name: 'pdf' };
    }

    // 初始缩放：让第一页大致铺满宽度
    try {
      const first = await state.doc.getPage(1);
      const natural = first.getViewport({ scale: 1 });
      const available = Math.max(280, canvasHost.clientWidth - 40);
      state.scale = Math.max(0.4, Math.min(2.5, available / natural.width));
    } catch (error) { /* 用默认 1.0 */ }

    ctx.info(T('页数'), String(state.doc.numPages) + T(' 页'));
    if (ctx.meta.note) ctx.note(ctx.meta.note, 'warn');
    else if (ctx.meta.text_state === 'ok') {
      ctx.info(T('文本层'), T('已索引，可在全文检索中命中并定位到页'));
    }
    await render();

    return {
      name: 'pdf',
      gotoPage(number) { go(number); },
      destroy() { if (state.doc) { try { state.doc.destroy(); } catch (error) { /* 忽略 */ } } },
    };
  },
});


/* --- HTML：沙箱 iframe + 服务端 CSP（双重隔离） --- */

Viewers.register({
  id: 'html',
  kinds: ['html'],
  canOpen: (meta, kind) => kind === 'html',
  open(ctx) {
    const host = U.el('div', { class: 'frame-host' });
    // sandbox 只给 allow-same-origin：禁脚本、禁表单、禁顶层跳转。
    // 服务端另有一层 CSP（/api/file/view），两层都在，才叫沙箱化（设计文档 §14）。
    host.appendChild(U.el('iframe', {
      sandbox: 'allow-same-origin',
      referrerpolicy: 'no-referrer',
      src: ctx.apiUrl('api/file/view'),
      title: ctx.meta.name,
    }));
    ctx.host.appendChild(host);
    ctx.note(T('HTML 以沙箱方式呈现：脚本、表单与外部请求都被禁用。'), null);
    ctx.controls([
      { icon: 'external', title: T('在新标签页打开'), onclick: () => window.open(ctx.apiUrl('api/file/view'), '_blank', 'noopener') },
    ]);
    return { name: 'html' };
  },
});


/* --- Markdown / Word：服务端渲染好的、已转义的 HTML --- */

function documentViewer(id, kinds, endpoint) {
  return {
    id: id,
    kinds: kinds,
    canOpen: (meta, kind) => kinds.indexOf(kind) >= 0,
    async open(ctx) {
      const holder = U.el('div', { class: 'kb-viewer-body', style: 'flex:1;overflow:auto' });
      const reader = U.el('div', { class: 'reader' }, [
        U.el('div', { class: 'tree-empty', text: T('正在渲染…') }),
      ]);
      holder.appendChild(reader);
      ctx.host.appendChild(holder);

      const outlineHost = [];
      ctx.controls([
        { icon: 'minus', title: T('收窄阅读宽度'), onclick: () => { document.body.classList.remove('reader-wide', 'reader-full'); } },
        { icon: 'panel', title: T('加宽阅读宽度'), onclick: () => { document.body.classList.add('reader-wide'); document.body.classList.remove('reader-full'); } },
        { icon: 'scan', title: T('全宽'), onclick: () => { document.body.classList.add('reader-full'); document.body.classList.remove('reader-wide'); } },
      ]);

      try {
        const data = await API.get(endpoint + '?path=' + encodeURIComponent(ctx.meta.path));
        if (!data.ok) throw new Error(data.error || T('渲染失败'));
        // ⚠️ 这里的 html 是**服务端转义后**生成的（app/render.py），
        // 不是原始文件内容 —— 直接插入是安全的。见 tests/test_render.py。
        reader.innerHTML = data.html || '';
        if (data.truncated) {
          ctx.note(T('文件较长，只索引并显示了前面一部分。'), 'warn');
        }
        if (data.encoding) ctx.info(T('编码'), data.encoding);
        const headings = reader.querySelectorAll('h1, h2, h3, h4');
        headings.forEach((node, index) => { node.id = 'sec-' + index; });
        if (headings.length) {
          ctx.outline(Array.prototype.map.call(headings, (node, index) => ({
            label: node.textContent, anchor: 'sec-' + index,
          })));
        }
      } catch (error) {
        reader.innerHTML = '';
        reader.appendChild(UI.empty('alert', T('无法渲染'),
          U.esc(error.message || String(error)) + T('<br>可尝试「下载」后本地打开。')));
      }
      return { name: id };
    },
  };
}

Viewers.register(documentViewer('markdown', ['markdown'], 'api/file/html'));
Viewers.register(documentViewer('docx', ['docx'], 'api/file/html'));


/* --- 演示文稿：每页一张卡片 --- */

Viewers.register({
  id: 'slides',
  kinds: ['pptx'],
  canOpen: (meta, kind) => kind === 'pptx',
  async open(ctx) {
    const holder = U.el('div', { class: 'kb-viewer-body', style: 'flex:1;overflow:auto;padding:0 18px 40px' });
    holder.appendChild(U.el('div', { class: 'tree-empty', text: T('正在解析…') }));
    ctx.host.appendChild(holder);
    try {
      const data = await API.get('api/file/html?path=' + encodeURIComponent(ctx.meta.path));
      holder.innerHTML = '';
      if (!data.ok || !data.slides) throw new Error(data.error || T('解析失败'));
      data.slides.forEach((slide) => {
        holder.appendChild(U.el('div', { class: 'slide-card', dataset: { slide: slide.index } }, [
          U.el('div', { class: 'no', text: T('第 ') + slide.index + T(' 页') }),
          slide.title ? U.el('h3', { text: slide.title }) : null,
          U.el('div', { html: slide.html || '' }),
        ]));
      });
      ctx.info(T('页数'), data.slides.length + T(' 页'));
      ctx.note(T('演示文稿按页提取文字呈现，不还原原始版式。'), null);
      ctx.outline(data.slides.map((slide) => ({
        label: T('第 ') + slide.index + T(' 页') + (slide.title ? '：' + slide.title : ''),
        onclick: () => {
          const node = holder.querySelector('[data-slide="' + slide.index + '"]');
          if (node) node.scrollIntoView({ block: 'start' });
        },
      })));
    } catch (error) {
      holder.innerHTML = '';
      holder.appendChild(UI.empty('alert', T('无法解析'),
        U.esc(error.message || String(error)) + T('<br>可尝试「下载」后本地打开。')));
    }
    return { name: 'slides' };
  },
});


/* --- 表格：CSV / XLSX 分页 + 区域复制 --- */

Viewers.register({
  id: 'sheet',
  kinds: ['csv', 'xlsx'],
  canOpen: (meta, kind) => kind === 'csv' || kind === 'xlsx',
  async open(ctx) {
    const state = { sheet: 0, offset: 0, limit: 200, total: null, hasMore: false, data: null };
    const pageSize = 200;

    const bar = U.el('div', { class: 'kb-toolbar' });
    const sheetSel = U.el('select');
    const pageInfo = U.el('span', { class: 'meta' });
    const copyBtn = U.el('button', { class: 'icon-btn', html: Icons.svg('copy') + T(' 复制本页') });
    bar.appendChild(U.el('span', { class: 'meta', text: T('工作表') }));
    bar.appendChild(sheetSel);
    bar.appendChild(U.el('span', { class: 'spacer' }));
    bar.appendChild(pageInfo);
    bar.appendChild(copyBtn);

    const grid = U.el('div', { class: 'sheet-grid' });
    const foot = U.el('div', { class: 'sheet-foot' });
    const prev = U.el('button', { class: 'icon-btn', html: Icons.svg('chevronUp') + T(' 上一页') });
    const next = U.el('button', { class: 'icon-btn', html: Icons.svg('chevronDown') + T(' 下一页') });
    const label = U.el('span', { class: 'meta' });
    foot.appendChild(prev); foot.appendChild(next);
    foot.appendChild(U.el('span', { class: 'spacer' }));
    foot.appendChild(label);

    const shell = U.el('div', { class: 'sheet-host' }, [bar, grid, foot]);
    ctx.host.appendChild(shell);
    grid.appendChild(U.el('div', { class: 'tree-empty', text: T('正在读取…') }));

    async function loadSheet() {
      try {
        const data = await API.get('api/file/sheet?path='
          + encodeURIComponent(ctx.meta.path)
          + '&sheet=' + state.sheet
          + '&offset=' + state.offset + '&limit=' + state.limit);
        if (!data.ok) throw new Error(data.error || T('无法读取表格'));
        state.data = data;
        state.total = data.total_rows;
        state.hasMore = !!data.has_more;
        if (data.sheets && !sheetSel.options.length) {
          data.sheets.forEach((sheet) => {
            sheetSel.appendChild(U.el('option', { value: String(sheet.index), text: sheet.name }));
          });
          sheetSel.value = String(state.sheet);
          if (data.sheets.length < 2) sheetSel.disabled = true;
        }
        draw();
      } catch (error) {
        grid.innerHTML = '';
        grid.appendChild(UI.empty('alert', T('无法读取表格'),
          U.esc(error.message || String(error)) + T('<br>可尝试「下载」后本地打开。')));
      }
    }

    function draw() {
      const data = state.data;
      grid.innerHTML = '';
      const table = U.el('table');
      const thead = U.el('thead');
      const headRow = U.el('tr', {}, [U.el('th', { class: 'rownum', text: '#' })]);
      (data.columns || []).forEach((column) => {
        headRow.appendChild(U.el('th', { text: String(column) }));
      });
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = U.el('tbody');
      (data.rows || []).forEach((row) => {
        const tr = U.el('tr', {}, [U.el('td', { class: 'rownum', text: String(row.r) })]);
        (row.c || []).forEach((cell) => {
          tr.appendChild(U.el('td', { text: cell, title: String(cell) }));
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      grid.appendChild(table);

      const from = data.rows && data.rows.length ? data.rows[0].r : 0;
      const to = data.rows && data.rows.length ? data.rows[data.rows.length - 1].r : 0;
      label.textContent = to
        ? (T('第 ') + from + '–' + to + T(' 行') + (state.total ? T(' / 共 ') + state.total + T(' 行') : ''))
        : T('没有数据');
      pageInfo.textContent = state.total ? (T('共 ') + state.total + T(' 行')) : '';
      prev.disabled = state.offset <= 0;
      next.disabled = !state.hasMore;
      if (data.encoding) ctx.info(T('编码'), data.encoding);
      ctx.info(T('列数'), String((data.columns || []).length));
      if (state.total) ctx.info(T('行数'), String(state.total));
      else ctx.note(T('本机未读取到工作表的精确行数，翻到末尾即可确认。'), null);
    }

    prev.addEventListener('click', () => {
      state.offset = Math.max(0, state.offset - pageSize); loadSheet();
    });
    next.addEventListener('click', () => {
      state.offset += pageSize; loadSheet();
    });
    sheetSel.addEventListener('change', () => {
      state.sheet = Number(sheetSel.value) || 0;
      state.offset = 0; state.total = null; state.hasMore = false;
      loadSheet();
    });
    copyBtn.addEventListener('click', async () => {
      const data = state.data;
      if (!data || !data.rows || !data.rows.length) return;
      const lines = [data.columns.join('\t')]
        .concat(data.rows.map((row) => row.c.map((c) => String(c)).join('\t')));
      try {
        await navigator.clipboard.writeText(lines.join('\n'));
        UI.ok(T('已复制本页 ') + data.rows.length + T(' 行'));
      } catch (error) {
        UI.warn(T('浏览器未允许写入剪贴板，请手动选择后复制'));
      }
    });

    await loadSheet();
    return { name: 'sheet' };
  },
});


/* --- 文本 / 代码 / JSON / XML / YAML --- */

Viewers.register({
  id: 'code',
  kinds: ['text', 'code', 'json', 'xml', 'yaml'],
  canOpen: (meta, kind) => ['text', 'code', 'json', 'xml', 'yaml'].indexOf(kind) >= 0,
  async open(ctx) {
    const state = { offset: 0, text: '', wrap: false, lang: '' };
    const bar = U.el('div', { class: 'kb-toolbar' });
    const wrapBtn = U.el('button', { class: 'icon-btn', html: Icons.svg('type') + T(' 自动换行') });
    const findInput = U.el('input', { type: 'search', placeholder: T('在此文件中查找…') });
    const findOut = U.el('span', { class: 'meta' });
    bar.appendChild(wrapBtn);
    bar.appendChild(U.el('span', { class: 'spacer' }));
    bar.appendChild(findInput);
    bar.appendChild(findOut);

    const wrap = U.el('div', { class: 'code-wrap' });
    const shell = U.el('div', { style: 'height:100%;display:flex;flex-direction:column' }, [bar, wrap]);
    ctx.host.appendChild(shell);

    async function load(more) {
      try {
        const data = await API.get('api/file/text?path='
          + encodeURIComponent(ctx.meta.path)
          + '&offset=' + state.offset + '&limit=' + (256 * 1024));
        if (!data.ok) throw new Error(data.error || T('无法读取文件'));
        state.text = more ? state.text + data.text : data.text;
        state.lang = ctx.meta.lang || data.lang || '';
        if (data.encoding) ctx.info(T('编码'), data.encoding);
        if (data.truncated && !more) {
          ctx.note(T('这是文件的开始部分。点「继续读取」可加载更多。'), null);
        }
        draw(more);
        if (data.has_more) {
          state.offset = data.next_offset;
          if (!bar.querySelector('.more')) {
            const more = U.el('button', { class: 'icon-btn more', text: T('继续读取…') });
            more.addEventListener('click', () => load(true));
            bar.insertBefore(more, findInput);
          }
        } else {
          const more = bar.querySelector('.more');
          if (more) more.remove();
        }
      } catch (error) {
        wrap.innerHTML = '';
        wrap.appendChild(UI.empty('alert', T('无法读取'),
          U.esc(error.message || String(error))));
      }
    }

    function draw(append) {
      if (!append) wrap.innerHTML = '';
      if (append) {
        // 追加时只渲染新增部分，避免重排整个大文件
        const existing = wrap.querySelector('table');
        const startLine = existing ? existing.querySelectorAll('tr').length : 0;
        const lines = state.text.split('\n');
        renderCode(lines.slice(startLine).join('\n'), state.lang, wrap);
        return;
      }
      renderCode(state.text, state.lang, wrap);
      if (state.wrap) applyWrap();
    }

    function applyWrap() {
      wrap.querySelectorAll('.code-table td.line').forEach((node) => {
        node.style.whiteSpace = state.wrap ? 'pre-wrap' : 'pre';
        node.style.wordBreak = state.wrap ? 'break-word' : 'normal';
      });
    }

    wrapBtn.addEventListener('click', () => {
      state.wrap = !state.wrap;
      wrapBtn.classList.toggle('on', state.wrap);
      applyWrap();
    });

    findInput.addEventListener('input', () => {
      const needle = findInput.value.trim();
      wrap.querySelectorAll('mark').forEach((node) => {
        const parent = node.parentNode;
        parent.replaceChild(document.createTextNode(node.textContent), node);
        parent.normalize();
      });
      if (!needle) { findOut.textContent = ''; return; }
      let hits = 0;
      const re = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      wrap.querySelectorAll('.code-table td.line').forEach((cell) => {
        const text = cell.textContent;
        if (!re.test(text)) return;
        re.lastIndex = 0;
        cell.textContent = '';
        let last = 0;
        let match = re.exec(text);
        while (match) {
          cell.appendChild(document.createTextNode(text.slice(last, match.index)));
          const mark = document.createElement('mark');
          mark.textContent = match[0];
          cell.appendChild(mark);
          last = match.index + match[0].length;
          hits += 1;
          match = re.exec(text);
        }
        cell.appendChild(document.createTextNode(text.slice(last)));
      });
      findOut.textContent = hits ? hits + T(' 处命中') : T('未找到');
    });

    await load(false);
    return { name: 'code' };
  },
});


/* --- 音频 / 视频：浏览器原生播放器（Range 让拖动进度可用） --- */

Viewers.register({
  id: 'media',
  kinds: ['audio', 'video'],
  canOpen: (meta, kind) => kind === 'audio' || kind === 'video',
  open(ctx) {
    const kind = ctx.meta.kind;
    const node = kind === 'audio'
      ? U.el('audio', { controls: true, src: ctx.url(), preload: 'metadata' })
      : U.el('video', { controls: true, playsinline: true, src: ctx.url(), preload: 'metadata' });
    ctx.host.appendChild(U.el('div', { class: 'media-host' }, [node]));
    ctx.note(T('播放器由浏览器提供。若该编码不被浏览器支持，可「下载」后用本地播放器打开。'), null);
    return { name: 'media', destroy() { try { node.pause(); } catch (error) { /* 忽略 */ } } };
  },
});


/* --- 目录：列出内容，点击进入 --- */

Viewers.register({
  id: 'dir',
  kinds: [],
  canOpen: (meta) => !!meta.is_dir,
  async open(ctx) {
    const list = U.el('div', { style: 'padding:14px 16px' });
    ctx.host.appendChild(list);
    list.appendChild(U.el('div', { class: 'tree-empty', text: T('正在读取…') }));
    try {
      const kbId = ctx.meta.kb_id;
      const rel = ctx.meta.rel_path;
      const data = await API.get('api/kb/' + kbId + '/tree?parent=' + encodeURIComponent(rel));
      list.innerHTML = '';
      if (!data.entries || !data.entries.length) {
        list.appendChild(UI.empty('inbox', T('这个目录是空的'), T('里面没有已收录的文件。')));
        return { name: 'dir' };
      }
      const ul = U.el('ul', { class: 'info-list' });
      data.entries.forEach((entry) => {
        const li = U.el('li', {}, [
          U.el('span', { html: Icons.svg(entry.is_dir ? 'folder' : entry.icon.value, { size: 15 }) }),
          U.el('span', { class: 'n', text: entry.name }),
          U.el('span', { class: 'r', text: entry.is_dir ? T('目录') : U.size(entry.size) }),
        ]);
        li.addEventListener('click', () => {
          if (ctx.hook.open) ctx.hook.open(entry.path, entry);
        });
        ul.appendChild(li);
      });
      list.appendChild(ul);
      ctx.info(T('条目数'), String(data.entries.length));
    } catch (error) {
      list.innerHTML = '';
      list.appendChild(UI.empty('alert', T('无法读取目录'), U.esc(error.message || String(error))));
    }
    return { name: 'dir' };
  },
});
