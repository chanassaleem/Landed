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
      view,           // open on this one, overriding what was remembered
      /* mark(row, col) -> reason, or nothing. A figure the data
         cannot yet support gets a visible mark and the reason on
         hover, instead of being printed to the same two decimal
         places as one that is settled. Used for a landed cost whose
         shipment has a charge that could not be divided properly: the
         number is the best available and worth showing, but a price
         set off it should be set knowingly. */
      mark,
      editable = [],  // column keys the server says may be typed into
      onEdit,         // (row, key, value) -> Promise
      onOpen,         // (row) -> void: the row's own screen
      flag,           // (row) -> class name
    } = opts;

    const state = load(key);
    state.widths = state.widths || {};
    state.hidden = state.hidden || [];
    state.order = state.order || [];
    /* An asked-for view wins over the remembered one: somebody who
       clicked "running out" wants to see what is running out, not
       whatever they were last looking at on this screen. */
    state.view = view || state.view || (views[0] ? views[0].id : 'all');
    state.display = state.display || 'comfortable';
    state.sort = state.sort || null;        // { key, dir } or none

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
      const say = sayEditable();
      if (say) panel.append(say);
    };

    /* Said once, under the table, because an affordance nobody knows
       about is not an affordance.
       
       Editing worked: the editor opened, Enter saved, the server was
       called. What was missing was any way to find out it was there.
       Of the eight columns the stock screen shows by default exactly
       one accepts typing, it was marked only on hover, and the hover
       mark was the colour of the table's own hairlines. Someone who
       did not happen to hover over that column and wait for a tooltip
       had no reason to think the table could be corrected at all. */
    const sayEditable = () => {
      const can = cols.filter((c) => editable.includes(c.key));
      if (!can.length || !onEdit) return null;
      const shownNow = ordered().filter((c) => editable.includes(c.key));
      const note = el('p', 'grid-note');
      const names = (shownNow.length ? shownNow : can)
        .map((c) => c.label);
      const list = names.length > 1
        ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
        : names[0];
      note.textContent = shownNow.length
        ? `Click ${list} to correct ${shownNow.length > 1 ? 'them' : 'it'}`
          + ' \u2014 the underlined headings are the columns you can type'
          + ' into.'
        : `${can.length} more columns can be corrected once you show`
          + ' them from Columns.';
      return note;
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

    /* ---------------------------------------------------------- sorting

       The grid had none. Every module screen draws its table through
       here, so for fourteen screens a column heading was something to
       drag or resize and not something to order by -- which is the
       first thing anybody does to a table of two hundred rows.

       Nulls sort last whichever way the column is pointing. A missing
       figure is not a zero and must not travel with the zeros: an item
       with no demand yet and an item selling nothing are different
       facts, and this product spends a lot of effort keeping them
       apart. */

    const NUMERIC_SORT = new Set(['money', 'qty', 'num', 'pct', 'months',
                                  'hours', 'days']);

    function compare(a, b, c) {
      const x = a[c.key];
      const y = b[c.key];
      const xEmpty = x === null || x === undefined || x === '';
      const yEmpty = y === null || y === undefined || y === '';
      if (xEmpty || yEmpty) return xEmpty && yEmpty ? 0 : (xEmpty ? 1 : -1);
      if (NUMERIC_SORT.has(c.fmt)) return Number(x) - Number(y);
      if (c.fmt === 'date') {
        const dx = Date.parse(x);
        const dy = Date.parse(y);
        if (!Number.isNaN(dx) && !Number.isNaN(dy)) return dx - dy;
      }
      /* localeCompare with numeric: true, so RGT-2 comes before
         RGT-10. A plain string sort puts it after, and a list of SKUs
         is the one place that looks most obviously broken. */
      return String(x).localeCompare(String(y), undefined,
                                     { numeric: true, sensitivity: 'base' });
    }

    function sorted(body) {
      const c = byKey[state.sort.key];
      const sign = state.sort.dir === 'descending' ? -1 : 1;
      /* A copy: `rows` is the caller's array, and sorting it in place
         would reorder what every other view of it sees. */
      return [...body].sort((a, b) => {
        const by = compare(a, b, c);
        // Empties stay last in both directions, so the sign is
        // applied to the comparison and not to their placement.
        if (by === 0) return 0;
        const xEmpty = a[c.key] === null || a[c.key] === undefined
          || a[c.key] === '';
        const yEmpty = b[c.key] === null || b[c.key] === undefined
          || b[c.key] === '';
        if (xEmpty !== yEmpty) return by;
        return by * sign;
      });
    }

    /* `column`, not `key`. Named `key` it shadowed the grid's own key
       -- the one every other save in here uses -- so the sort was
       written to localStorage under the name of the column instead of
       the name of the grid. It survived the click and vanished on
       reload, and left a stray `landed.grid.on_hand` entry behind as
       the only sign. */
    function sortBy(column) {
      const was = state.sort && state.sort.key === column
        ? state.sort.dir : null;
      // Ascending, then descending, then back to the order the server
      // sent -- which is usually the one that matters most, so it has
      // to be reachable again.
      state.sort = was === 'ascending' ? { key: column, dir: 'descending' }
        : was === 'descending' ? null
          : { key: column, dir: 'ascending' };
      save(key, state);
      draw();
    }

    /* -------------------------------------------------------- the table */

    function table() {
      const shown = ordered();
      const keep = (views.find((v) => v.id === state.view) || {}).keep;
      let body = keep ? rows.filter(keep) : rows;
      if (state.sort && byKey[state.sort.key]) body = sorted(body);

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

        /* A button inside the heading rather than a click on the
           heading itself. The th is already draggable for reordering
           and carries the resize handle, and a plain click handler on
           it fires at the end of a drag that was meant to move the
           column. A button also brings the keyboard and the screen
           reader with it. */
        const label = el('button', 'th-label');
        label.type = 'button';
        label.textContent = c.label;
        /* Which columns accept typing, said at the heading rather than
           on every cell.
           
           It was marked on the cell and only on hover, with a cue the
           same colour as the table's own hairlines -- so in the
           default view of eight columns, exactly one was editable and
           nothing on screen said so. The reasoning behind that was
           sound as far as it went: a table where a third of the cells
           advertise themselves is a table nobody can read. But the
           thing being advertised is the column, not the cell, and the
           heading is where a column says what it is. */
        if (editable.includes(c.key) && onEdit) {
          th.dataset.editable = '1';
          label.title = `${c.label} can be corrected here`;
        }
        const dir = state.sort && state.sort.key === c.key
          ? state.sort.dir : null;
        if (dir) {
          th.setAttribute('aria-sort', dir);
          th.dataset.sorted = dir === 'descending' ? 'desc' : 'asc';
        }
        label.setAttribute('aria-label', dir
          ? `${c.label}, sorted ${dir === 'descending' ? 'Z to A' : 'A to Z'}`
          : `Sort by ${c.label}`);
        label.addEventListener('click', () => sortBy(c.key));
        th.append(label);
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
          const why = mark ? mark(r, c) : null;
          if (why) {
            td.classList.add('unsure');
            /* Marked in the text, not only in colour: this says the
               figure will move, which colour alone cannot carry to
               somebody reading a printout or not seeing the hue. */
            const flag = el('span', 'unsure-mark', '~');
            flag.setAttribute('aria-hidden', 'true');
            td.prepend(flag);
            td.title = why;
            /* So a screen reader hears it as part of the value
               rather than as a stray tilde. */
            /* The figure, then the warning as a sentence. Reading
               "provisional: Provisional." aloud is how a label
               written by concatenation gives itself away. */
            td.setAttribute('aria-label',
              `${td.textContent.replace('~', '').trim()}. ${why}`);
          }
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
            /* A save that leaves no mark is a save nobody is sure
               about -- and this one can be triggered by clicking
               away, which is the easiest kind to miss. Brief, because
               the value itself is the real confirmation. */
            td.dataset.saved = '1';
            setTimeout(() => { delete td.dataset.saved; }, 1400);
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
