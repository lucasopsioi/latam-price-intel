/* 图元原语层 —— 没有业务，只有形状。
 *
 * ★ 为什么分两层（这是从 nubimetrics 抄来的骨架，也是本文件存在的理由）：
 *   第一层（本文件）是「无业务的图元」：哑铃、区间条、发散条、热力、堆叠…
 *   第二层（boards.js）只做数据映射，每个业务图 10~30 行。
 *   混在一起写的后果是每张新图都要重新处理配色、暗色、空态、动画、resize，
 *   于是没人愿意加图，看板就永远停在表格阶段 —— 我们现在就卡在这。
 *
 * ★ 三条不可协商（同样来自参考实现，都是踩出来的）：
 *   1. **永不用双 Y 轴**。两条线的交叉点完全由各自缩放决定，是视觉巧合，
 *      而人眼一定会去读它。要对比两个量纲就用上下子图共享 X 轴。
 *   2. **方向语义必须显式声明**（upIsBad）。同一个看板里
 *      「价格涨 = 坏」和「销量涨 = 好」并存，靠调用方记是记不住的。
 *   3. **颜色语义会分岔**：单一方向语义的图用涨跌发散色；一旦同图存在两套
 *      相反语义、或需要表达实体身份（品牌），颜色必须改为编码实体/口径。
 *
 * ★ 配色从 CSS 变量实时读取，不写死 —— 这样图表自动跟随 app 的浅色/暗色，
 *   而不是自己维护一套主题（两套主题一定会漂移）。
 */
(function (global) {
  'use strict';

  var pool = new Map();          // elId -> ECharts 实例
  var specs = new Map();         // elId -> 最后一次的 (fn, opts)，主题切换时重放

  /* ---------------------------------------------------------------- 主题 */

  function tok(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function C() {
    return {
      text: tok('--text', '#1d1d1f'),
      text2: tok('--text-2', '#6e6e73'),
      text3: tok('--text-3', '#8e8e93'),
      line: tok('--line', 'rgba(0,0,0,.08)'),
      lineStrong: tok('--line-strong', 'rgba(0,0,0,.14)'),
      elev: tok('--bg-elev', '#fff'),
      accent: tok('--accent', '#007aff'),
      // ★ 涨跌色不用 --red/--green：那两个是界面语义色（危险/成功），
      //   图表要的是"变差/变好"，在暗色下需要各自的明度补偿。
      up: isDark() ? '#ff6961' : '#d70015',
      down: isDark() ? '#4ad06f' : '#0a7f2e',
      warn: tok('--orange', '#ff9500'),
    };
  }

  function isDark() {
    // 手动主题（data-theme）压过系统偏好；没设则跟随系统
    var t = document.documentElement.getAttribute('data-theme');
    if (t === 'light') return false;
    if (t === 'dark') return true;
    return matchMedia('(prefers-color-scheme: dark)').matches;
  }

  /** Acme身份色 —— 用户指定，任何逻辑都不许改写。 */
  var ACME = 'rgb(199, 0, 11)';

  /* ★ 序列槽位：这几个色在浅/暗两种底上都能相互分开。
   *   超过 8 条一律并成"其他"，**永不生成新色相** ——
   *   自动生成的第 9、10 个颜色必然和前面某个撞车，而撞车的两条线
   *   在图上就是一条线。 */
  var SLOTS_LIGHT = ['#2a78d6', '#eb6834', '#4a3aa7', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#8e8e93'];
  var SLOTS_DARK = ['#3987e5', '#d95926', '#9085e9', '#199e70', '#c98500', '#d55181', '#00a34a', '#98989d'];
  var OTHER = '#898781';

  var BRAND_COLOR = {
    'Acme': ACME, 'Acme': ACME,
    'Samsung': '#1428a0', 'Apple': '#7d7d82', 'Xiaomi': '#eb6834',
    'OPPO': '#008300', 'Honor': '#1baf7a', 'Motorola': '#e87ba4',
    'vivo': '#4a3aa7', 'Lenovo': '#7a3fa8', 'realme': '#eda100',
  };

  /** 给一组序列名分配颜色：品牌有身份色的用身份色，其余按槽位顺序。 */
  function assignColors(keys) {
    var slots = isDark() ? SLOTS_DARK : SLOTS_LIGHT;
    var used = {}, i = 0, map = {};
    keys.forEach(function (k) {
      if (BRAND_COLOR[k]) { map[k] = BRAND_COLOR[k]; used[BRAND_COLOR[k]] = 1; }
    });
    keys.forEach(function (k) {
      if (map[k]) return;
      while (i < slots.length && used[slots[i]]) i++;
      map[k] = i < slots.length ? slots[i++] : OTHER;
    });
    return map;
  }

  /* ---------------------------------------------------------------- 底座 */

  function inst(elId) {
    var el = document.getElementById(elId);
    if (!el) return null;
    var ch = pool.get(elId);
    // ★ 只在容器里没有 canvas 时才清空。
    //   直接 innerHTML='' 会删掉池化实例的画布而实例本身不知道，
    //   之后它继续往一个脱离文档的 canvas 上画 —— 整块白屏且不报任何错。
    if (ch && !ch.isDisposed() && el.querySelector('canvas')) return ch;
    if (ch && !ch.isDisposed()) ch.dispose();
    // ★★ 还要问 ECharts 自己：这个 DOM 上是不是挂着一个**池子不知道的**实例。
    //   池子会在两种情况下失忆：Charts.empty() 清过这个容器，
    //   或者主题切换时 repaint 到一半。此时直接 init 会叠出第二个 canvas，
    //   两张画布重叠、旧的那张永远不再更新 —— 实测热力图容器里堆了 3 个。
    var stray = echarts.getInstanceByDom(el);
    if (stray && !stray.isDisposed()) stray.dispose();
    el.innerHTML = '';
    ch = echarts.init(el, null, { renderer: 'canvas' });
    pool.set(elId, ch);
    return ch;
  }

  /** 所有图共用的底座：留白、提示框、动画、轴样式。 */
  function frame(o) {
    var c = C();
    return {
      backgroundColor: 'transparent',
      animation: !reduceMotion(),
      animationDuration: 520,
      animationDurationUpdate: 380,
      // ★ 缓动用 cubicOut：起步快、收尾慢，读数时元素已经停稳。
      //   默认的 linear 会让人在动画还在走的时候去读数，读到的是中间态。
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicOut',
      textStyle: { fontFamily: 'inherit', color: c.text2 },
      tooltip: {
        confine: true,
        backgroundColor: c.elev,
        borderColor: c.lineStrong,
        borderWidth: 1,
        padding: [8, 11],
        extraCssText: 'box-shadow:0 4px 20px rgba(0,0,0,.13);border-radius:8px',
        textStyle: { color: c.text, fontSize: 12, fontFamily: 'inherit' },
      },
      grid: { left: 8, right: 24, top: 24, bottom: 26, containLabel: true },
    };
  }

  function reduceMotion() {
    return matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function axisCat(o) {
    var c = C();
    return {
      type: 'category',
      axisLine: { lineStyle: { color: c.line } },
      axisTick: { show: false },
      axisLabel: {
        color: c.text2, fontSize: 11.5,
        width: (o && o.width) || 150, overflow: 'truncate',
      },
    };
  }

  function axisVal(fmt, name) {
    var c = C();
    return {
      type: 'value', name: name || '', nameTextStyle: { color: c.text3, fontSize: 11 },
      nameGap: 8,
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: c.text3, fontSize: 11, formatter: fmt },
      splitLine: { lineStyle: { color: c.line, type: 'dashed' } },
    };
  }

  var nfmt = function (v) {
    if (v == null || isNaN(v)) return '—';
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (a >= 1e4) return (v / 1e3).toFixed(0) + 'k';
    if (a >= 1e3) return (v / 1e3).toFixed(1) + 'k';
    return (Math.round(v * 10) / 10).toString();
  };
  var pfmt = function (v) { return (v > 0 ? '+' : '') + (Math.round(v * 10) / 10) + '%'; };

  /** 记住这张图是怎么画的，主题切换时原样重放。 */
  function remember(elId, fn, o) { specs.set(elId, { fn: fn, o: o }); }

  /* ---------------------------------------------------------------- 空态 */

  /**
   * ★ 空态是设计的一部分，不是兜底。
   *   "没有数据"和"这个筛选下没有数据"和"这个功能还没接通"
   *   在界面上长得一样，用户唯一能做的就是怀疑软件坏了。
   *   所以必须说清：缺什么、为什么、下一步能做什么。
   */
  function empty(elId, o) {
    var el = document.getElementById(elId);
    if (!el) return;
    var ch = pool.get(elId);
    if (ch && !ch.isDisposed()) { ch.dispose(); pool.delete(elId); }
    specs.delete(elId);
    el.innerHTML =
      '<div class="chart-empty">' +
      '<div class="ce-title">' + esc(o.title || '这个筛选下没有数据') + '</div>' +
      (o.reason ? '<div class="ce-reason">' + esc(o.reason) + '</div>' : '') +
      (o.action ? '<div class="ce-action">' + esc(o.action) + '</div>' : '') +
      '</div>';
  }

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"]/g, function (m) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m];
    });
  }

  /* ---------------------------------------------------------------- 图元 */

  /**
   * 哑铃图：两点一线，表达「从 A 变到 B」。
   * ★ 比两根并排柱子好在：起止值与变化幅度**同时可读**。
   *   并排柱只能读出两个高度，差值要自己心算。
   * rows: [{label, from, to, size?, hollow?, raw?}]
   */
  function dumbbell(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, dumbbell, o);
    var c = C(), fmt = o.fmt || nfmt;
    var rows = o.rows.slice().reverse();          // ECharts Y 轴自下而上
    var maxS = Math.max.apply(null, rows.map(function (r) { return r.size || 1; }).concat([1]));
    var upBad = o.upIsBad !== false;
    var colOf = function (r) { return ((r.to > r.from) === upBad) ? c.up : c.down; };
    var szOf = function (r) { return 6 + 9 * Math.sqrt((r.size || 1) / maxS); };

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 78, top: 26, bottom: 26, containLabel: true },
      xAxis: axisVal(fmt, o.xlab),
      yAxis: Object.assign(axisCat({ width: 170 }), { data: rows.map(function (r) { return r.label; }) }),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'item',
        formatter: function (p) {
          var r = rows[p.dataIndex]; if (!r) return '';
          var d = r.to - r.from, pc = r.from ? d / r.from * 100 : 0;
          return '<b>' + esc(r.label) + '</b><br>' +
            '前：' + fmt(r.from) + '<br>后：' + fmt(r.to) + '<br>' +
            '<span style="color:' + colOf(r) + '">变化：' + (d > 0 ? '+' : '') + fmt(d) +
            '（' + pfmt(pc) + '）</span>' +
            (r.note ? '<br><span style="opacity:.7">' + esc(r.note) + '</span>' : '');
        },
      }),
      series: [
        { // 连线
          type: 'custom', silent: true,
          renderItem: function (params, api) {
            var i = api.value(0);
            var a = api.coord([rows[i].from, i]), b = api.coord([rows[i].to, i]);
            return {
              type: 'line', shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] },
              style: { stroke: colOf(rows[i]), lineWidth: 2.5, lineCap: 'round' },
            };
          },
          data: rows.map(function (_, i) { return [i]; }),
        },
        { // 起点：空心
          type: 'scatter', symbolSize: function (_, p) { return szOf(rows[p.dataIndex]) * 0.72; },
          itemStyle: {
            color: c.elev,
            borderColor: function (p) { return colOf(rows[p.dataIndex]); }, borderWidth: 2,
          },
          data: rows.map(function (r) { return [r.from, r.label]; }),
        },
        { // 终点：实心（hollow=证据不足时改空心）
          type: 'scatter', symbolSize: function (_, p) { return szOf(rows[p.dataIndex]); },
          itemStyle: {
            color: function (p) { return rows[p.dataIndex].hollow ? 'transparent' : colOf(rows[p.dataIndex]); },
            borderColor: function (p) { return colOf(rows[p.dataIndex]); },
            borderWidth: function (p) { return rows[p.dataIndex].hollow ? 2 : 0; },
            borderType: function (p) { return rows[p.dataIndex].hollow ? 'dashed' : 'solid'; },
          },
          label: {
            show: true, position: 'right', distance: 8, fontSize: 11.5,
            fontFamily: 'ui-monospace,Consolas,monospace',
            color: function (p) { return colOf(rows[p.dataIndex]); },
            formatter: function (p) {
              var r = rows[p.dataIndex], pc = r.from ? (r.to - r.from) / r.from * 100 : 0;
              return pfmt(pc);
            },
          },
          data: rows.map(function (r) { return [r.to, r.label]; }),
        },
      ],
    }), true);
    bindPick(elId, ch, rows);
  }

  /**
   * 区间条：P25–P75 带中位刻线。价格分布的标准画法。
   * ★ 一个均价说不了任何事 —— 一个横跨入门到旗舰的品牌，
   *   它的均价落在中间那个价位上**一台机器都没有**。
   * rows: [{label, p25, med, p75, n?, ours?}]
   */
  function rangeBar(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, rangeBar, o);
    var c = C(), fmt = o.fmt || nfmt;
    var rows = o.rows.slice().reverse();
    var cmap = assignColors(rows.map(function (r) { return r.label; }));

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 68, top: 26, bottom: 26, containLabel: true },
      xAxis: axisVal(fmt, o.xlab),
      yAxis: Object.assign(axisCat({ width: 150 }), { data: rows.map(function (r) { return r.label; }) }),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'item',
        formatter: function (p) {
          var r = rows[p.dataIndex]; if (!r) return '';
          return '<b>' + esc(r.label) + '</b><br>' +
            'P25：' + fmt(r.p25) + '<br>中位：<b>' + fmt(r.med) + '</b><br>P75：' + fmt(r.p75) +
            '<br><span style="opacity:.7">区间宽度 ' + fmt(r.p75 - r.p25) +
            (r.n ? ' · ' + r.n + ' 条挂牌' : '') + '</span>';
        },
      }),
      series: [
        { // 用堆叠实现浮动条：透明打底到 P25，再画 P25→P75
          type: 'bar', stack: 'b', silent: true, itemStyle: { color: 'transparent' },
          data: rows.map(function (r) { return r.p25; }), barWidth: 13,
        },
        {
          type: 'bar', stack: 'b', barWidth: 13,
          itemStyle: {
            borderRadius: 2,
            color: function (p) {
              var r = rows[p.dataIndex];
              return r.ours ? ACME : cmap[r.label];
            },
            opacity: function (p) { return rows[p.dataIndex].ours ? 0.85 : 0.42; },
          },
          data: rows.map(function (r) { return r.p75 - r.p25; }),
          label: {
            show: true, position: 'right', distance: 8, fontSize: 11,
            color: c.text3, fontFamily: 'ui-monospace,Consolas,monospace',
            formatter: function (p) { return rows[p.dataIndex].n ? 'n=' + rows[p.dataIndex].n : ''; },
          },
        },
        { // 中位刻线
          type: 'custom', silent: true,
          renderItem: function (params, api) {
            var i = api.value(0), r = rows[i];
            var pt = api.coord([r.med, i]);
            var h = api.size([0, 1])[1] * 0.52;
            return {
              type: 'line',
              shape: { x1: pt[0], y1: pt[1] - h, x2: pt[0], y2: pt[1] + h },
              style: { stroke: r.ours ? ACME : cmap[r.label], lineWidth: 2.6, lineCap: 'round' },
            };
          },
          data: rows.map(function (_, i) { return [i]; }),
        },
      ],
    }), true);
    bindPick(elId, ch, rows);
  }

  /**
   * 发散条：以 0 为中心的左右条。适合「比基准高/低多少」。
   * ★ 只有一个关键数字时，发散条最直接 —— 不需要读轴就能看出方向和量级。
   * rows: [{label, v, color?}]
   */
  function diverge(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, diverge, o);
    var c = C(), fmt = o.fmt || pfmt;
    var rows = o.rows.slice().reverse();
    var upBad = o.upIsBad !== false;
    var colOf = function (r) { return r.color || (((r.v >= 0) === upBad) ? c.up : c.down); };

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 58, top: 24, bottom: 26, containLabel: true },
      xAxis: Object.assign(axisVal(fmt, o.xlab), {
        splitLine: { show: false },
        axisLine: { show: true, lineStyle: { color: c.lineStrong } },
      }),
      yAxis: Object.assign(axisCat({ width: 150 }), {
        data: rows.map(function (r) { return r.label; }),
        axisLine: { show: false },
      }),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'item',
        formatter: function (p) {
          var r = rows[p.dataIndex];
          return '<b>' + esc(r.label) + '</b><br>' + fmt(r.v) +
            (r.note ? '<br><span style="opacity:.7">' + esc(r.note) + '</span>' : '');
        },
      }),
      series: [{
        type: 'bar', barWidth: 13,
        itemStyle: { borderRadius: 2, color: function (p) { return colOf(rows[p.dataIndex]); } },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: c.lineStrong, width: 1 },
          data: [{ xAxis: 0 }], label: { show: false },
        },
        label: {
          show: true, fontSize: 11.5, fontFamily: 'ui-monospace,Consolas,monospace',
          position: function (p) { return rows[p.dataIndex].v >= 0 ? 'right' : 'left'; },
          distance: 6,
          color: function (p) { return colOf(rows[p.dataIndex]); },
          formatter: function (p) { return fmt(rows[p.dataIndex].v); },
        },
        data: rows.map(function (r) { return r.v; }),
      }],
    }), true);
    bindPick(elId, ch, rows);
  }

  /**
   * 热力图：两个分类维度交叉 + 一个数值。
   * ★ 有天然中性点的量**必须用发散色阶**（折扣率的大盘中位、表现指数的 1.0）。
   *   顺序色阶会把中性值画成中等深浅的某个颜色，读者无从知道分界在哪；
   *   发散色阶把中性点画成近乎无色，两侧红蓝，「偏强/偏弱」不看图例就能读。
   * ★ 没有观测到的格子要**留空**，不能填 0 —— 「没涨」和「没观测到」是两件事。
   */
  function heatmap(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, heatmap, o);
    var c = C(), fmt = o.fmt || nfmt, center = o.center || 0;
    var vals = o.cells.filter(function (k) { return k.v != null; }).map(function (k) { return Math.abs(k.v - center); });
    var mx = Math.max.apply(null, vals.concat([1e-9]));

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 12, top: 30, bottom: 24, containLabel: true },
      xAxis: Object.assign(axisCat({ width: 90 }), {
        data: o.xs, position: 'top',
        axisLabel: { color: c.text2, fontSize: 11.5, interval: 0 },
      }),
      yAxis: Object.assign(axisCat({ width: 90 }), { data: o.ys, inverse: true }),
      tooltip: Object.assign(frame(o).tooltip, {
        formatter: function (p) {
          var k = p.data.k;
          return '<b>' + esc(k.y) + ' · ' + esc(k.x) + '</b><br>' +
            (k.v == null ? '未观测到' : fmt(k.v) +
              '<br><span style="opacity:.7">基准 ' + fmt(center) + '，偏离 ' +
              ((k.v - center) > 0 ? '+' : '') + fmt(k.v - center) + '</span>');
        },
      }),
      series: [{
        type: 'heatmap',
        data: o.cells.map(function (k) {
          return { value: [k.x, k.y, k.v == null ? '-' : k.v], k: k };
        }),
        itemStyle: {
          borderColor: c.elev, borderWidth: 2, borderRadius: 3,
          color: function (p) {
            var v = p.data.k.v;
            if (v == null) return 'transparent';
            var t = (v - center) / mx;
            var base = t >= 0 ? c.up : c.down;
            return withAlpha(base, 0.1 + Math.min(1, Math.abs(t)) * 0.75);
          },
        },
        label: {
          show: true, fontSize: 11, fontFamily: 'ui-monospace,Consolas,monospace',
          formatter: function (p) {
            var v = p.data.k.v;
            return v == null ? '·' : fmt(v);
          },
          color: function (p) {
            var v = p.data.k.v;
            if (v == null) return c.text3;
            return Math.abs((v - center) / mx) > 0.55 ? '#fff' : c.text2;
          },
        },
        emphasis: { itemStyle: { borderColor: c.text, borderWidth: 2 } },
      }],
    }), true);
    ch.off('click');
    if (o.onPick) ch.on('click', function (p) { o.onPick(p.data && p.data.k); });
  }

  function withAlpha(col, a) {
    if (col.charAt(0) === '#') {
      var s = col.slice(1);
      if (s.length === 3) s = s.split('').map(function (x) { return x + x; }).join('');
      var n = parseInt(s, 16);
      return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
    }
    return col.replace(/^rgb\(/, 'rgba(').replace(/\)$/, ',' + a + ')');
  }

  /**
   * 100% 堆叠横条：构成对比。
   * ★ 段的顺序必须**固定**，不能按每行各自的大小排 ——
   *   顺序一变，跨行就没法对比同一段的位置了。
   */
  function stack100(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, stack100, o);
    var c = C();
    var rows = o.rows.slice().reverse();
    var cmap = o.colors || {};
    var slots = isDark() ? SLOTS_DARK : SLOTS_LIGHT;

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 16, top: 34, bottom: 24, containLabel: true },
      legend: {
        show: true, top: 0, left: 0, itemWidth: 10, itemHeight: 10, itemGap: 14,
        textStyle: { color: c.text2, fontSize: 11.5 }, icon: 'roundRect',
      },
      xAxis: Object.assign(axisVal(function (v) { return Math.round(v * 100) + '%'; }), {
        max: 1, splitLine: { show: false }, axisLabel: { show: false },
      }),
      yAxis: Object.assign(axisCat({ width: 130 }), { data: rows.map(function (r) { return r.label; }) }),
      tooltip: Object.assign(frame(o).tooltip, { trigger: 'axis', axisPointer: { type: 'shadow' } }),
      series: o.order.map(function (seg, i) {
        return {
          name: seg.name, type: 'bar', stack: 'x', barWidth: 15,
          itemStyle: { color: cmap[seg.k] || slots[i % slots.length] },
          data: rows.map(function (r) {
            var tot = o.order.reduce(function (a, s) { return a + (r[s.k] || 0); }, 0) || 1;
            return (r[seg.k] || 0) / tot;
          }),
          label: {
            show: true, fontSize: 10.5, color: '#fff',
            formatter: function (p) { return p.value > 0.09 ? Math.round(p.value * 100) + '%' : ''; },
          },
        };
      }),
    }), true);
  }

  /**
   * 四象限散点：表达两个指标的关系。
   * ★ 象限是它的核心价值 —— 「降价放量」和「涨价掉量」在图上是两个固定位置，
   *   看一眼就知道落在哪个格子，不用比对两列数字。
   */
  function scatter(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, scatter, o);
    var c = C();
    var groups = [];
    o.points.forEach(function (p) { if (p.group && groups.indexOf(p.group) < 0) groups.push(p.group); });
    var cmap = assignColors(groups);
    var maxS = Math.max.apply(null, o.points.map(function (p) { return p.size || 1; }).concat([1]));

    var marks = [];
    if (o.refX != null) marks.push({ xAxis: o.refX });
    if (o.refY != null) marks.push({ yAxis: o.refY });

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 20, top: 26, bottom: 32, containLabel: true },
      xAxis: axisVal(o.xFmt || nfmt, o.xlab),
      yAxis: axisVal(o.yFmt || nfmt, o.ylab),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'item',
        formatter: function (p) {
          var d = p.data.p;
          return '<b>' + esc(d.label) + '</b>' + (d.group ? ' · ' + esc(d.group) : '') + '<br>' +
            esc(o.xlab || 'x') + '：' + (o.xFmt || nfmt)(d.x) + '<br>' +
            esc(o.ylab || 'y') + '：' + (o.yFmt || nfmt)(d.y) +
            (d.size ? '<br><span style="opacity:.7">' + esc(o.sizeName || '量') + ' ' + nfmt(d.size) + '</span>' : '');
        },
      }),
      series: [{
        type: 'scatter',
        symbolSize: function (v, p) { return 9 + 20 * Math.sqrt((p.data.p.size || 1) / maxS); },
        itemStyle: {
          opacity: 0.72,
          color: function (p) { return p.data.p.group ? cmap[p.data.p.group] : c.accent; },
        },
        emphasis: { itemStyle: { opacity: 1, borderColor: c.text, borderWidth: 1.5 } },
        markLine: marks.length ? {
          silent: true, symbol: 'none',
          lineStyle: { color: c.lineStrong, width: 1, type: 'dashed' },
          data: marks, label: { show: false },
        } : undefined,
        data: o.points.map(function (p) { return { value: [p.x, p.y], p: p }; }),
      }],
    }), true);
    ch.off('click');
    if (o.onPick) ch.on('click', function (p) { o.onPick(p.data && p.data.p); });
  }

  /**
   * 斜率图：左右两根轴，每条数据一条线。
   * ★ 专门表达「方向与相对变化」—— 谁在涨、谁在跌、谁交叉了，一眼可见。
   */
  function slope(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, slope, o);
    var c = C(), fmt = o.fmt || nfmt;
    var upBad = o.upIsBad !== false;
    var groups = [];
    o.rows.forEach(function (r) { if (r.group && groups.indexOf(r.group) < 0) groups.push(r.group); });
    var cmap = assignColors(groups);

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 92, right: 92, top: 34, bottom: 26 },
      xAxis: {
        type: 'category', data: [o.fromLab || '上期', o.toLab || '本期'],
        boundaryGap: false, position: 'top',
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: c.text3, fontSize: 11.5 },
      },
      yAxis: Object.assign(axisVal(fmt), { splitLine: { show: false }, axisLabel: { show: false } }),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'item',
        formatter: function (p) {
          var r = o.rows[p.seriesIndex];
          var d = r.to - r.from, pc = r.from ? d / r.from * 100 : 0;
          return '<b>' + esc(r.label) + '</b><br>' + fmt(r.from) + ' → ' + fmt(r.to) +
            '<br><span style="color:' + (((d > 0) === upBad) ? c.up : c.down) + '">' + pfmt(pc) + '</span>';
        },
      }),
      series: o.rows.map(function (r, i) {
        var col = r.group ? cmap[r.group] : (((r.to > r.from) === upBad) ? c.up : c.down);
        return {
          name: r.label, type: 'line', data: [r.from, r.to],
          symbolSize: 7, lineStyle: { width: 2, color: col },
          itemStyle: { color: col },
          label: {
            show: true, fontSize: 11, color: col,
            fontFamily: 'ui-monospace,Consolas,monospace',
            formatter: function (p) { return p.dataIndex === 0 ? '' : fmt(p.value); },
            position: 'right', distance: 6,
          },
          endLabel: { show: false },
          markPoint: i >= 0 ? {
            silent: true, symbol: 'none',
            data: [{ coord: [0, r.from], value: r.label }],
            label: {
              position: 'left', distance: 8, color: c.text2, fontSize: 11.5,
              formatter: function () { return r.label; },
            },
          } : undefined,
        };
      }),
    }), true);
  }

  /**
   * 直方图：看分布形状。
   * ★ 均值会骗人 —— 「平均折扣 28%」既可能是大家都打 28 折，
   *   也可能是一半不打折一半打 5 折，这两种局面的应对完全相反。
   * bins: [{label, n, hot?}]
   */
  function hist(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, hist, o);
    var c = C();
    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: 16, top: 26, bottom: 30, containLabel: true },
      xAxis: Object.assign(axisCat({ width: 60 }), {
        data: o.bins.map(function (b) { return b.label; }),
        axisLabel: { color: c.text3, fontSize: 11, interval: 0, rotate: o.rotate || 0 },
      }),
      yAxis: axisVal(nfmt, o.ylab || '条数'),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var b = o.bins[ps[0].dataIndex];
          return '<b>' + esc(b.label) + '</b><br>' + b.n + ' 条' +
            (b.note ? '<br><span style="opacity:.7">' + esc(b.note) + '</span>' : '');
        },
      }),
      series: [{
        type: 'bar', barWidth: '72%',
        itemStyle: {
          borderRadius: [3, 3, 0, 0],
          color: function (p) { return o.bins[p.dataIndex].hot ? c.up : c.accent; },
          opacity: 0.85,
        },
        label: {
          show: true, position: 'top', fontSize: 11, color: c.text3,
          fontFamily: 'ui-monospace,Consolas,monospace',
          formatter: function (p) { return p.value || ''; },
        },
        data: o.bins.map(function (b) { return b.n; }),
      }],
    }), true);
    ch.off('click');
    if (o.onPick) ch.on('click', function (p) { o.onPick(o.bins[p.dataIndex]); });
  }

  /**
   * 折线：时间序列。
   * ★ 三条纪律：
   *   1. x 轴用**真实日期**，不是点的序号 —— 否则断采几天和连续采样长得一样。
   *   2. 缺口**不连线**（connectNulls:false）：没抓到 ≠ 价格没变。
   *   3. 不用双 Y 轴。
   */
  function line(elId, o) {
    var ch = inst(elId); if (!ch) return;
    remember(elId, line, o);
    var c = C(), fmt = o.fmt || nfmt;
    var cmap = assignColors(o.series.map(function (s) { return s.name; }));
    // 图例带固定篮子件数（s.n）；颜色仍按**裸品牌名**查身份色，
    // 否则 "Samsung·12件" 匹配不上 BRAND_COLOR，Acme红等身份色全丢
    var disp = function (s) { return s.n ? s.name + '·' + s.n + '件' : s.name; };

    ch.setOption(Object.assign(frame(o), {
      grid: { left: 8, right: o.series.length <= 8 ? 58 : 18,   // 线末标价要留白
              top: 30, bottom: 26, containLabel: true },
      legend: o.series.length > 1 ? {
        show: true, top: 0, left: 0, itemWidth: 14, itemHeight: 3, itemGap: 14,
        textStyle: { color: c.text2, fontSize: 11.5 },
      } : { show: false },
      xAxis: Object.assign(axisCat({ width: 80 }), {
        data: o.xs, boundaryGap: false,
        axisLabel: { color: c.text3, fontSize: 11 },
      }),
      yAxis: axisVal(fmt, o.ylab),
      tooltip: Object.assign(frame(o).tooltip, {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: c.lineStrong, type: 'dashed' } },
        formatter: function (ps) {
          var s = '<b>' + esc(ps[0].axisValue) + '</b>';
          ps.forEach(function (p) {
            s += '<br><span style="display:inline-block;width:8px;height:8px;border-radius:2px;' +
              'background:' + p.color + ';margin-right:6px"></span>' +
              esc(p.seriesName) + '：' + (p.value == null ? '未采集' : fmt(p.value))
              + (p.data && p.data.filled ? '（延续上次观测）' : '');
          });
          return s;
        },
      }),
      series: o.series.map(function (s) {
        // ★ 延续点（LOCF 填充的）画成小号半透明点，与真实观测区分开：
        //   曲线连续了，但哪天是真采到的、哪天是"挂牌价未变"的推定，一眼可辨。
        var data = s.filled
          ? s.pts.map(function (v, i) {
              return s.filled[i]
                ? { value: v, filled: true, symbolSize: 3,
                    itemStyle: { opacity: 0.35 } }
                : v;
            })
          : s.pts;
        var col = s.color || cmap[s.name];
        return {
          name: disp(s), type: 'line', data: data,
          connectNulls: false,             // ★ 缺口不连
          smooth: 0.25, symbolSize: 5, showSymbol: true,
          // ★ 线末标价（用户：图里没有价格，看不出结论）——
          //   shiftY 防重叠；≤8 条线时才开，再多就是糊墙
          endLabel: o.series.length <= 8 ? {
            show: true, fontSize: 10, fontWeight: 600, color: col,
            distance: 6,
            formatter: function (p) {
              var v = (p.value && p.value.value !== undefined) ? p.value.value : p.value;
              return v == null ? '' : fmt(v);
            },
          } : undefined,
          labelLayout: { moveOverlap: 'shiftY' },
          lineStyle: { width: 2.2, color: col },
          itemStyle: { color: col },
          areaStyle: o.area ? {
            opacity: 0.1, color: s.color || cmap[s.name],
          } : undefined,
          markArea: s.bands ? {
            silent: true,
            itemStyle: { color: withAlpha(c.warn, 0.1) },
            data: s.bands.map(function (b) { return [{ xAxis: b[0] }, { xAxis: b[1] }]; }),
          } : undefined,
        };
      }),
    }), true);
  }

  /* ------------------------------------------------------------ 交互与生命周期 */

  function bindPick(elId, ch, rows) {
    ch.off('click');
    var spec = specs.get(elId);
    var cb = spec && spec.o && spec.o.onPick;
    if (!cb) return;
    ch.on('click', function (p) {
      var r = rows[p.dataIndex];
      if (r) cb(r.raw || r);
    });
  }

  function resizeAll() {
    pool.forEach(function (ch) { if (!ch.isDisposed()) ch.resize(); });
  }

  function dispose(elId) {
    var ch = pool.get(elId);
    if (ch && !ch.isDisposed()) ch.dispose();
    pool.delete(elId); specs.delete(elId);
  }

  /** 主题切换：把每张图按原参数重画一遍（颜色从 CSS 变量重新读取）。 */
  function repaintAll() {
    specs.forEach(function (spec, elId) {
      var ch = pool.get(elId);
      if (ch && !ch.isDisposed()) ch.dispose();
      pool.delete(elId);
      spec.fn(elId, spec.o);
    });
  }

  var rt;
  window.addEventListener('resize', function () {
    clearTimeout(rt); rt = setTimeout(resizeAll, 120);
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', repaintAll);

  /* ══════════════════ 语义层：一个问题 = 一种图 ══════════════════
   *
   * ★ 为什么加这一层：用户的原话是「图表之前没有逻辑」。审计下来是真的 ——
   *   同一个问题在不同看板用了不同图形：
   *     bp-seller（谁贵谁便宜）用哑铃图，bp-own（我方 vs 友商，同一个问题）用发散条；
   *     voc-src（构成）用 100% 堆叠，voc-cov（覆盖率，也是构成）用发散条。
   *   单看每张都合理，连起来读没有叙事 —— 因为**图形不是按问题选的**。
   *
   * ★ 光在文档里写规矩没用，下次照样散。所以把规矩变成**唯一的调用入口**：
   *   看板不再直接挑图元，而是声明"我要回答哪个问题"，由这里决定用什么图。
   *   要改某类问题的画法，改这一处，全站一起变。
   */
  var GRAMMAR = {
    // 问题 → 图元。改这张表 = 全站同类图一起变
    compare:   { fn: dumbbell,  zh: '谁贵谁便宜（两点比较）' },
    change:    { fn: line,      zh: '涨跌了多少（时间序列）' },
    spread:    { fn: hist,      zh: '价位段密度（分布）' },
    position:  { fn: rangeBar,  zh: '价格带卡位（区间与落点）' },
    share:     { fn: stack100,  zh: '谁占多少（构成）' },
    deviation: { fn: diverge,   zh: '相对基线偏离多少' },
    intensity: { fn: heatmap,   zh: '哪里强/哪里狠（二维强度）' },
    relation:  { fn: scatter,   zh: '两个连续量的关系' },
    path:      { fn: slope,     zh: '两期之间各自怎么挪的' },
  };

  /** 按「问题」画图。question 见 GRAMMAR。 */
  function ask(question, elId, o) {
    var g = GRAMMAR[question];
    if (!g) {
      console.error('[Charts.ask] 未知问题类型：' + question +
                    '，可用：' + Object.keys(GRAMMAR).join('/'));
      return empty(elId, {
        title: '图表配置有误',
        reason: '声明了未知的问题类型「' + question + '」',
        action: '请在 charts.js 的 GRAMMAR 表里登记它，而不是绕过语义层直接调图元',
      });
    }
    o = o || {};

    // ── 硬规则①：跨币种只能画指数 ──
    // 六国六币种差三个数量级，绝对价放一根轴上会把小面值币种压成贴底直线；
    // 而跨币种的中位数比的是币种不是价格。
    if (question === 'change' && o.mixedCurrency && !o.indexed) {
      return empty(elId, {
        title: '跨币种不能比绝对价位',
        reason: '这批数据横跨 ' + ((o.currencies || []).join('/') || '多个币种') +
                '，绝对价的中位数比的是币种不是价格',
        action: '切到指数口径（基期=100）再看',
      });
    }
    // ── 硬规则②：缺口不连线（没采到 ≠ 价格为零）──
    if (question === 'change') o.connectNulls = false;

    return g.fn(elId, o);
  }

  global.Charts = {
    // ★ 首选入口：按问题画图
    ask: ask, GRAMMAR: GRAMMAR,
    // 图元仍然导出：语义层覆盖不到的一次性图形还得用，但**新看板一律走 ask**
    dumbbell: dumbbell, rangeBar: rangeBar, diverge: diverge, heatmap: heatmap,
    stack100: stack100, scatter: scatter, slope: slope, hist: hist, line: line,
    empty: empty, dispose: dispose, resizeAll: resizeAll, repaintAll: repaintAll,
    colors: C, assignColors: assignColors, ACME: ACME,
    fmt: { n: nfmt, pct: pfmt },
  };
})(window);
