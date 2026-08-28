/* 业务图层 —— 只做「数据 → 图元」的映射，形状全部来自 charts.js。
 *
 * ★ 每个函数刻意保持在 10~30 行。一旦某个函数开始处理配色、空态、
 *   动画或 resize，就说明有东西该沉到 charts.js 里去了。
 */

/* ---------------------------------------------------------------- 工具 */

const money = cur => v => {
  if (v == null) return '—';
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (a >= 1e3) return (v / 1e3).toFixed(0) + 'k';
  return Math.round(v).toLocaleString();
};

/** 卡片右上角的口径说明 —— 每张图都必须能自证用了什么口径。 */
function setNote(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text || '';
}

/** 一组 KPI 数字卡。 */
function kpis(id, cards) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = cards.map(c => `
    <div class="card stat${c.tone ? ' tone-' + c.tone : ''}">
      <div class="k">${esc(c.k)}</div>
      <div class="v">${esc(c.v)}</div>
      <div class="d">${esc(c.d || '')}</div>
    </div>`).join('');
}

/* ---------------------------------------------------------------- 价格看板 */

async function loadPriceBoard() {
  const cc = $('#bp-country').value, cat = $('#bp-category').value;

  // ① 价格带卡位
  const b = await api(`/api/board/price-band?country=${cc}&category=${cat}&days=7`);
  setNote('bp-band-note',
    `${b.country} · ${b.currency} · 近 7 天 · 仅整机新品（排除配件/翻新/捆绑）`
    + (b.flagged?.length ? ` · 已剔除 ${b.flagged.length} 个可疑品牌` : ''));
  if (!b.items.length) {
    Charts.empty('bp-band', {
      title: '这个国家/产业下还没有足够的整机挂牌',
      reason: `需要每个品牌至少 ${b.min_n} 条整机新品报价`,
      action: '换一个国家，或等下一轮采集',
    });
  } else {
    Charts.ask('position', 'bp-band', {
      rows: b.items, xlab: `成交价（${b.currency}）`, fmt: money(b.currency),
      onPick: r => toast(`${r.label}：P25 ${money()(r.p25)} / 中位 ${money()(r.med)} / P75 ${money()(r.p75)}，${r.n} 条挂牌`),
    });
  }
  // 被哨兵剔掉的要显式列出来 —— 静默丢弃等于假装它不存在
  const fl = $('#bp-flagged');
  fl.innerHTML = (b.flagged || []).map(f =>
    `<div class="warn-line">⚠ <b>${esc(f.label)}</b> 未画出：${esc(f.suspect)}
      （中位 ${money()(f.med)}，${f.n} 条）</div>`).join('');

  // ② 折扣热力
  const h = await api('/api/board/discount-heat?days=7');
  setNote('bp-heat-note',
    `中性点 = 大盘中位折扣 ${h.center}% · 红=促销更狠 绿=更温和 · 样本<${h.min_n} 的格子留空`);
  Charts.ask('intensity', 'bp-heat', {
    xs: h.xs, ys: h.ys, cells: h.cells, center: h.center,
    fmt: v => v + '%',
    onPick: k => k && k.v != null && toast(`${k.y} · ${k.x}：平均折扣 ${k.v}%（${k.n} 条）`),
  });

  // ③ 自营 vs 三方
  const s = await api(`/api/board/seller-spread?country=${cc}&category=${cat}&days=7`);
  setNote('bp-seller-note',
    `同一商品同一配置比价 · 空心=自营/官方 实心=第三方最低 · 共 ${s.total} 组`);
  if (!s.items.length) {
    Charts.empty('bp-seller', {
      title: '没有同时出现自营与第三方报价的商品',
      reason: '需要同一款同一配置在同一国既有自营也有三方在售',
      action: '换个国家试试，或等渠道覆盖变宽',
    });
  } else {
    Charts.ask('compare', 'bp-seller', {
      rows: s.items, xlab: '价格', fmt: money(), upIsBad: true,
      onPick: r => toast(`${r.label}：三方比自营 ${r.gap_pct > 0 ? '贵' : '便宜'} ${Math.abs(r.gap_pct)}%`),
    });
  }

  // ④ 我方 vs 友商价差（百分比 —— 唯一能跨国同屏的价格视图）
  const o = await api(`/api/board/own-vs-rivals?country=${cc}&category=${cat}`);
  setNote('bp-own-note',
    `共 ${o.total} 对匹配 · 单位是%，所以六国可以放一张图`
    + (o.unverified ? ` · ${o.unverified} 对未经规格校验（空心）` : ''));
  if (!o.items.length) {
    Charts.empty('bp-own', {
      title: '这个筛选下还没有我方与友商的匹配对',
      reason: '匹配需要我方产品在该国有价格、且有规格或价格带相近的友商机型',
      action: '换个国家，或去「竞品对照」页看全量匹配',
    });
  } else {
    Charts.ask('deviation', 'bp-own', {
      rows: o.items, upIsBad: false,   // 正数=对标机更贵=我方有价格优势
      fmt: v => (v > 0 ? '+' : '') + v.toFixed(1) + '%',
      xlab: '对标机相对我方的价差',
      onPick: x => toast(x.note, 5000),
    });
  }
}

/* ---------------------------------------------------------------- 涨价看板 */

async function loadRiseBoard() {
  const cc = $('#br-country').value, cat = $('#br-category').value;
  const tier = $('#br-tier').value;
  const d = await api(`/api/board/moves?direction=up&days=30&country=${cc}`
    + `&category=${cat}&tier=${tier}`);

  // ★ 自检数放在最前面：涨价均值远大于降价均值 = 污染的直接证据
  const skew = d.avg_abs_pct && d.opposite_avg_pct
    ? (d.avg_abs_pct / d.opposite_avg_pct) : null;
  kpis('br-kpi', [
    { k: '涨价条数', v: d.total, d: `可信 ${d.tiers.credible} · 存疑 ${d.tiers.suspect} · 几乎必错 ${d.tiers.implausible}` },
    { k: '涨价平均幅度', v: (d.avg_abs_pct ?? '—') + '%', tone: skew > 2 ? 'bad' : '' ,
      d: `降价 ${d.opposite_n} 条平均 ${d.opposite_avg_pct}%` },
    { k: '不对称度', v: skew ? skew.toFixed(1) + '×' : '—', tone: skew > 2 ? 'bad' : 'good',
      d: skew > 2 ? '涨幅均值远大于降幅 —— 混进了噪声' : '两侧幅度接近，分布正常' },
    { k: '可信占比', v: d.total ? Math.round(d.tiers.credible / d.total * 100) + '%' : '—',
      d: '只有这部分能用来下结论' },
  ]);

  // ① 幅度分档 —— 看板第一张图先看噪声有多少
  setNote('br-bins-note', '阈值按品类给：低单价品类（音频/穿戴）的正常波动本来就更大');
  Charts.ask('spread', 'br-bins', {
    bins: d.bins, ylab: '条数',
    onPick: b => toast(`${b.label}：${b.n} 条${b.note ? '，' + b.note : ''}`),
  });

  // ② 涨价清单哑铃图
  const top = d.items.slice(0, 14);
  setNote('br-list-note',
    `点的大小 = 评论量（销量代理）· 空心 = 拿不到评论量，不代表没人买`
    + (tier ? ` · 已筛「${{credible:'可信',suspect:'存疑',implausible:'几乎必错'}[tier]}」` : ''));
  if (!top.length) {
    Charts.empty('br-list', {
      title: '这个筛选下没有涨价记录',
      reason: `近 30 天共 ${d.total} 条涨价，当前筛选后为 0`,
      action: '把可信度筛选切回「全部」看看',
    });
  } else {
    Charts.ask('compare', 'br-list', {
      rows: top.map(r => ({
        label: `${r.model} · ${r.country_code}`,
        from: r.prev_price, to: r.curr_price,
        size: r.proxy_volume || null,
        hollow: !r.proxy_volume,          // 拿不到代理量 → 空心，不假装是小点
        note: `${r.cat_zh} · ${r.channel || ''} · ${r.tier_zh}：${r.tier_note}`,
        raw: r,
      })),
      xlab: '价格（本币，跨国不可比）', fmt: money(),
      onPick: r => toast(`${r.raw.model}：${r.raw.tier_zh} —— ${r.raw.tier_note}`, 5000),
    });
  }

  // ③ 促销收缩（先行指标）
  const p = await api(`/api/board/promo-shrink?days=14&country=${cc}`);
  setNote('br-promo-note', p.note || '');
  if (p.insufficient) {
    Charts.empty('br-promo', {
      title: p.no_movement ? '窗口内没有可检出的促销收缩' : '数据还不够算促销收缩',
      reason: p.note,
      action: p.no_movement ? '继续积累数据，趋势要 2~3 周才看得出'
        : '连续采集 2~3 周后这张图会自动有内容',
    });
  } else {
    Charts.ask('compare', 'br-promo', {
      rows: p.items.slice(0, 12), xlab: '在促商品占比 %',
      fmt: v => v.toFixed(0) + '%',
      upIsBad: false,                     // ★ 语义反转：占比下降才是要警惕的
      onPick: r => toast(r.note, 5000),
    });
  }
}

/* ---------------------------------------------------------------- 口碑看板 */

async function loadVocBoard() {
  const cc = $('#vf-country').value;
  const cat = $('#vr-category').value, brand = $('#vr-brand').value;
  const kind = $('#vr-kind').value || 'product';
  const d = await api(`/api/board/voc-rank?country=${cc}&category=${cat}`
    + `&brand=${brand}&days=365&kind=${kind}`);
  const r = d.rank;

  // ① 维度排行（发散条，相对基线）
  setNote('vr-rank-note',
    r.baseline != null
      ? `基线 = 按提及量加权的整体好评率 ${r.baseline}% · 仅收 ≥${r.min_mentions} 次提及的维度`
      : '');
  if (!r.items.length) {
    Charts.empty('voc-rank', {
      title: '这个筛选下还没有足够的维度标注',
      reason: `需要每个维度至少 ${r.min_mentions || 8} 次提及`,
      action: '换个产业/品牌，或先点「重跑 VOC 分析」',
    });
  } else {
    Charts.ask('deviation', 'voc-rank', {
      rows: r.items, upIsBad: false,      // 好评率高是好事
      fmt: v => (v > 0 ? '+' : '') + v.toFixed(1) + 'pp',
      xlab: '相对基线的好评率偏离',
      onPick: x => toast(`${x.label}：${x.note}`, 4000),
    });
  }
  $('#vr-thin').innerHTML = (r.dropped_thin || []).length
    ? `<div class="warn-line">样本不足未入榜：${esc((r.dropped_thin || []).join('、'))}</div>` : '';

  // ② 情感来源分层 —— 诚实性视图
  setNote('vr-src-note',
    '继承 = 从整条评论摊派给各维度，精度低于逐维度判定；重跑 VOC 分析会逐步转成逐维度');
  Charts.ask('share', 'voc-src', {
    rows: d.source.items,
    order: [{ k: 'aspect', name: '逐维度判定' }, { k: 'review', name: '整条继承' }],
    colors: { aspect: Charts.colors().accent, review: Charts.colors().text3 },
  });

  // ③ 覆盖漏斗
  setNote('vr-cov-note', `分母 = ${d.coverage.base.toLocaleString()} 个有价格的商品页`);
  // ★ 覆盖率是 0~100% 的**占比**，没有负值 ——
  //   原来用发散条（围绕 0 分正负）是把"有方向的偏离"这个语义浪费掉了，
  //   而且会让人以为存在"负覆盖"。占比问题按语法表走 share。
  Charts.ask('share', 'voc-cov', {
    rows: d.coverage.items.map(i => ({
      label: i.label,
      covered: i.v,
      rest: Math.max(0, d.coverage.base - i.v),
      note: `${i.v.toLocaleString()} / ${d.coverage.base.toLocaleString()}`,
    })),
    order: [{ name: '已覆盖', k: 'covered' }, { name: '未覆盖', k: 'rest' }],
    colors: { covered: Charts.colors().accent, rest: Charts.colors().line },
    onPick: x => toast(`${x.label}：${x.note}`),
  });
}

/* ================================================================ 价格曲线 */

let TREND_PICKS = [];   // 当前选中的对比对象

async function loadTrendBoard() {
  const d = await api('/api/trend/candidates?limit=60');
  const fill = (id, opts) => {
    const el = $(id); if (!el) return;
    el.innerHTML = opts.map(o => `<option value="${esc(o.v)}">${esc(o.t)}</option>`).join('');
  };
  fill('#tc-product', d.products.map(p => ({
    v: p.id, t: `${p.brand} ${p.model_name}（${p.cat_zh}·${p.obs_days}天）` })));
  fill('#tc-category', d.categories.map(c => ({ v: c.code, t: c.name_zh })));
  fill('#tc-brand', d.brands.map(b => ({
    v: b.name, t: `${b.name}${b.is_ours ? '（我方）' : ''}·${b.obs_days}天` })));
  // ★★ 必须在**重建选项之后**再套一次全局上下文。
  //   go() 是先 applyCtx() 再调 loader 的，而这个 loader 会把三个下拉的
  //   options 整个重建 —— 刚写进去的值当场被冲掉。
  //   实测症状：全局选了 Samsung，曲线页却显示 Amazfit（列表第一个），
  //   而且**不报错**，看起来就像全局筛选对这页无效。
  //   凡是 loader 里重建 options 的页面，都要在重建后补这一句。
  if (window.applyCtx) applyCtx();
  syncKindPicker();
  renderPicks();
  if (TREND_PICKS.length) drawCompare(); else drawSingle();
}

/** 任何筛选器一动就重画。
 *  ★ 以前只有「查询」按钮会重画，改了下拉却什么都不动 ——
 *    用户实测把国家从"全部"改成"墨西哥"，图和说明都还是全部国家的旧结果，
 *    于是"墨西哥"配着"横跨 5 个币种"的提示，看起来像算错了。
 *    筛选器改了就该立刻生效，这是最基本的预期。 */
function redrawCurve() {
  return TREND_PICKS.length ? drawCompare() : drawSingle();
}

/** 三个对象选择器按 kind 显示对应的那个 */
function syncKindPicker() {
  const kind = $('#tc-kind').value;
  ['product', 'category', 'brand'].forEach(k => {
    const el = $('#tc-' + k);
    if (el) el.style.display = (k === kind) ? '' : 'none';
  });
}

function currentPick() {
  const kind = $('#tc-kind').value;
  const sel = $('#tc-' + kind);
  return { kind, sel, key: sel ? sel.value : '' };
}

/** 单对象曲线：绝对价位 / 链式指数 两种口径 */
async function drawSingle() {
  const { kind, key } = currentPick();
  const cc = $('#tc-country').value, days = $('#tc-days').value;
  if (!key) return Charts.empty('curve-chart', { title: '先选一个对象' });

  const r = await api(`/api/trend/series?kind=${kind}&key=${encodeURIComponent(key)}`
    + `&country=${cc}&days=${days}&by_channel=${$('#tc-bychannel').checked}`);
  setNote('tc-note', r.note || '');

  const s0 = r.series[0] || {};
  // 跨币种时后端不再给绝对价位，这里把开关拨到指数并说明原因 ——
  // 拨了不说等于骗人，不拨就只能给一张空图。
  const forced = r.mixed_currency && !$('#tc-index').checked;
  if (forced) $('#tc-index').checked = true;
  const showIndex = $('#tc-index').checked && s0.index;
  const series = r.series.map(s => ({
    name: s.name, pts: showIndex ? (s.index || s.pts) : s.pts,
  }));
  if (!series.some(s => s.pts.some(v => v != null))) {
    return Charts.empty('curve-chart', {
      title: '这个对象还画不出曲线',
      reason: r.insufficient
        ? `固定篮子只有 ${r.basket} 件商品，不足以代表整体`
        : '所选区间内没有足够的连续观测',
      action: '换个对象或拉长区间；曲线要连续采集几天才成形',
    });
  }
  Charts.ask('change', 'curve-chart', {
    xs: r.xs, series,
    ylab: showIndex ? '指数（基期=100）' : `价格（${s0.currency || '本币'}）`,
    fmt: showIndex ? (v => v == null ? '—' : v.toFixed(1))
      : (v => v == null ? '未采集' : Math.round(v).toLocaleString()),
    area: r.series.length === 1,
  });

  // 篮子/断点/币种要摆出来 —— 这些决定这条线可不可信
  const meta = [];
  if (r.basket != null) meta.push(`固定篮子 ${r.basket} 件（排除 ${r.dropped} 件出现天数不足的）`);
  if (r.breaks && r.breaks.length) {
    meta.push(`⚠ 指数在 ${r.breaks.join('、')} 处断开，断点两侧不可直接比`);
  }
  if (r.mixed_currency) {
    meta.push(`⚠ 篮子横跨 ${(r.currencies || []).join('/')} —— `
      + `绝对价位已停用${forced ? '，已自动切到指数口径' : ''}。`
      + `跨币种的中位数比的是币种不是价格：中位那件今天落在秘鲁索尔、`
      + `明天落在墨西哥比索，差一个数量级，画出来像崩盘其实一分没降`);
  }
  $('#tc-meta').innerHTML = meta.length
    ? `<div class="warn-line">${meta.map(esc).join('　·　')}</div>` : '';

  // ★ 聚合曲线画的是"一篮子商品"，但篮子本身是看不见的 ——
  //   用户第一次打开这页的原话是"没看懂这页在干什么，啥产品都没有"。
  //   把篮子成分列出来，这条线代表什么就具体了。
  const items = r.basket_items || [];
  $('#tc-basket').innerHTML = items.length ? `
    <details>
      <summary style="cursor:pointer;font-size:12.5px;color:var(--text-2)">
        这条线由 <b>${r.basket}</b> 件商品合成 —— 点开看是哪些
      </summary>
      <div class="table-wrap" style="margin-top:8px;max-height:320px;overflow:auto">
        <table><thead><tr><th>商品</th><th class="num">在架天数</th>
          <th class="num">最近价格</th></tr></thead><tbody>
          ${items.map(x => `<tr>
            <td style="font-size:11.5px">${x.url
              ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>`
              : esc(x.title)}</td>
            <td class="num">${x.days}</td>
            <td class="num">${x.last == null ? '—'
              : Math.round(x.last).toLocaleString() + ' ' + esc(x.cur || '')}</td>
          </tr>`).join('')}
        </tbody></table>
      </div>
      <div class="hint" style="margin-top:6px">
        只列在架天数最多的前 ${items.length} 件（篮子共 ${r.basket} 件）。
        点商品名可以打开原始页面核对价格。
      </div>
    </details>` : '';
}

/** 多对象对比：跨币种自动指数化 */
async function drawCompare() {
  const r = await fetch('/api/trend/compare', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entities: TREND_PICKS, days: Number($('#tc-days').value) }),
  }).then(x => x.json());
  setNote('tc-note', r.note || '');
  if (!(r.series || []).some(s => s.pts.some(v => v != null))) {
    return Charts.empty('curve-chart', {
      title: '选中的对象都还画不出曲线',
      reason: '需要每个对象在区间内有足够的连续观测',
      action: '减少对象，或拉长时间区间',
    });
  }
  Charts.ask('change', 'curve-chart', {
    xs: r.xs, series: r.series, ylab: r.unit,
    fmt: r.indexed ? (v => v == null ? '—' : v.toFixed(1))
      : (v => v == null ? '未采集' : Math.round(v).toLocaleString()),
  });
  $('#tc-meta').innerHTML = r.indexed
    ? `<div class="warn-line">已指数化：每条线以自己第一个有数据的日子为 100。
       跨币种时这是<b>唯一</b>能同屏的口径 —— 绝对价六国差三个数量级，
       放一根轴上会把小面值币种压成贴底直线。</div>` : '';
}

function addPick() {
  const { kind, sel } = currentPick();
  if (!sel || !sel.value) return;
  if (TREND_PICKS.length >= 8) return toast('最多同屏 8 条 —— 再多就分不清了');
  const e = { kind, key: sel.value, country: $('#tc-country').value || '',
              label: sel.options[sel.selectedIndex].text };
  if (TREND_PICKS.some(p => p.kind === e.kind && String(p.key) === String(e.key)
      && p.country === e.country)) return toast('已经在对比里了');
  TREND_PICKS.push(e); renderPicks(); drawCompare();
}

function dropPick(i) {
  TREND_PICKS.splice(i, 1); renderPicks();
  TREND_PICKS.length ? drawCompare() : drawSingle();
}

function clearPicks() { TREND_PICKS = []; renderPicks(); drawSingle(); }

function renderPicks() {
  $('#tc-picks').innerHTML = TREND_PICKS.length
    ? TREND_PICKS.map((p, i) => `<span class="pick">${esc(p.label)}${
        p.country ? ' · ' + esc(p.country) : ''}<a onclick="dropPick(${i})">×</a></span>`).join('')
      + `<button onclick="clearPicks()" style="margin-left:6px">清空</button>`
    : '<span class="hint">未加入对比 —— 当前显示单对象曲线。点「加入对比」可叠加多条。</span>';
}

/* ================================================================ 关注清单与预警 */

async function loadWatchBoard() {
  const d = await api('/api/watchlist');
  const wl = d.items || [];
  $('#wl-list').innerHTML = wl.length ? `<thead><tr>
    <th>优先级</th><th>对象</th><th class="num">降价阈值</th><th class="num">涨价阈值</th>
    <th class="num">未读</th><th>备注</th><th></th></tr></thead><tbody>`
    + wl.map(w => `<tr>
        <td><span class="prio p${w.priority[1]}">${esc(w.priority)}·${esc(w.priority_zh)}</span></td>
        <td><b>${esc(w.label)}</b></td>
        <td class="num">${w.eff_drop}%</td><td class="num">${w.eff_rise}%</td>
        <td class="num">${w.unread ? `<b style="color:var(--red)">${w.unread}</b>` : '0'}</td>
        <td style="font-size:11.5px;color:var(--text-2)">${esc(w.note || '')}</td>
        <td><button onclick="dropWatch(${w.id})">移除</button></td>
      </tr>`).join('') + '</tbody>'
    : `<tbody><tr><td><div class="empty"><div class="big">🎯</div>
        <h4>关注清单是空的</h4><p>预警<b>只针对清单里的对象</b> ——
        友商产品有两千多个，全都盯等于都不盯。
        从下面的候选里挑几个真正要盯的，设好优先级。</p></div></td></tr></tbody>`;

  const c = d.candidates || [];
  $('#wl-cand').innerHTML = `<thead><tr><th>品牌</th><th>机型</th><th>产业</th>
    <th class="num">观测天</th><th class="num">渠道</th><th class="num">变动</th>
    <th>加入并设优先级</th></tr></thead><tbody>`
    + c.map(p => `<tr>
        <td>${esc(p.brand)}</td><td><b>${esc(p.model_name)}</b></td><td>${esc(p.cat_zh)}</td>
        <td class="num">${p.obs_days}</td><td class="num">${p.channels}</td>
        <td class="num">${p.moves}</td>
        <td>${p.watched ? '<span class="hint">已在清单</span>'
          : ['P0', 'P1', 'P2'].map(pr =>
              `<button onclick="addWatch('product',${p.id},'${pr}')">${pr}</button>`).join(' ')}
        </td></tr>`).join('') + '</tbody>';

  const a = await api('/api/alerts?limit=60');
  const items = a.items || [];
  const zh = { credible: '可信', suspect: '存疑', implausible: '存疑' };
  $('#wl-alerts').innerHTML = items.length ? `<thead><tr>
    <th>优先级</th><th>对象</th><th class="num">幅度</th><th class="num">前 → 后</th>
    <th>可信度</th><th>日期</th></tr></thead><tbody>`
    + items.map(x => `<tr>
        <td><span class="prio p${(x.priority || 'P1')[1]}">${esc(x.priority)}</span></td>
        <td><b>${esc(x.label)}</b></td>
        <td class="num" style="color:${x.direction === 'up' ? 'var(--red)' : 'var(--green)'}">
          ${x.change_pct > 0 ? '+' : ''}${x.change_pct.toFixed(1)}%</td>
        <td class="num">${Math.round(x.prev_price).toLocaleString()} →
            ${Math.round(x.curr_price).toLocaleString()} ${esc(x.currency || '')}</td>
        <td><span class="tier t-${esc(x.tier)}">${esc(zh[x.tier] || x.tier)}</span></td>
        <td>${esc(x.alert_date)}</td></tr>`).join('') + '</tbody>'
    : `<tbody><tr><td><div class="empty"><p>还没有预警。
        先把对象加进关注清单，再点「扫描预警」。</p></div></td></tr></tbody>`;
}

async function addWatch(scope, key, priority) {
  await fetch('/api/watchlist/add', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope, key, priority, country: ($('#wl-country') || {}).value || '' }),
  });
  toast(`已加入关注（${priority}）`); loadWatchBoard();
}

async function dropWatch(id) {
  await fetch(`/api/watchlist/${id}/remove`, { method: 'POST' });
  loadWatchBoard();
}

async function addWatchScope() {
  const scope = $('#wl-scope').value;
  const key = scope === 'brand' ? $('#wl-brand').value : $('#wl-category').value;
  if (!key) return toast('先选一个对象');
  await addWatch(scope, key, $('#wl-prio').value);
}

async function scanAlerts() {
  const r = await fetch('/api/alerts/scan?days=7', { method: 'POST' }).then(x => x.json());
  toast(r.note, 8000); loadWatchBoard();
}

async function pushAlerts() {
  const r = await fetch('/api/alerts/push', { method: 'POST' }).then(x => x.json());
  toast(r.pushed ? `已推送 ${r.pushed} 条到 Telegram`
    : `没有推送：${r.message || '没有待推送的 P0/P1 预警'}`, 6000);
  loadWatchBoard();
}

/* ================================================================ 我的价格站位 */

const BAND_TONE = {
  high_hard: 'var(--red)', high_soft: 'var(--orange)',
  even: 'var(--green)', low_soft: 'var(--accent)', low_hard: 'var(--purple)',
  thin: 'var(--text-3)',
};

/** 组合层面的价格站位：一屏看完我该先看哪几款。 */
async function loadPosition() {
  const cc = $('#mf-country') ? $('#mf-country').value : '';
  const d = await api(`/api/position?country=${cc}`);
  setNote('pos-note',
    `${d.total} 个（产品×国家）组合`
    + (d.thin_n ? ` · ${d.thin_n} 个对位不足 ${d.min_field} 款，只给价差不下判断` : ''));
  $('#pos-sign').innerHTML =
    `<b>${esc(d.sign_note)}</b><br>${esc(d.note)}`;

  const solid = (d.items || []).filter(x => x.band !== 'thin');
  if (!solid.length) {
    Charts.empty('pos-chart', {
      title: '还没有足够的对位竞品来判断站位',
      reason: `需要每款产品在该国至少匹配到 ${d.min_field} 个对位机型`,
      action: '换个国家，或先在「竞品对照」页点重算匹配',
    });
  } else {
    // 站位 = 相对「对位中位价」这条基线的偏离，所以是 deviation
    Charts.ask('deviation', 'pos-chart', {
      rows: solid.slice(0, 16).map(x => ({
        label: `${x.my_name} · ${x.country_code}`,
        v: x.my_vs_field_pct,
        color: BAND_TONE[x.band],
        note: `我 ${Math.round(x.my_price).toLocaleString()} vs 对位中位 `
          + `${Math.round(x.field_median).toLocaleString()} ${x.currency}`
          + `（${x.rival_n} 款对位）`,
      })),
      upIsBad: true,          // ★ 我方更贵 = 需要警惕，红色一侧
      fmt: v => (v > 0 ? '+' : '') + v.toFixed(1) + '%',
      xlab: '我方相对对位中位价（正数=我更贵）',
      onPick: x => toast(`${x.label}：${x.note}`, 6000),
    });
  }

  $('#pos-table').innerHTML = `<thead><tr>
      <th>产品</th><th>国</th><th class="num">我方价</th>
      <th class="num">对位中位</th><th class="num">价差</th>
      <th class="num">对位数</th><th>站位</th><th>最像的对位机型</th>
    </tr></thead><tbody>`
    + (d.items || []).map(x => `<tr>
        <td><b>${esc(x.my_name)}</b></td>
        <td>${esc(x.country_code)}</td>
        <td class="num">${Math.round(x.my_price).toLocaleString()}</td>
        <td class="num">${Math.round(x.field_median).toLocaleString()} ${esc(x.currency)}</td>
        <td class="num" style="color:${x.band === 'thin' ? 'var(--text-3)'
          : (x.my_vs_field_pct > 0 ? 'var(--red)' : 'var(--green)')}">
          ${x.my_vs_field_pct > 0 ? '+' : ''}${x.my_vs_field_pct.toFixed(1)}%</td>
        <td class="num">${x.rival_n}</td>
        <td><span class="tier" style="color:${BAND_TONE[x.band]}">${esc(x.band_zh)}</span></td>
        <td style="font-size:11.5px;color:var(--text-2)">${
          (x.top_rivals || []).slice(0, 3).map(r =>
            `${esc(r.brand)} ${esc(r.model)}`).join('、')}</td>
      </tr>`).join('') + '</tbody>';
}
