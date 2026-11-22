/* A table somebody can actually work in.
 *
 * The tables before this printed what the engine returned, in the
 * order it was declared, at whatever width the content happened to
 * need. That is fine for reading a figure and wrong for the screen
 * people spend their day in: forty rows, a dozen columns, and the two
 * columns that matter today are the two you cannot see.
 *
 * So: columns resize, columns reorder, columns hide, and the
 * arrangement is remembered. A named view answers the question
 * somebody actually has -- what needs a decision, what is standing
 * still -- and the filters are for when none of them is quite it.
 *
 * Two deliberate limits.
 *
 * The arrangement is per module and per browser, in localStorage, not
 * on the server. It is a preference about a screen, not a fact about
 * the business, and syncing it would mean one person's column widths
 * arriving on somebody else's laptop.
 *
 * Only some cells can be typed into, and which ones comes from the
 * server rather than from here. Editing a fact about an item and
 * recording that forty units went out are different acts: the first
 * corrects something somebody wrote down, the second is a ledger
 * event that can only ever be reversed. A grid that let you type over
 * a quantity would put a number on screen that no movement supports.
 */

(function () {
  'use strict';

  let L, $, el;

  function bind() {
    if (L) return true;
    if (!window.LANDED) return false;
    L = window.LANDED;
    ({ $, el } = L);
    return true;
  }

  /* ------------------------------------------------------- remembering */

  function load(key) {
    try {
      return JSON.parse(localStorage.getItem(`landed.grid.${key}`)) || {};
    } catch (e) {
      return {};
    }
  }

  function save(key, state) {
    try {
      localStorage.setItem(`landed.grid.${key}`, JSON.stringify(state));
    } catch (e) { /* private browsing, a full quota: the grid still works */ }
  }

  /* How wide a column starts.
     
     With `table-layout: fixed` -- which resizing needs, because an
     auto table re-measures and throws away the width somebody just
     dragged -- a column with no width shares out whatever is left.
     Set none at all and every column is equal, so a four-character
     SKU gets as much room as a description and the figures end up
     pushed into the far corner.
     
     So each kind of column gets a sensible default and exactly one is
     left to absorb the slack: the one carrying a sentence, which is
     the only column that can use it. */
  const FLEXES = new Set([
    'description', 'name', 'summary', 'title', 'reason', 'label',
    'customer_name', 'fits', 'blurb', 'effect', 'fix', 'lines',
    'supplier', 'customer', 'who', 'what',
  ]);

  const WIDTH = {
    text: 150, said: 130, state: 110, bool: 90, date: 104,
    money: 118, qty: 96, num: 88, pct: 86, months: 86, hours: 90,
    days: 92, join: 320,
  };

  function startWidth(col) {
    if (FLEXES.has(col.key)) return null;
    return WIDTH[col.fmt] || 130;
  }

  /* ----------------------------------------------------------- the grid */

  function render(opts) {
    if (!bind()) return el('div');

    const {
      key,            // where the arrangement is remembered
      cols,           // [{ key, label, fmt, edit }]
      rows,
      fmt,            // the formatter table from platform.js
      numeric,        // which formats are right-aligned
      views = [],     // [{ id, label, blurb, keep(row) }]
      editable = [],  // column keys the server says may be typed into
      onEdit,         // (row, key, value) -> Promise
      onOpen,         // (row) -> void: the row's own screen
      flag,           // (row) -> class name
    } = opts;

    const state = load(key);
    state.widths = state.widths || {};
    state.hidden = state.hidden || [];
    state.order = state.order || [];
    state.view = state.view || (views[0] ? views[0].id : 'all');
    state.display = state.display || 'comfortable';

    const byKey = {};
    cols.forEach((c) => { byKey[c.key] = c; });

    /* Remembered order first, then anything added since -- so a new
       column appears rather than vanishing because an older
       arrangement never heard of it. */
    const ordered = () => {
      const seen = new Set();
      const out = [];
      state.order.forEach((k) => {
        if (byKey[k] && !seen.has(k)) { out.push(byKey[k]); seen.add(k); }
      });
      cols.forEach((c) => {
        if (!seen.has(c.key)) { out.push(c); seen.add(c.key); }
      });
      return out.filter((c) => !state.hidden.includes(c.key));
    };

    const root = el('div', 'grid');
    const bar = el('div', 'grid-bar');
    const panel = el('div', 'panel grid-panel');
    root.append(bar);
    root.append(panel);

    const draw = () => {
      panel.textContent = '';
      panel.append(table());
    };

    /* ------------------------------------------------------ the toolbar */

    if (views.length) {
      const chips = el('div', 'grid-views');
      views.forEach((v) => {
        /* Its own class, not `.view-chip`. That one belongs to the
           hand-built stock screen, which is still in the page and
           hidden -- so a selector for it finds something invisible
           first, and anything styling it reaches across two screens
           that have no reason to agree. */
        const b = el('button', 'grid-chip', v.label);
        b.type = 'button';
        if (v.id === state.view) b.dataset.on = '1';
        if (v.blurb && window.LANDED_PLATFORM) {
          window.LANDED_PLATFORM.explains(b, v.label, v.blurb);
        }
        b.addEventListener('click', () => {
          state.view = v.id;
          save(key, state);
          chips.querySelectorAll('button').forEach((x) => {
            delete x.dataset.on;
          });
          b.dataset.on = '1';
          draw();
        });
        chips.append(b);
      });
      bar.append(chips);
    }

    bar.append(el('span', 'grow'));

    const display = el('select', 'picker');
    display.setAttribute('aria-label', 'Row height');
    [['comfortable', 'Comfortable'], ['compact', 'Compact'],
      ['roomy', 'Roomy']].forEach(([v, label]) => {
      const o = el('option', '', label);
      o.value = v;
      display.append(o);
    });
    display.value = state.display;
    display.addEventListener('change', () => {
      state.display = display.value;
      save(key, state);
      panel.dataset.display = state.display;
    });
    bar.append(display);

    const pick = el('button', 'grid-btn', 'Columns');
    pick.type = 'button';
    pick.setAttribute('aria-haspopup', 'true');
    pick.addEventListener('click', () => {
      const open = root.querySelector('.grid-picker');
      if (open) { open.remove(); return; }
      const menu = el('div', 'grid-picker');
      cols.forEach((c) => {
        const row = el('label');
        const box = el('input');
        box.type = 'checkbox';
        box.checked = !state.hidden.includes(c.key);
        box.addEventListener('change', () => {
          state.hidden = box.checked
            ? state.hidden.filter((k) => k !== c.key)
            : [...state.hidden, c.key];
          save(key, state);
          draw();
        });
        row.append(box);
        row.append(el('span', '', c.label));
        menu.append(row);
      });
      const reset = el('button', 'grid-btn', 'Reset the layout');
      reset.type = 'button';
      reset.addEventListener('click', () => {
        state.widths = {};
        state.hidden = [];
        state.order = [];
        save(key, state);
        menu.remove();
        draw();
      });
      menu.append(reset);
      root.append(menu);
    });
    bar.append(pick);

    /* -------------------------------------------------------- the table */

    function table() {
      const shown = ordered();
      const keep = (views.find((v) => v.id === state.view) || {}).keep;
      const body = keep ? rows.filter(keep) : rows;

      const t = el('table', 'grid-table');
      const thead = el('thead');
      const hr = el('tr');

      /* Matches the action cell on every body row. Without it every
         column sits one place left of its heading, which looks like a
         data bug and is a markup bug. */
      if (onOpen) {
        const th = el('th', 'grid-open-cell');
        th.style.width = '64px';
        hr.append(th);
      }

      shown.forEach((c, index) => {
        const th = el('th', numeric.has(c.fmt) ? 'num' : '');
        th.append(el('span', 'th-label', c.label));
        const w = state.widths[c.key] || startWidth(c);
        if (w) th.style.width = `${w}px`;

        /* Reorder. Native drag and drop rather than pointer maths:
           it brings the drag image, the drop target and the keyboard
           story with it. */
        th.draggable = true;
        th.dataset.col = c.key;
        th.addEventListener('dragstart', (e) => {
          e.dataTransfer.setData('text/plain', c.key);
          e.dataTransfer.effectAllowed = 'move';
          th.dataset.dragging = '1';
        });
        th.addEventListener('dragend', () => { delete th.dataset.dragging; });
        th.addEventListener('dragover', (e) => {
          e.preventDefault();
          th.dataset.over = '1';
        });
        th.addEventListener('dragleave', () => { delete th.dataset.over; });
        th.addEventListener('drop', (e) => {
          e.preventDefault();
          delete th.dataset.over;
          const moved = e.dataTransfer.getData('text/plain');
          if (!moved || moved === c.key) return;
          const keys = ordered().map((x) => x.key);
          keys.splice(keys.indexOf(moved), 1);
          keys.splice(keys.indexOf(c.key), 0, moved);
          state.order = keys;
          save(key, state);
          draw();
        });

        /* Resize. The handle stops the drag starting, or every attempt
           to widen a column reorders it instead. */
        const grip = el('span', 'th-grip');
        grip.addEventListener('dragstart', (e) => e.preventDefault());
        grip.addEventListener('mousedown', (e) => {
          e.preventDefault();
          e.stopPropagation();
          th.draggable = false;
          const x0 = e.clientX;
          const w0 = th.getBoundingClientRect().width;
          const move = (ev) => {
            const w = Math.max(56, Math.round(w0 + ev.clientX - x0));
            state.widths[c.key] = w;
            th.style.width = `${w}px`;
          };
          const up = () => {
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', up);
            th.draggable = true;
            save(key, state);
          };
          document.addEventListener('mousemove', move);
          document.addEventListener('mouseup', up);
        });
        th.append(grip);
        hr.append(th);
      });
      thead.append(hr);
      t.append(thead);

      const tb = el('tbody');
      body.forEach((r) => {
        const tr = el('tr');
        if (flag) {
          const f = flag(r);
          if (f) tr.classList.add(f);
        }
        /* The way in to the row itself: its ledger, and the operations
           that write to it. Its own column rather than a click on the
           row, because a click on the row would fight the click that
           starts an edit -- and because somebody looking for where to
           enter a receipt should be able to see it rather than
           discover it. */
        if (onOpen) {
          const td = el('td', 'grid-open-cell');
          const b = el('button', 'grid-open', 'Open');
          b.type = 'button';
          b.title = 'Movements, and recording one';
          b.addEventListener('click', (e) => {
            e.stopPropagation();
            onOpen(r);
          });
          td.append(b);
          tr.append(td);
        }
        shown.forEach((c) => {
          const can = editable.includes(c.key);
          const td = el('td', numeric.has(c.fmt) ? 'num' : '');
          td.dataset.label = c.label;
          td.textContent = (fmt[c.fmt] || fmt.text)(r[c.key]);
          if (can && onEdit) {
            td.classList.add('can-edit');
            td.tabIndex = 0;
            td.title = 'Click to correct';
            const start = () => edit(td, r, c);
            td.addEventListener('dblclick', start);
            td.addEventListener('click', start);
            td.addEventListener('keydown', (e) => {
              /* Only when the cell itself has focus. Without this the
                 Enter that commits an edit bubbles up from the input
                 to the cell, whose handler opens a second editor --
                 so the value saved and the cell stayed in edit mode,
                 which reads as the save having failed. */
              if (e.target !== td) return;
              if (e.key === 'Enter') { e.preventDefault(); start(); }
            });
          }
          tr.append(td);
        });
        tb.append(tr);
      });
      t.append(tb);

      if (!body.length) {
        const empty = el('div', 'grid-empty');
        empty.append(L.empty('Nothing in this view',
          'No rows match it. Another view, or clear the filters.'));
        const wrap = el('div');
        wrap.append(t);
        wrap.append(empty);
        return wrap;
      }
      return t;
    }

    function edit(td, row, col) {
      if (td.querySelector('input')) return;
      const was = row[col.key];
      const input = el('input');
      const bool = typeof was === 'boolean';
      input.type = bool ? 'checkbox'
        : (numeric.has(col.fmt) ? 'number' : 'text');
      if (bool) input.checked = !!was;
      else input.value = was === null || was === undefined ? '' : was;
      if (input.type === 'number') input.step = 'any';
      td.textContent = '';
      td.append(input);
      input.focus();
      if (input.select) input.select();

      let done = false;
      const stop = (commit) => {
        if (done) return;
        done = true;
        const next = bool ? input.checked
          : (input.value === '' ? null
            : (input.type === 'number' ? Number(input.value) : input.value));
        td.textContent = (fmt[col.fmt] || fmt.text)(commit ? next : was);
        if (!commit || next === was) return;
        td.dataset.saving = '1';
        Promise.resolve(onEdit(row, col.key, next)).then((ok) => {
          delete td.dataset.saving;
          if (ok === false) {
            /* Put back what was there. A cell that keeps a value the
               server refused is a screen quietly disagreeing with the
               database. */
            row[col.key] = was;
            td.textContent = (fmt[col.fmt] || fmt.text)(was);
            td.dataset.refused = '1';
            setTimeout(() => { delete td.dataset.refused; }, 1800);
          } else {
            row[col.key] = next;
          }
        });
      };

      input.addEventListener('blur', () => stop(true));
      input.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== 'Escape') return;
        e.preventDefault();
        e.stopPropagation();
        stop(e.key === 'Enter');
      });
      if (bool) input.addEventListener('change', () => stop(true));
    }

    panel.dataset.display = state.display;
    draw();
    return root;
  }

  window.LANDED_GRID = { render };
}());
