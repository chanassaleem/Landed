/* Landed — the shell.
   ----------------------------------------------------------------------
   No framework and no build step, for the same reason the engine is pure
   functions: the fewer moving parts between a figure and the screen, the
   fewer places a figure can go wrong on the way.

   One idea runs through all of it. The money line at the top is not a
   chart that sits above the content, it is the filter for the content.
   Click "on the water" and every screen below narrows to the stock that
   is on the water. The most prominent thing on the page is also the main
   control, which means there is one mental model rather than two.
   ------------------------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const S = {
  data: null,
  branch: 'ALL',
  segment: null,          // which part of the money line is selected
  view: 'today',
  actionOnly: false,
};

/* ------------------------------------------------------------ formatting
   Money is rounded to a unit a person can hold in their head. Nobody
   makes a reorder decision on the rupee, and eleven digits in a table
   cell is just noise to scroll past. */

const money = (n) => {
  if (n === null || n === undefined) return '—';
  const a = Math.abs(n);
  const s = n < 0 ? '−' : '';
  if (a >= 1e7) return `${s}${(a / 1e7).toFixed(2)}cr`;
  if (a >= 1e5) return `${s}${(a / 1e5).toFixed(1)}L`;
  if (a >= 1e3) return `${s}${Math.round(a / 1e3)}k`;
  return `${s}${Math.round(a)}`;
};

/* One unit for a whole set of figures.

   `money` picks a unit per figure, which is right for a figure standing
   on its own and wrong the moment two sit side by side: "2.7L" next to
   "2.35cr" cannot be compared without arithmetic, and a reader doing
   arithmetic has stopped reading. So a set of figures meant to be
   compared -- the money line, a column -- is scaled once, off the
   largest of them, and every figure in it carries the same unit. */
const UNITS = [
  { div: 1e7, suffix: 'cr', dp: 2 },
  { div: 1e5, suffix: 'L',  dp: 1 },
  { div: 1e3, suffix: 'k',  dp: 0 },
  { div: 1,   suffix: '',   dp: 0 },
];

function unit(values) {
  const vals = values.map((v) => Math.abs(v || 0)).filter((v) => v > 0);
  if (!vals.length) return UNITS[3];
  const max = Math.max(...vals);
  const min = Math.min(...vals);

  let i = UNITS.findIndex((u) => max >= u.div);
  if (i < 0) i = UNITS.length - 1;

  /* Step down while the smallest figure in the set would round to
     almost nothing. Scaling off the largest alone turns a real 2.7 lakh
     into "0.03cr" beside a 2.35cr -- technically the same unit, but the
     small figure has lost every digit that made it a figure. One step
     down costs the large one nothing while the set stays under 1000. */
  while (i < UNITS.length - 1
         && min / UNITS[i].div < 0.5
         && max / UNITS[i + 1].div < 1000) i += 1;
  return UNITS[i];
}

/* Zero is written as a dash, not 0.0L. A row of 0.0L reads as a
   measurement; a dash reads as nothing there, which is what it is. */
const inUnit = (n, u) => {
  if (n === null || n === undefined) return '—';
  if (n === 0) return '—';
  const s = n < 0 ? '−' : '';
  return `${s}${(Math.abs(n) / u.div).toFixed(u.dp)}${u.suffix}`;
};

const qty = (n) =>
  n === null || n === undefined ? '—' : Math.round(n).toLocaleString('en-US');

const months = (n) =>
  n === null || n === undefined ? '—' : n >= 100 ? '99+' : n.toFixed(1);

/* English plurals that the +s rule gets wrong. Short on purpose: this
   is a list of the words this interface actually uses, not an attempt
   at the language. "3 branchs" on the stock screen is the kind of
   mistake that makes a buyer wonder what else was not checked. */
const PLURALS = { branch: 'branches', box: 'boxes', batch: 'batches' };
const plural = (n, word) =>
  `${n} ${n === 1 ? word : PLURALS[word] || `${word}s`}`;

/* ------------------------------------------------------------------ nav */

/* Destinations, in the order the day runs: what needs deciding, then
   what you are deciding about, then the records behind it, then setup.
   `sep` draws a rule, which groups them without three more headings. */
const ICON = {
  today: 'M2 8h3l2-4 3 8 2-4h2',
  stock: 'M2 5.5l6-3 6 3v5l-6 3-6-3zM2 5.5l6 3 6-3M8 8.5v6',
  shipments: 'M2 11h12M3 11V7l4-2 4 2v4M6 7h3M8 2v3',
  items: 'M3 3h10v10H3zM3 6.5h10M6.5 6.5V13',
  import: 'M8 2v7M5 6.5L8 9.5l3-3M3 12.5h10',
  cash: 'M2 4.5h12v7H2zM2 7h12M4.5 9.5h2',
};

const VIEWS = [
  { id: 'today',     label: 'Today' },
  { sep: true },
  { id: 'stock',     label: 'Stock' },
  { id: 'shipments', label: 'On the water' },
  { sep: true },
  { id: 'items',     label: 'Items' },
  { sep: true },
  { id: 'import',    label: 'Import' },
  { id: 'cash',      label: 'Cash' },
];

/* The money line belongs to the screens it filters, not to the chrome.
   It made no sense sitting above the import screen. */
const WITH_LINE = new Set(['today', 'stock', 'shipments']);

function icon(path) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.4');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const d = document.createElementNS(ns, 'path');
  d.setAttribute('d', path);
  svg.append(d);
  return svg;
}

function buildNav() {
  const host = $('rail-items');
  VIEWS.forEach((v) => {
    if (v.sep) { host.append(el('div', 'sep')); return; }
    const a = el('a');
    a.href = `#${v.id}`;
    a.dataset.view = v.id;
    a.title = v.label;
    a.append(icon(ICON[v.id] || ICON.items));
    a.append(el('span', 'rail-label', v.label));
    a.append(el('span', 'count'));
    a.addEventListener('click', (e) => { e.preventDefault(); go(v.id); });
    host.append(a);
  });

  const pin = $('rail-pin');
  const rail = $('rail');
  try {
    if (localStorage.getItem('landed.rail') === '1') rail.dataset.pinned = '1';
  } catch (e) { /* storage can throw; the default stands */ }
  pin.addEventListener('click', () => {
    const on = rail.dataset.pinned === '1';
    rail.dataset.pinned = on ? '0' : '1';
    pin.setAttribute('aria-pressed', String(!on));
    try { localStorage.setItem('landed.rail', on ? '0' : '1'); } catch (e) {}
  });
}

function moveMarker() { /* the rail marks the current item in CSS */ }

function go(view) {
  S.view = view;
  document.querySelectorAll('.view').forEach((v) => {
    v.classList.toggle('on', v.id === `v-${view}`);
  });
  document.querySelectorAll('.rail a').forEach((a) => {
    if (a.dataset.view === view) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  const line = $('money-line');
  if (line) line.hidden = !WITH_LINE.has(view);
  closeDetail();
  $('main').scrollTop = 0;
  history.replaceState(null, '', `#${view}`);
}

/* ------------------------------------------------------------ money line
   Segment widths are shares of the total, with a floor so a small but
   real amount is still visible and still clickable. A segment that
   rounds to nothing on screen is a figure the person cannot reach. */

const SEGMENTS = [
  { key: 'on_order', label: 'On order',  cls: 's-order', note: 'placed, not shipped' },
  { key: 'on_water', label: 'On the water', cls: 's-water', note: 'shipped, not arrived' },
  { key: 'at_port',  label: 'At the port', cls: 's-port', note: 'arrived, not cleared' },
  { key: 'on_shelf', label: 'On the shelf', cls: 's-shelf', note: 'within policy' },
  { key: 'idle',     label: 'Standing still', cls: 's-idle', note: 'above the cover limit' },
  { key: 'running_out', label: 'Running out', cls: 's-out', note: 'below the reorder point' },
];

function renderLine() {
  const line = S.data.line;
  const band = $('band');
  const legend = $('legend');
  band.textContent = '';
  legend.textContent = '';

  const total = SEGMENTS.reduce((t, s) => t + (line[s.key] || 0), 0);
  const u = unit(SEGMENTS.map((s) => line[s.key] || 0));
  /* Whether a segment can hold its own label is a question about pixels,
     not percentages: 8% of a phone is nothing and 8% of a desktop is
     plenty. Measuring means the same band labels as much as it can at
     every width, instead of hiding a figure that would have fitted. */
  const bandWidth = band.getBoundingClientRect().width || 900;
  $('total').textContent = `${S.data.currency} ${money(total)}`;
  $('asof').textContent = `as at ${S.data.asof}`;
  $('foot-asof').textContent = S.data.asof;

  SEGMENTS.forEach((s) => {
    const v = line[s.key] || 0;
    const share = total > 0 ? v / total : 0;

    const b = el('button', `seg ${s.cls}`);
    b.type = 'button';
    b.style.width = '0%';          // animated to its share on the next frame
    b.title = `${s.label} — ${S.data.currency} ${inUnit(v, u)} (${s.note})`;
    b.setAttribute('aria-label', `${s.label}, ${S.data.currency} ${inUnit(v, u)}`);
    b.setAttribute('aria-pressed', String(S.segment === s.key));
    b.addEventListener('click', () => pick(s.key));

    /* The figure and its name live inside the segment when the segment is
       wide enough to hold them. A band you have to read against a legend
       underneath is two things to look at; this is one. Narrow segments
       fall back to the legend, which is why the legend still exists. */
    const wide = share * bandWidth >= 86;   // the label's real width
    if (wide) {
      const inner = el('span', 'seg-in');
      inner.append(el('span', 'seg-v', inUnit(v, u)));
      inner.append(el('span', 'seg-l', s.label.toLowerCase()));
      b.append(inner);
    }
    band.append(b);
    requestAnimationFrame(() => {
      b.style.width = `${Math.max(share * 100, v > 0 ? 1.5 : 0.4)}%`;
    });

    if (wide) return;             // already named inside the band
    const g = el('button', s.cls.replace('s-', 'l-'));
    g.type = 'button';
    g.append(el('span', 'dot'));
    g.append(el('span', 'v', inUnit(v, u)));
    g.append(el('span', null, s.label.toLowerCase()));
    g.addEventListener('click', () => pick(s.key));
    legend.append(g);
  });

  band.dataset.filtered = S.segment ? '1' : '0';
}

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (S.data) renderLine(); moveMarker(); }, 120);
});

function pick(key) {
  S.segment = S.segment === key ? null : key;
  renderLine();
  renderAll();
}

/* ---------------------------------------------------------- the panel
   What you clicked, beside what you clicked it in. The list keeps its
   scroll position and its filter, so acting on one line does not cost
   you your place among the other thirty-five -- which is the whole
   reason this is a panel and not a page. */

function closeDetail() {
  const d = $('detail');
  if (d) d.hidden = true;
  document.querySelectorAll('tr.picked').forEach(
    (r) => r.classList.remove('picked'));
}

function fact(label, value, said) {
  const f = el('div', said ? 'fact said' : 'fact');
  f.append(el('span', null, label));
  f.append(el('span', null, value));
  return f;
}

function block(title, facts) {
  const b = el('div', 'detail-block');
  if (title) b.append(el('h3', null, title));
  const g = el('div', 'facts');
  facts.forEach((f) => g.append(f));
  b.append(g);
  return b;
}

function openDetail(row, tr) {
  const d = $('detail');
  document.querySelectorAll('tr.picked').forEach(
    (r) => r.classList.remove('picked'));
  if (tr) tr.classList.add('picked');

  $('detail-title').textContent = row.description;
  $('detail-sub').textContent = `${row.sku} · ${row.branch}`;

  const u = unit([row.value, row.idle_value, row.unit_cost]);
  const body = $('detail-body');
  body.textContent = '';

  body.append(block('Position', [
    fact('On hand', qty(row.on_hand)),
    fact('On order', row.on_order ? qty(row.on_order) : '—'),
    fact('Months of cover', row.cover === null ? 'no demand yet'
                                               : months(row.cover)),
    fact('State', row.state, true),
  ]));

  body.append(block('Policy', [
    fact('Reorder at', qty(row.reorder_point)),
    fact('Order up to', qty(row.order_up_to ?? 0)),
    fact('Safety stock', qty(row.safety_stock ?? 0)),
    fact('Lead time', `${qty(row.lead_time_days ?? 0)} days`, true),
    fact('Demand pattern', row.demand_class || '—', true),
  ]));

  body.append(block('Money', [
    fact('Unit cost', `${S.data.currency} ${money(row.unit_cost)}`),
    fact('At cost', `${S.data.currency} ${money(row.value)}`),
    fact('Standing still', row.idle_value
         ? `${S.data.currency} ${money(row.idle_value)}` : '—'),
  ]));

  /* Said plainly, because the number above is the answer to a question
     and the question is worth stating once. */
  const why = el('div', 'detail-block');
  why.append(el('h3', null, 'Why it says that'));
  why.append(el('p', 'drop-sub', row.why || reasonFor(row)));
  why.style.textAlign = 'left';
  body.append(why);

  const acts = $('detail-actions');
  acts.textContent = '';
  if (row.suggested) {
    const b = el('button', 'primary', `Order ${qty(row.suggested)}`);
    b.type = 'button';
    b.style.width = 'auto';
    b.addEventListener('click', () => notYet('Raising a purchase order'));
    acts.append(b);
  }
  [['Receive stock', 'Receiving stock'],
   ['Record a sale', 'Recording a sale'],
   ['Adjust count', 'Adjusting a count']].forEach(([label, what]) => {
    const b = el('button', 'btn', label);
    b.type = 'button';
    b.addEventListener('click', () => notYet(what));
    acts.append(b);
  });

  d.hidden = false;
}

/* An action that does not exist yet says so, here, rather than doing
   nothing and leaving somebody clicking it twice. */
function notYet(what) {
  const acts = $('detail-actions');
  let note = document.getElementById('not-yet');
  if (!note) {
    note = el('p', 'drop-sub');
    note.id = 'not-yet';
    note.style.flexBasis = '100%';
    acts.append(note);
  }
  note.textContent = `${what} is not built yet. It is next, once the `
    + 'import is in.';
}

function reasonFor(row) {
  if (row.state === 'running out') {
    return 'Below the reorder point with under a month of cover, so an '
      + 'order placed today does not arrive before it runs dry.';
  }
  if (row.state === 'reorder') {
    return 'Below the reorder point. An order placed now arrives before '
      + 'the shelf is empty.';
  }
  if (row.state === 'standing still') {
    return 'More cover than the policy limit. The money is not lost, but '
      + 'it is not working, and another branch may want it.';
  }
  if (row.state === 'expiring') {
    return 'Enough stock, but some of it expires before it will sell.';
  }
  return 'Above the reorder point and inside the cover limit.';
}

$('detail-close').addEventListener('click', closeDetail);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('detail').hidden) closeDetail();
});

/* --------------------------------------------------------------- filters */

function rows() {
  let r = S.data.stock;
  if (S.branch !== 'ALL') r = r.filter((x) => x.branch === S.branch);
  if (S.segment) r = r.filter((x) => x.segment === S.segment);
  return r;
}

const NEEDS_DECISION = new Set(['running out', 'reorder', 'standing still', 'expiring']);

/* --------------------------------------------------------------- screens */

function renderToday() {
  const d = $('decks');
  d.textContent = '';
  const list = S.data.decisions;

  $('today-sub').textContent = list.length
    ? `${plural(list.length, 'thing')} worth your morning.`
    : 'Nothing needs you today.';

  /* The three figures are read against each other -- that is the whole
     point of putting them in a row -- so they share a unit too. */
  const du = unit(list.map((c) => c.value));

  list.forEach((c) => {
    const card = el('div', `deck d-${c.tone}`);
    const h = el('h3');
    h.append(c.title);
    card.append(h);
    card.append(el('div', 'big', `${S.data.currency} ${inUnit(c.value, du)}`));
    card.append(el('p', null, c.body));
    const b = el('button', 'act', c.action);
    b.type = 'button';
    b.addEventListener('click', () => { S.segment = c.segment; renderLine(); renderAll(); go(c.goto); });
    card.append(b);
    d.append(card);
  });

  const soon = rows()
    .filter((r) => r.cover !== null && r.cover < 3)
    .sort((a, b) => a.cover - b.cover)
    .slice(0, 8);

  $('soon').textContent = '';
  if (!soon.length) {
    $('soon').append(empty(
      'Nothing runs out inside three months',
      'Every line that has demand history has more than three months of cover at its branch.'));
    return;
  }
  $('soon').append(table(
    [['Item', ''], ['Branch', ''], ['Cover', 'num'], ['On hand', 'num'],
     ['Reorder at', 'num'], ['Order now', 'num'], ['State', '']],
    soon.map((r) => [
      cell(r.description, 'lead'),
      cell(r.branch, 'dim'),
      coverCell(r),
      cell(qty(r.on_hand), 'num'),
      cell(qty(r.reorder_point), 'num'),
      cell(r.suggested ? qty(r.suggested) : '—', 'num'),
      stateCell(r.state),
    ]), soon));
}

function renderStock() {
  let r = rows();
  if (S.actionOnly) r = r.filter((x) => NEEDS_DECISION.has(x.state));
  r = r.slice().sort((a, b) => (b.idle_value + b.shortfall_value) - (a.idle_value + a.shortfall_value));

  $('stock-sub').textContent = S.segment
    ? `${plural(r.length, 'line')} in ${SEGMENTS.find((s) => s.key === S.segment).label.toLowerCase()}.`
    : `${plural(r.length, 'line')} across ${plural(S.data.branches.length - 1, 'branch')}.`;

  const host = $('stock');
  host.textContent = '';
  if (!r.length) {
    host.append(empty('Nothing here',
      S.segment ? 'No stock is in that part of the journey right now. Click the band again to clear the filter.'
                : 'No stock matches this branch.'));
    return;
  }

  /* A column that is empty for every row is width spent on nothing, so
     it is not drawn. On order is empty until purchase orders are in the
     system, which for a business on its first day is all of them. */
  const anyOrdered = r.some((x) => x.on_order);
  const u = unit(r.flatMap((x) => [x.value, x.idle_value]));

  const cols = [['Item', ''], ['Branch', ''], ['Cover', 'num'],
                ['On hand', 'num']];
  if (anyOrdered) cols.push(['On order', 'num']);
  cols.push([`Value ${u.suffix}`.trim(), 'num'],
            [`Standing still ${u.suffix}`.trim(), 'num'], ['State', '']);

  host.append(table(cols, r.map((x) => {
    const cells = [
      cell(x.description, 'lead'),
      cell(x.branch, 'dim'),
      coverCell(x),
      cell(qty(x.on_hand), 'num'),
    ];
    if (anyOrdered) cells.push(cell(x.on_order ? qty(x.on_order) : '—', 'num'));
    cells.push(
      cell(inUnit(x.value, u), 'num'),
      cell(inUnit(x.idle_value, u), 'num idle'),
      stateCell(x.state),
    );
    return cells;
  }), r));
}

function renderShipments() {
  const s = S.data.shipments.filter(
    (x) => S.branch === 'ALL' || x.branch === S.branch);
  $('ship-sub').textContent = s.length
    ? `${plural(s.length, 'shipment')} between the supplier and your shelf.`
    : '';

  const host = $('shipments');
  host.textContent = '';
  if (!s.length) {
    host.append(empty('Nothing in transit',
      'Shipments appear here once a purchase order is marked as shipped.'));
    return;
  }

  host.append(table(
    [['Reference', ''], ['Supplier', ''], ['Mode', ''], ['Lines', 'num'],
     ['Goods', 'num'], ['Freight and duty', 'num'], ['Landed', 'num'],
     ['Arrives', ''], ['State', '']],
    s.map((x) => [
      cell(x.ref, 'lead mono'),
      cell(x.supplier, 'dim'),
      cell(x.mode, 'dim'),
      cell(qty(x.lines), 'num'),
      cell(money(x.goods), 'num'),
      cell(money(x.landed_cost), 'num'),
      cell(money(x.goods + x.landed_cost), 'num'),
      cell(x.eta, 'dim'),
      stateCell(x.state),
    ])));
}

/* ----------------------------------------------------------- table parts */

/* The same table, readable on a phone without scrolling sideways.

   Every column label is copied onto its own cell as a data attribute.
   At full width that is unused; below the breakpoint the stylesheet
   stops laying the rows out as a grid and shows each cell as a labelled
   line, so a row becomes a small record instead of the seventh column
   of something you have to drag into view.

   The alternative -- the one that was here -- was a minimum width and a
   horizontal scrollbar. Nothing is cut off, so it looks like it works,
   and in practice nobody drags a table sideways to find the number they
   came for. */
function table(cols, bodyRows, rows_) {
  const t = el('table');
  const thead = el('thead');
  const tr = el('tr');
  cols.forEach(([label, cls]) => tr.append(el('th', cls, label)));
  thead.append(tr);
  t.append(thead);

  const tb = el('tbody');
  bodyRows.forEach((cells, n) => {
    const r = el('tr');
    cells.forEach((c, i) => {
      const label = cols[i] ? cols[i][0] : '';
      if (label) c.dataset.label = label;
      r.append(c);
    });
    const row = (rows_ || [])[n];
    if (row) {
      r.dataset.click = '1';
      r.tabIndex = 0;
      r.addEventListener('click', () => openDetail(row, r));
      r.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openDetail(row, r);
        }
      });
    }
    tb.append(r);
  });
  t.append(tb);
  return t;
}

function cell(text, cls) {
  return el('td', cls, text);
}

/* Cover is the number that decides everything, so it gets the only bar in
   the table: the fill is months of cover against the target, and the tick
   is the target itself. The figure is still there to be read. */
function coverCell(r) {
  const td = el('td', 'num');
  if (r.cover === null) { td.textContent = '—'; return td; }
  const g = el('div', 'gauge');
  const tone = r.cover < 1 ? 'var(--urgent)'
    : r.cover < r.target_cover ? 'var(--attention)'
    : r.cover > r.excess_cover ? 'var(--idle)'
    : 'var(--text-3)';
  g.style.color = tone;
  const track = el('div', 'track');
  const fill = el('span', 'fill');
  const span = Math.max(r.excess_cover * 1.4, 1);
  fill.style.width = `${Math.min(r.cover / span, 1) * 100}%`;
  track.append(fill);
  const mark = el('span', 'mark');
  mark.style.left = `${Math.min(r.target_cover / span, 1) * 100}%`;
  track.append(mark);
  g.append(track);
  g.append(el('span', null, months(r.cover)));
  td.append(g);
  return td;
}

const STATE_CLASS = {
  'running out': 'st-out',
  'reorder': 'st-order',
  'standing still': 'st-idle',
  'within policy': 'st-ok',
  'no demand': 'st-ok',
  'no history': 'st-ok',
  'expiring': 'st-order',
  'on the water': 'st-water',
  'at the port': 'st-order',
  'cleared': 'st-ok',
};

function stateCell(state) {
  const td = el('td');
  const s = el('span', `state ${STATE_CLASS[state] || 'st-ok'}`, state);
  td.append(s);
  return td;
}

function empty(title, body) {
  const d = el('div', 'empty');
  d.append(el('strong', null, title));
  d.append(el('p', null, body));
  return d;
}

/* --------------------------------------------------------------- palette */

function palette() {
  const scrim = $('scrim');
  const q = $('q');
  const results = $('results');
  let items = [];
  let sel = 0;

  const open = () => {
    scrim.hidden = false;
    q.value = '';
    draw('');
    q.focus();
  };
  const close = () => { scrim.hidden = true; };

  const draw = (term) => {
    const t = term.trim().toLowerCase();
    items = [];
    VIEWS.forEach((v) => items.push({
      label: `Go to ${v.label}`, hint: 'view', run: () => go(v.id),
    }));
    S.data.stock.forEach((r) => items.push({
      label: r.description, hint: `${r.sku} · ${r.branch}`,
      run: () => { S.segment = null; renderLine(); go('stock'); },
    }));
    S.data.shipments.forEach((x) => items.push({
      label: x.ref, hint: x.supplier, run: () => go('shipments'),
    }));
    if (t) {
      items = items.filter((i) =>
        `${i.label} ${i.hint}`.toLowerCase().includes(t));
    }
    items = items.slice(0, 40);
    sel = 0;
    paint();
  };

  const paint = () => {
    results.textContent = '';
    items.forEach((i, n) => {
      const li = el('li', null);
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', String(n === sel));
      li.append(el('span', null, i.label));
      li.append(el('span', 'hint', i.hint));
      li.addEventListener('click', () => { i.run(); close(); });
      results.append(li);
    });
  };

  q.addEventListener('input', () => draw(q.value));
  q.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, items.length - 1); paint(); e.preventDefault(); }
    if (e.key === 'ArrowUp')   { sel = Math.max(sel - 1, 0); paint(); e.preventDefault(); }
    if (e.key === 'Enter' && items[sel]) { items[sel].run(); close(); }
    if (e.key === 'Escape') close();
  });
  scrim.addEventListener('click', (e) => { if (e.target === scrim) close(); });
  $('open-palette').addEventListener('click', open);

  addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      scrim.hidden ? open() : close();
    }
    if (e.key === 'Escape' && !scrim.hidden) close();
  });
}

/* ------------------------------------------------------------------ boot */

function renderAll() {
  renderToday();
  renderStock();
  renderShipments();
  counts();
}

function counts() {
  const needing = S.data.stock.filter(
    (r) => (S.branch === 'ALL' || r.branch === S.branch) && NEEDS_DECISION.has(r.state));
  const out = needing.filter((r) => r.state === 'running out').length;
  const set = (view, n, alarm) => {
    // The rail, not the old horizontal nav. Guarded because a count for
    // a destination that does not exist should do nothing, not throw
    // and take the rest of the render down with it.
    const a = document.querySelector(`.rail a[data-view="${view}"]`);
    if (!a) return;
    a.querySelector('.count').textContent = n ? String(n) : '';
    a.dataset.alarm = alarm ? '1' : '0';
  };
  set('today', out, out > 0);
  set('stock', needing.length, false);
  set('shipments', S.data.shipments.length, false);
  set('items', new Set(S.data.stock.map((r) => r.sku)).size, false);
}

function branches() {
  const sel = $('branch');
  sel.textContent = '';
  S.data.branches.forEach((b) => {
    const o = el('option', null, b === 'ALL' ? 'All branches' : b);
    o.value = b;
    sel.append(o);
  });
  sel.value = S.branch;
  sel.addEventListener('change', () => {
    S.branch = sel.value;
    $('foot-scope').textContent =
      S.branch === 'ALL' ? 'All branches' : `Branch ${S.branch}`;
    renderAll();
  });
}

function theme() {
  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('landed.theme', next); } catch (e) { /* fine */ }
  });
}

/* The app is open to anyone when auth is off. That is useful for a
   demo and dangerous as a default, so it is stated on the screen
   rather than left to be discovered. */
async function announceOpenAccess() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    if (d.auth_configured) return;
    const b = document.getElementById('sample-banner');
    if (!b) return;
    b.innerHTML =
      '<b>Sign-in is off.</b> <span>No Supabase credentials are set, so ' +
      'this deployment is open to anyone with the link and is showing ' +
      'sample data. <a href="/health/auth">What is missing</a></span>';
    b.classList.add('warn');
  } catch (e) {}
}

/* Before anything else: does this account have an organisation?
   Everything that writes needs one, and an app that renders first and
   discovers it later shows a dashboard that cannot be used and gives
   no reason. */
async function needsOrganisation() {
  try {
    const r = await fetch('/api/me');
    const d = await r.json();
    if (!d.signed_in) return false;          // auth is off; sample data
    if (d.organisations === null) return false;   // database unreachable
    return d.organisations.length === 0;
  } catch (e) {
    return false;
  }
}

function showSetup() {
  $('setup').hidden = false;
  $('app').hidden = true;
  const form = $('setup-form');
  const err = $('setup-error');
  $('org-name').focus();

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('org-name').value.trim();
    if (!name) { err.hidden = false; err.textContent = 'Give it a name.'; return; }
    const btn = $('setup-go');
    btn.disabled = true;
    btn.textContent = 'Creating…';
    err.hidden = true;
    try {
      const r = await fetch('/api/organisation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'That did not work.');
      location.reload();
    } catch (ex) {
      err.hidden = false;
      err.textContent = ex.message;
      btn.disabled = false;
      btn.textContent = 'Create it';
    }
  }, { once: false });
}

async function boot() {
  if (await needsOrganisation()) { showSetup(); return; }
  buildNav();
  theme();
  wireImport();

  const res = await fetch('/api/state');
  if (!res.ok) {
    $('main').textContent = '';
    $('main').append(empty('Could not load',
      'The server answered, but not with data. Try again in a moment.'));
    return;
  }
  S.data = await res.json();

  branches();
  $('foot-scope').textContent = 'All branches';
  $('foot-source').textContent = S.data.source;
  $('sample-banner').hidden = S.data.source !== 'Sample data';
  announceOpenAccess();

  renderLine();
  renderAll();
  palette();

  $('only-action').addEventListener('click', (e) => {
    S.actionOnly = !S.actionOnly;
    e.target.textContent = S.actionOnly
      ? 'Show every line' : 'Only what needs a decision';
    renderStock();
  });

  const start = (location.hash || '#today').slice(1);
  go(VIEWS.some((v) => v.id === start) ? start : 'today');
  addEventListener('resize', moveMarker);
}

/* boot() is called at the very bottom of this file, after every
   declaration it reaches. Called from here it ran before the
   import block below had been evaluated, and touching a `let`
   from above its declaration is an error, not a hoist. */

/* ---------------------------------------------------------- importing
   Two steps, because an import is not undoable and the first anyone
   should know about what it does is before it does it. The file is
   sent twice rather than held on the server between the steps: server
   state would need a lifetime and a cleanup, and reading a spreadsheet
   again costs nothing. */

let pendingFile = null;

/* Read a response without assuming it is JSON.

   Twice now a failure has reached the screen as "Unexpected end of
   JSON input", which says only that the body was not JSON -- not what
   the body was, not what the status was, not what to do. The cases
   that actually happen here all produce it: a sleeping instance
   answering with its host's waking-up page, a gateway returning 502
   with an empty body, a redirect that swallowed the POST.
   
   So: look at the status and the content type first, and say
   something the person can act on. */
async function readResponse(r, what) {
  const type = (r.headers.get('content-type') || '').toLowerCase();
  const text = await r.text();

  if (type.includes('application/json')) {
    try {
      return { ok: r.ok, data: JSON.parse(text) };
    } catch (e) {
      throw new Error(
        `${what} came back as damaged JSON (HTTP ${r.status}).`);
    }
  }

  if (!text.trim()) {
    throw new Error(
      `${what} came back empty (HTTP ${r.status}). On a free Render `
      + 'instance this usually means the server was asleep and is '
      + 'starting up. Wait about thirty seconds and try again.');
  }
  if (/<html|application loading|service waking/i.test(text)) {
    throw new Error(
      'The server is still starting up, so the host answered instead of '
      + 'the app. Wait about thirty seconds and try again.');
  }
  if (r.status === 413) {
    throw new Error('That file is too large to upload.');
  }
  throw new Error(
    `${what} came back as ${type || 'an unknown type'} (HTTP ${r.status}): `
    + text.trim().slice(0, 160));
}

function importReport(d) {
  const host = $('import-report');
  host.textContent = '';
  host.hidden = false;

  const c = d.counts || {};
  const head = el('div', 'section-head');
  const v = el('div', 'verdict');
  v.append(el('b', null, d.ok ? 'Ready to write' : 'Not ready yet'));
  v.append(el('span', null, d.ok
    ? 'Nothing has been written. Check the figures, then write it.'
    : 'Fix what is listed below and upload the file again.'));
  head.append(v);
  host.append(head);

  const rows = [
    ['Branches', c.branches], ['Suppliers', c.suppliers],
    ['Items', c.items], ['Stock lines', c.stock],
    ['Months of history', c.history], ['Open orders', c.orders],
    ['Shipment costs', c.shipment_costs],
  ].filter(([, n]) => n !== undefined);

  host.append(table(
    [['What', ''], ['Rows', 'num']],
    rows.map(([label, n]) => [cell(label, 'lead'), cell(qty(n), 'num')])));

  if (d.value) {
    const p = el('p', 'drop-sub',
      `Opening stock at cost: ${S.data ? S.data.currency : 'PKR'} `
      + `${money(d.value)}. Sense-check that against your own figure `
      + 'before writing.');
    p.style.padding = '10px 14px';
    host.append(p);
  }

  if (d.problems && d.problems.length) {
    const h = el('div', 'section-head');
    h.append(el('h2', null,
      d.problem_count > d.problems.length
        ? `Problems (showing ${d.problems.length} of ${d.problem_count})`
        : 'Problems'));
    host.append(h);
    host.append(table(
      [['Sheet', ''], ['Row', 'num'], ['What is wrong', '']],
      d.problems.map((p) => [
        cell(p.sheet, 'lead'),
        cell(p.row ? String(p.row) : '—', 'num'),
        cell(p.message, p.fatal ? 'bad' : 'warn'),
      ])));
  }

  $('import-go').hidden = !d.ok;
}

/* A free instance sleeps after fifteen idle minutes and takes most of
   a minute to come back. Uploading into that produces a reply from the
   host rather than the app, which is not a thing to explain after the
   fact when it can be waited for instead. */
async function wakeServer(say) {
  for (let i = 0; i < 20; i += 1) {
    try {
      const r = await fetch('/health', { cache: 'no-store' });
      if (r.ok && (r.headers.get('content-type') || '').includes('json')) {
        return true;
      }
    } catch (e) { /* not up yet */ }
    if (i === 0) say('The server was asleep. Waking it up…');
    await new Promise((done) => setTimeout(done, 3000));
  }
  return false;
}

async function previewFile(file) {
  if (!file) return;
  if (!/\.xlsx$/i.test(file.name)) {
    importReport({ ok: false, counts: {}, problems: [
      { sheet: 'workbook', row: null, fatal: true,
        message: 'That is not an .xlsx file. Export it from Excel as a '
          + 'workbook and try again.' }], problem_count: 1 });
    return;
  }
  pendingFile = file;

  const waiting = (msg) => importReport({
    ok: false, counts: {}, problem_count: 1,
    problems: [{ sheet: 'server', row: null, fatal: false, message: msg }],
  });
  if (!(await wakeServer(waiting))) {
    waiting('The server did not come back. Give it a minute and try again.');
    return;
  }

  const body = new FormData();
  body.append('file', file);
  try {
    const r = await fetch('/api/import/preview', { method: 'POST', body });
    if (r.status === 401) {
      throw new Error('You are not signed in any more. Reload the page, '
        + 'sign in, and try again.');
    }
    const { ok, data } = await readResponse(r, 'The check');
    if (!ok && data.error) throw new Error(data.error);
    importReport(data);
  } catch (e) {
    importReport({ ok: false, counts: {}, problem_count: 1, problems: [
      { sheet: 'workbook', row: null, fatal: true, message: e.message }] });
  }
}

let importWired = false;

function wireImport() {
  const drop = $('drop');
  const input = $('file');
  // boot() runs again after a successful import, to pull the new
  // figures in. Without this guard every listener here would be
  // attached a second time and the next upload would fire twice.
  if (!drop || importWired) return;
  importWired = true;

  $('browse').addEventListener('click', () => input.click());
  input.addEventListener('change', () => previewFile(input.files[0]));

  ['dragenter', 'dragover'].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add('over');
    }));
  ['dragleave', 'drop'].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove('over');
    }));
  drop.addEventListener('drop', (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) previewFile(f);
  });

  $('cancel-import').addEventListener('click', () => {
    pendingFile = null;
    input.value = '';
    $('import-report').hidden = true;
    $('import-go').hidden = true;
  });

  $('commit').addEventListener('click', async () => {
    if (!pendingFile) return;
    const btn = $('commit');
    btn.disabled = true;
    btn.textContent = 'Writing…';
    const body = new FormData();
    body.append('file', pendingFile);
    try {
      const r = await fetch('/api/import/commit', { method: 'POST', body });
      if (r.status === 401) {
        throw new Error('You are not signed in any more. Reload the page, '
          + 'sign in, and try again.');
      }
      const { data: d } = await readResponse(r, 'The import');
      if (d.needs_organisation) {
        throw new Error(d.error + ' Reload the page and you will be asked '
          + 'for it.');
      }
      if (!d.ok) throw new Error(d.error || 'The import was refused.');
      const w = d.written || {};
      $('import-report').textContent = '';
      const done = el('div', 'empty');
      done.append(el('strong', null, 'Imported'));
      done.append(el('p', null,
        `${qty(w.items)} items across ${qty(w.branches)} branches, with `
        + `${qty(w.movements)} opening entries and ${qty(w.history)} months `
        + 'of history. Your figures are on every screen now.'));
      $('import-report').append(done);
      $('import-go').hidden = true;
      pendingFile = null;
      /* Reload the state rather than ask them to: the whole app is
         about to be wrong by exactly the amount they just imported. */
      await boot();
      go('today');
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Write this to my organisation';
      importReport({ ok: false, counts: {}, problem_count: 1, problems: [
        { sheet: 'import', row: null, fatal: true, message: e.message }] });
    }
  });
}

boot();
