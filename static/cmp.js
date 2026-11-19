/* ============================================================
   速图AIGC · 通用「原图 / 成品」并排对比组件（cmp.js）
   四板块共用：pt 图案提取 / wm 批量去水印 / hd 高清修复 / duotu 多图编辑
   零依赖、无模态、可多张并排；按钮在卡片操作区，点击后「图区原地」切成左右两格。

   —— 用法（推荐 · 零 JS）——
   1) 把图区容器加上 class="cmp-host"：
        <div class="prev cmp-host"><img src="...thumb..."></div>
   2) 在卡片操作区（图区之外）放一个按钮：
        <button class="cmp-btn" data-cmp
                data-src="原图URL" data-out="成品URL">⇄ 对比原图</button>
      可选：data-fit="contain|cover|fill"（成品格，默认 contain）
            data-fit-src="contain|cover|fill"（原图格，默认 contain=整图可见）
            data-lab-src="原图"  data-lab-out="成品"
            data-host="#某选择器"（跨层级时显式指定图区）

   —— 用法（编程式）——
     PTCompare.bind(btnEl, {src, out, host, fit, labSrc, labOut});
     PTCompare.toggle(btnEl);            // 开/收
     PTCompare.open(hostEl, {src, out}); // 直接对图区开
     PTCompare.close(hostEl);            // 收
     PTCompare.zoom({src, out});         // 全屏灯箱：两张并排铺满屏幕
     PTCompare.zoom({src, out, tight:true});  // 紧凑：按真实比例等高、零缝隙紧贴
     PTCompare.closeZoom();

   —— 全屏放大预览（2026-10-14 加）——
     就地并排受卡片宽度限制（每格只有卡片一半宽），看细节不够。点击并排图区任意一格
     （或任意挂 data-zoom 的元素，读同样的 data-src / data-out）→ 弹出全屏灯箱，
     原图 / 成品 两格铺满视口，右下角显示真实像素尺寸，Esc / 点空白 / 点图片 关闭。

   —— 紧凑模式（2026-10-15 加 · opt-in）——
     灯箱默认两格等宽（flex:1 1 0），窄图会被撑出一大圈空白、中间还有 10px 缝。
     传 tight:true（或元素挂 data-tight）后改成「按每张图真实宽高比、等高、零缝隙」拼接：
     两图按 a=w/h 分宽 → 并成一块整体居中 → 外圈 1px 描边收边、中间绝不描边（一描就是缝）。
     默认关闭：pt / wm / hd / duotu 四板块不传参，行为与之前一字不差。
     配套：图源若已由后端 /api/trim 裁过白边，这里量到的 naturalWidth/Height 就是裁后尺寸。
     2026-10-15b 修：首开尺寸 0×0。旧版只等 img 的 load，而大图（线上常见 2048×4516）
     完整解码要好几秒，在此之前舞台一直是 0×0；改为「naturalWidth 一就绪就排 + 50ms 轮询兜底」。

  铁律（勿改）：
  · 2026-10-14 起由「滑块」改成「并排」：左格=原图、右格=成品，同屏同尺寸对照。
    滑块时代一次只看得到每张图的半幅，用户实测「根本没法比」。
  · 两格等宽（flex:1 1 0），格高由「原图/成品里较大的高宽比」锁定 → 两张都完整可见、都不裁切；
    两张图各自 object-fit:contain 居中（滑块时代原图用 cover 对齐，并排下会裁掉原图，故弃用）。
    —— 例外：灯箱传 tight:true 时改为「按真实宽高比分配宽高、零缝隙」（素材库在用），
    主流程（就地并排的 .cmp-stage）与四板块一律不受影响。
  · 格高有上限 min(80vh, 1040px)：9:16 长图不会被撑成一根竹竿。
  · 图区若处在 CSS Grid 里（hd 任务卡 112px 缩略图格 / duotu 结果卡 250px），
    展开时自动把所在栅格项拉成整行 —— 否则并排后每格比原来还小。
  · 收起时原样恢复图区 innerHTML（各板块 re-render 时状态自然销毁）。
  · 键盘 Esc / 双击图区收回；单击图区 = 全屏并排放大（两张一起放大，不是单张）。
  ============================================================ */
(function () {
  "use strict";
  if (window.PTCompare) return;

  /* 组件版本：排障第一抓手。用户在控制台敲 `PTCompare.VERSION` 即可知道
     自己浏览器到底加载了哪一版 —— 2026-10-14 雪碧两次反馈「对比还是只有一张图」，
     服务端早已是新版，全是本地缓存旧文件；有版本号后一眼可判，不用再猜。 */
  var VERSION = "20261015b-tight";
  try { console.info("[PTCompare] 已加载 v" + VERSION); } catch (e) { }

  /* ---------- 样式（自注入，避免各页重复维护） ---------- */
  var CSS = [
    '.cmp-host{position:relative}',
    '.cmp-stage{position:relative;display:flex;gap:8px;width:100%;height:340px;outline:none}',
    '.cmp-stage:focus-visible{outline:2px solid rgba(13,148,136,.45);outline-offset:2px}',
    '.cmp-cell{position:relative;flex:1 1 0;min-width:0;height:100%;overflow:hidden;border-radius:var(--r-sm,8px);',
    'background:var(--card,#fff);box-shadow:inset 0 0 0 1px var(--bd,#e5e7eb)}',
    '.cmp-stage .cmp-cell img.cmp-img{position:absolute;inset:0;width:100%;height:100%;max-width:none;max-height:none;',
    'object-fit:contain;display:block;margin:0;padding:0;border:0;border-radius:0;background:transparent;flex:none;',
    'pointer-events:none;-webkit-user-drag:none;-webkit-user-select:none;user-select:none}',
    '.cmp-tag{position:absolute;top:8px;left:8px;z-index:2;font-size:11px;line-height:1;font-weight:700;padding:4px 7px;',
    'border-radius:var(--r-pill,999px);background:rgba(17,24,39,.68);color:#fff;pointer-events:none;letter-spacing:.02em}',
    '.cmp-tag.out{background:rgba(13,148,136,.92)}',
    '.cmp-load{position:absolute;inset:0;z-index:3;display:flex;align-items:center;justify-content:center;',
    'border-radius:var(--r-sm,8px);background:var(--fill,#f9fafb);color:var(--sub,#6b7280);font-size:12.5px}',
    '.cmp-stage.ready .cmp-load{display:none}',
    '.cmp-btn{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;font-weight:600;cursor:pointer;',
    'padding:7px 11px;border-radius:var(--r-ctl,10px);border:1px solid var(--pri-200,#99f6e4);',
    'background:var(--pri-050,#f0fdfa);color:var(--pri-dark,#0f766e);-webkit-tap-highlight-color:transparent;',
    'font-family:inherit;white-space:nowrap}',
    '.cmp-btn:hover{background:var(--pri-100,#ccfbf1);border-color:var(--pri-300,#5eead4)}',
    '.cmp-btn.on{background:var(--grad-pri,linear-gradient(135deg,#0d9488,#14b8a6));border-color:transparent;color:#fff}',
    '.cmp-btn.on:hover{opacity:.92}',
    '.cmp-btn[disabled]{opacity:.5;cursor:not-allowed}',
    /* ---- 全屏放大预览（灯箱）：点并排图区任意一格 → 两张同时铺满屏幕 ---- */
    '.cmp-zoom{position:absolute;right:8px;bottom:8px;z-index:3;font-size:11px;font-weight:700;padding:4px 8px;',
    /* 常显（opacity .88）：并排展开后，卡片里原来那颗「🔍 放大对比」链接会被 stage 替换掉，
       若角标再藏到 hover 才出现，用户就完全看不到「还能放大」的入口 → 触屏尤其致命。 */
    'border-radius:var(--r-pill,999px);background:rgba(17,24,39,.72);color:#fff;pointer-events:none;opacity:.88;transition:.15s}',
    '.cmp-cell:hover .cmp-zoom{opacity:1;background:rgba(17,24,39,.92)}',
    '.cmp-lb{position:fixed;inset:0;z-index:2147483000;background:rgba(9,12,18,.92);display:flex;',
    'flex-direction:column;align-items:center;justify-content:center;padding:16px;gap:10px}',
    '.cmp-lb-stage{display:flex;gap:10px;width:100%;height:calc(100% - 44px);align-items:stretch}',
    '.cmp-lb-cell{position:relative;flex:1 1 0;min-width:0;height:100%;overflow:hidden;border-radius:10px;',
    'background:#0b1220;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}',
    '.cmp-lb-cell img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block;',
    'cursor:zoom-out;-webkit-user-drag:none;-webkit-user-select:none;user-select:none}',
    '.cmp-lb-tag{position:absolute;top:10px;left:10px;z-index:2;font-size:12px;font-weight:700;padding:5px 10px;',
    'border-radius:var(--r-pill,999px);background:rgba(17,24,39,.7);color:#fff;letter-spacing:.02em}',
    '.cmp-lb-tag.out{background:rgba(13,148,136,.94)}',
    '.cmp-lb-meta{position:absolute;bottom:10px;right:10px;z-index:2;font-size:11px;padding:4px 9px;',
    'border-radius:var(--r-pill,999px);background:rgba(17,24,39,.62);color:#d1d5db;font-variant-numeric:tabular-nums}',
    '.cmp-lb-bar{flex:0 0 auto;display:flex;align-items:center;gap:14px;color:#9ca3af;font-size:12px;flex-wrap:wrap;justify-content:center}',
    '.cmp-lb-x{position:absolute;top:12px;right:14px;z-index:6;width:34px;height:34px;border:0;border-radius:50%;',
    'cursor:pointer;background:rgba(255,255,255,.14);color:#fff;font-size:17px;line-height:1;font-family:inherit}',
    '.cmp-lb-x:hover{background:rgba(255,255,255,.28)}',
    /* ---- 紧凑模式（opt-in）---- */
    '.cmp-lb.tight{padding:14px 10px}',
    '.cmp-lb-stage.tight{width:auto;height:auto;min-height:0;gap:0;align-items:center;justify-content:center;',
    'border-radius:3px;background:#0b1220;box-shadow:0 0 0 1px rgba(255,255,255,.18)}',
    '.cmp-lb-stage.tight .cmp-lb-cell{flex:0 0 auto;border-radius:0;box-shadow:none;background:transparent}',
    '@media (max-width:760px){.cmp-lb-stage{flex-direction:column;height:calc(100% - 60px)}}'
  ].join('');

  function injectCSS() {
    if (document.getElementById('pt-cmp-css')) return;
    var s = document.createElement('style');
    s.id = 'pt-cmp-css';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  var GAP = 8;   /* 与 CSS .cmp-stage gap 保持一致（算格高用） */

  /* ---------- 工具 ---------- */
  function preload(url) {
    return new Promise(function (res) {
      if (!url) return res({ ok: false });
      var im = new Image();
      im.onload = function () { res({ ok: true, w: im.naturalWidth || 0, h: im.naturalHeight || 0 }); };
      im.onerror = function () { res({ ok: false }); };
      im.src = url;
    });
  }

  function pick(o, k, d) { return (o && o[k] !== undefined && o[k] !== null && o[k] !== '') ? o[k] : d; }

  // 图区定位：显式指定 > 从按钮向上找最近的 .cmp-host
  function resolveHost(btn, opt) {
    if (opt && opt.host) {
      if (typeof opt.host === 'string') { try { return document.querySelector(opt.host); } catch (e) { return null; } }
      return opt.host;
    }
    if (btn && btn.getAttribute) {
      var sel = btn.getAttribute('data-host');
      if (sel) { try { var e0 = document.querySelector(sel); if (e0) return e0; } catch (e) { } }
    }
    var el = btn && btn.parentElement, i = 0;
    while (el && i++ < 9) {
      var h = el.querySelector ? el.querySelector('.cmp-host') : null;
      if (h) return h;
      el = el.parentElement;
    }
    return null;
  }

  function toBool(v) {
    return v === true || v === 1 || v === '' || v === '1' || v === 'true' || v === 'yes';
  }

  /* tight（紧凑紧贴）取值顺序：显式 override > 元素 data-tight > bind 时登记。
     data-tight 是「布尔属性」写法（无值即开），空串必须当 true，故不能走 pick（pick 把 '' 当缺省）。 */
  function tightOf(btn, override) {
    if (override && override.tight !== undefined && override.tight !== null
      && override.tight !== '') return toBool(override.tight);
    if (btn && btn.hasAttribute && btn.hasAttribute('data-tight')) {
      return toBool(btn.getAttribute('data-tight'));
    }
    var base = (btn && btn.__cmp) || {};
    return base.tight === true;
  }

  function optsOf(btn, override) {
    var ds = (btn && btn.dataset) || {};
    var base = (btn && btn.__cmp) || {};
    var o = {
      src: pick(override, 'src', pick(ds, 'src', base.src)),
      out: pick(override, 'out', pick(ds, 'out', base.out)),
      fit: pick(override, 'fit', pick(ds, 'fit', base.fit || 'contain')),
      fitSrc: pick(override, 'fitSrc', pick(ds, 'fitSrc', base.fitSrc || 'contain')),
      labSrc: pick(override, 'labSrc', pick(ds, 'labSrc', base.labSrc || '原图')),
      labOut: pick(override, 'labOut', pick(ds, 'labOut', base.labOut || '成品')),
      host: pick(override, 'host', pick(ds, 'host', base.host)),
      tight: tightOf(btn, override)
    };
    return o;
  }

  // 成品层兜底：按钮没写 data-out 时，直接取图区当前那张 <img> 的 src
  // （批量看板里图区会随「换版」变，这样不需要同步维护按钮属性）
  function deriveOut(host, o) {
    if (o.out) return o.out;
    var im = host && host.querySelector ? host.querySelector('img') : null;
    if (im) return im.getAttribute('src') || im.src || '';
    return '';
  }

  // 展开时把「图区所在的 Grid 栅格项」拉成整行（hd / duotu 的窄卡片必需）
  function widen(host, on) {
    if (on) {
      var el = host, i = 0;
      while (el && i++ < 4) {
        var p = el.parentElement;
        if (!p) break;
        var d = '';
        try { d = (window.getComputedStyle ? getComputedStyle(p).display : '') || ''; } catch (e) { d = ''; }
        if (d.indexOf('grid') >= 0) {
          host.__cmpGrid = el;
          el.__cmpGc = el.style.gridColumn || '';
          el.style.gridColumn = '1 / -1';
          return;
        }
        el = p;
      }
    } else if (host.__cmpGrid) {
      var g = host.__cmpGrid;
      try { g.style.gridColumn = g.__cmpGc || ''; } catch (e) { }
      host.__cmpGrid = null;
    }
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* ---------- 全屏灯箱：原图 / 成品 两张并排铺满屏幕 ----------
     2026-10-14 雪碧第三次反馈：「缩略图对比是放在一起了，但点开放大预览也要两张同时放大」。
     就地并排受卡片宽度限制（每格只有卡片的一半），看细节不够 —— 灯箱把两格拉到整屏，
     且直接吃组件里已有的 src/out（pt 页这两个 URL 本来就是全尺寸 flink，不是缩略图）。 */
  var lbEl = null;

  function lbCell(url, lab, kind) {
    var c = document.createElement('div');
    c.className = 'cmp-lb-cell' + (kind === 'out' ? ' out' : '');
    var im = document.createElement('img');
    im.className = 'cmp-img';
    im.alt = lab || (kind === 'out' ? '成品' : '原图');
    im.decoding = 'async';
    im.draggable = false;
    var tg = document.createElement('div');
    tg.className = 'cmp-lb-tag' + (kind === 'out' ? ' out' : '');
    tg.textContent = lab || (kind === 'out' ? '成品' : '原图');
    var mt = document.createElement('div');
    mt.className = 'cmp-lb-meta';
    mt.textContent = '加载中…';
    im.onload = function () { mt.textContent = (im.naturalWidth || '?') + ' × ' + (im.naturalHeight || '?'); };
    im.onerror = function () { mt.textContent = '加载失败'; };
    im.src = url;
    c.appendChild(im); c.appendChild(tg); c.appendChild(mt);
    return c;
  }

  function zoomClose() {
    if (!lbEl) return;
    var el = lbEl; lbEl = null;
    try { document.removeEventListener('keydown', el.__onKey, true); } catch (e) { }
    if (el.__onResize) { try { window.removeEventListener('resize', el.__onResize); } catch (e) { } }
    if (el.parentNode) el.parentNode.removeChild(el);
  }

  /* 紧凑排版：两张图按各自真实宽高比 a=w/h 分宽、等高、零缝隙并成「一块整体」再居中。
     为什么不用 flex-grow 近似：flex-grow 只在「容器宽 = 格高 × 两图 a 之和」时才刚好填满，
     其余情况格内仍留白。这里直接算像素尺寸，保证「图 = 格」，一格空白都不多留。 */
  function fitTight(lb, stage, cells) {
    var ims = [], n = [], i;
    for (i = 0; i < cells.length; i++) ims.push(cells[i].querySelector('img'));
    for (i = 0; i < ims.length; i++) {
      var w = ims[i].naturalWidth || 0, h = ims[i].naturalHeight || 0;
      if (!w || !h) return;                 /* 任一张还没量到尺寸 → 先不动，等 onload 再排 */
      n.push(w / h);
    }
    var bar = lb.querySelector('.cmp-lb-bar');
    var avW = Math.max(120, lb.clientWidth - 20);                                  /* .cmp-lb.tight 左右内边距 */
    var avH = Math.max(120, lb.clientHeight - 28 - (bar ? bar.offsetHeight : 0));  /* 上下内边距 + 底栏 + 间隔 */
    var vert = (window.innerWidth || 0) <= 760;      /* 窄屏：CSS 媒体查询本来就把舞台改成竖排 */
    var sum = n[0] + n[1], dims;
    if (!vert) {
      var H = Math.max(80, Math.min(avH, avW / sum));
      dims = [[Math.round(H * n[0]), Math.round(H)], [Math.round(H * n[1]), Math.round(H)]];
    } else {
      var s = Math.min(1, avH / (avW / n[0] + avW / n[1]));
      var W = Math.round(avW * s);
      dims = [[W, Math.round(W / n[0])], [W, Math.round(W / n[1])]];
    }
    stage.style.flexDirection = vert ? 'column' : 'row';
    stage.style.width = Math.round(vert ? dims[0][0] : dims[0][0] + dims[1][0]) + 'px';
    stage.style.height = Math.round(vert ? dims[0][1] + dims[1][1] : dims[0][1]) + 'px';
    for (i = 0; i < cells.length; i++) {
      cells[i].style.width = dims[i][0] + 'px';
      cells[i].style.height = dims[i][1] + 'px';
    }
  }

  function zoom(o) {
    if (!o || !o.src || !o.out) return;   /* 缺一张不开：单张放大会丢失「并排」的意义 */
    zoomClose();
    injectCSS();
    var ls = o.labSrc || '原图', lo = o.labOut || '成品';
    var tight = !!o.tight;
    var lb = document.createElement('div');
    lb.className = 'cmp-lb' + (tight ? ' tight' : '');
    lb.setAttribute('role', 'dialog');
    lb.setAttribute('aria-label', ls + ' / ' + lo + ' 放大对比');

    var stage = document.createElement('div');
    stage.className = 'cmp-lb-stage' + (tight ? ' tight' : '');
    var c1 = lbCell(o.src, ls, 'src');
    var c2 = lbCell(o.out, lo, 'out');
    stage.appendChild(c1);
    stage.appendChild(c2);
    var cells = [c1, c2];

    var x = document.createElement('button');
    x.type = 'button'; x.className = 'cmp-lb-x'; x.textContent = '✕';
    x.setAttribute('aria-label', '关闭放大预览');
    x.onclick = function (e) { e.stopPropagation(); zoomClose(); };

    var bar = document.createElement('div');
    bar.className = 'cmp-lb-bar';
    bar.innerHTML = '<span>左：<b style="color:#e5e7eb">' + esc(ls) + '</b> · 右：<b style="color:#5eead4">'
      + esc(lo) + '</b>' + (tight ? '<span style="color:#6b7280"> · 已裁掉四周空白</span>' : '') + '</span>'
      + '<span>Esc / 点空白处 / 点图片 关闭</span>';

    lb.appendChild(stage); lb.appendChild(bar); lb.appendChild(x);
    lb.addEventListener('click', function (e) {
      var t = e.target;
      /* 遮罩 / 舞台空隙 / 底栏 / 图片本身 → 关闭；✕ 自己走 onclick（已 stopPropagation） */
      if (t === lb || t === stage || t === bar || (t && t.tagName === 'IMG')) zoomClose();
    });
    var onKey = function (e) {
      if (e.key === 'Escape') { zoomClose(); e.preventDefault(); e.stopPropagation(); }
    };
    lb.__onKey = onKey;
    document.addEventListener('keydown', onKey, true);
    (document.body || document.documentElement).appendChild(lb);
    lbEl = lb;

    if (tight) {
      var fit = function () { try { fitTight(lb, stage, cells); } catch (e) { } };
      /* 铁律：不能只等 img 的 load —— 大图（线上常见 2048×4516）完整解码要好几秒，
         load 前舞台一直是 0×0（旧版就死在这）。naturalWidth 在「元数据解析完」就有，
         所以这里改为「尺寸一就绪立即排」+ 50ms 短轮询兜底（上限 ~10s），load 只作加速。 */
      var dimsReady = function () {
        for (var i = 0; i < cells.length; i++) {
          var im = cells[i].querySelector('img');
          if (!im || !im.naturalWidth || !im.naturalHeight) return false;
        }
        return true;
      };
      var settled = false, tries = 0;
      var tick = function () {
        fit();
        if (settled) return;
        if (dimsReady()) { settled = true; fit(); return; }     /* 再排一次：拿到最终尺寸 */
        if (++tries < 200) setTimeout(tick, 50);
      };
      cells.forEach(function (c) {
        var im = c.querySelector('img');
        if (!im) return;
        var hit = function () {
          im.removeEventListener('load', hit);
          im.removeEventListener('error', hit);
          if (settled) return;
          fit();
          if (dimsReady()) settled = true;    /* 只有两张都就绪才算定稿；否则继续交给轮询 */
        };
        im.addEventListener('load', hit);
        im.addEventListener('error', hit);
      });
      lb.__onResize = function () {
        fit();
        if (!settled) { tries = 0; tick(); }                    /* 轮询已放弃时，窗口变化可重新唤起 */
      };
      window.addEventListener('resize', lb.__onResize);
      tick();
    }
    try { x.focus(); } catch (e) { }
  }

  function mkImg(src, alt, fit, cls) {
    var im = document.createElement('img');
    im.className = 'cmp-img ' + cls;
    im.alt = alt;
    im.draggable = false;
    im.decoding = 'async';
    im.style.objectFit = fit || 'contain';
    im.src = src;
    return im;
  }

  /* ---------- 核心：构建并排两格 ---------- */
  function buildStage(o, host, btn) {
    var stage = document.createElement('div');
    stage.className = 'cmp-stage';
    stage.tabIndex = 0;
    stage.setAttribute('role', 'group');
    stage.setAttribute('aria-label', o.labSrc + ' / ' + o.labOut + ' 并排对比');

    /* 左格 = 原图 */
    var cellS = document.createElement('div');
    cellS.className = 'cmp-cell cmp-cell-src';
    cellS.appendChild(mkImg(o.src, o.labSrc, o.fitSrc, 'cmp-src'));
    var tagS = document.createElement('div');
    tagS.className = 'cmp-tag'; tagS.textContent = o.labSrc;
    cellS.appendChild(tagS);
    var zs = document.createElement('div');
    zs.className = 'cmp-zoom'; zs.textContent = '⤢ 点击放大';
    cellS.appendChild(zs);

    /* 右格 = 成品 */
    var cellO = document.createElement('div');
    cellO.className = 'cmp-cell cmp-cell-out';
    cellO.appendChild(mkImg(o.out, o.labOut, o.fit, 'cmp-out'));
    var tagO = document.createElement('div');
    tagO.className = 'cmp-tag out'; tagO.textContent = o.labOut;
    cellO.appendChild(tagO);
    var zo = document.createElement('div');
    zo.className = 'cmp-zoom'; zo.textContent = '⤢ 点击放大';
    cellO.appendChild(zo);

    var load = document.createElement('div');
    load.className = 'cmp-load'; load.textContent = '加载对比图…';

    stage.appendChild(cellS);
    stage.appendChild(cellO);
    stage.appendChild(load);

    /* --- 格高：按两张图里较大的高宽比锁定，含上限 --- */
    var ratio = 0;   /* 拿到图片尺寸前按 3:4 兜底 */
    function refit() {
      var w = stage.clientWidth || (host && host.clientWidth) || 0;
      if (!w) return;
      var cellW = (w - GAP) / 2;
      if (cellW < 40) cellW = 40;
      var h = cellW * (ratio || (4 / 3));
      var maxH = Math.min(Math.round(window.innerHeight * 0.80), 1040);
      if (maxH < 220) maxH = 220;
      if (h > maxH) h = maxH;
      if (h < 160) h = 160;
      var hs = Math.round(h) + 'px';
      if (stage.style.height !== hs) stage.style.height = hs;
    }
    function takeRatio(r) {
      if (!r || !r.ok || !r.w || !r.h) return false;
      var v = r.h / r.w;
      if (v > ratio) { ratio = v; return true; }
      return false;
    }
    refit();

    /* 容器宽度变化（窗口缩放 / iframe 尺寸变 / 侧栏收起）重新算格高 */
    var onWinResize = function () { refit(); };
    window.addEventListener('resize', onWinResize);
    var ro = null;
    if (window.ResizeObserver) {
      try {
        ro = new ResizeObserver(function () { refit(); });
        ro.observe(stage);
      } catch (e) { ro = null; }
    }
    stage.__cleanup = function () {
      window.removeEventListener('resize', onWinResize);
      if (ro) { try { ro.disconnect(); } catch (e) { } }
    };
    stage.__refit = refit;   /* widen() 改完栅格宽度后要再算一次（RO 兜底之外的第一手） */

    Promise.all([preload(o.out), preload(o.src)]).then(function (rs) {
      var a = takeRatio(rs[0]), b = takeRatio(rs[1]);
      if (a || b) refit();
      stage.classList.add('ready');
    });

    /* 单击 = 放大预览（两张并排）；双击 = 收起对比（保留原交互）。
       单击延时 260ms 等一等，双击到来就取消，避免「双击收起」顺带弹出灯箱。 */
    var clickT = null;
    stage.addEventListener('click', function (e) {
      e.stopPropagation();   /* 图区内的点击不冒泡到卡片（否则会被当选版 / 开大图） */
      if (clickT) return;
      clickT = setTimeout(function () { clickT = null; zoom(o); }, 260);
    });
    stage.addEventListener('dblclick', function (e) {
      e.preventDefault();
      if (clickT) { clearTimeout(clickT); clickT = null; }
      close(host, btn);
    });
    stage.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(host, btn); e.preventDefault(); }
    });

    return stage;
  }

  /* ---------- 开 / 收 ---------- */
  function open(host, o, btn) {
    if (!host || host.__cmpOpen) return;
    o.out = deriveOut(host, o);
    if (!o.src || !o.out) return;   // 缺原图或缺成品 → 不展开（按钮应由各板块自行隐藏）
    injectCSS();
    host.__cmpBackupHTML = host.innerHTML;
    host.__cmpOpen = true;
    host.classList.add('cmp-open');
    host.innerHTML = '';
    var st = buildStage(o, host, btn);
    host.__cmpStageEl = st;
    host.appendChild(st);
    widen(host, true);
    if (st.__refit) { try { st.__refit(); } catch (e) { } }
    setBtn(btn, true);
  }

  function close(host, btn) {
    if (!host || !host.__cmpOpen) return;
    host.__cmpOpen = false;
    host.classList.remove('cmp-open');
    widen(host, false);
    if (host.__cmpStageEl && host.__cmpStageEl.__cleanup) {
      try { host.__cmpStageEl.__cleanup(); } catch (e) { }
    }
    host.__cmpStageEl = null;
    if (host.__cmpBackupHTML != null) {
      host.innerHTML = host.__cmpBackupHTML;
      host.__cmpBackupHTML = null;
    }
    setBtn(btn, false);
  }

  function setBtn(btn, on) {
    if (!btn || !btn.classList) return;
    btn.classList.toggle('on', !!on);
    var lab = btn.getAttribute('data-label') || '⇄ 对比原图';
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.innerHTML = on ? '« 收起对比' : lab;
  }

  function findBtn(host) {
    var el = host, i = 0;
    while (el && i++ < 6) {
      var b = el.querySelector ? el.querySelector('.cmp-btn[data-cmp],.cmp-btn') : null;
      if (b) return b;
      el = el.parentElement;
    }
    return null;
  }

  /* ---------- 对外 API ---------- */
  var API = {
    VERSION: VERSION,
    injectCSS: injectCSS,
    bind: function (btn, opt) { if (btn) btn.__cmp = opt || {}; return btn; },
    host: function (btn) { return resolveHost(btn, (btn && btn.__cmp) || {}); },
    isOpen: function (host) { return !!(host && host.__cmpOpen); },
    open: function (host, opt) { open(host, optsOf(null, opt), findBtn(host)); },
    close: function (host) { close(host, findBtn(host)); },
    zoom: function (opt) { zoom(optsOf(null, opt)); },      /* 全屏并排放大：{src,out,labSrc,labOut} */
    closeZoom: function () { zoomClose(); },
    toggle: function (btn, opt) {
      var o = optsOf(btn, opt);
      var host = resolveHost(btn, o);
      if (!host) return;
      if (host.__cmpOpen) close(host, btn); else open(host, o, btn);
      return host;
    }
  };

  /* 事件委托：任意 [data-cmp] 元素点击即开/收（capture 阶段，避免被卡片其它点击处理抢走） */
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var b = t.closest('[data-cmp]');
    if (b && !b.disabled) {
      e.preventDefault();
      e.stopPropagation();
      API.toggle(b);
      return;
    }
    /* [data-zoom]：任意元素（如「🔍 放大对比」链接）挂上即可开全屏并排放大，
       读 data-src / data-out / data-lab-src / data-lab-out，与 data-cmp 同源。 */
    var z = t.closest('[data-zoom]');
    if (z) {
      e.preventDefault();
      e.stopPropagation();
      API.zoom(optsOf(z, {}));
    }
  }, true);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectCSS);
  } else { injectCSS(); }

  window.PTCompare = API;
})();
