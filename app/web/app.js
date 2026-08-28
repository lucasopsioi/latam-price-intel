/* 拉美竞品情报中枢 —— 前端逻辑（原生 JS，零依赖） */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 0) => v == null ? '—' :
  Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });

let META = { countries: [], categories: [], brands: [], products: [] };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { }
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), ms);
}

/* ================================================================ 全局上下文
 *
 * ★ 解决的问题：全站有 9 个国家选择器、8 个产业选择器、4 个品牌选择器，
 *   各自独立。想看「墨西哥 · 手机 · Samsung」，要在不同页面重选五六遍 ——
 *   这是"页面之间不连贯"最直接、最机械的来源。
 *
 * ★ 做法是**同步而不是替换**：全局栏一改，就把值推进各页面自己的选择器里，
 *   再触发那一页重新加载。页面代码一行不用改，风险最低；
 *   哪天某个页面要脱离全局上下文，把它从下面的映射表里删掉即可。
 */
const CTX_KEY = 'latam.ctx.v1';
let CTX = { country: '', category: '', brand: '', days: '30' };
let CUR_PAGE = 'overview';

// 每个全局字段要同步到哪些页面级选择器
const CTX_TARGETS = {
  country: ['#pf-country', '#df-country', '#tf-country', '#if-country', '#br-country',
            '#tc-country', '#wl-country', '#mf-country', '#vf-country', '#cf-country',
            '#bp-country'],
  category: ['#pf-category', '#df-category', '#tf-category', '#if-category',
             '#br-category', '#tc-category', '#wl-category', '#bp-category',
             '#vr-category', '#vr2-category'],
  brand: ['#pf-brand', '#tc-brand', '#vr-brand', '#vr2-brand', '#wl-brand'],
  days: ['#pf-days', '#df-days', '#if-days', '#tf-days', '#tc-days'],
};

/** 把一个值写进 select。★ 各页的选项集合并不一致 ——
 *  时间窗有的页是 1/7/30/365、有的是 30/90/180/365。直接赋一个不存在的值，
 *  浏览器会**静默忽略**（select.value 保持原样），用户以为切了其实没切。
 *  所以：选项里没有就取最接近的那个；非数字的就跳过不动。 */
function setSel(sel, val, numericFallback) {
  const el = $(sel);
  if (!el) return false;
  const vals = [...el.options].map(o => o.value);
  if (vals.includes(val)) { el.value = val; return true; }
  if (val === '') return false;          // 该页没有"全部"选项（如价格看板必须锁国家）
  if (numericFallback) {
    const n = Number(val);
    const cands = vals.map(Number).filter(x => !Number.isNaN(x) && x > 0);
    if (n > 0 && cands.length) {
      const best = cands.reduce((a, b) => Math.abs(b - n) < Math.abs(a - n) ? b : a);
      el.value = String(best);
      return true;
    }
  }
  return false;
}

/** 把全局上下文推进各页选择器。
 *
 *  ★★ 一个容易搞错的前提：**所有页面的 DOM 是同时存在的**（.page 只是靠
 *    class 切换显示），所以 `$('#pf-country')` 在任何页面上都找得到 ——
 *    "这个选择器在不在当前页"不能用存不存在来判断。
 *    第一版就是这么写的，结果在情报流页上提示"本页不支持按产业筛选"，
 *    而它明明有产业筛选 —— 因为统计到了别的页面的选择器。
 *
 *  写入照旧覆盖全部（各页提前同步好，切过去就是对的），
 *  但"本页支持不支持"的判断**只看当前页容器内部**。
 */
function applyCtx() {
  const page = $('#page-' + CUR_PAGE);
  const missed = [], applied_fields = [];
  for (const [field, sels] of Object.entries(CTX_TARGETS)) {
    const val = CTX[field];
    let wanted = 0, ok = 0;
    for (const s of sels) {
      if (!$(s)) continue;
      const applied = setSel(s, val, field === 'days');
      if (page && $(s, page)) {         // ★ 只有当前页里的才计入提示统计
        wanted++;
        if (applied) ok++;
      }
    }
    if (val !== '' && wanted === 0) missed.push(field);        // 本页压根没有这个维度
    else if (val !== '' && ok < wanted) missed.push(field);    // 有但装不下这个值
    else if (wanted > 0 && val !== '') applied_fields.push(field);
  }
  // 选中态点亮：否则用户会忘了自己锁着墨西哥，
  // 然后奇怪为什么各页数字互相对不上
  ['country', 'category', 'brand', 'days'].forEach(f => {
    const el = $('#ctx-' + f);
    if (el) el.classList.toggle('ctx-on', f === 'days' ? el.value !== '30' : !!el.value);
  });
  const hint = $('#ctx-hint');
  if (hint) {
    const zh = { country: '国家', category: '产业', brand: '品牌', days: '时间窗' };
    const anySet = Object.values(CTX).some((v, i) =>
      v !== '' && !(Object.keys(CTX)[i] === 'days' && v === '30'));
    // ★ 正面说法比罗列"不支持什么"清楚得多。
    //   第一版在周报页上列出"不支持按国家/产业/品牌/时间窗筛选"——
    //   四个全列一遍，读者的第一反应是"是不是坏了"，其实只是这页没有这些维度。
    if (!anySet) hint.textContent = '';
    else if (!applied_fields.length) hint.textContent = '本页不受全局筛选影响';
    else if (missed.length) hint.textContent = '本页只按' +
      applied_fields.map(f => zh[f]).join('、') + '生效';
    else hint.textContent = '';
  }
  return missed;
}

function saveCtx() {
  try { localStorage.setItem(CTX_KEY, JSON.stringify(CTX)); } catch (e) { }
}

function loadCtx() {
  try {
    const raw = localStorage.getItem(CTX_KEY);
    if (raw) CTX = Object.assign(CTX, JSON.parse(raw));
  } catch (e) { }
  ['country', 'category', 'brand', 'days'].forEach(f => {
    const el = $('#ctx-' + f);
    if (el && [...el.options].some(o => o.value === CTX[f])) el.value = CTX[f];
  });
}

/** 全局栏一改：存盘 → 推进页面选择器 → 重新加载当前页 */
function onCtxChange() {
  ['country', 'category', 'brand', 'days'].forEach(f => {
    const el = $('#ctx-' + f);
    if (el) CTX[f] = el.value;
  });
  saveCtx();
  applyCtx();
  const [, loader] = PAGES[CUR_PAGE] || [];
  if (loader) loader();
}

/* ---------------- 导航 ---------------- */
const PAGES = {
  overview: ['总览', loadOverview],
  dash: ['竞争动态', loadDash],
  weekly: ['竞品周报', loadWeekly],
  prices: ['价格分析', loadPrices],
  trend: ['价格趋势', loadTrend],
  // ★ 这两个 loader 在 boards.js 里，而 boards.js 是在 app.js **之后**加载的。
  //   直接写函数名会在求值 PAGES 这一刻抛 ReferenceError，
  //   于是 app.js 整个挂掉、PAGES 卡在 TDZ，**全站所有页面都点不动** ——
  //   而控制台只有一行 "Cannot access 'PAGES' before initialization"，
  //   看起来像导航坏了，跟新看板毫无关系。
  //   包一层箭头函数把名字解析推迟到点击那一刻，加载顺序就无所谓了。
  pboard: ['价格看板', () => loadPriceBoard()],
  rboard: ['涨价看板', () => loadRiseBoard()],
  curve: ['价格曲线', () => loadTrendBoard()],
  watch: ['关注与预警', () => loadWatchBoard()],
  matches: ['竞品对照', loadMatches],
  launches: ['上市看板', loadLaunches],
  voc: ['消费者口碑', loadVoc],
  intel: ['情报流', loadIntel],
  products: ['我的产品', loadProducts],
  channels: ['渠道健康', loadChannels],
  live: ['实时过程', startLive],
  agents: ['Agent 编排', loadAgents],
  runs: ['运行记录', loadRuns],
  settings: ['设置', loadSettings],
};
$$('.nav-item').forEach(el => el.onclick = () => go(el.dataset.page));

/* ── 主题开关：跟随系统 → 白天 → 黑夜 三态循环 ── */
(function () {
  const btn = $('#theme-toggle');
  if (!btn) return;
  const ICON = { auto: '🌗', light: '☀️', dark: '🌙' };
  const cur = () => {
    try { return localStorage.getItem('latam.theme') || 'auto'; } catch (e) { return 'auto'; }
  };
  const apply = (mode) => {
    if (mode === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', mode);
    btn.textContent = ICON[mode];
    btn.title = { auto: '跟随系统（点击切白天）', light: '白天（点击切黑夜）',
                  dark: '黑夜（点击跟随系统）' }[mode];
    // 图表配色是从 CSS 变量实时读的，切主题必须重放，否则图还是旧配色
    if (window.Charts) requestAnimationFrame(() => Charts.repaintAll());
  };
  btn.onclick = () => {
    const next = { auto: 'light', light: 'dark', dark: 'auto' }[cur()];
    try { localStorage.setItem('latam.theme', next); } catch (e) { }
    apply(next);
    toast({ light: '已切到白天模式', dark: '已切到黑夜模式',
            auto: '已跟随系统外观' }[next]);
  };
  apply(cur());
})();

// 全局上下文栏的交互
['country', 'category', 'brand', 'days'].forEach(f => {
  const el = $('#ctx-' + f);
  if (el) el.onchange = onCtxChange;
});
if ($('#ctx-reset')) {
  $('#ctx-reset').onclick = () => {
    CTX = { country: '', category: '', brand: '', days: '30' };
    ['country', 'category', 'brand', 'days'].forEach(f => {
      const el = $('#ctx-' + f);
      if (el) el.value = CTX[f];
    });
    saveCtx(); applyCtx();
    const [, loader] = PAGES[CUR_PAGE] || [];
    if (loader) loader();
    toast('已重置为全部');
  };
}

function go(page) {
  CUR_PAGE = page;
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page));
  const [title, loader] = PAGES[page] || ['', null];
  $('#page-title').textContent = title;
  // ★ 必须在 loader() **之前**套用全局上下文 ——
  //   页面的 loader 是直接读自己那些选择器的值去请求的，
  //   套晚了这一次请求还是用旧值，要点两次才对。
  applyCtx();
  if (loader) loader();
  // ★ 切页之后必须让图表重算尺寸。
  //   ECharts 在 display:none 的容器里初始化时量到的宽高是 0，
  //   切过去之后画布仍是 0×0 —— 图看起来"没画出来"，但**不报任何错**。
  //   放在下一帧执行，等 .active 的样式生效之后再量。
  if (window.Charts) requestAnimationFrame(() => Charts.resizeAll());
}

/* ---------------- 总览 ---------------- */
async function loadOverview() {
  const d = await api('/api/overview');
  META.countries = d.countries;

  $('#ov-stats').innerHTML = [
    ['我方产品', d.totals.my_products, d.totals.my_products ? '' : '未录入 → 竞品匹配无法计算'],
    ['友商产品', d.totals.rival_products, '抓取中自动沉淀'],
    ['价格观测', num(d.totals.price_obs), `近7天 ${num(d.totals.price_obs_7d)} 条`],
    ['竞品对照', d.totals.matches, '通过三条规则判定'],
  ].map(([k, v, dd]) => `<div class="card stat"><div class="k">${k}</div>
      <div class="v">${v}</div><div class="d">${esc(dd)}</div></div>`).join('');

  // 顶部告警：把"没数据"的真实原因摆出来，不让人误以为友商没动静
  const alerts = [];
  if (!d.totals.my_products)
    alerts.push(['warn', '还没有录入我方产品。竞品匹配需要先有「我的产品」才能算 —— ' +
      '到「我的产品」页下载模板填好导入。']);
  if (!d.configured.minimax)
    alerts.push(['warn', '未配置 MiniMax API Key。清洗、价格审计、情报分析三个 Agent ' +
      '会退回规则模式（能跑，但灰色地带判不了、新闻摘要不翻译）。']);
  if (!d.configured.meli)
    alerts.push(['bad', 'MercadoLibre 未配置 access token。实测 ML 对未登录访客强制跳登录墙，' +
      '有头无头都被拦 —— 六国最重要的渠道目前抓不到数据，阿根廷更是只有这一个渠道。']);
  $('#ov-alerts').innerHTML = alerts.map(([c, t]) =>
    `<div class="note ${c}">${esc(t)}</div>`).join('');

  $('#ov-countries').innerHTML = `<thead><tr><th>国家</th><th>币种</th>
      <th class="num">启用渠道</th><th class="num">近7天观测</th>
      <th class="num">覆盖机型</th></tr></thead><tbody>` +
    d.countries.map(c => `<tr><td>${esc(c.name_zh)}</td><td>${esc(c.currency)}</td>
      <td class="num">${c.channels}</td>
      <td class="num">${c.obs_7d ? num(c.obs_7d) : '<span class="tag bad">0</span>'}</td>
      <td class="num">${c.products_7d || 0}</td></tr>`).join('') + '</tbody>';

  const a = d.audit, tot = a.accepted + a.rejected + a.pending;
  const rate = tot ? a.rejected / (a.accepted + a.rejected || 1) : 0;
  $('#ov-audit').innerHTML = tot ? `
    <div class="grid c3" style="margin-bottom:10px">
      <div class="stat"><div class="k">通过</div><div class="v" style="color:var(--green)">${num(a.accepted)}</div></div>
      <div class="stat"><div class="k">剔除</div><div class="v" style="color:var(--red)">${num(a.rejected)}</div></div>
      <div class="stat"><div class="k">待审</div><div class="v" style="color:var(--text-3)">${num(a.pending)}</div></div>
    </div>
    <div class="bar"><i style="width:${(a.accepted / tot * 100).toFixed(1)}%"></i></div>
    ${rate > 0.01 ? `<div class="note warn" style="margin-top:10px">
      剔除率 ${(rate * 100).toFixed(1)}% 超过 1% 告警线。经验上这更可能是<b>基线分组键出了问题</b>
      （少了国家或型号维度），而不是当天脏数据特别多 —— 请先核对分组再信结论。</div>` : ''}
  ` : '<div class="empty"><p>还没有价格数据</p></div>';

  const r = d.last_run;
  $('#ov-lastrun').innerHTML = r ? `
    <div class="grid c3">
      <div class="stat"><div class="k">成功</div><div class="v" style="font-size:20px">${r.pages_ok || 0}</div></div>
      <div class="stat"><div class="k">被拦</div><div class="v" style="font-size:20px">${r.pages_blocked || 0}</div></div>
      <div class="stat"><div class="k">写入</div><div class="v" style="font-size:20px">${num(r.rows_written)}</div></div>
    </div>
    <div style="margin-top:8px;color:var(--text-2);font-size:11.5px">
      ${esc(r.started_at)} · ${esc(r.mode)} · ${esc(r.status)}</div>`
    : '<div class="empty"><p>还没跑过采集</p></div>';

  fillMeta();
}

const CAT_ZH = { phone: '手机', tablet: '平板', wearable: '穿戴',
                 audio: '音频', pc: 'PC' };

async function fillMeta() {
  const opts = (list, val, label) => list.map(x =>
    `<option value="${esc(x[val])}">${esc(x[label])}</option>`).join('');
  ['#pf-country', '#mf-country', '#cf-country', '#vf-country'].forEach(sel => {
    const el = $(sel); if (!el) return;
    const cur = el.value;
    el.innerHTML = '<option value="">全部国家</option>' +
      opts(META.countries, 'code', 'name_zh');
    el.value = cur;
  });

  // ★ 品类与品牌下拉框以前从来没被填充过 —— 永远只有「全部」一个选项，
  //   点开是空的，用户看到的就是一个不能用的摆设。
  //   选项从**实际有数据的行**里取，并把条数标出来：
  //   让用户在下拉框里选一个必然 0 结果的品牌，比不给选项更糟。
  try {
    const f = await api('/api/meta/filters');
    const setOpts = (sel, list, allLabel, labelFn) => {
      const el = $(sel); if (!el) return;
      const cur = el.value;
      el.innerHTML = `<option value="">${allLabel}</option>` + list.map(x =>
        `<option value="${esc(x.code)}">${esc(labelFn(x))}（${x.n}）</option>`).join('');
      el.value = cur;
    };
    setOpts('#pf-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#df-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#df-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#tf-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#tf-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#if-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#if-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#vr-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#vr2-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#bp-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#br-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#br-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#tc-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#wl-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#wl-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    // ★ 价格看板的国家**不给"全部"选项**：它画的是绝对价位，
    //   六国六币种放同一根轴上纵轴会塌。必须锁一个国家。
    const bpc = $('#bp-country');
    if (bpc) {
      const cur = bpc.value;
      bpc.innerHTML = f.countries.map(x =>
        `<option value="${esc(x.code)}">${esc(x.name || x.code)}（${x.n}）</option>`).join('');
      bpc.value = cur || (f.countries[0] || {}).code || '';
    }
    setOpts('#mf-country', f.countries, '全部国家', x => x.name || x.code);

    // ★ 品牌下拉要按**配置口径**列全，不能只列"有数据的"。
    //   Positivo / Multilaser / B&O 这些配了但还没抓到的，若不列出来，
    //   用户会以为系统根本不跟踪它们 —— 分不清「没配」和「配了没抓到」。
    //   所以 0 条的照样列，标成灰色的「0」，一眼看出是采集缺口不是覆盖缺口。
    BRANDS_BY_CAT = f.brands_by_category || {};
    ALL_BRANDS = f.brands || [];

    // 全局上下文栏的选项
    setOpts('#ctx-country', f.countries, '全部国家', x => x.name || x.code);
    setOpts('#ctx-category', f.categories, '全部产业', x => CAT_ZH[x.code] || x.code);
    setOpts('#wf-category', f.categories, '全产业', x => CAT_ZH[x.code] || x.code);
    const cb = $('#ctx-brand');
    if (cb) {
      const cur = cb.value;
      cb.innerHTML = '<option value="">全部品牌</option>' + ALL_BRANDS.map(x =>
        `<option value="${esc(x.code)}">${esc(x.code)}${x.is_ours ? ' · 我方' : ''}</option>`
      ).join('');
      cb.value = cur;
    }
    loadCtx();          // 恢复上次的选择（localStorage）
    applyCtx();
    syncBrandOptions();

    // ★ 关注清单的品牌下拉必须放在 ALL_BRANDS **赋值之后**。
    //   第一版写在上面 20 行处，那时 ALL_BRANDS 还是初始的 []，
    //   下拉框永远是空的 —— 而外层 catch 是空的（"筛选项拉不到不影响主数据"），
    //   所以既不报错也没有任何提示，只能靠肉眼发现框里没东西。
    const wb = $('#wl-brand');
    if (wb) {
      const cur = wb.value;
      // 友商排前面、我方排后面并标注：清单本身是「友商关注清单」，
      // 但盯自己的价格也是正当需求，所以不隐藏，只做区分。
      const rivals = ALL_BRANDS.filter(x => !x.is_ours);
      const ours = ALL_BRANDS.filter(x => x.is_ours);
      const opt = x => `<option value="${esc(x.code)}">${esc(x.code)}`
        + `（${x.n}${x.is_ours ? ' · 我方' : ''}）</option>`;
      wb.innerHTML = rivals.map(opt).join('')
        + (ours.length ? `<optgroup label="我方品牌">${ours.map(opt).join('')}</optgroup>` : '');
      wb.value = cur;
    }
    // 口碑维度的品牌下拉：列全部品牌（不跟随产业，维度页本来就要跨产业横比）
    const vb = $('#vr-brand');
    if (vb) {
      const cur = vb.value;
      vb.innerHTML = '<option value="">全部品牌</option>' + ALL_BRANDS.map(x =>
        `<option value="${esc(x.code)}">${esc(x.code)}（${x.n}）</option>`).join('');
      vb.value = cur;
    }
    const vb2 = $('#vr2-brand');
    if (vb2) {
      const cur = vb2.value;
      vb2.innerHTML = '<option value="">全部品牌</option>' + ALL_BRANDS.map(x =>
        `<option value="${esc(x.code)}">${esc(x.code)}（${x.n}）</option>`).join('');
      vb2.value = cur;
    }
    const pc = $('#pf-category');
    if (pc && !pc._brandSync) { pc._brandSync = 1; pc.addEventListener('change', syncBrandOptions); }
  } catch (e) { /* 筛选项拉不到不影响主数据展示 */ }
}

let BRANDS_BY_CAT = {}, ALL_BRANDS = [];

/* 选了产业就只列该产业该跟踪的品牌；没选产业就列全部有数据的 */
function syncBrandOptions() {
  const el = $('#pf-brand'); if (!el) return;
  const cat = ($('#pf-category') || {}).value || '';
  const list = cat ? (BRANDS_BY_CAT[cat] || []) : ALL_BRANDS;
  const cur = el.value;
  el.innerHTML = '<option value="">全部品牌</option>' + list.map(x =>
    `<option value="${esc(x.code)}"${x.n ? '' : ' style="color:var(--text-3)"'}>` +
    `${esc(x.code)}${x.is_ours ? '（我方）' : ''}（${x.n}）</option>`).join('');
  // 换产业后原来选的品牌可能不在新列表里 —— 不清空的话会查出 0 条却看不出原因
  el.value = list.some(x => x.code === cur) ? cur : '';
}

/* ---------------- 竞争动态看板 ---------------- */
const SIG_ZH = {
  clearance: ['bad', '老品清库'], pre_launch_cut: ['warn', '新品前降价'],
  promo_prep: ['warn', '大促铺垫'], cost_hike: ['bad', '成本涨价'],
  price_war: ['bad', '价格战'], new_launch_premium: ['info', '新品高定'],
  restore: ['', '促销回价'], store_open: ['info', '开店'],
  launch_event: ['info', '发布会'], campaign: ['', '营销'],
  partnership: ['', '合作'], expansion: ['info', '扩张'],
  exit: ['bad', '退出'], supply: ['warn', '供应链'], award: ['ok', '获奖'],
};

async function loadDash() {
  const grain = $('#df-grain').value, cc = $('#df-country').value;
  const cat = $('#df-category').value, days = $('#df-days').value;
  const official = $('#df-official').checked;

  // ① 各国 × 各产业 态势
  const s = await api(`/api/dashboard/summary?days=${days}`);
  $('#dash-summary').innerHTML = `<thead><tr>
    <th>国家</th><th>产业</th><th class="num">观测</th><th class="num">型号</th>
    <th class="num">品牌</th><th class="num">均折扣</th><th class="num">深促</th>
    <th class="num">降价</th><th class="num">其中官方</th><th class="num">策略信号</th>
    </tr></thead><tbody>` + s.rows.map(r => `<tr>
      <td>${esc(r.country)}</td>
      <td><span class="tag">${esc(CAT_ZH[r.cat] || r.cat || '—')}</span></td>
      <td class="num">${num(r.obs)}</td>
      <td class="num">${r.products}</td>
      <td class="num">${r.brands}</td>
      <td class="num">${r.avg_discount != null ? r.avg_discount + '%' : '—'}</td>
      <td class="num">${r.deep_promo ? `<span class="tag warn">${r.deep_promo}</span>` : '—'}</td>
      <td class="num">${r.n_down || '—'}</td>
      <td class="num">${r.n_down_official
        ? `<span class="tag bad">${r.n_down_official}</span>` : '—'}</td>
      <td class="num">${r.n_signals ? `<span class="tag info">${r.n_signals}</span>` : '—'}</td>
    </tr>`).join('') + '</tbody>';

  // ② 时间矩阵
  const m = await api(`/api/dashboard/matrix?grain=${grain}&days=${days}`
    + `&country=${cc}&category=${cat}`);
  const note = $('#df-note');
  if (!m.cells.length) {
    note.style.display = 'block'; note.className = 'note warn';
    note.textContent = '这个时间范围内没有数据。价格变动需要至少两天的观测才能算出来。';
  } else note.style.display = 'none';

  $('#dash-matrix').innerHTML = `<thead><tr>
    <th>周期</th><th>国</th><th>产业</th><th class="num">观测</th>
    <th class="num">型号</th><th class="num">新品</th>
    <th class="num">降价</th><th class="num">涨价</th>
    <th class="num">官方降</th><th class="num">均降幅</th><th class="num">最大降幅</th>
    </tr></thead><tbody>` + m.cells.map(c => `<tr>
      <td class="mono">${esc(c.period)}</td>
      <td>${esc(c.cc)}</td>
      <td><span class="tag">${esc(CAT_ZH[c.cat] || c.cat || '—')}</span></td>
      <td class="num">${num(c.obs)}</td>
      <td class="num">${c.products}</td>
      <td class="num">${c.n_new ? `<span class="tag info">+${c.n_new}</span>` : '—'}</td>
      <td class="num">${c.n_down ? `<span class="tag" style="color:var(--green)">${c.n_down}</span>` : '—'}</td>
      <td class="num">${c.n_up ? `<span class="tag" style="color:var(--red)">${c.n_up}</span>` : '—'}</td>
      <td class="num">${c.n_down_official
        ? `<span class="tag bad" title="官方渠道降价 = 厂商定价动作">${c.n_down_official}</span>` : '—'}</td>
      <td class="num">${c.avg_cut != null ? c.avg_cut + '%' : '—'}</td>
      <td class="num">${c.max_cut != null ? `<b>${c.max_cut}%</b>` : '—'}</td>
    </tr>`).join('') + '</tbody>';

  // ③ 策略信号
  const sig = await api(`/api/dashboard/signals?days=${days}&country=${cc}`);
  $('#dash-signals').innerHTML = sig.rows.length ? sig.rows.map(x => {
    const [cls, zh] = SIG_ZH[x.signal_type] || ['', x.signal_type];
    return `<div style="padding:11px 13px;border-bottom:1px solid var(--line)">
      <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
        <span class="tag ${cls}">${esc(zh)}</span>
        <span class="tag">${esc(x.country_code || '—')}</span>
        <b>${esc(x.brand || '')}</b>
        <span style="color:var(--text-2)">${esc(x.model_name || '')}</span>
        ${x.confidence != null
          ? `<span class="hint">置信 ${(x.confidence * 100).toFixed(0)}%</span>` : ''}
      </div>
      <div style="margin-top:5px;font-size:12.5px">${esc(x.summary_zh)}</div>
      ${x.impact_zh ? `<div style="margin-top:4px;font-size:11.5px;color:var(--accent)">
        → 对我方：${esc(x.impact_zh)}</div>` : ''}
      ${x.suggested_action ? `<div style="margin-top:3px;font-size:11.5px;color:var(--text-3)">
        建议：${esc(x.suggested_action)}</div>` : ''}
    </div>`;
  }).join('') : '<div class="empty"><p>还没有策略信号</p><p style="font-size:11px">'
    + '点上方「跑策略分析」让 Agent 从价格变动里推断厂商意图</p></div>';

  // ④ 变动 TOP
  const mv = await api(`/api/dashboard/movers?days=${days}&country=${cc}`
    + `&category=${cat}&official_only=${official}&limit=25`);
  $('#dash-movers').innerHTML = `<thead><tr>
    <th>日期</th><th>国</th><th>品牌 / 型号</th><th class="num">原价</th>
    <th class="num">现价</th><th class="num">变动</th><th>渠道</th>
    </tr></thead><tbody>` + mv.rows.map(r => `<tr>
      <td class="mono">${esc(r.move_date?.slice(5))}</td>
      <td>${esc(r.country_code)}</td>
      <td><b>${esc(r.brand || '—')}</b>
        <div class="trunc" style="font-size:11px;color:var(--text-2)">${esc(r.model_name || '')}</div></td>
      <td class="num" style="color:var(--text-3)">${num(r.prev_price)}</td>
      <td class="num"><b>${num(r.curr_price)}</b></td>
      <td class="num"><span class="tag ${r.direction === 'down' ? 'ok' : 'bad'}">
        ${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(1)}%</span></td>
      <td><div class="trunc" style="max-width:120px;font-size:11px">${esc(r.channel)}
        ${r.is_official ? '<span class="tag ok">官方</span>' : ''}</div></td>
    </tr>`).join('') + '</tbody>';

  const nb = $('#nb-dash');
  if (nb) nb.textContent = sig.rows.length;
}

/* ---------------- 竞品周报 ---------------- */
/* 期次：每月 5→20「周报」、20→次月 5「双周报」。与后端 _period_bounds 同口径。 */
function periodOf(d) {
  const y = d.getFullYear(), m = d.getMonth();
  if (d.getDate() >= 20) {
    const s = new Date(y, m, 20), e = new Date(y, m + 1, 5);
    return { kind: '双周报', start: s, end: e };
  }
  if (d.getDate() >= 5) return { kind: '周报', start: new Date(y, m, 5), end: new Date(y, m, 20) };
  return { kind: '双周报', start: new Date(y, m - 1, 20), end: new Date(y, m, 5) };
}
const _iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
  + `-${String(d.getDate()).padStart(2, '0')}`;

/** 期次下拉：本期 + 往前 5 期 */
function fillPeriods() {
  const sel = $('#wf-period');
  if (!sel) return;
  const out = [];
  let cur = new Date();
  for (let i = 0; i < 6; i++) {
    const p = periodOf(cur);
    const endShown = new Date(p.end.getTime() - 86400000);   // 右端是开区间
    out.push(`<option value="${_iso(p.start)}">${i === 0 ? '本期・' : ''}`
      + `${p.kind} ${_iso(p.start)} ~ ${_iso(endShown)}</option>`);
    cur = new Date(p.start.getTime() - 86400000);            // 跳到上一期内
  }
  sel.innerHTML = out.join('');
}

function genWeekly() {
  run('weekly', {
    period: $('#wf-period') ? $('#wf-period').value : '',
    category: $('#wf-category') ? $('#wf-category').value : '',
  });
}

/** 导出当前这份报告。★ 用 <a download> 而不是 fetch+blob：
 *  后端已设 Content-Disposition，交给浏览器原生下载最省事，也保住中文文件名。 */
function exportWeekly(fmt) {
  const sel = $('#wf-report');
  const id = (sel && sel.value) || (window._lastWeeklyId || '');
  if (!id) return toast('还没有可导出的报告，先生成一份');
  toast(`正在生成 ${fmt.toUpperCase()}…（含图表，需要几秒）`, 4000);
  window.location.href = `/api/weekly/${id}/export/${fmt}`;
}

async function loadWeekly() {
  fillPeriods();
  const d = await api('/api/weekly/list');
  const sel = $('#wf-report');
  sel.innerHTML = '<option value="">最新一期</option>' + d.reports.map(r =>
    `<option value="${r.id}">${esc(r.week_start)} ~ ${esc(r.week_end)}（${esc(r.scope)}）</option>`
  ).join('');
  const id = sel.value || (d.reports[0] && d.reports[0].id);
  if (!id) {
    $('#weekly-body').innerHTML = '<div class="empty"><p>还没有周报</p>'
      + '<p style="font-size:11px">点上方「生成本周周报」，汇总 Agent 会把本周的'
      + '价格变动、策略信号、品牌动态整理成一份中文报告</p></div>';
    return;
  }
  window._lastWeeklyId = id;
  const r = await api('/api/weekly/' + id);
  let met = {};
  try { met = JSON.parse(r.metrics || '{}'); } catch (e) { }
  const charts = met.charts || [];

  // ★ 正文里的 ![chart:xxx] 占位换成真容器，图由 charts.js 的语义层画 ——
  //   周报与看板必须用**同一套图表语法**，否则同一个问题在两处长得不一样，
  //   读者要重新学一遍怎么看。
  // ★ 占位替换必须在 mdToHtml **之后**：它内部第一步就是 esc()，
  //   先注入的 <div> 会被转义成可见文字。
  const html = mdToHtml(r.content_md || '').replace(
    /!\[chart:([\w-]+)\]/g,
    (_, el) => `<div class="chart chart-sm" id="${el}"></div>`);

  $('#weekly-body').innerHTML = `<h2 style="margin:0 0 4px">${esc(r.title || '竞品报告')}</h2>
    <div class="hint" style="margin-bottom:14px">${esc(r.week_start)} ~ ${esc(r.week_end)}
      · 生成于 ${esc(String(r.created_at).slice(0, 16))}
      ${met.brief_chars ? ` · 正文 ${met.brief_chars} 字` : ''}</div>
    <div class="md">${html}</div>`;

  // ★ 原来写的是「取不到容器就 return」——**静默跳过**，于是图一张都没画出来
  //   而控制台干净、报告正文完好，看起来像"这份报告本来就没有图"。
  //   改成短暂重试 + 画不出来时在原位显示原因：宁可丑，不可无声消失。
  drawReportCharts(charts);
}

/** 把周报里的图画出来。容器是 innerHTML 刚塞进去的，允许它晚一两帧就位。 */
function drawReportCharts(charts, tries = 0) {
  const pending = [];
  (charts || []).forEach(c => {
    const el = document.getElementById(c.el);
    if (!el) { pending.push(c); return; }
    try {
      Charts.ask(c.question, c.el, Object.assign({ xlab: c.xlab }, c.opt || {}));
    } catch (e) {
      el.innerHTML = `<div class="empty"><p>这张图没画出来：${esc(e.message)}</p></div>`;
      console.error('[weekly] 画图失败', c.el, e);
    }
  });
  if (pending.length && tries < 10) {
    requestAnimationFrame(() => drawReportCharts(pending, tries + 1));
  } else if (pending.length) {
    console.error('[weekly] 这些图的容器始终没出现：', pending.map(c => c.el));
  }
}

/* 极简 Markdown 渲染 —— 周报是 Agent 生成的 md，不引第三方库 */
function mdToHtml(md) {
  // ★ 表格必须在按行处理之前先整块吃掉 —— 否则后面的「换行→<br>」会把表拆散，
  //   而周报的「价格预警」「重点变化」都是表格，散了就没法读。
  const table = (block) => {
    const rows = block.trim().split('\n')
      .map(l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()));
    if (rows.length < 2) return block;
    const head = rows[0];
    const body = rows.filter((r, i) => i > 1);   // 第 2 行是 |---| 分隔线
    return '<div class="table-wrap"><table><thead><tr>'
      + head.map(h => `<th>${h}</th>`).join('')
      + '</tr></thead><tbody>'
      + body.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
      + '</tbody></table></div>';
  };
  return esc(md)
    .replace(/(?:^\|.*\|[ \t]*$\n?)+/gm, m => table(m))
    .replace(/^#### (.*)$/gm, '<h5>$1</h5>')
    .replace(/^### (.*)$/gm, '<h4>$1</h4>')
    .replace(/^## (.*)$/gm, '<h3>$1</h3>')
    .replace(/^# (.*)$/gm, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/^\- (.*)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`)
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>');
}

/* ---------------- 价格 ---------------- */
async function loadPrices() {
  const p = new URLSearchParams({
    q: ($('#pf-q')?.value || '').trim(),
    country: $('#pf-country').value, category: $('#pf-category').value,
    brand: $('#pf-brand').value, status: $('#pf-status').value,
    seller_kind: $('#pf-seller')?.value || '',
    only_device: $('#pf-device')?.checked ? 'true' : 'false',
    sort: $('#pf-sort')?.value || 'date',
    days: $('#pf-days').value, limit: '300',
  });
  const d = await api('/api/prices?' + p);
  const note = $('#pf-note');
  const cnt = $('#pf-count');
  if (cnt) cnt.textContent = d.total != null
    ? `显示 ${d.prices.length} / 共 ${d.total} 条` : `${d.prices.length} 条`;
  if (!d.prices.length) {
    note.style.display = 'block';
    note.className = 'note warn';
    note.innerHTML = '这个筛选条件下没有数据。放宽条件试试（把时间范围改成「全部」、' +
      '清空搜索词），或到「渠道健康」跑一次体检。';
  } else note.style.display = 'none';

  const SK = { self_operated: ['ok', '自营'], brand_official: ['ok', '品牌店'],
               third_party: ['warn', '第三方'], unknown: ['', '未知'] };
  $('#price-table').innerHTML = `<thead><tr>
    <th>日期</th><th>国</th><th>渠道</th><th>品牌</th><th>商品 / SKU</th>
    <th class="num">现价</th><th class="num">原价</th><th class="num">off</th>
    <th>卖家</th><th>审计</th></tr></thead><tbody>` + d.prices.map(r => {
    const [skCls, skTxt] = SK[r.seller_kind] || SK.unknown;
    return `<tr>
      <td class="mono">${esc(r.obs_date?.slice(5))}</td>
      <td>${esc(r.country_code)}</td>
      <td>${esc(r.channel || '')}</td>
      <td>${esc(r.brand || '—')}</td>
      <td><div class="trunc" title="${esc(r.title)}">${esc(r.title)}</div>
        ${r.sku_code ? `<span class="tag purple" title="权威SKU">${esc(r.sku_code)}</span>` : ''}
        ${r.model_name && r.model_name !== r.sku_code
          ? `<span class="tag info">${esc(r.model_name)}</span>` : ''}
        ${r.rom_gb ? `<span class="tag">${r.ram_gb ? r.ram_gb + '+' : ''}${r.rom_gb}G</span>` : ''}
        ${r.condition !== 'new' ? `<span class="tag warn">${esc(r.condition)}</span>` : ''}
        ${r.product_kind === 'accessory' ? '<span class="tag">配件</span>' : ''}
        ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener"
            class="hint-link" style="font-size:11px">↗</a>` : ''}</td>
      <td class="num"><b>${num(r.sale_price)}</b>
        <div style="font-size:10px;color:var(--text-3)">${esc(r.currency)}</div></td>
      <td class="num" style="color:var(--text-3)">${r.list_price ? num(r.list_price) : '—'}</td>
      <td class="num">${r.discount_pct
        ? `<span class="tag ${r.discount_pct >= 20 ? 'bad' : 'warn'}">-${Math.round(r.discount_pct)}%</span>`
        : '—'}</td>
      <td><div class="trunc" style="max-width:120px">${esc(r.seller_name || '—')}</div>
        <span class="tag ${skCls}">${skTxt}</span></td>
      <td><span class="tag ${r.audit_status === 'accepted' ? 'ok' : r.audit_status === 'rejected' ? 'bad' : ''}"
          title="${esc(r.audit_reason || '')}">${r.audit_status === 'accepted' ? '通过' :
      r.audit_status === 'rejected' ? '剔除' : '待审'}</span></td>
    </tr>`;
  }).join('') + '</tbody>';
}

/* ---------------- 竞品对照 ---------------- */
async function loadMatches() {
  // 组合层面的站位先画 —— 「我该先看哪几款」比「某一款的明细」更前置
  try { await loadPosition(); } catch (e) { console.warn('站位加载失败', e); }

  const p = new URLSearchParams({
    product_id: $('#mf-product').value || 0, country: $('#mf-country').value,
  });
  const d = await api('/api/matches?' + p);
  if (!d.matches.length) {
    $('#match-list').innerHTML = `<div class="card"><div class="empty">
      <div class="big">⚖️</div><h4>还没有竞品对照</h4>
      <p>需要三件事齐备：①「我的产品」已录入且填了分国定价；
      ②该国已抓到友商价格数据；③价格审计已通过。
      然后点上面的「重算竞品匹配」。</p></div></div>`;
    return;
  }
  const byProduct = {};
  d.matches.forEach(m => {
    const k = m.my_name;
    (byProduct[k] = byProduct[k] || {});
    (byProduct[k][m.country_name] = byProduct[k][m.country_name] || []).push(m);
  });
  $('#match-list').innerHTML = Object.entries(byProduct).map(([prod, countries]) => `
    <div class="card"><h3>📦 ${esc(prod)}</h3>` +
    Object.entries(countries).map(([co, list]) => `
      <div style="margin-bottom:14px">
        <div style="font-size:12px;font-weight:600;color:var(--text-2);margin-bottom:6px">
          ${esc(co)} · 我方 ${num(list[0].my_price_local)} ${esc(list[0].currency)}</div>
        <table><thead><tr><th>#</th><th>竞品</th><th class="num">价格</th>
          <th class="num">价差</th><th class="num">规格</th><th class="num">总分</th>
          <th>判定依据</th><th></th></tr></thead><tbody>` +
      list.map(m => `<tr>
          <td>${m.rank_in_country || ''}</td>
          <td><b>${esc(m.rival_brand)}</b> ${esc(m.rival_name)}
            ${m.is_confirmed ? '<span class="tag ok">已确认</span>' : ''}</td>
          <td class="num">${num(m.rival_price_local)}</td>
          <td class="num ${m.price_gap_pct > 0 ? 'pct-up' : 'pct-down'}">
            ${m.price_gap_pct != null ? m.price_gap_pct.toFixed(1) : '—'}%</td>
          <!-- ★ 规格未知时不能显示 50。spec_score 缺省是 0.5，印成 "50" 看着像
               "相似度中等"，而真实含义是"这条根本没做规格校验" —— 判定依据里
               明明写着"未经规格校验"，表格却给个数，两边自相矛盾。
               分不清"中等"和"不知道"会让人把没校验的匹配当成校验过的。 -->
          <td class="num">${(m.reasons && m.reasons.spec_confidence > 0)
      ? (m.spec_score * 100).toFixed(0)
      : '<span class="tag warn" title="规格数据缺失，未做规格校验">未校验</span>'}</td>
          <td class="num"><b>${(m.total_score * 100).toFixed(0)}</b></td>
          <td><ul class="reason-list">${(m.reasons.reasons || []).map(x =>
        `<li>${esc(x)}</li>`).join('')}</ul></td>
          <td><button class="sm" onclick="markMatch(${m.id},'confirm')">确认</button>
              <button class="sm danger" onclick="markMatch(${m.id},'exclude')">排除</button></td>
        </tr>`).join('') + '</tbody></table></div>').join('') + '</div>').join('');
}

async function markMatch(id, action) {
  await api(`/api/matches/${id}/mark`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, value: true }),
  });
  toast(action === 'confirm' ? '已确认为竞品' : '已排除');
  loadMatches();
}

/* ---------------- 上市看板 ---------------- */
async function loadLaunches() {
  const d = await api('/api/launches');
  $('#launch-pending').innerHTML = d.pending_latam.length ?
    `<thead><tr><th>品牌</th><th>机型</th><th>产业</th><th>全球首发</th>
      <th class="num">距今</th><th class="num">已在几国上架</th></tr></thead><tbody>` +
    d.pending_latam.map(p => `<tr>
      <td>${esc(p.brand)}</td><td><b>${esc(p.model_name)}</b></td>
      <td>${esc(p.category_code)}</td><td class="mono">${esc(p.global_launch_date)}</td>
      <td class="num">${p.days_since_global} 天</td>
      <td class="num"><span class="tag ${p.countries_on_shelf ? 'info' : 'warn'}">
        ${p.countries_on_shelf}/6</span></td></tr>`).join('') + '</tbody>'
    : '<tbody><tr><td><div class="empty"><p>暂无有出处的首发日期。</p></div></td></tr></tbody>';

  // ★ 只展示首发日期有出处的产品。挡掉了多少、为什么，必须写出来 ——
  //   不写的话「行变少了」会被当成数据丢了，而真相是之前那些是猜的。
  const ln = $('#launch-note');
  if (ln) ln.innerHTML =
    (d.suppressed_note ? `<div class="note warn">${esc(d.suppressed_note)}</div>` : '')
    + (d.filtered_note ? `<div class="note">${esc(d.filtered_note)}</div>` : '');

  // ★ 上市日期这种「结论性」信息必须能点回原文核对。
  //   这些事件是 Agent 从新闻里读出来的，置信度只有 0.6 —— 机型名可能读错、
  //   事件类型可能归错类。给了出处链接，你**一眼就能自己判断可不可信**；
  //   不给的话，一个读错的日期和一个读对的日期在界面上长得一模一样。
  const srcHost = u => {
    try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return '出处'; }
  };
  $('#launch-events').innerHTML = d.events.length ?
    `<thead><tr><th>日期</th><th>品牌</th><th>机型</th><th>国家</th>
      <th>事件</th><th class="num">距全球首发</th><th class="num">拉美第N国</th>
      <th>信息源</th></tr></thead><tbody>` +
    d.events.map(e => `<tr>
      <td class="mono">${esc(e.event_date)}</td><td>${esc(e.brand || '')}</td>
      <td>${esc(e.model_name || '')}${e.notes
        ? `<div class="hint" style="font-size:11px;margin-top:2px">${esc(e.notes)}</div>` : ''}</td>
      <td>${esc(e.country_name || '全球')}</td>
      <td><span class="tag ${e.event_type === 'global_launch' ? 'purple' : 'info'}">
        ${e.event_type === 'global_launch' ? '全球首发' : '本国开卖'}</span>
        ${e.confidence != null
          ? `<div class="hint" style="font-size:11px">置信 ${(e.confidence * 100).toFixed(0)}%</div>` : ''}</td>
      <td class="num">${e.days_after_global != null ? e.days_after_global + ' 天' : '—'}</td>
      <td class="num">${e.country_rank || '—'}</td>
      <td>${e.evidence_url
        ? `<a href="${esc(e.evidence_url)}" target="_blank" rel="noopener noreferrer"
             title="${esc(e.evidence_url)}">${esc(srcHost(e.evidence_url))} ↗</a>`
        : '<span class="hint">无出处</span>'}</td>
      </tr>`).join('') + '</tbody>'
    : '<tbody><tr><td><div class="empty"><p>暂无上市事件</p></div></td></tr></tbody>';
}

/* ---------------- 消费者口碑 VOC ---------------- */
async function loadVoc() {
  const p = new URLSearchParams({
    country: $('#vf-country').value, hot_only: $('#vf-hot').checked,
  });
  const d = await api('/api/voc?' + p);
  const s = d.stats || {};

  $('#voc-stats').innerHTML = [
    ['已抓评论', num(s.total), ''],
    ['已翻译分析', num(s.translated), s.total ? `${((s.translated || 0) / s.total * 100).toFixed(0)}%` : ''],
    ['正面', num(s.pos), ''],
    ['负面', num(s.neg), s.total ? `${((s.neg || 0) / s.total * 100).toFixed(0)}%` : ''],
  ].map(([k, v, dd]) => `<div class="card stat"><div class="k">${k}</div>
      <div class="v">${v}</div><div class="d">${esc(dd)}</div></div>`).join('');

  $('#voc-profiles').innerHTML = d.profiles.length ? `<thead><tr>
    <th>品牌</th><th>机型</th><th>国</th><th>渠道</th>
    <th class="num">评论总数</th><th class="num">已抓</th><th class="num">均分</th>
    <th>归因（评论为什么这么多）</th></tr></thead><tbody>` +
    d.profiles.map(p => `<tr>
      <td>${esc(p.brand || '—')}</td>
      <td>${p.is_hot ? '🔥 ' : ''}<b>${esc(p.model_name || '—')}</b></td>
      <td>${esc(p.country_code)}</td><td>${esc(p.channel || '')}</td>
      <td class="num"><b>${num(p.total_reviews)}</b></td>
      <td class="num">${num(p.fetched_reviews)}</td>
      <td class="num">${p.avg_rating ? Number(p.avg_rating).toFixed(1) : '—'}</td>
      <td style="font-size:11.5px;color:var(--text-2);max-width:400px">
        ${esc(p.hot_reason || (p.is_hot ? '待归因' : ''))}</td>
    </tr>`).join('') + '</tbody>'
    : '<tbody><tr><td><div class="empty"><div class="big">💬</div><h4>还没有评论数据</h4><p>VOC 抓取在每轮采集的第⑤阶段自动进行，抓完价格后会对商品逐个读评论。也可以点上面「重跑 VOC 分析」只做分析。</p></div></td></tr></tbody>';

  $('#voc-insights').innerHTML = d.insights.length ? d.insights.map(i => `
    <div class="card" style="margin-bottom:12px">
      <h3>${esc(i.brand)} ${esc(i.model_name)}
        <span class="sub">${esc(i.country_name || i.country_code)} ·
        ${i.review_count} 条 · ${i.avg_rating ? Number(i.avg_rating).toFixed(1) + '★' : ''}</span></h3>
      ${i.summary_zh ? `<div style="margin-bottom:10px">${esc(i.summary_zh)}</div>` : ''}
      <div class="grid c3">
        <div><div style="font-size:11.5px;font-weight:600;color:var(--green);margin-bottom:4px">
          👍 好评点</div><ul class="reason-list">${(i.praise_points || []).map(x =>
      `<li>${esc(x)}</li>`).join('') || '<li>—</li>'}</ul></div>
        <div><div style="font-size:11.5px;font-weight:600;color:var(--red);margin-bottom:4px">
          👎 差评点</div><ul class="reason-list">${(i.complaint_points || []).map(x =>
        `<li>${esc(x)}</li>`).join('') || '<li>—</li>'}</ul></div>
        <div><div style="font-size:11.5px;font-weight:600;color:var(--orange);margin-bottom:4px">
          ⚠ 需关注</div><ul class="reason-list">${(i.watch_signals || []).map(x =>
          `<li>${esc(x)}</li>`).join('') || '<li>—</li>'}</ul></div>
      </div>
      ${i.vs_acme ? `<div class="note" style="margin-top:10px;margin-bottom:0">
        <b>评论里提到Acme：</b>${esc(i.vs_acme)}</div>` : ''}
    </div>`).join('')
    : '<div class="empty"><p>还没有口碑洞察。需要先抓到评论，并配置 MiniMax Key。</p></div>';

  $('#nb-voc').textContent = s.total || 0;
  loadRadar();
  loadDecay();
  loadVocBoard();     // 维度排行 / 情感来源 / 覆盖漏斗（boards.js）
}

/* 口碑维度雷达（方向15）。用条形而不是真雷达图：
   维度数会随筛选变化，雷达图在维度数不固定时读起来会误导 ——
   同一个形状在 6 边和 12 边下面积含义完全不同。 */
async function loadRadar() {
  const p = new URLSearchParams({
    kind: $('#vr2-kind').value || 'product',
    category: $('#vr2-category').value || '',
    brand: $('#vr2-brand').value || '',
    country: $('#vf-country').value || '', days: 365,
  });
  const d = await api('/api/voc/radar?' + p);
  const items = d.items || [];

  // ★ 继承占比高就必须说出来：老数据的维度情感是从整条评论摊派的，
  //   和逐维度判定精度不一样，不提示等于假装数据比实际更细。
  $('#vr-warn').innerHTML = d.note
    ? `<div class="note" style="border-left-color:var(--orange);font-size:12px">
         ⚠ ${esc(d.note)}（继承占比 ${d.inherited_pct}%）</div>` : '';

  if (!items.length) {
    $('#voc-radar').innerHTML = `<tbody><tr><td><div class="empty">
      <div class="big">🕸️</div><h4>这个筛选下还没有维度数据</h4>
      <p>维度来自评论的逐条标注。换个产业/品牌，或先点「重跑 VOC 分析」。</p>
      </div></td></tr></tbody>`;
    return;
  }
  const max = Math.max(...items.map(i => i.mentions));
  $('#voc-radar').innerHTML = `<thead><tr>
    <th>维度</th><th class="num">提及</th><th>提及量</th>
    <th class="num">好评率</th><th class="num">差评</th><th>口碑</th></tr></thead><tbody>`
    + items.map(i => {
      const pr = i.pos_rate;
      // 好评率低的标红 —— 这是这张表唯一要人一眼看到的东西
      const col = pr === null ? 'var(--text-2)'
        : pr >= 85 ? 'var(--green)' : pr >= 65 ? 'var(--orange)' : 'var(--red)';
      return `<tr>
        <td><b>${esc(i.name)}</b></td>
        <td class="num">${num(i.mentions)}</td>
        <td><div style="background:var(--bd);height:8px;border-radius:4px;width:120px">
          <div style="background:var(--blue);height:8px;border-radius:4px;
                      width:${Math.round(i.mentions / max * 120)}px"></div></div></td>
        <td class="num" style="color:${col};font-weight:600">
          ${pr === null ? '—' : pr + '%'}</td>
        <td class="num">${i.negative || 0}</td>
        <td><div style="background:var(--bd);height:8px;border-radius:4px;width:110px;
                        display:flex;overflow:hidden">
          <div style="background:var(--green);height:8px;width:${(i.positive / i.mentions * 110) || 0}px"></div>
          <div style="background:var(--text-3,#999);height:8px;width:${(i.neutral / i.mentions * 110) || 0}px"></div>
          <div style="background:var(--red);height:8px;width:${(i.negative / i.mentions * 110) || 0}px"></div>
        </div></td></tr>`;
    }).join('') + '</tbody>';
}

/* 上市后口碑衰减（方向17） */
async function loadDecay() {
  const d = await api('/api/voc/decay');
  const s = d.series || [], ex = d.excluded || [];
  let html = '';
  if (s.length) {
    html += s.map(p => `<div style="margin-bottom:14px">
      <div style="font-size:12.5px;font-weight:600">${esc(p.brand)} ${esc(p.model)}
        <span class="sub">上市 ${esc(p.launch)} · ${p.reviews} 条评论</span></div>
      <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
        ${p.buckets.map(b => `<div style="text-align:center;font-size:11px;
            opacity:${b.thin ? 0.45 : 1}" title="${b.n} 条评论${b.thin ? '（样本不足）' : ''}">
          <div style="font-weight:600">${b.avg_rating}★</div>
          <div class="sub">D${b.day_from}-${b.day_to}</div>
          <div class="sub">n=${b.n}${b.thin ? '*' : ''}</div></div>`).join('')}
      </div></div>`).join('');
  }
  // ★ 排除清单必须显示。只画能画的、不说没画谁，会让人以为"就这几个产品有口碑"
  if (ex.length) {
    html += `<div class="note" style="font-size:12px;margin-top:10px">
      <b>未画出 ${ex.length} 个产品</b>（如实说明原因，不是没有数据就默默不显示）：
      <ul class="reason-list">${ex.slice(0, 8).map(e =>
        `<li>${esc(e.brand)} ${esc(e.model)} —— ${esc(e.reason)}</li>`).join('')}</ul>
      ${ex.length > 8 ? `<div class="sub">…… 另有 ${ex.length - 8} 个</div>` : ''}</div>`;
  }
  $('#voc-decay').innerHTML = html || `<div class="empty">
    <div class="big">📉</div><h4>还画不出衰减曲线</h4>
    <p>需要三样同时具备：评论有日期、评论有星级、产品有真实上市日期。
       目前<b>星级</b>是缺口 —— 零售站把星级渲染成图标而不是文字，
       已加上从 DOM 属性读取，下一轮采集后这张图才会有数据。</p></div>`;
}

/* ---------------- 情报 ---------------- */
async function loadIntel() {
  const cc = $('#if-country').value, cat = $('#if-category').value;
  const d = await api(`/api/intel?days=${$('#if-days').value}`
    + `&min_importance=${$('#if-imp').value}&country=${cc}&category=${cat}`);

  // ★ 两块看板画在列表上方：先看"哪个国家/产业有动静"，再点进去看条目。
  //   关键是**把 0 也显示出来** —— 之前 CL 只有 77 条而 BR 有 910 条，
  //   不摆出来就会被读成"智利本周很平静"，实际是我们没去智利看。
  const board = (el, rows, labelFn, cur, pick) => {
    const t = $(el); if (!t) return;
    const tot = rows.reduce((a, r) => a + r.n, 0) || 1;
    t.innerHTML = `<thead><tr><th></th><th class="num">条数</th>
        <th class="num">重要</th><th style="width:38%"></th></tr></thead><tbody>` +
      rows.map(r => `<tr class="clickable ${r.code === cur ? 'sel' : ''}"
          onclick="${pick}('${esc(r.code === cur ? '' : r.code)}')">
        <td>${esc(labelFn(r))}</td>
        <td class="num"><b>${r.n}</b></td>
        <td class="num">${r.important ? `<span class="tag warn">${r.important}</span>` : '—'}</td>
        <td><div class="bar" style="height:6px"><i style="width:${(r.n / tot * 100).toFixed(0)}%"></i></div></td>
      </tr>`).join('') + '</tbody>';
  };
  board('#intel-by-country', d.by_country || [], r => r.name || r.code, cc, 'pickIntelCountry');
  board('#intel-by-category', d.by_category || [], r =>
    r.code === '—' ? '未判定产业' : (CAT_ZH[r.code] || r.code), cat, 'pickIntelCategory');

  if (!d.items.length) {
    $('#intel-list').innerHTML = `<div class="card"><div class="empty">
      <div class="big">📰</div><h4>暂无情报</h4>
      <p>${cc || cat ? '当前筛选下没有情报 —— 换个国家/产业，或把时间窗放宽。'
        : '情报源是 Google News 与全球 top 科技站 RSS，在国内网络通常需要代理 —— 到「设置」里填 proxy 后再点「立即扫描情报源」。'}</p>
      </div></div>`;
    return;
  }
  $('#intel-list').innerHTML = '<div class="card">' + d.items.map(i => `
    <div style="padding:9px 0;border-bottom:1px solid var(--line)">
      <div style="display:flex;gap:7px;align-items:center;margin-bottom:3px;flex-wrap:wrap">
        ${i.tag ? `<span class="tag info">${esc(i.tag)}</span>` : ''}
        ${i.brand ? `<span class="tag">${esc(i.brand)}</span>` : ''}
        ${i.geo_named ? `<span class="tag info">${esc(i.country_name || i.country_code)}</span>`
          : (i.country_name ? `<span class="tag" style="opacity:.55" title="仅新闻源所在国，原文未点名">来源:${esc(i.country_name)}</span>` : '')}
        <span class="tag ${i.importance >= 4 ? 'bad' : ''}">重要度 ${i.importance}</span>
        <span style="color:var(--text-3);font-size:11px">${esc(i.source_name || '')}
          ${esc(i.published_at || '')}</span>
      </div>
      <div style="font-weight:500">${esc(i.summary_zh || i.title)}</div>
      ${i.summary_zh ? `<div style="color:var(--text-3);font-size:11.5px">${esc(i.title)}</div>` : ''}
      ${i.url ? `<a href="${esc(i.url)}" target="_blank"
        style="font-size:11px;color:var(--accent)">原文 ↗</a>` : ''}
    </div>`).join('') + '</div>';
}

/* ---------------- 产品 ---------------- */
async function loadProducts() {
  const d = await api('/api/products');
  META.products = d.products;
  $('#nb-products').textContent = d.products.length;
  const sel = $('#mf-product');
  if (sel) sel.innerHTML = '<option value="">全部产品</option>' +
    d.products.map(p => `<option value="${p.id}">${esc(p.marketing_name)}</option>`).join('');

  if (!d.products.length) {
    $('#product-list').innerHTML = `<div class="card"><div class="empty">
      <div class="big">📦</div><h4>还没有录入产品</h4>
      <p>点上面「下载录入模板」，按三张表填好后导入。<br>
      导入后到「竞品对照」页点「重算竞品匹配」即可生成分国竞品对照表。</p></div></div>`;
    return;
  }
  $('#product-list').innerHTML = `<div class="card"><div class="table-wrap"><table>
    <thead><tr><th>产业</th><th>营销名</th><th>内部代号</th><th>芯片</th>
      <th class="num">SKU</th><th class="num">在售国</th><th class="num">竞品</th>
      <th>发货时间</th></tr></thead><tbody>` +
    d.products.map(p => `<tr>
      <td>${esc(p.icon || '')} ${esc(p.category_name || '')}</td>
      <td><b>${esc(p.marketing_name)}</b></td>
      <td class="mono">${esc(p.internal_code || '—')}</td>
      <td>${esc(p.chipset || '—')}</td>
      <td class="num">${p.sku_count}</td>
      <td class="num">${p.country_count}</td>
      <td class="num">${p.match_count ? `<span class="tag info">${p.match_count}</span>`
      : '<span class="tag warn">0</span>'}</td>
      <td class="mono">${esc(p.launch_earliest || '—')}</td></tr>`).join('') +
    '</tbody></table></div></div>';
}

async function importList() {
  toast('导入中…');
  try {
    const r = await api('/api/products/import-list', { method: 'POST' });
    if (r.error) { toast(r.error, 5000); return; }
    toast(`清单导入完成：新建 ${r.created}，更新 ${r.updated}，共 ${r.total} 行`, 5000);
    if (r.errors?.length) alert('导入提示：\n\n' + r.errors.join('\n'));
    loadProducts();
  } catch (e) { toast('导入失败：' + e.message, 5000); }
}

async function importProducts(input) {
  if (!input.files.length) return;
  const fd = new FormData(); fd.append('file', input.files[0]);
  toast('导入中…');
  try {
    const r = await api('/api/products/import', { method: 'POST', body: fd });
    let msg = `导入完成：产品 ${r.products}，SKU ${r.skus}，定价 ${r.pricing}`;
    if (r.errors?.length) msg += `｜${r.errors.length} 条错误`;
    toast(msg, 5000);
    if (r.errors?.length || r.warnings?.length) {
      alert('导入报告：\n\n' + [...(r.errors || []), ...(r.warnings || [])].join('\n'));
    }
    loadProducts();
  } catch (e) { toast('导入失败：' + e.message, 5000); }
  input.value = '';
}

/* ---------------- 渠道 ---------------- */
// ★ 状态必须能区分「要你做一件事才能通」和「站点真的拦我们」——
//   它们的处置完全不同，混显成同一个标签会让人以为都是反爬问题。
//   实测：4 个 MercadoLibre 显示"被风控"，其实只是没配 token（登录墙）。
const VERDICT = {
  healthy: ['ok', '健康'],
  degraded: ['warn', '降级'],
  selector_broken: ['bad', '解析失效'],
  empty: ['warn', '空·原因待查'],
  rate_limited: ['warn', '被风控'],
  need_login: ['info', '待配 Key'],
  need_captcha: ['warn', '需人工过验证'],
  brand_absent: ['ok', '无该品牌'],
  dead: ['bad', '不可用'],
  unknown: ['muted', '待评估'],
};
// 每种状态该怎么办 —— 光给标签不给动作，用户只会问"那我该干嘛"
const VERDICT_ACTION = {
  need_login: '配置对应 API token 或跑 tools/site_login.py 登录一次',
  need_captcha: '跑 tools\\site_login.py <站点> 由你本人过一次验证，会话可用数周',
  rate_limited: '已自动降频；持续出现可改到当地凌晨跑',
  selector_broken: '需要写专用适配器（页面结构与通用解析器不匹配）',
  brand_absent: '该渠道不卖这个品牌，属正常，无需处理',
  dead: '连续多轮失败，建议核对 URL 或暂时停用',
};
async function loadChannels() {
  const d = await api('/api/channels');
  const cc = $('#cf-country').value;
  const rows = d.channels.filter(c => !cc || c.country_code === cc);
  $('#channel-table').innerHTML = `<thead><tr><th>国</th><th>渠道</th><th>类型</th>
    <th>引擎</th><th>卖家默认</th><th>健康</th><th class="num">均条数</th>
    <th>说明</th><th>启用</th></tr></thead><tbody>` + rows.map(c => {
    const [cls, txt] = VERDICT[c.verdict] || ['', c.verdict || '未体检'];
    return `<tr style="${c.enabled ? '' : 'opacity:.45'}">
      <td>${esc(c.country_code)}</td>
      <td><b>${esc(c.name)}</b></td>
      <td><span class="tag">${esc(c.kind)}</span></td>
      <td>${c.force_engine ? `<span class="tag purple">${esc(c.force_engine)}</span>`
        : '<span class="tag">自动</span>'}</td>
      <td>${c.default_seller_type === 'official' ? '<span class="tag ok">官方</span>'
        : '<span class="tag warn">需审计</span>'}</td>
      <td><span class="tag ${cls}" title="${esc(VERDICT_ACTION[c.verdict] || '')}"
        >${esc(txt)}</span></td>
      <td class="num">${c.avg_items != null ? Number(c.avg_items).toFixed(0) : '—'}</td>
      <td><div class="trunc" title="${esc(c.notes || '')}"
        style="font-size:11px;color:var(--text-3)">${
          VERDICT_ACTION[c.verdict]
            ? `<b style="color:var(--accent)">→ ${esc(VERDICT_ACTION[c.verdict])}</b>`
            : esc(c.notes || '')}</div></td>
      <td><button class="sm" onclick="toggleChannel(${c.id},${c.enabled ? 0 : 1})">
        ${c.enabled ? '关闭' : '启用'}</button></td></tr>`;
  }).join('') + '</tbody>';
}
async function toggleChannel(id, enabled) {
  await api(`/api/channels/${id}/toggle`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  loadChannels();
}
function runDoctor() { run('doctor', { country: $('#cf-country').value }); }

/* ---------------- Agent ---------------- */
async function loadAgents() {
  const d = await api('/api/agents');
  $('#agent-roster').innerHTML = d.roster.map(a => `
    <div style="padding:11px 13px;background:var(--bg);border-radius:var(--radius-sm)">
      <div style="display:flex;gap:7px;align-items:center;margin-bottom:4px">
        <b>${esc(a.zh)}</b><span class="tag info">${esc(a.role)}</span>
        <span class="mono" style="color:var(--text-3)">${esc(a.name)}</span></div>
      <div style="font-size:11.5px;color:var(--text-2)">${esc(a.desc)}</div></div>`).join('');

  $('#agent-plans').innerHTML = d.plans.length ? d.plans.map(p => `
    <details class="step"><summary>
      <span class="mono">${esc(p.plan_date)}</span>
      <span class="tag info">${esc(p.mode)}</span>
      ${p.rotation_category ? `<span class="tag">轮值 ${esc(p.rotation_category)}</span>` : ''}
      <span style="color:var(--text-2)">${p.unit_count} 个采集单元</span></summary>
      <div class="lbl">研判理由</div><pre>${esc(p.reasoning || '')}</pre>
      <div class="lbl">渠道健康度</div><pre>${esc(p.health_summary || '')}</pre>
    </details>`).join('')
    : '<div class="empty"><p>还没有抓取计划。跑一次采集，主 Agent 会先研判再产出计划。</p></div>';

  $('#agent-runs').innerHTML = d.runs.length ? d.runs.map(r => `
    <details class="step" ontoggle="if(this.open)loadSteps(${r.id},this)">
      <summary>
        <span class="mono">#${r.id}</span>
        <span class="tag ${r.status === 'ok' ? 'ok' : r.status === 'running' ? 'info' : 'bad'}">
          ${esc(r.agent_name)}</span>
        <span style="color:var(--text-2);flex:1">${esc(r.output_summary || r.input_summary || '')}</span>
        <span style="color:var(--text-3);font-size:11px">${r.steps} 步
          ${r.tokens_used ? '· ' + num(r.tokens_used) + ' tokens' : ''}</span>
      </summary><div class="steps-box"><span class="spinner"></span></div>
    </details>`).join('')
    : '<div class="empty"><p>还没有 Agent 运行记录</p></div>';
}

async function loadSteps(runId, el) {
  const box = $('.steps-box', el);
  if (box.dataset.loaded) return;
  const d = await api(`/api/agents/${runId}/steps`);
  box.dataset.loaded = 1;
  box.innerHTML = d.steps.length ? d.steps.map(s => `
    <div style="border-left:2px solid var(--line-strong);padding-left:11px;margin:9px 0">
      <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
        <span class="tag">${s.step_no}</span><b>${esc(s.step_name)}</b>
        ${s.decision ? `<span class="tag ${s.status === 'ok' ? 'ok' : 'warn'}">${esc(s.decision)}</span>` : ''}
        ${s.tokens ? `<span style="font-size:10.5px;color:var(--text-3)">${s.tokens} tok</span>` : ''}
        ${s.input_ref ? `<span class="mono" style="color:var(--text-3)">${esc(s.input_ref)}</span>` : ''}
      </div>
      ${s.reason ? `<div style="font-size:11.5px;color:var(--text-2);margin-top:3px">${esc(s.reason)}</div>` : ''}
      ${s.prompt_digest ? `<div class="lbl">送进模型的提示词</div><pre>${esc(s.prompt_digest)}</pre>` : ''}
      ${s.raw_response ? `<div class="lbl">模型原始回复</div><pre>${esc(s.raw_response)}</pre>` : ''}
      ${s.parsed_result ? `<div class="lbl">解析结果</div><pre>${esc(s.parsed_result)}</pre>` : ''}
    </div>`).join('') : '<div style="color:var(--text-3);padding:9px">无步骤记录</div>';
}

/* ---------------- 运行 ---------------- */
async function loadRuns() {
  const d = await api('/api/runs');
  $('#run-list').innerHTML = d.runs.length ? '<div class="card"><div class="table-wrap"><table>' +
    `<thead><tr><th>#</th><th>开始</th><th>模式</th><th>状态</th>
      <th class="num">成功</th><th class="num">被拦</th><th class="num">失败</th>
      <th class="num">写入</th><th>警告</th></tr></thead><tbody>` +
    d.runs.map(r => `<tr>
      <td>${r.id}</td><td class="mono">${esc((r.started_at || '').slice(5, 16))}</td>
      <td>${esc(r.mode)}</td>
      <td><span class="tag ${r.status === 'ok' ? 'ok' : r.status === 'running' ? 'info' : 'warn'}">
        ${esc(r.status)}</span></td>
      <td class="num">${r.pages_ok || 0}</td><td class="num">${r.pages_blocked || 0}</td>
      <td class="num">${r.pages_failed || 0}</td><td class="num">${num(r.rows_written)}</td>
      <td><div style="font-size:11px;color:var(--text-3)">
        ${(r.warnings || []).slice(0, 2).map(w => esc(w)).join('<br>')}
        ${(r.warnings || []).length > 2 ? `<br>…另 ${r.warnings.length - 2} 条` : ''}</div></td>
    </tr>`).join('') + '</tbody></table></div></div>'
    : '<div class="card"><div class="empty"><div class="big">📋</div><h4>还没跑过采集</h4><p>点上面「立即采集」开始。建议先用「只做研判（不抓）」看看主 Agent 的计划。</p></div></div>';
}

/* ---------------- 设置 ---------------- */
// 每个 Key：标签 / 说明 / 申请地址 / 重要程度
const SECRET_META = {
  minimax_api_key: ['MiniMax API Key',
    '★★★ 必填。七个 Agent 全靠它：清洗、价格审计、VOC 分析、规格补全、情报。不填则全部退回规则模式，抓得到价格但没有分析。',
    'https://platform.minimaxi.com', 'must'],
  meli_access_token: ['MercadoLibre Access Token',
    '★★★ 强烈建议。六国最大电商。实测未登录访客被强制跳登录墙 —— 不配这个阿根廷完全没有数据（该国只配了这一个渠道）。免费注册。',
    'https://developers.mercadolibre.com', 'must'],
  telegram_bot_token: ['Telegram Bot Token',
    '要每天收简报就填。在 Telegram 里找 @BotFather 发 /newbot 创建，它会给你一串 token。',
    'https://t.me/BotFather', 'nice'],
  x_bearer_token: ['X (Twitter) Bearer Token',
    '可选。付费套餐才有搜索权限。不填则靠新闻源覆盖社媒动态（多数品牌活动当地科技媒体都会报道）。',
    'https://developer.x.com', 'optional'],
  openai_api_key: ['OpenAI API Key',
    '可选备用模型。MiniMax 不可用时的后备。', 'https://platform.openai.com/api-keys', 'optional'],
  proxy_password: ['代理密码', '仅当你的代理需要认证时填', '', 'optional'],
};
const PLAIN_META = {
  llm_base_url: ['模型接口地址', '默认 https://api.minimaxi.com/v1。报错时先核对这项与控制台一致', '', 'optional'],
  llm_model: ['模型名', '如 MiniMax-Text-01 / MiniMax-M1，以你控制台可用的为准', '', 'optional'],
  proxy: ['代理地址', 'Google News 与多数海外站点在国内需要代理，如 http://127.0.0.1:7890', '', 'nice'],
  telegram_chat_id: ['Telegram Chat ID',
    '给你的机器人随便发一条消息，然后访问 https://api.telegram.org/bot<你的token>/getUpdates，里面 chat.id 就是',
    '', 'nice'],
};

async function loadSettings() {
  const d = await api('/api/settings');
  const byKey = Object.fromEntries(d.settings.map(s => [s.key, s]));
  const RANK = { must: ['必填', 'warn'], nice: ['建议', 'info'], optional: ['可选', ''] };
  const field = (k, meta, type) => {
    const s = byKey[k] || {};
    const [label, hint, link, rank] = meta[k] || [k, '', '', 'optional'];
    const [rankTxt, rankCls] = RANK[rank] || RANK.optional;
    const status = s.is_set
      ? `<span class="tag ok">已配置 ${s.updated_at ? esc(String(s.updated_at).slice(0, 10)) : ''}</span>`
      : `<span class="tag ${rankCls || 'muted'}">${rankTxt}</span>`;
    const getLink = link
      ? ` <a href="${esc(link)}" target="_blank" rel="noopener" class="hint-link">去申请 ↗</a>` : '';
    return `<label class="field key-field"><span>${esc(label)} ${status}</span>
      <input type="${type}" data-key="${k}" autocomplete="off" spellcheck="false"
             placeholder="${s.is_set ? esc(s.value) + '（留空则不改动）' : '粘贴到这里'}">
      <span class="hint">${esc(hint)}${getLink}</span></label>`;
  };

  const nSet = d.settings.filter(s => s.is_set).length;
  const mustMissing = Object.entries(SECRET_META)
    .filter(([k, m]) => m[3] === 'must' && !(byKey[k] || {}).is_set)
    .map(([, m]) => m[0]);
  $('#set-secrets').innerHTML =
    (mustMissing.length
      ? `<div class="callout warn">还差 ${mustMissing.length} 个必填项：<b>${
          mustMissing.map(esc).join('、')}</b><br>
         <span class="hint">填好后点下面「保存」，再点「测试连接」立刻验证是否可用。
         密钥加密存在本机 data/intel.db，界面只回显掩码。</span></div>`
      : `<div class="callout ok">必填项已齐（共配置 ${nSet} 项）。可以点「测试连接」验证。</div>`) +
    Object.keys(SECRET_META)
      .filter(k => !['proxy_password', 'telegram_bot_token'].includes(k))
      .map(k => field(k, SECRET_META, 'password')).join('') +
    `<div class="row" style="margin-top:10px">
       <button onclick="testKeys()">🔌 测试连接</button>
       <span class="hint" id="key-test-result"></span>
     </div>`;
  $('#set-telegram').innerHTML =
    field('telegram_bot_token', SECRET_META, 'password') +
    field('telegram_chat_id', PLAIN_META, 'text');

  const rt = d.runtime;
  $('#set-runtime').innerHTML = `
    <label class="field"><span>主抓取引擎</span>
      <select data-rt="scrape.engine">
        <option value="selenium"${rt.scrape.engine === 'selenium' ? ' selected' : ''}>Selenium（推荐，反爬更强）</option>
        <option value="playwright"${rt.scrape.engine === 'playwright' ? ' selected' : ''}>Playwright（更快但更易被拦）</option>
      </select><span class="hint">实测 Liverpool 只有 Selenium 过得去。另一个引擎自动作为兜底：
      同域被主引擎连拦 3 次后接管</span></label>
    <label class="field"><span>兜底引擎</span>
      <select data-rt="scrape.fallback_engine">
        <option value="playwright"${rt.scrape.fallback_engine === 'playwright' ? ' selected' : ''}>Playwright</option>
        <option value="selenium"${rt.scrape.fallback_engine === 'selenium' ? ' selected' : ''}>Selenium</option>
        <option value="none"${rt.scrape.fallback_engine === 'none' ? ' selected' : ''}>关闭兜底</option>
      </select><span class="hint">必须与主引擎不同，否则等于没有兜底</span></label>
    <label class="field"><span>每次搜索取前 N 个商品</span>
      <input type="number" data-rt="scrape.max_products_per_query" value="${rt.scrape.max_products_per_query}">
      <span class="hint">你选了「全部进详情页」，这个数直接决定每天跑多久</span></label>
    <label class="field"><span>抓取间隔（秒）</span>
      <input type="number" step="0.5" data-rt="scrape.min_delay" value="${rt.scrape.min_delay}">
      <span class="hint">礼貌抓取下限，不建议调小于 2</span></label>
    <label class="field"><span>规格相似度阈值</span>
      <input type="number" step="0.05" data-rt="matching.spec_min" value="${rt.matching.spec_min}">
      <span class="hint">低于此值不算竞品。0.55 表示"竞争力大差不差"</span></label>
    <label class="field"><span>价格带 ±</span>
      <input type="number" step="0.05" data-rt="matching.price_band_pct" value="${rt.matching.price_band_pct}">
      <span class="hint">0.30 = 价差超过 ±30% 就不算同一价位段的竞品</span></label>
    <label class="field"><span>每国最多列几个竞品</span>
      <input type="number" data-rt="matching.top_n_per_country" value="${rt.matching.top_n_per_country}"></label>
    <label class="field"><span>每日定时时间</span>
      <input type="text" data-rt="schedule.daily_time" value="${esc(rt.schedule.daily_time)}">
      <span class="hint">如 07:30。抓取时段建议避开当地白天高峰</span></label>`;
}

async function saveSettings() {
  const settings = {};
  $$('[data-key]').forEach(el => { if (el.value.trim()) settings[el.dataset.key] = el.value.trim(); });
  const runtime = {};
  $$('[data-rt]').forEach(el => {
    const path = el.dataset.rt.split('.');
    let v = el.value;
    if (el.type === 'number') v = parseFloat(v);
    let node = runtime;
    path.slice(0, -1).forEach(p => node = (node[p] = node[p] || {}));
    node[path[path.length - 1]] = v;
  });
  const r = await api('/api/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings, runtime }),
  });
  toast(`已保存 ${r.saved.length} 项`);
  $$('[data-key]').forEach(el => el.value = '');
  loadSettings(); pollTask();
}

async function testKeys() {
  const el = $('#key-test-result');
  el.textContent = '测试中…';
  try {
    const r = await api('/api/settings/test', { method: 'POST' });
    el.innerHTML = r.results.map(x =>
      `<span class="tag ${x.ok ? 'ok' : 'warn'}">${esc(x.name)}：${esc(x.message)}</span>`
    ).join(' ');
  } catch (e) {
    el.textContent = '测试失败：' + e.message;
  }
}

async function tgTest() {
  const r = await api('/api/telegram/test', { method: 'POST' });
  toast(r.ok ? 'Telegram 测试消息已发送' : '失败：' + r.message, 5000);
}
async function tgBrief() {
  toast('生成中…');
  const r = await api('/api/telegram/brief', { method: 'POST' });
  $('#tg-preview').innerHTML = `<div class="lbl" style="margin-top:12px">简报预览
    （${r.length} 字符）</div><pre style="background:var(--bg);padding:11px;
    border-radius:8px;font-size:11.5px;white-space:pre-wrap;max-height:340px;
    overflow:auto;margin-top:6px">${esc(r.preview)}</pre>`;
  toast(r.ok ? '简报已发送' : '未发送：' + r.message, 5000);
}

/* ---------------- 实时过程 ---------------- */
const LIVE_ICON = {
  search: ['🔍', 'var(--accent)'], found: ['✓', 'var(--green)'],
  block: ['⛔', 'var(--orange)'], error: ['✕', 'var(--red)'],
  agent: ['🤖', 'var(--purple)'], stage: ['▸', 'var(--text)'],
  page: ['·', 'var(--text-3)'], voc: ['💬', 'var(--accent)'],
  log: ['·', 'var(--text-3)'],
  // 细粒度过程事件：没有这几类的话，一个渠道跑三五分钟界面毫无动静
  query: ['›', 'var(--accent)'], query_done: ['‹', 'var(--text-2)'],
  detail: ['◦', 'var(--text-2)'], warn: ['⚠', 'var(--orange)'],
};
let liveSeq = 0, liveTimer = null, liveCount = { found: 0, blocked: 0 };

function startLive() {
  if (liveTimer) return;
  liveSeq = 0;
  $('#live-feed').innerHTML = '';
  const tick = async () => {
    if (!$('#page-live').classList.contains('active')) { liveTimer = null; return; }
    try {
      const d = await api('/api/live?after=' + liveSeq);
      if (d.events.length) appendLive(d.events);
      $('#live-stat').textContent = d.task.running
        ? `运行中：${d.task.name} · ${d.task.progress}｜抓到 ${liveCount.found} 批，受阻 ${liveCount.blocked}`
        : `空闲｜抓到 ${liveCount.found} 批，受阻 ${liveCount.blocked}`;
    } catch (e) { }
    liveTimer = setTimeout(tick, 1500);
  };
  tick();
}

function appendLive(events) {
  const feed = $('#live-feed');
  if (feed.querySelector('.empty')) feed.innerHTML = '';
  const frag = document.createDocumentFragment();
  events.forEach(e => {
    liveSeq = Math.max(liveSeq, e.seq);
    if (e.kind === 'found') liveCount.found++;
    if (e.kind === 'block' || e.kind === 'error') liveCount.blocked++;
    const [ico, color] = LIVE_ICON[e.kind] || LIVE_ICON.log;
    const div = document.createElement('div');
    div.style.cssText = 'padding:2px 0;display:flex;gap:8px;line-height:1.5';
    div.innerHTML = `<span style="color:var(--text-3);flex-shrink:0">${esc(e.ts)}</span>
      <span style="color:${color};flex-shrink:0;width:14px">${ico}</span>
      <span style="color:${e.kind === 'found' ? 'var(--text)' : 'var(--text-2)'};
        word-break:break-word">${esc(e.message)}</span>`;
    frag.appendChild(div);
  });
  feed.appendChild(frag);
  // 只保留最近 600 行，长任务跑一整天也不会把浏览器拖死
  while (feed.children.length > 600) feed.removeChild(feed.firstChild);
  if ($('#live-auto').checked) feed.scrollTop = feed.scrollHeight;
}

async function clearLive() {
  await api('/api/live/clear', { method: 'POST' });
  liveSeq = 0; liveCount = { found: 0, blocked: 0 };
  $('#live-feed').innerHTML = '';
}

/* ---------------- 任务轮询 ---------------- */
async function run(action, payload) {
  try {
    await api('/api/run/' + action, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
    toast('任务已启动');
    pollTask();
  } catch (e) { toast(e.message, 4000); }
}

let polling = false;
async function pollTask() {
  if (polling) return;
  polling = true;
  const led = $('#led-task'), txt = $('#txt-task');
  const tick = async () => {
    let t;
    try { t = await api('/api/task'); } catch (e) { polling = false; return; }
    if (t.running) {
      led.className = 'led busy';
      txt.textContent = `${t.name}：${t.progress}`.slice(0, 30);
      setTimeout(tick, 2500);
    } else {
      led.className = t.error ? 'led off' : 'led on';
      txt.textContent = t.error ? '上次任务失败' : '空闲';
      if (t.error) toast('任务失败：' + t.error, 6000);
      else if (t.result) toast('任务完成', 3000);
      polling = false;
      const active = $('.nav-item.active')?.dataset.page;
      if (active && PAGES[active]) PAGES[active][1]();
    }
  };
  tick();
}

/* ---------------- 启动 ---------------- */
(async function init() {
  try {
    const d = await api('/api/overview');
    $('#led-llm').className = 'led ' + (d.configured.minimax ? 'on' : 'warn');
    $('#txt-llm').textContent = d.configured.minimax ? '模型已配置' : '模型未配置';
    $('#led-tg').className = 'led ' + (d.configured.telegram ? 'on' : 'warn');
    $('#txt-tg').textContent = d.configured.telegram ? 'Telegram 已配置' : 'Telegram 未配置';
    $('#nb-prices').textContent = d.totals.price_obs_7d;
    $('#nb-matches').textContent = d.totals.matches;
    $('#nb-intel').textContent = d.totals.dynamics_7d;
    $('#nb-voc').textContent = d.totals.reviews;
    // 未读预警的角标 —— 这是唯一需要"催人去看"的数字
    fetch('/api/alerts?unread=true&limit=1').then(r => r.json()).then(a => {
      const n = (a.items || []).length;
      const el = $('#nb-alerts');
      if (el) { el.textContent = n; el.style.background = n ? 'var(--red)' : ''; }
    }).catch(() => {});
    $('#nb-products').textContent = d.totals.my_products;
    if (d.task.running) pollTask();
  } catch (e) { toast('后端连接失败：' + e.message, 6000); }
  go('overview');
})();

/* ---------------- 价格趋势 ---------------- */
let TREND_SEL = null;

async function loadTrend() {
  const cc = $('#tf-country').value, cat = $('#tf-category').value;
  const days = $('#tf-days').value;
  const d = await api(`/api/trend/products?country=${cc}&category=${cat}&days=${days}`);

  $('#trend-list').innerHTML = `<thead><tr><th>品牌 / 型号</th><th class="num">观测天</th>
      <th class="num">国</th><th class="num">渠道</th></tr></thead><tbody>` +
    (d.rows.length ? d.rows.map(r => `<tr class="clickable ${r.id === TREND_SEL ? 'sel' : ''}"
        onclick="showTrend(${r.id})">
      <td><b>${esc(r.brand)}</b> ${esc(r.model_name)}
        <span class="tag">${CAT_ZH[r.cat] || r.cat}</span></td>
      <td class="num"><b>${r.obs_days}</b></td>
      <td class="num">${r.countries}</td><td class="num">${r.channels}</td>
    </tr>`).join('')
      : '<tr><td colspan="4" class="empty">这个筛选下没有产品</td></tr>') + '</tbody>';

  // ★ 把"为什么没有趋势"直接写在页面上。只有 1~2 天数据时，
  //   画出来的是一两个点 —— 不说清楚会被读成"价格很稳定"。
  const maxDays = d.rows.reduce((m, r) => Math.max(m, r.obs_days), 0);
  $('#tf-hint').innerHTML = maxDays >= 2
    ? `共 ${d.rows.length} 个产品有连续观测，最长 ${maxDays} 天`
    : `<span style="color:var(--red)">目前最多只有 ${maxDays} 个观测日 —— ` +
      `价格趋势至少要 2 天。每日自动采集已开启（07:30），明天起自动累积。</span>`;

  if (d.rows.length) showTrend(TREND_SEL && d.rows.some(r => r.id === TREND_SEL)
    ? TREND_SEL : d.rows[0].id);
}

async function showTrend(pid) {
  TREND_SEL = pid;
  $$('#trend-list tr').forEach(tr => tr.classList.remove('sel'));
  const cc = $('#tf-country').value, days = $('#tf-days').value;
  const off = $('#tf-official').checked;
  const d = await api(`/api/trend/${pid}?country=${cc}&days=${days}&official_only=${off}`);

  $('#trend-title').innerHTML =
    `${esc(d.product.brand)} ${esc(d.product.model_name)} 价格走势`;

  if (!d.series.length) {
    $('#trend-chart').innerHTML = '<div class="empty"><p>这个产品在当前筛选下没有价格</p></div>';
    return;
  }
  $('#trend-chart').innerHTML = d.series.map(s => trendSvg(s)).join('') +
    (d.trendable ? '' : `<div class="note warn">${esc(d.note)}</div>`);
}

/* 每条线单独一张图：★ 6 个国家 6 种货币，画在同一坐标系里毫无意义 */
function trendSvg(s) {
  const W = 520, H = 130, PAD = { l: 62, r: 12, t: 14, b: 22 };
  const pts = s.points;
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const lo = Math.min(...pts.map(p => p.price)), hi = Math.max(...pts.map(p => p.price));
  const span = (hi - lo) || hi * 0.02 || 1;
  const x = i => PAD.l + (pts.length === 1 ? iw / 2 : i * iw / (pts.length - 1));
  const y = v => PAD.t + ih - (v - (lo - span * 0.15)) / (span * 1.3) * ih;

  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.price).toFixed(1)}`).join('');
  const dots = pts.map((p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(p.price).toFixed(1)}"
      r="3" fill="var(--accent)"><title>${esc(p.date)}  ${num(p.price)} ${esc(s.currency)}
${p.listings} 条挂牌</title></circle>`).join('');
  const chg = s.change_pct;
  const chgTag = chg == null ? '' :
    `<span class="tag ${chg < 0 ? 'ok' : chg > 0 ? 'bad' : ''}">${chg > 0 ? '+' : ''}${chg.toFixed(1)}%</span>`;

  return `<div style="margin-bottom:14px">
    <div style="font-size:12px;margin-bottom:2px">
      <b>${esc(s.country_code)}</b> · ${esc(s.channel)}
      <span style="color:var(--text-3)">${esc(s.currency)}</span> ${chgTag}
      <span style="color:var(--text-3);font-size:11px">${s.days} 个观测日</span></div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">
      <line x1="${PAD.l}" y1="${PAD.t + ih}" x2="${W - PAD.r}" y2="${PAD.t + ih}"
            stroke="var(--border)"/>
      <text x="4" y="${y(hi) + 4}" font-size="10" fill="var(--text-3)">${num(hi)}</text>
      <text x="4" y="${y(lo) + 4}" font-size="10" fill="var(--text-3)">${num(lo)}</text>
      <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2"/>
      ${dots}
      <text x="${x(0)}" y="${H - 6}" font-size="10" fill="var(--text-3)">${esc(pts[0].date.slice(5))}</text>
      ${pts.length > 1 ? `<text x="${W - PAD.r}" y="${H - 6}" font-size="10"
         text-anchor="end" fill="var(--text-3)">${esc(pts[pts.length - 1].date.slice(5))}</text>` : ''}
    </svg></div>`;
}


/* 国家看板 / 产业看板：点一下就把筛选切过去（再点一次取消） */
function pickIntelCountry(code) { $('#if-country').value = code; loadIntel(); }
function pickIntelCategory(code) { $('#if-category').value = code === '—' ? '' : code; loadIntel(); }
