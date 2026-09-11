/* ════════════════════════════════════════════════════════════════════
   grid.js —— 类 Excel 的可编辑表格（2026-09-07 用户要求）

   要点（逐条对应用户提的）：
     · 单击选中单元格、双击进入编辑、选中后**直接敲字**即覆盖原值
     · 状态栏实时给 求和 / 计数 / 平均（还有最大最小，顺手）
     · 列宽可拖（拖表头右边界；双击边界按内容自适应）
     · 冻结／取消冻结（首行、首列，或以当前选中格为界冻结）
     · 键盘：方向键移动、Shift+方向扩选、Tab/Enter 走格、Esc 取消、Delete 清空
     · Ctrl+C / Ctrl+V 走 TSV，能和 Excel 直接对拷

   实现取舍：
     · 用真实 <table> + position:sticky 做冻结 —— 虚拟滚动在几百行的量级上
       只会带来对不齐的麻烦，而本项目的录入表就是百行量级。
     · 列宽落在 <colgroup> 上，改一个 <col> 的 width 即可，不用逐格改样式。
     · 编辑器是**一个**浮在选中格上的 <input>，不是每格一个 input ——
       几百个 input 会让首屏卡顿，而且 Tab 序会乱。
   ════════════════════════════════════════════════════════════════════ */

(function (global) {
  'use strict';

  const A1 = n => {                        // 0 → A, 25 → Z, 26 → AA
    let s = '';
    n = n + 1;
    while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - m - 1) / 26; }
    return s;
  };
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  const isNum = v => v !== '' && v != null && isFinite(Number(String(v).replace(/,/g, '')));
  const num = v => Number(String(v).replace(/,/g, ''));
  const fmt = n => (Math.abs(n) >= 1000 ? n.toLocaleString('en-US', { maximumFractionDigits: 2 })
    : String(Math.round(n * 100) / 100));

  class Grid {
    /**
     * @param {HTMLElement} host 容器
     * @param {Object} opt
     *   columns: [{key, title, type:'text'|'number'|'select', width, options, readonly, align}]
     *   rows:    [{...}]  纯数据对象数组
     *   rowKey:  取行主键的函数（保存时回传）
     *   onDirty: (n) => void   脏格数变化
     */
    constructor(host, opt) {
      this.host = host;
      this.cols = (opt.columns || []).map(c => Object.assign({ width: 130, type: 'text' }, c));
      this.rows = opt.rows || [];
      this.rowKey = opt.rowKey || (r => r.id);
      this.onDirty = opt.onDirty || (() => { });
      this.freezeRows = opt.freezeRows || 0;      // 冻结的数据行数（表头永远冻）
      this.freezeCols = opt.freezeCols || 0;
      this.dirty = new Map();                     // "r:c" -> {row, key, old, val}
      this.sel = { r: 0, c: 0, r2: 0, c2: 0 };    // 选区（锚点 + 延伸点）
      this.editing = null;
      this._build();
      this.render();
    }

    /* ────────── DOM 骨架 ────────── */
    _build() {
      this.host.innerHTML = '';
      this.host.classList.add('xl');

      const bar = document.createElement('div');
      bar.className = 'xl-toolbar';
      bar.innerHTML = `
        <span class="xl-addr" title="当前单元格"></span>
        <span class="xl-sep"></span>
        <button data-act="freeze-head" title="冻结首行">冻结首行</button>
        <button data-act="freeze-col" title="冻结首列">冻结首列</button>
        <button data-act="freeze-here" title="以当前单元格为界冻结上方与左侧">冻结到此</button>
        <button data-act="unfreeze" title="取消全部冻结">取消冻结</button>
        <span class="xl-sep"></span>
        <button data-act="fit" title="按内容自适应所有列宽">列宽自适应</button>
        <button data-act="addrow" title="在末尾追加一行">＋ 行</button>
        <span class="xl-grow"></span>
        <span class="xl-dirty"></span>
        <button data-act="save" class="xl-save" disabled>保存</button>`;
      bar.addEventListener('click', e => {
        const b = e.target.closest('button[data-act]');
        if (b) this._toolbar(b.dataset.act);
      });

      const scroll = document.createElement('div');
      scroll.className = 'xl-scroll';
      const table = document.createElement('table');
      table.className = 'xl-table';
      scroll.appendChild(table);

      const status = document.createElement('div');
      status.className = 'xl-status';

      this.host.append(bar, scroll, status);
      this.el = { bar, scroll, table, status, addr: bar.querySelector('.xl-addr') };

      /* 一个复用的编辑器，浮在选中格上 */
      const ed = document.createElement('input');
      ed.className = 'xl-editor';
      ed.style.display = 'none';
      ed.addEventListener('keydown', e => this._editorKey(e));
      ed.addEventListener('blur', () => this.commit());
      scroll.appendChild(ed);
      this.el.editor = ed;

      table.addEventListener('mousedown', e => this._mousedown(e));
      table.addEventListener('dblclick', e => {
        if (e.target.closest('td')) this.beginEdit();
      });
      scroll.addEventListener('scroll', () => this._placeEditor());
      this.host.tabIndex = 0;
      this.host.addEventListener('keydown', e => this._key(e));
      this.host.addEventListener('copy', e => this._copy(e));
      this.host.addEventListener('paste', e => this._paste(e));
    }

    /* ────────── 渲染 ────────── */
    render() {
      const t = this.el.table;
      // ★ 左侧行号栏（gutter）：Excel 感最强的一件，也是定位行最快的方式。
      //   点行号选整行、点左上角全选 —— 与 Excel 一致。
      const cg = '<col style="width:46px">'
        + this.cols.map(c => `<col style="width:${c.width}px">`).join('');
      const th = '<th class="xl-corner" title="全选">◤</th>'
        + this.cols.map((c, i) =>
          `<th data-c="${i}" class="${c.align === 'right' ? 'num' : ''}">
           <span class="xl-th-t">${esc(c.title)}</span>
           <span class="xl-col-letter">${A1(i)}</span>
           <span class="xl-resizer" data-rz="${i}"></span></th>`).join('');
      const body = this.rows.map((row, r) =>
        `<tr data-r="${r}"><th class="xl-rowhead" data-rh="${r}">${r + 1}</th>`
        + this.cols.map((c, i) => {
          const v = row[c.key];
          const d = this.dirty.has(r + ':' + i);
          return `<td data-r="${r}" data-c="${i}" class="${c.align === 'right' || c.type === 'number' ? 'num' : ''}${d ? ' xl-dirty-cell' : ''}${c.readonly ? ' xl-ro' : ''}">${esc(v)}</td>`;
        }).join('') + '</tr>').join('');
      t.innerHTML = `<colgroup>${cg}</colgroup>
        <thead><tr>${th}</tr></thead><tbody>${body}</tbody>`;
      this._applyFreeze();
      this._paint();
      this._status();
      t.querySelectorAll('.xl-resizer').forEach(h =>
        h.addEventListener('mousedown', e => this._resizeStart(e)));
      t.querySelectorAll('.xl-resizer').forEach(h =>
        h.addEventListener('dblclick', e => {
          e.stopPropagation();
          this._fitCol(+h.dataset.rz);
        }));
    }

    /* 冻结：表头永远 sticky；额外冻结的行/列按累计偏移设 sticky */
    _applyFreeze() {
      const t = this.el.table;
      t.querySelectorAll('.xl-frozen-col, .xl-frozen-row').forEach(el => {
        el.classList.remove('xl-frozen-col', 'xl-frozen-row');
        el.style.left = ''; el.style.top = '';
      });
      // ★ 行号栏本身永远 sticky 在最左，宽 46px；
      //   冻结列的 left 偏移必须从它之后开始算，否则第一冻结列会盖在行号上。
      const GUT = 46;
      let left = GUT;
      for (let i = 0; i < this.freezeCols && i < this.cols.length; i++) {
        const w = this.cols[i].width;
        t.querySelectorAll(`th[data-c="${i}"], td[data-c="${i}"]`).forEach(el => {
          el.classList.add('xl-frozen-col');
          el.style.left = left + 'px';
        });
        left += w;
      }
      const headH = t.tHead ? t.tHead.getBoundingClientRect().height : 30;
      let top = headH;
      for (let r = 0; r < this.freezeRows && r < this.rows.length; r++) {
        const tr = t.querySelector(`tr[data-r="${r}"]`);
        if (!tr) break;
        const h = tr.getBoundingClientRect().height;
        tr.querySelectorAll('td').forEach(el => {
          el.classList.add('xl-frozen-row');
          el.style.top = top + 'px';
        });
        top += h;
      }
      this.host.classList.toggle('xl-has-freeze', this.freezeCols > 0 || this.freezeRows > 0);
      // 冻结分界线
      t.querySelectorAll('.xl-freeze-edge-c, .xl-freeze-edge-r')
        .forEach(el => el.classList.remove('xl-freeze-edge-c', 'xl-freeze-edge-r'));
      if (this.freezeCols > 0) {
        t.querySelectorAll(`th[data-c="${this.freezeCols - 1}"], td[data-c="${this.freezeCols - 1}"]`)
          .forEach(el => el.classList.add('xl-freeze-edge-c'));
      }
      if (this.freezeRows > 0) {
        const tr = t.querySelector(`tr[data-r="${this.freezeRows - 1}"]`);
        if (tr) tr.querySelectorAll('td').forEach(el => el.classList.add('xl-freeze-edge-r'));
      }
    }

    /* ────────── 选区绘制与状态栏 ────────── */
    _rect() {
      const s = this.sel;
      return {
        r1: Math.min(s.r, s.r2), r2: Math.max(s.r, s.r2),
        c1: Math.min(s.c, s.c2), c2: Math.max(s.c, s.c2),
      };
    }

    _paint() {
      const t = this.el.table;
      t.querySelectorAll('.xl-sel, .xl-anchor').forEach(el =>
        el.classList.remove('xl-sel', 'xl-anchor'));
      const q = this._rect();
      for (let r = q.r1; r <= q.r2; r++) {
        for (let c = q.c1; c <= q.c2; c++) {
          const td = t.querySelector(`td[data-r="${r}"][data-c="${c}"]`);
          if (td) td.classList.add('xl-sel');
        }
      }
      const a = t.querySelector(`td[data-r="${this.sel.r}"][data-c="${this.sel.c}"]`);
      if (a) a.classList.add('xl-anchor');
      // 表头与行号跟着高亮 —— Excel 靠这个让人一眼看出"我在第几行第几列"
      t.querySelectorAll('th[data-c]').forEach(th => {
        const c = +th.dataset.c;
        th.classList.toggle('xl-col-active', c >= q.c1 && c <= q.c2);
      });
      t.querySelectorAll('.xl-rowhead').forEach(rh => {
        const r = +rh.dataset.rh;
        rh.classList.toggle('xl-row-active', r >= q.r1 && r <= q.r2);
      });
      this.el.addr.textContent = (q.r1 === q.r2 && q.c1 === q.c2)
        ? A1(this.sel.c) + (this.sel.r + 1)
        : `${A1(q.c1)}${q.r1 + 1}:${A1(q.c2)}${q.r2 + 1}`;
    }

    _status() {
      const q = this._rect();
      const vals = [];
      let cells = 0, filled = 0;
      for (let r = q.r1; r <= q.r2; r++) {
        for (let c = q.c1; c <= q.c2; c++) {
          cells++;
          const v = this.rows[r] ? this.rows[r][this.cols[c].key] : '';
          if (v !== '' && v != null) { filled++; if (isNum(v)) vals.push(num(v)); }
        }
      }
      const el = this.el.status;
      if (!vals.length) {
        el.innerHTML = `<span class="xl-st"><b>计数</b>${filled}</span>
          <span class="xl-st"><b>选中</b>${cells} 格</span>
          <span class="xl-st xl-muted">选区内没有数值</span>`;
        return;
      }
      const sum = vals.reduce((a, b) => a + b, 0);
      el.innerHTML =
        `<span class="xl-st"><b>求和</b>${fmt(sum)}</span>
         <span class="xl-st"><b>平均</b>${fmt(sum / vals.length)}</span>
         <span class="xl-st"><b>计数</b>${filled}</span>
         <span class="xl-st"><b>数值</b>${vals.length}</span>
         <span class="xl-st"><b>最小</b>${fmt(Math.min(...vals))}</span>
         <span class="xl-st"><b>最大</b>${fmt(Math.max(...vals))}</span>
         <span class="xl-st xl-muted">选中 ${cells} 格</span>`;
    }

    /* ────────── 鼠标 ────────── */
    _mousedown(e) {
      if (e.target.classList.contains('xl-resizer')) return;
      // 行号 = 选整行；左上角 = 全选；表头 = 选整列（与 Excel 一致）
      const rh = e.target.closest('.xl-rowhead');
      if (rh) {
        this.commit();
        const r = +rh.dataset.rh;
        this.sel = { r, c: 0, r2: r, c2: this.cols.length - 1 };
        this._paint(); this._status(); this.host.focus();
        return;
      }
      if (e.target.closest('.xl-corner')) {
        this.commit();
        this.sel = { r: 0, c: 0, r2: this.rows.length - 1, c2: this.cols.length - 1 };
        this._paint(); this._status(); this.host.focus();
        return;
      }
      const chHead = e.target.closest('th[data-c]');
      if (chHead) {
        this.commit();
        const c = +chHead.dataset.c;
        this.sel = { r: 0, c, r2: Math.max(0, this.rows.length - 1), c2: c };
        this._paint(); this._status(); this.host.focus();
        return;
      }
      const td = e.target.closest('td');
      if (!td) return;
      this.commit();
      const r = +td.dataset.r, c = +td.dataset.c;
      if (e.shiftKey) { this.sel.r2 = r; this.sel.c2 = c; }
      else { this.sel = { r, c, r2: r, c2: c }; }
      this._paint(); this._status();
      this.host.focus();
      const move = ev => {
        const t2 = document.elementFromPoint(ev.clientX, ev.clientY);
        const td2 = t2 && t2.closest && t2.closest('td');
        if (td2 && td2.dataset.r !== undefined) {
          this.sel.r2 = +td2.dataset.r; this.sel.c2 = +td2.dataset.c;
          this._paint(); this._status();
        }
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
    }

    _resizeStart(e) {
      e.preventDefault(); e.stopPropagation();
      const i = +e.target.dataset.rz;
      const x0 = e.clientX, w0 = this.cols[i].width;
      this.host.classList.add('xl-resizing');
      const move = ev => {
        this.cols[i].width = Math.max(48, w0 + (ev.clientX - x0));
        const col = this.el.table.querySelectorAll('colgroup col')[i];
        if (col) col.style.width = this.cols[i].width + 'px';
        this._applyFreeze();
      };
      const up = () => {
        this.host.classList.remove('xl-resizing');
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    }

    _fitCol(i) {
      const probe = document.createElement('span');
      probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:12.5px system-ui';
      document.body.appendChild(probe);
      let w = 0;
      probe.textContent = this.cols[i].title; w = probe.offsetWidth + 34;
      for (const row of this.rows) {
        probe.textContent = String(row[this.cols[i].key] ?? '');
        w = Math.max(w, probe.offsetWidth + 22);
      }
      probe.remove();
      this.cols[i].width = Math.min(360, Math.max(56, w));
      this.render();
    }

    /* ────────── 键盘 ────────── */
    _key(e) {
      if (this.editing) return;
      const q = this._rect();
      const move = (dr, dc, extend) => {
        const nr = Math.max(0, Math.min(this.rows.length - 1, (extend ? this.sel.r2 : this.sel.r) + dr));
        const nc = Math.max(0, Math.min(this.cols.length - 1, (extend ? this.sel.c2 : this.sel.c) + dc));
        if (extend) { this.sel.r2 = nr; this.sel.c2 = nc; }
        else { this.sel = { r: nr, c: nc, r2: nr, c2: nc }; }
        this._paint(); this._status(); this._scrollIntoView();
        e.preventDefault();
      };
      const k = e.key;
      if (k === 'ArrowUp') return move(-1, 0, e.shiftKey);
      if (k === 'ArrowDown' || k === 'Enter') return move(1, 0, e.shiftKey && k !== 'Enter');
      if (k === 'ArrowLeft') return move(0, -1, e.shiftKey);
      // ★ 右箭头与 Tab 的 Shift 含义不同，别混在一条里：
      //   Shift+Right = **扩选**；Shift+Tab = 反向**移动**（不扩选）。
      //   第一版把两者写成同一句，结果 Shift+Right 变成了单纯右移，扩不出选区。
      if (k === 'ArrowRight') { e.preventDefault(); return move(0, 1, e.shiftKey); }
      if (k === 'Tab') { e.preventDefault(); return move(0, e.shiftKey ? -1 : 1, false); }
      if (k === 'Home') return move(0, -9999, e.shiftKey);
      if (k === 'End') return move(0, 9999, e.shiftKey);
      if (k === 'PageDown') return move(12, 0, e.shiftKey);
      if (k === 'PageUp') return move(-12, 0, e.shiftKey);
      if (k === 'F2') { e.preventDefault(); return this.beginEdit(); }
      if (k === 'Delete' || k === 'Backspace') {
        e.preventDefault();
        for (let r = q.r1; r <= q.r2; r++)
          for (let c = q.c1; c <= q.c2; c++) this._set(r, c, '');
        this.render();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === 'a') {
        e.preventDefault();
        this.sel = { r: 0, c: 0, r2: this.rows.length - 1, c2: this.cols.length - 1 };
        this._paint(); this._status();
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      // ★ 选中状态直接敲字 = 覆盖原值并进入编辑（Excel 行为）
      if (k.length === 1) { e.preventDefault(); this.beginEdit(k); }
    }

    _scrollIntoView() {
      const td = this.el.table.querySelector(`td[data-r="${this.sel.r}"][data-c="${this.sel.c}"]`);
      if (td) td.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    /* ────────── 编辑 ────────── */
    beginEdit(seed) {
      const { r, c } = this.sel;
      const col = this.cols[c];
      if (!col || col.readonly) return;
      const td = this.el.table.querySelector(`td[data-r="${r}"][data-c="${c}"]`);
      if (!td) return;
      this.editing = { r, c };
      const ed = this.el.editor;
      ed.value = seed !== undefined ? seed : (this.rows[r][col.key] ?? '');
      ed.style.display = 'block';
      ed.className = 'xl-editor' + (col.type === 'number' ? ' num' : '');
      this._placeEditor();
      ed.focus();
      if (seed === undefined) ed.select();
      else ed.setSelectionRange(ed.value.length, ed.value.length);
    }

    _placeEditor() {
      if (!this.editing) return;
      const { r, c } = this.editing;
      const td = this.el.table.querySelector(`td[data-r="${r}"][data-c="${c}"]`);
      const ed = this.el.editor;
      if (!td) { ed.style.display = 'none'; return; }
      const a = td.getBoundingClientRect(), b = this.el.scroll.getBoundingClientRect();
      ed.style.left = (a.left - b.left + this.el.scroll.scrollLeft) + 'px';
      ed.style.top = (a.top - b.top + this.el.scroll.scrollTop) + 'px';
      ed.style.width = a.width + 'px';
      ed.style.height = a.height + 'px';
    }

    _editorKey(e) {
      const k = e.key;
      if (k === 'Escape') { e.preventDefault(); this.editing = null; this.el.editor.style.display = 'none'; this.host.focus(); return; }
      if (k === 'Enter') { e.preventDefault(); this.commit(); this.sel.r = this.sel.r2 = Math.min(this.rows.length - 1, this.sel.r + 1); this._paint(); this._status(); this.host.focus(); return; }
      if (k === 'Tab') { e.preventDefault(); this.commit(); this.sel.c = this.sel.c2 = Math.min(this.cols.length - 1, this.sel.c + (e.shiftKey ? -1 : 1)); this._paint(); this._status(); this.host.focus(); return; }
      e.stopPropagation();
    }

    commit() {
      if (!this.editing) return;
      const { r, c } = this.editing;
      const v = this.el.editor.value;
      this.editing = null;
      this.el.editor.style.display = 'none';
      if (this._set(r, c, v)) this.render();
    }

    _set(r, c, v) {
      const col = this.cols[c];
      if (!col || col.readonly || !this.rows[r]) return false;
      const old = this.rows[r][col.key];
      if (col.type === 'number' && v !== '' && !isNum(v)) return false;   // 非法数字不写
      const val = col.type === 'number' && v !== '' ? num(v) : v;
      if (String(old ?? '') === String(val ?? '')) return false;
      this.rows[r][col.key] = val;
      const k = r + ':' + c;
      const first = this.dirty.get(k);
      this.dirty.set(k, { r, c, key: col.key, row: this.rows[r], old: first ? first.old : old, val });
      this._dirtyUI();
      return true;
    }

    _dirtyUI() {
      const n = this.dirty.size;
      this.el.bar.querySelector('.xl-dirty').textContent = n ? `${n} 处未保存` : '';
      this.el.bar.querySelector('.xl-save').disabled = !n;
      this.onDirty(n);
    }

    /* ────────── 剪贴板：与 Excel 互通的 TSV ────────── */
    _copy(e) {
      const q = this._rect();
      const out = [];
      for (let r = q.r1; r <= q.r2; r++) {
        const line = [];
        for (let c = q.c1; c <= q.c2; c++) line.push(this.rows[r][this.cols[c].key] ?? '');
        out.push(line.join('\t'));
      }
      e.clipboardData.setData('text/plain', out.join('\n'));
      e.preventDefault();
    }

    _paste(e) {
      const txt = e.clipboardData.getData('text/plain');
      if (!txt) return;
      e.preventDefault();
      const lines = txt.replace(/\r/g, '').replace(/\n$/, '').split('\n');
      const { r: r0, c: c0 } = this.sel;
      lines.forEach((line, dr) => line.split('\t').forEach((v, dc) => {
        this._set(r0 + dr, c0 + dc, v.trim());
      }));
      this.render();
    }

    /* ────────── 工具栏 ────────── */
    _toolbar(act) {
      if (act === 'freeze-head') { this.freezeRows = this.freezeRows ? 0 : 1; this.render(); }
      else if (act === 'freeze-col') { this.freezeCols = this.freezeCols ? 0 : 1; this.render(); }
      else if (act === 'freeze-here') { this.freezeRows = this.sel.r; this.freezeCols = this.sel.c; this.render(); }
      else if (act === 'unfreeze') { this.freezeRows = this.freezeCols = 0; this.render(); }
      else if (act === 'fit') { this.cols.forEach((_, i) => this._fitCol(i)); }
      else if (act === 'addrow') {
        const blank = {};
        this.cols.forEach(c => { blank[c.key] = ''; });
        this.rows.push(blank);
        this.render();
        this.sel = { r: this.rows.length - 1, c: 0, r2: this.rows.length - 1, c2: 0 };
        this._paint(); this._scrollIntoView();
      } else if (act === 'save') {
        if (this.onSave) this.onSave(this.changes());
      }
    }

    /** 按行聚合的改动，交给调用方保存 */
    changes() {
      const byRow = new Map();
      for (const d of this.dirty.values()) {
        const k = this.rowKey(d.row);
        if (!byRow.has(k)) byRow.set(k, { key: k, row: d.row, fields: {} });
        byRow.get(k).fields[d.key] = d.val;
      }
      return [...byRow.values()];
    }

    markSaved() { this.dirty.clear(); this._dirtyUI(); this.render(); }
    setRows(rows) { this.rows = rows; this.dirty.clear(); this._dirtyUI(); this.sel = { r: 0, c: 0, r2: 0, c2: 0 }; this.render(); }
  }

  global.Grid = Grid;
})(window);
