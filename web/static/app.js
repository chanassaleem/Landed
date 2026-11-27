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

/* How large numbers are grouped and named, which is a fact about the
   reader and not about the number.
   
   Half the world groups in thousands and half in lakh and crore, and
   a figure shown the wrong way is not slightly awkward, it is
   unreadable: 1,20,00,000 and 12,000,000 are the same number and only
   one of them is legible to a given person. This was hard-coded to
   lakh and crore, which made the whole product Pakistani whatever the
   currency label said. */
const SCALES = {
  western: [[1e9, 'bn', 2], [1e6, 'm', 2], [1e3, 'k', 0]],
  indian: [[1e7, 'cr', 2], [1e5, 'L', 1], [1e3, 'k', 0]],
};

const scale = () => SCALES[(S.data && S.data.scale) || 'indian']
                 || SCALES.indian;

const money = (n) => {
  if (n === null || n === undefined) return '—';
  const a = Math.abs(n);
  const sign = n < 0 ? '−' : '';
  for (const [div, suffix, dp] of scale()) {
    if (a >= div) return `${sign}${(a / div).toFixed(dp)}${suffix}`;
  }
  return `${sign}${Math.round(a)}`;
};

/* One unit for a whole set of figures.

   `money` picks a unit per figure, which is right for a figure standing
   on its own and wrong the moment two sit side by side: "2.7L" next to
   "2.35cr" cannot be compared without arithmetic, and a reader doing
   arithmetic has stopped reading. So a set of figures meant to be
   compared -- the money line, a column -- is scaled once, off the
   largest of them, and every figure in it carries the same unit. */
function units() {
  return [
    ...scale().map(([div, suffix, dp]) => ({ div, suffix, dp })),
    { div: 1, suffix: '', dp: 0 },
  ];
}

function unit(values) {
  const UNITS = units();
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

/* months() caps at 99+ because a hundred months of cover is the same
   decision as two hundred. A percentage and a count of hours are not
   like that: 312 hours capped to "99+" is a wrong number on the
   screen, and 110% recovered reading as "99+%" hides the only machine
   that has paid for itself. */
const pct = (n) =>
  n === null || n === undefined ? '—' : `${(n * 100).toFixed(1)}%`;

const hours = (n) =>
  n === null || n === undefined ? '—' : n.toFixed(n < 10 ? 1 : 0);

/* The engine's status names are precise; these are what a person
   would say. Shown raw, "at_risk" tells somebody the screen is
   printing a variable name. */
const SAID = {
  paid_back: 'paid back', at_risk: 'at risk', behind: 'behind',
  on_track: 'on track', ramping: 'still ramping up',
  ended: 'contract ended',
  /* The five stock states in the words the rest of the product uses.
     The engine's enum is REORDER and EXCESS because that is what the
     arithmetic is doing; a person reading a row wants to know whether
     the thing is running out or sitting there. The distinction
     between running out and due a reorder is real and worth keeping:
     one is already short, the other is the point at which ordering
     should start. */
  ok: 'on the shelf', reorder: 'due a reorder', critical: 'running out',
  excess: 'standing still', expiry_risk: 'near expiry',
};
const said = (w) => SAID[w] || String(w).replace(/_/g, ' ');

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
  home: 'M2.5 7l5.5-4.5L13.5 7v6a1 1 0 01-1 1h-9a1 1 0 01-1-1z',
  settings: 'M8 10a2 2 0 100-4 2 2 0 000 4zM13 8a5 5 0 01-.1 1l1.2.9-1 1.7'
    + '-1.4-.5a5 5 0 01-1.7 1L9.8 14H7.8l-.2-1.4a5 5 0 01-1.7-1l-1.4.5-1-1.7'
    + 'L4.7 9a5 5 0 010-2l-1.2-.9 1-1.7 1.4.5a5 5 0 011.7-1L7.8 2h2l.2 1.4'
    + 'a5 5 0 011.7 1l1.4-.5 1 1.7-1.2.9A5 5 0 0113 8z',
  today: 'M2 8h3l2-4 3 8 2-4h2',
  stock: 'M2 5.5l6-3 6 3v5l-6 3-6-3zM2 5.5l6 3 6-3M8 8.5v6',
  shipments: 'M2 11h12M3 11V7l4-2 4 2v4M6 7h3M8 2v3',
  items: 'M3 3h10v10H3zM3 6.5h10M6.5 6.5V13',
  import: 'M8 2v7M5 6.5L8 9.5l3-3M3 12.5h10',
  cash: 'M2 4.5h12v7H2zM2 7h12M4.5 9.5h2',
  owed: 'M8 2v12M5 4.5h4.5a1.75 1.75 0 010 3.5h-3a1.75 1.75 0 000 3.5H11',
  equipment: 'M3 3h10v7H3zM6 10v3M10 10v3M4.5 13h7M5.5 5.5h5M5.5 7.5h3',
  /* The modules the rail gained. Drawn as what the thing is, not as a
     company's logo for it: a ledger is a book, a forecast is a curve,
     a pipeline is a funnel. */
  buy: 'M2.5 3.5h2l1.5 7h7M5 13.5h.01M11 13.5h.01M6.5 6.5h7l-1 3h-5',
  freight: 'M1.5 11h13M2.5 11V7.5l3-1.5 3 1.5V11M9 11V5l3 1v5',
  demand: 'M2 12l3-4 3 2 3-6 3 3',
  money: 'M8 2v12M5 4.5h4.5a1.75 1.75 0 010 3.5h-3a1.75 1.75 0 000 3.5H11',
  sell: 'M2.5 2.5h3l1 2h7l-1.5 5h-6zM5 13h.01M11 13h.01',
  books: 'M3 2.5h4.5V13H3zM8.5 2.5H13V13H8.5zM3 7.5h4.5M8.5 7.5H13',
  customers: 'M6 7a2 2 0 100-4 2 2 0 000 4zM2.5 13c0-2 1.6-3.5 3.5-3.5'
    + 'S9.5 11 9.5 13M11 6.5a1.5 1.5 0 100-3M11.5 13c0-1.3-.4-2.4-1-3',
  people: 'M6 7a2 2 0 100-4 2 2 0 000 4zM2.5 13c0-2 1.6-3.5 3.5-3.5'
    + 'S9.5 11 9.5 13M11 6.5a1.5 1.5 0 100-3M11.5 13c0-1.3-.4-2.4-1-3',
  service: 'M9.5 3a3 3 0 00-4 4L3 9.5 6.5 13 9 10.5a3 3 0 004-4l-2 2-2-2z',
  intelligence: 'M2 3h12l-4.5 5.5V13L6.5 11V8.5z',
};

const VIEWS = [
  { id: 'home',      label: 'Home' },
  { id: 'today',     label: 'Today' },
  { sep: true },
  { id: 'stock',     label: 'Stock' },
  { id: 'shipments', label: 'On the water' },
  { id: 'items',     label: 'Items' },
  { sep: true },
  { id: 'owed',      label: 'Money owed' },
  { id: 'equipment', label: 'Equipment' },
  { id: 'cash',      label: 'Cash' },
  { sep: true },
  { id: 'import',    label: 'Import' },
  { id: 'settings',  label: 'Settings' },
];

/* The three things this is for, and what each one is worth today.
   Named by the job rather than by the table, because nobody has a
   "stock_movement" problem -- they have money sitting in the wrong
   place. */

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
  if (view === 'home' && S.data) renderHome();
  if (view === 'settings') renderSettings();
  if (view === 'owed') renderOwed();
  if (view === 'equipment') renderEquipment();
  if (view === 'import') renderModel();
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
  renderHome();
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

/* The engine's names and the drawer's names.

   The drawer was written against /api/stock, which called a SKU `sku`
   and a branch `branch`. The modules return the engine's own names --
   `item_key`, `location_id` -- because that is what an ItemSnapshot
   carries, and renaming them on the way out would mean the API and
   the engine disagreed about what a field is called.

   So the rename happens once, here, named, rather than as
   `row.sku || row.item_key` scattered through six functions. If a
   third caller ever needs it, it calls this. */
const DRAWER_NAMES = {
  item_key: 'sku',
  location_id: 'branch',
  stock_value: 'value',
  excess_value: 'idle_value',
  open_po: 'on_order',
  months_cover: 'cover',
  status: 'state',
  reason: 'why',
};

function forDrawer(row) {
  if (row.sku !== undefined) return row;   // already the old shape
  const out = Object.assign({}, row);
  Object.keys(DRAWER_NAMES).forEach((from) => {
    if (row[from] !== undefined) out[DRAWER_NAMES[from]] = row[from];
  });
  return out;
}

function openDetail(given, tr) {
  const row = forDrawer(given);
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
    /* The same words the grid uses. The old /api/stock sent this
       already in words; the module sends the engine's own enum,
       and two screens calling one state by two names is how a
       person starts to wonder whether they are the same thing. */
    fact('State', said(String(row.state || '').toLowerCase()), true),
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

  /* The movements behind the figures above. Asked for on open rather
     than held in the payload: the list screens need none of it, and a
     year of ledger for every line would be the bulk of the response. */
  const log = el('div', 'detail-block');
  log.append(el('h3', null, 'What has happened'));
  const logBody = el('div', 'facts');
  logBody.append(el('p', 'drop-sub', 'Reading the ledger…'));
  log.append(logBody);
  body.append(log);
  loadHistory(row, logBody);

  const acts = $('detail-actions');
  acts.textContent = '';
  OPS.forEach((op) => {
    if (op.when && !op.when(row)) return;
    const b = el('button', op.primary ? 'primary' : 'btn', op.label(row));
    b.type = 'button';
    if (op.primary) b.style.width = 'auto';
    b.addEventListener('click', () => openForm(op, row));
    acts.append(b);
  });

  d.hidden = false;
}

async function loadHistory(row, host) {
  try {
    const r = await fetch(
      `/api/line/history?sku=${encodeURIComponent(row.sku)}`
      + `&branch=${encodeURIComponent(row.branch)}`);
    const d = await r.json();
    host.textContent = '';
    if (!d.ok || !d.movements.length) {
      host.append(el('p', 'drop-sub',
        d.ok ? 'Nothing recorded against this line yet.'
             : 'The ledger could not be read.'));
      return;
    }
    d.movements.forEach((m) => {
      const f = el('div', 'fact said');
      const what = el('span', null, `${m.on}  ${WORDS[m.reason] || m.reason}`);
      const n = el('span', null, (m.qty > 0 ? '+' : '') + qty(m.qty));
      n.style.fontFamily = 'var(--mono)';
      n.style.color = m.qty > 0 ? 'var(--transit)' : 'var(--text-2)';
      f.append(what); f.append(n);
      host.append(f);
    });
  } catch (e) {
    host.textContent = '';
    host.append(el('p', 'drop-sub', 'The ledger could not be read.'));
  }
}

const WORDS = {
  opening: 'opening balance', grn: 'received', sale: 'sold',
  issue: 'issued', adjustment: 'count corrected',
  transfer_in: 'transferred in', transfer_out: 'transferred out',
  return_in: 'returned by customer', return_out: 'returned to supplier',
  write_off: 'written off', reversal: 'reversed',
};

/* Each operation is the same shape: a few fields, one call, and the
   screen reloads from the server afterwards rather than guessing at
   the new figure. Guessing is how a screen and a database start to
   disagree. */
const OPS = [
  {
    key: 'grn', label: () => 'Receive stock', primary: true,
    title: 'Receive stock',
    note: 'Goods arriving at this branch. Cost is per unit, landed if '
      + 'you know it. Enter 0 if it genuinely cost nothing \u2014 a '
      + 'blank would let these units take the price of whatever else '
      + 'is on the shelf.',
    fields: [
      { name: 'qty', label: 'How many', type: 'number', required: true },
      { name: 'unit_cost', label: 'Cost each', type: 'number',
        required: true },
      { name: 'note', label: 'Note', type: 'text' },
    ],
    /* Number(), not `|| null`. A cost of 0 is a cost -- a free sample,
       a warranty replacement -- and `0 || null` is null, so the one
       figure the server now insists on was the one the browser threw
       away. */
    send: (row, v) => ['/api/movement', {
      sku: row.sku, branch: row.branch, kind: 'grn',
      qty: v.qty,
      unit_cost: v.unit_cost === '' ? null : Number(v.unit_cost),
      note: v.note, client_ref: v.client_ref }],
  },
  {
    key: 'sale', label: () => 'Record a sale',
    title: 'Record a sale',
    note: 'Leaving the shelf. The cost comes from the running average, '
      + 'so there is nothing to type.',
    fields: [
      { name: 'qty', label: 'How many', type: 'number', required: true },
      { name: 'note', label: 'Customer or note', type: 'text' },
    ],
    send: (row, v) => ['/api/movement', {
      sku: row.sku, branch: row.branch, kind: 'sale',
      qty: v.qty, note: v.note, client_ref: v.client_ref }],
  },
  {
    key: 'adjust', label: () => 'Adjust count',
    title: 'Adjust count',
    note: 'Type what is actually on the shelf. Not the difference -- '
      + 'the ledger works that out.',
    fields: [
      { name: 'counted', label: 'Counted on the shelf', type: 'number',
        required: true },
      { name: 'note', label: 'Why', type: 'text' },
    ],
    send: (row, v) => ['/api/adjust', {
      sku: row.sku, branch: row.branch, counted: v.counted, note: v.note }],
  },
  {
    key: 'transfer', label: () => 'Send to another branch',
    when: () => (S.data.branches || []).length > 2,
    title: 'Send to another branch',
    note: 'Two entries at once: out of here, into there. The total does '
      + 'not change.',
    fields: [
      { name: 'to', label: 'To which branch', type: 'branch', required: true },
      { name: 'qty', label: 'How many', type: 'number', required: true },
      { name: 'note', label: 'Note', type: 'text' },
    ],
    send: (row, v) => ['/api/transfer', {
      sku: row.sku, from: row.branch, to: v.to, qty: v.qty, note: v.note }],
  },
];

function openForm(op, row) {
  const body = $('detail-body');
  body.textContent = '';
  const wrap = el('div', 'detail-block');
  wrap.append(el('h3', null, op.title));
  wrap.append(el('p', 'drop-sub', op.note));

  const form = el('form', 'op-form');
  const inputs = {};
  op.fields.forEach((f) => {
    const lab = el('label', null, f.label);
    lab.htmlFor = `op-${f.name}`;
    form.append(lab);
    let input;
    if (f.type === 'branch') {
      input = el('select');
      (S.data.branches || []).filter((b) => b !== 'ALL' && b !== row.branch)
        .forEach((b) => {
          const o = el('option', null, b);
          o.value = b;
          input.append(o);
        });
    } else {
      input = el('input');
      input.type = f.type === 'number' ? 'number' : 'text';
      if (f.type === 'number') { input.step = 'any'; input.min = '0'; }
    }
    input.id = `op-${f.name}`;
    if (f.required) input.required = true;
    inputs[f.name] = input;
    form.append(input);
  });

  const err = el('p', 'gate-error');
  err.hidden = true;
  form.append(err);

  const go = el('button', 'primary', op.title);
  go.type = 'submit';
  form.append(go);

  const back = el('button', 'btn', 'Back');
  back.type = 'button';
  back.addEventListener('click', () => openDetail(row, null));
  form.append(back);

  /* One key per attempt at this form, made once and reused by every
     retry of it. If the reply to a submission is lost -- a flaky
     connection, a closed laptop -- the next attempt carries the same
     key and the ledger refuses the duplicate rather than recording a
     second sale. A new key is minted only when the form is opened
     again, which is a person deciding to do it twice. */
  const attempt = (crypto.randomUUID ? crypto.randomUUID()
    : String(Date.now()) + Math.random().toString(16).slice(2));

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const values = { client_ref: attempt };
    Object.entries(inputs).forEach(([k, i]) => { values[k] = i.value; });
    go.disabled = true;
    go.textContent = 'Recording…';
    err.hidden = true;
    try {
      const [url, payload] = op.send(row, values);
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const { data } = await readResponse(r, 'That');
      if (!data.ok) throw new Error(data.error || 'It was refused.');
      /* Reload from the server rather than patch the figure here. A
         screen that works out its own new total is a screen that can
         disagree with the database, and nobody can tell which is
         wrong.

         Which screen to reload is the screen that is showing. boot()
         rebuilds the old top-level views; on a module screen those
         are not in the page, so it reached for elements that are not
         there and threw after the movement had already been written
         -- a write that succeeded, reported as a failure. */
      const onModule = location.hash.startsWith('#m/');
      if (onModule && window.LANDED_PLATFORM) {
        const id = location.hash.slice(3);
        const mod = ((window.LANDED_MODULES || {}).modules || [])
          .find((m) => m.id === id);
        closeDetail();
        if (mod) await window.LANDED_PLATFORM.open(mod);
      } else {
        await boot();
        go2(row);
      }
    } catch (ex) {
      err.hidden = false;
      err.textContent = ex.message;
      go.disabled = false;
      go.textContent = op.title;
    }
  });

  wrap.append(form);
  body.append(wrap);
  $('detail-actions').textContent = '';
}

/* After a movement, find the line again in the freshly loaded data and
   reopen it, so the panel shows the new position rather than the one
   that was true before. */
function go2(row) {
  const fresh = (S.data.stock || []).find(
    (r) => r.sku === row.sku && r.branch === row.branch);
  if (fresh) openDetail(fresh, null);
  else closeDetail();
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
    b.addEventListener('click', () => {
      S.segment = c.segment; renderLine(); renderAll(); go(c.goto);
    });
    card.append(b);
    d.append(card);
  });

  loadInsights();
}

/* The ranked cross-module list.
 *
 * It replaces a table of what runs out inside three months, which was
 * one module's opinion on one screen. This reads all fourteen and
 * ranks them against each other in money, with one exception: a
 * blocking data fault comes first whatever the amount, because acting
 * on a confidently wrong figure is worse than acting on none.
 */
const INSIGHT_TONE = { alarm: 'urgent', caution: 'attention', watch: 'transit' };

async function loadInsights() {
  const host = $('insights');
  if (!host) return;
  host.textContent = '';
  host.append(el('p', 'loading', 'Reading every module…'));

  let d;
  try {
    const r = await fetch('/api/today');
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || `Status ${r.status}.`);
  } catch (e) {
    host.textContent = '';
    host.append(empty('The list could not be read', e.message));
    return;
  }

  const rows = d.insights || [];
  host.textContent = '';
  if (!rows.length) {
    host.append(empty('Nothing is asking for you',
      'No module has anything above its threshold. That is not the same '
      + 'as everything being right -- the checks find what they look for.'));
    return;
  }

  const cur = d.currency || 'PKR';
  /* One unit across the whole list, because the ranking is a
     comparison: a lakh next to a crore has to look like a lakh. */
  const u = unit(rows.map((r) => Math.abs(r.value || 0)));

  const list = el('div', 'insights');
  rows.forEach((r) => {
    const row = el('div', `insight i-${INSIGHT_TONE[r.severity] || 'transit'}`);

    const main = el('div', 'insight-main');
    main.append(el('h3', null, r.title));
    if (r.detail) main.append(el('p', 'insight-detail', r.detail));
    if (r.action) main.append(el('p', 'insight-action', r.action));
    row.append(main);

    const side = el('div', 'insight-side');
    /* A data fault has no money attached and inventing a zero for it
       would put it at the bottom of a column it is deliberately at
       the top of. */
    if (r.value) {
      side.append(el('div', 'insight-value', `${cur} ${inUnit(r.value, u)}`));
    }
    if (r.module) {
      const b = el('button', 'btn', 'Open');
      b.type = 'button';
      b.addEventListener('click', () => { location.hash = `#m/${r.module}`; });
      side.append(b);
    }
    row.append(side);
    list.append(row);
  });
  host.append(list);
}

/* Named views, because a filter is a mechanism and a view is a
   question. "Running out" is what somebody came here to find; "state
   equals running out and sort by cover ascending" is how you would
   have to ask for it otherwise. */
const STOCK_VIEWS = [
  { id: 'all', label: 'Everything', pick: () => true,
    sort: (a, b) => (b.idle_value + b.shortfall_value)
                  - (a.idle_value + a.shortfall_value) },
  { id: 'order', label: 'Order today',
    pick: (r) => r.state === 'running out' || r.state === 'reorder',
    sort: (a, b) => (a.cover ?? 99) - (b.cover ?? 99) },
  { id: 'idle', label: 'Money standing still',
    pick: (r) => r.idle_value > 0,
    sort: (a, b) => b.idle_value - a.idle_value },
  { id: 'expiring', label: 'Expiring',
    pick: (r) => r.state === 'expiring' || (r.days_to_expiry ?? 9e9) < 180,
    sort: (a, b) => (a.days_to_expiry ?? 9e9) - (b.days_to_expiry ?? 9e9) },
  { id: 'quiet', label: 'No demand',
    pick: (r) => r.cover === null,
    sort: (a, b) => b.value - a.value },
];

const SORTS = {
  Item: (r) => r.description,
  Branch: (r) => r.branch,
  Cover: (r) => (r.cover === null ? Infinity : r.cover),
  'On hand': (r) => r.on_hand,
  'On order': (r) => r.on_order,
  State: (r) => r.state,
};

function stockRows() {
  const view = STOCK_VIEWS.find((v) => v.id === S.view_stock)
            || STOCK_VIEWS[0];
  let r = rows().filter(view.pick);

  const q = (S.q || '').trim().toLowerCase();
  if (q) {
    r = r.filter((x) => x.description.toLowerCase().includes(q)
                     || x.sku.toLowerCase().includes(q));
  }
  if (S.f_state) r = r.filter((x) => x.state === S.f_state);
  if (S.f_class) r = r.filter((x) => x.demand_class === S.f_class);

  r = r.slice();
  if (S.sort) {
    const get = SORTS[S.sort.col];
    const dir = S.sort.dir === 'ascending' ? 1 : -1;
    r.sort((a, b) => {
      const x = get(a);
      const y = get(b);
      if (x === y) return 0;
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
    });
  } else {
    r.sort(view.sort);
  }
  return r;
}

function renderViews() {
  const host = $('views');
  if (!host) return;
  host.textContent = '';
  const base = rows();
  STOCK_VIEWS.forEach((v) => {
    const b = el('button', 'view-chip');
    b.type = 'button';
    b.append(v.label);
    const n = base.filter(v.pick).length;
    b.append(el('span', 'n', String(n)));
    b.setAttribute('aria-pressed', String((S.view_stock || 'all') === v.id));
    b.addEventListener('click', () => {
      S.view_stock = v.id;
      S.sort = null;               // the view brings its own order
      renderStock();
    });
    host.append(b);
  });
}

function fillFilterOptions() {
  const base = rows();
  const fill = (id, values, blank) => {
    const sel = $(id);
    if (!sel) return;
    const was = sel.value;
    sel.textContent = '';
    const any = el('option', null, blank);
    any.value = '';
    sel.append(any);
    values.forEach((v) => {
      const o = el('option', null, v);
      o.value = v;
      sel.append(o);
    });
    sel.value = was;
  };
  fill('f-state', [...new Set(base.map((r) => r.state))].sort(), 'Any state');
  fill('f-class', [...new Set(base.map((r) => r.demand_class))].sort(),
       'Any pattern');
}

function renderStock() {
  renderViews();
  fillFilterOptions();
  const all = rows();
  let r = stockRows();

  const narrowed = r.length !== all.length;
  const shown = $('shown');
  if (shown) {
    shown.textContent = narrowed
      ? `${qty(r.length)} of ${qty(all.length)}` : `${qty(all.length)} lines`;
  }
  const clear = $('clear-filters');
  if (clear) clear.hidden = !(S.q || S.f_state || S.f_class
                              || (S.view_stock && S.view_stock !== 'all'));

  $('stock-sub').textContent = S.segment
    ? `${plural(all.length, 'line')} in ${SEGMENTS.find((s) => s.key === S.segment).label.toLowerCase()}.`
    : `${plural(all.length, 'line')} across ${plural(S.data.branches.length - 1, 'branch')}.`;

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
  cols.forEach(([label, cls]) => {
    const th = el('th', cls, label);
    if (SORTS[label]) {
      th.dataset.sort = label;
      th.tabIndex = 0;
      if (S.sort && S.sort.col === label) th.setAttribute('aria-sort', S.sort.dir);
      const flip = () => {
        const same = S.sort && S.sort.col === label;
        S.sort = {
          col: label,
          dir: same && S.sort.dir === 'ascending' ? 'descending' : 'ascending',
        };
        renderStock();
      };
      th.addEventListener('click', flip);
      th.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); }
      });
    }
    tr.append(th);
  });
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
    // And the badge inside it. The guard above stopped one level
    // short: the rail is built from the server's module list, so an
    // entry can exist without the span -- and this threw, inside
    // boot(), which is awaited after a successful import. The write
    // had already happened; what the person saw was "Cannot set
    // properties of null" under the heading "Not ready yet".
    const badge = a.querySelector('.count');
    if (badge) badge.textContent = n ? String(n) : '';
    a.dataset.alarm = alarm ? '1' : '0';
  };
  set('today', out, out > 0);
  set('stock', needing.length, false);
  set('shipments', S.data.shipments.length, false);
  set('items', new Set(S.data.stock.map((r) => r.sku)).size, false);
}

/* The branch picker, built into the stock screen's own filter row
   rather than into the top bar.

   In the chrome it looked like it narrowed the product and narrowed
   one screen: somebody picking Karachi read Karachi's stock above the
   whole group's cash, receivables and payback, and nothing said so.
   A filter belongs next to the figures it moves. */
function branches() {
  let sel = $('branch');
  if (!sel) {
    const host = document.querySelector('#v-stock .filters');
    if (!host) return;
    sel = el('select');
    sel.id = 'branch';
    sel.className = 'picker';
    sel.setAttribute('aria-label', 'Branch');
    host.prepend(sel);
  }
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
    /* The same answer already says whose business this is, so the
       chrome is named from it rather than fetching it again. */
    showWhose(d.organisations[0]);
    return d.organisations.length === 0;
  } catch (e) {
    return false;
  }
}

/* Whose business this is, and what you may do in it.

   One person is one business, so there is nothing to switch between
   and no menu pretending otherwise. But a figure on screen belongs to
   somebody, and an importer running two companies on two logins should
   never have to work out which one they are looking at.

   Left empty until it is known, so it never flashes the wrong name. */
function showWhose(org) {
  const host = $('whose');
  if (!host || !org || !org.name) return;
  const name = $('whose-org');
  name.textContent = org.name;
  /* The full name in the tooltip, because the visible one is truncated
     -- a supplier here can be called Shenzhen Mindray Bio-Medical
     Electronics Co., Ltd., and so can a customer's own company. */
  name.title = org.name;
  const role = $('whose-role');
  if (org.role) {
    role.textContent = org.role;
    /* Said in full to a screen reader. "owner" beside a company name
       reads as a word, not a role, when it arrives with no context. */
    role.setAttribute('aria-label', `your role: ${org.role}`);
    role.hidden = false;
  } else {
    role.hidden = true;
  }
  host.hidden = false;
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
  wireFilters();

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
  renderHome();
  palette();

  /* The "Only what needs a decision" button is gone: it was one view
     hard-coded as a toggle, and the view chips do the same job for
     five questions instead of one. Its handler outlived it and threw
     on every load, which took the rest of boot() with it. */

  /* The rail is built from the server's module list, not from the
     VIEWS array above: a module has to exist in engine/platform.py to
     appear here at all, which is what stops the browser offering a
     screen the API will refuse. If that call fails the hard-coded rail
     stands, so a module list that cannot be read costs the new screens
     and not the whole app. */
  let listed = null;
  if (window.LANDED_PLATFORM) {
    listed = await window.LANDED_PLATFORM.buildRail();
  }
  if (!listed) buildNav();

  const start = (location.hash || '#today').slice(1);
  if (start.startsWith('m/') && listed) {
    const want = start.slice(2);
    const mod = listed.modules.find((m) => m.id === want);
    if (mod) { window.LANDED_PLATFORM.open(mod); }
    else go('today');
  } else {
    go(VIEWS.some((v) => v.id === start) ? start : 'today');
  }
  addEventListener('resize', moveMarker);
}

/* What the platform layer is allowed to use. Exported rather than
   duplicated: app.js already decides how money, quantities and months
   read -- including the crore-and-lakh grouping that follows the
   country pack -- and a second formatter in another file would drift
   from this one the day somebody changed a country. */
window.LANDED = {
  $, el, go, icon, ICON, empty, money, qty, pct, months, said, plural,
  openDetail, closeDetail,
  get data() { return S.data; },
};

/* boot() is called at the very bottom of this file, after every
   declaration it reaches. Called from here it ran before the
   import block below had been evaluated, and touching a `let`
   from above its declaration is an error, not a hoist. */

/* ------------------------------------------------- money owed
   The other half of the money question. The stock screens say what
   you are holding; this says what you have already sold and not been
   paid for, which for an importer is usually the larger number. */

function figure(host, label, value, note, tone) {
  const d = el('div', `deck${tone ? ' d-' + tone : ''}`);
  d.append(el('h3', null, label));
  d.append(el('div', 'big', value));
  if (note) d.append(el('p', null, note));
  host.append(d);
}

async function renderOwed() {
  const host = $('owed');
  const figs = $('owed-figures');
  if (!host) return;
  host.textContent = '';
  figs.textContent = '';

  let d;
  try {
    const r = await fetch('/api/owed');
    ({ data: d } = await readResponse(r, 'Money owed'));
    if (!d.ok) throw new Error(d.error || 'It could not be read.');
  } catch (e) {
    host.append(empty('Money owed could not be read', e.message));
    return;
  }

  if (!d.invoices.length) {
    figs.textContent = '';
    host.append(empty('Nothing outstanding',
      'No unpaid invoices. Import them, or raise one against a customer.'));
    return;
  }

  const cur = S.data ? S.data.currency : 'PKR';
  const u = unit([d.outstanding, d.overdue, d.cash_released_at_target]);
  figure(figs, 'Owed to you', `${cur} ${inUnit(d.outstanding, u)}`,
         `${qty(d.invoices.length)} invoices still open.`, 'water');
  figure(figs, 'Already overdue', `${cur} ${inUnit(d.overdue, u)}`,
         d.intercompany_excluded
           ? `Your own companies owe a further ${cur} `
             + `${inUnit(d.intercompany_excluded, u)}, which is not counted.`
           : 'Past its due date.', 'urgent');
  figure(figs, 'Days to get paid',
         `${months(d.dso_days)} days`,
         `Target is ${qty(d.target_dso_days)} days. Meeting it would free `
         + `${cur} ${inUnit(d.cash_released_at_target, u)}.`, 'idle');

  const iu = unit(d.invoices.map((i) => i.open));
  host.append(table(
    [['Invoice', ''], ['Customer', ''], ['Due', ''],
     ['Days late', 'num'], [`Open ${iu.suffix}`.trim(), 'num'],
     ['Counted?', '']],
    d.invoices.map((i) => [
      cell(i.invoice, 'lead'),
      cell(i.customer + (i.public_sector ? '  · public sector' : ''), 'dim'),
      cell(i.due, 'dim'),
      cell(i.days_overdue ? qty(i.days_overdue) : '—',
           i.days_overdue > 60 ? 'num bad' : 'num'),
      cell(inUnit(i.open, iu), 'num'),
      cell(i.intercompany ? 'own company' : 'yes', 'dim'),
    ])));
}

/* ------------------------------------------------------- equipment */

async function renderEquipment() {
  const host = $('machines');
  const shop = $('workshop');
  const figs = $('equip-figures');
  if (!host) return;
  host.textContent = ''; shop.textContent = ''; figs.textContent = '';

  let d;
  try {
    const r = await fetch('/api/equipment');
    ({ data: d } = await readResponse(r, 'Equipment'));
    if (!d.ok) throw new Error(d.error || 'It could not be read.');
  } catch (e) {
    host.append(empty('Equipment could not be read', e.message));
    return;
  }

  const cur = S.data ? S.data.currency : 'PKR';
  const w = d.service;

  if (!d.machines.length) {
    host.append(empty('No machines placed',
      'A machine you put in a customer\'s lab, free or on contract, '
      + 'belongs here. The question it answers is whether the reagent '
      + 'it pulls has paid for it yet.'));
  } else {
    const mu = unit(d.machines.flatMap((m) => [m.capital, m.recovered]));
    const placed = d.machines.reduce((t, m) => t + m.capital, 0);
    const back = d.machines.reduce((t, m) => t + m.recovered, 0);
    figure(figs, 'Capital out there', `${cur} ${inUnit(placed, mu)}`,
           `${qty(d.machines.length)} machines placed.`, 'idle');
    figure(figs, 'Earned back', `${cur} ${inUnit(back, mu)}`,
           `${pct(placed ? back / placed : 0)} of what you put out.`,
           'water');

    host.append(table(
      [['Machine', ''], ['Customer', ''], [`Cost ${mu.suffix}`.trim(), 'num'],
       [`Back ${mu.suffix}`.trim(), 'num'], ['Recovered', 'num'],
       ['Months in', 'num'], ['State', '']],
      d.machines.map((m) => [
        cell(`${m.key}  ${m.model}`, 'lead'),
        cell(m.customer, 'dim'),
        cell(inUnit(m.capital, mu), 'num'),
        cell(inUnit(m.recovered, mu), 'num'),
        cell(pct(m.recovery_pct), (m.recovery_pct || 0) < 0.5
             ? 'num warn' : 'num'),
        cell(months(m.months_elapsed), 'num'),
        stateCell(said(m.status)),
      ])));
  }

  if (!w.jobs) {
    shop.append(empty('No service jobs yet',
      'Hours worked and hours invoiced, kept apart. The gap between them '
      + 'is work you did and did not charge for.'));
    return;
  }
  const su = unit([w.labour_value_lost, w.parts_value_lost, w.parts_cost]);
  shop.append(table(
    [['', ''], ['Done', 'num'], ['Charged', 'num'], ['Given away', 'num']],
    [
      [cell('Labour, hours', 'lead'), cell(hours(w.hours), 'num'),
       cell(hours(w.hours - w.hours_unbilled), 'num'),
       cell(hours(w.hours_unbilled),
            w.hours_unbilled ? 'num warn' : 'num')],
      [cell(`Parts, ${cur} ${su.suffix}`.trim(), 'lead'),
       cell(inUnit(w.parts_cost, su), 'num'),
       cell(inUnit(w.parts_cost - w.parts_uncharged, su), 'num'),
       cell(inUnit(w.parts_uncharged, su),
            w.parts_uncharged ? 'num warn' : 'num')],
      [cell('What that is worth', 'lead'), cell('', 'num'), cell('', 'num'),
       cell(`${cur} ` + inUnit(w.labour_value_lost + w.parts_value_lost, su),
            'num warn')],
    ]));
}

/* ------------------------------------------------------------- home
   What this is for, in three pieces, with what each is worth today.
   It exists because "inventory software" is four screens and a person
   arriving at them cold cannot tell which one answers their question. */

let orgName = null;

function renderHome() {
  const host = $('modules');
  if (!host) return;

  /* The business's own name, not the product's. Somebody opening this
     at seven in the morning knows what they bought; what they want to
     see is that it is theirs. */
  if (orgName === null) {
    orgName = '';
    fetch('/api/settings').then((r) => r.json()).then((d) => {
      if (d && d.name) {
        orgName = d.name;
        const h = $('home-org');
        if (h) h.textContent = d.name;
      }
    }).catch(() => {});
  } else if (orgName) {
    $('home-org').textContent = orgName;
  }

  host.textContent = '';
  host.append(el('p', 'loading', 'Reading every module…'));
  loadHome(host);
}

/* Home is the fourteen modules, each in one figure.

   It used to be three hand-written cards -- holding, on its way, and
   a cash card permanently marked "not set up" -- from before the
   platform existed. They routed to screens that have since been
   replaced, and the third was a promise rather than a card.

   One request does all fourteen. The server runs them over a single
   Facts instance, so the whole screen costs roughly what the most
   expensive single module costs rather than fourteen times it. */
async function loadHome(host) {
  let d;
  try {
    /* No branch. Home is the whole business by design: the branch
       filter belongs to the data on a module screen, not to the
       screen that says where the money is overall, and a home page
       narrowed to Karachi would quietly stop being a summary. */
    const r = await fetch('/api/home');
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || `Status ${r.status}.`);
  } catch (e) {
    host.textContent = '';
    host.append(empty('The modules could not be read', e.message));
    return;
  }

  host.textContent = '';
  const cur = d.currency || 'PKR';
  const cards = d.cards || [];

  /* Scaled together, so a crore on one card and a crore on another
     are the same length of number. Scaling each card to itself would
     make the biggest figure on the screen look like the smallest. */
  const u = unit(cards.map((c) => (c.head && c.head.kind === 'money'
                                   ? Math.abs(c.head.value) : 0)));

  (d.sections || []).forEach((sec) => {
    const mine = cards.filter((c) => c.section === sec.id);
    if (!mine.length) return;
    host.append(el('h2', 'home-section', sec.label));
    const grid = el('div', 'modules');
    mine.forEach((c) => grid.append(homeCard(c, cur, u)));
    host.append(grid);
  });
}

function homeFigure(h, which, cur, u) {
  const v = which === 'second' ? h.second : h.value;
  const kind = which === 'second' ? h.second_kind : h.kind;
  if (v === null || v === undefined) return '—';
  if (kind === 'money') {
    /* A zero renders as a dash by house style, and "PKR —" reads as a
       missing figure rather than as nothing to do. The currency goes
       with a number or not at all. */
    const n = inUnit(v, u);
    return n === '—' ? '—' : `${cur} ${n}`;
  }
  if (kind === 'pct') return pct(v);
  return qty(v);
}

function homeCard(c, cur, u) {
  const card = el('button', `module m-${c.state}`);
  card.type = 'button';

  const head = el('div', 'module-head');
  head.append(el('h2', null, c.name));
  if (c.state === 'waiting') head.append(el('span', 'tag', 'no figures yet'));
  if (c.state === 'broken') head.append(el('span', 'tag', 'could not read'));
  card.append(head);

  if (c.head) {
    card.append(el('div', 'module-figure',
      homeFigure(c.head, 'value', cur, u)));
    card.append(el('p', 'module-label', c.head.label));
    if (c.head.second !== null && c.head.second !== undefined) {
      /* The figure that stops the first being read wrongly. Stock
         worth a crore is good news until you see how much of it is
         standing still. */
      const sec = el('p', 'module-second');
      sec.append(el('span', 'module-second-n',
        homeFigure(c.head, 'second', cur, u)));
      sec.append(el('span', null, ` ${c.head.second_label}`));
      card.append(sec);
    }
  } else {
    card.append(el('div', 'module-figure dim', '—'));
    card.append(el('p', 'module-label',
      c.state === 'broken' ? (c.detail || 'something went wrong')
      : (c.missing || []).length
        ? `needs ${c.missing.map(said).join(', ')}`
        : 'nothing here yet'));
  }

  card.append(el('p', 'module-blurb', `${c.title}. ${c.blurb}`));
  card.addEventListener('click', () => {
    location.hash = `#m/${c.id}`;
  });
  return card;
}

/* --------------------------------------------------------- settings */

/* The settings screen used to carry its own copy for four of the
   engine's numbers. There are twenty-three, and each now arrives from
   /api/settings with its label, its bounds and the module it moves --
   one description, on the server, beside the validation that enforces
   it. Two copies would have disagreed the first time a bound changed. */

async function renderSettings() {
  const host = $('settings');
  if (!host) return;
  host.textContent = '';
  let d;
  try {
    const r = await fetch('/api/settings');
    ({ data: d } = await readResponse(r, 'Settings'));
  } catch (e) {
    host.append(empty('Settings could not be read', e.message));
    return;
  }
  if (d.error) { host.append(empty('No organisation yet', d.error)); return; }

  const form = el('form', 'settings-form');
  form.append(el('p', 'drop-sub',
    'Nothing here is fixed. A country pack fills these in with values '
    + 'that suit where you trade; every one of them stays yours to '
    + 'change afterwards.'));

  /* The country pack first, because it sets sensible values for
     everything below it. A starting point, not a rule: every figure
     it writes stays editable, and applying one does not touch
     anything already tuned by hand. */
  const place = el('div', 'detail-block');
  place.append(el('h3', null, 'Where you trade'));
  const sel = el('select');
  sel.id = 'set-country';
  const selLab = el('label', null, 'Country');
  selLab.htmlFor = 'set-country';
  place.append(selLab);
  place.append(sel);
  const packNote = el('p', 'drop-sub', '');
  place.append(packNote);
  const applyBtn = el('button', 'btn', 'Apply these defaults');
  applyBtn.type = 'button';
  applyBtn.style.marginTop = '8px';
  place.append(applyBtn);
  form.append(place);

  try {
    const cr = await fetch('/api/countries');
    const { data: cd } = await readResponse(cr, 'Countries');
    (cd.countries || []).forEach((c) => {
      const o = el('option', null,
        `${c.name} — ${c.currency_name} (${c.currency})`);
      o.value = c.code;
      o.dataset.notes = c.notes || '';
      o.dataset.scale = c.scale;
      sel.append(o);
    });
    sel.value = (d.settings || {}).country || d.country || 'PK';
    const describe = () => {
      const o = sel.selectedOptions[0];
      if (!o) return;
      packNote.textContent =
        `Numbers grouped the ${o.dataset.scale === 'indian'
          ? 'South Asian way (lakh, crore)'
          : 'western way (thousands, millions)'}. `
        + (o.dataset.notes || '')
        + ' Applying this overwrites only what it covers; anything you '
        + 'have already changed by hand stays.';
    };
    describe();
    sel.addEventListener('change', describe);
  } catch (e) {
    packNote.textContent = 'The country list could not be read.';
  }

  applyBtn.addEventListener('click', async () => {
    applyBtn.disabled = true;
    applyBtn.textContent = 'Applying…';
    try {
      const r = await fetch('/api/settings/country', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ country: sel.value }),
      });
      const { data } = await readResponse(r, 'Applying');
      if (!data.ok) throw new Error(data.error || 'refused');
      await boot();
      go('settings');
    } catch (ex) {
      packNote.textContent = ex.message;
      applyBtn.disabled = false;
      applyBtn.textContent = 'Apply these defaults';
    }
  });

  const who = el('div', 'detail-block');
  who.append(el('h3', null, 'This business'));
  const name = el('input');
  name.type = 'text';
  name.value = d.name || '';
  name.id = 'set-name';
  const nameLab = el('label', null, 'Name');
  nameLab.htmlFor = 'set-name';
  who.append(nameLab); who.append(name);
  const ccy = el('input');
  ccy.type = 'text';
  ccy.id = 'set-currency';
  ccy.maxLength = 3;
  ccy.value = (d.settings || {}).currency || d.currency || '';
  ccy.style.textTransform = 'uppercase';
  const ccyLab = el('label', null, 'Currency');
  ccyLab.htmlFor = 'set-currency';
  who.append(ccyLab); who.append(ccy);
  who.append(el('p', 'drop-sub',
    `Signed in as ${d.email}. ${d.branches.length} branches: `
    + `${d.branches.map((b) => b.code).join(', ')}.`));
  form.append(who);

  /* Every number the engine reads, from the server's own description
     of them, rather than from a list here. Four used to be shown and
     nineteen were unreachable: settable in the dataclass, invisible in
     the product, which is hard-coded in every way that matters to
     somebody who disagrees with a figure.

     Split in two. The eight on the first screen are the ones a person
     can answer without being told how a module works -- what money
     costs you a year, what an engineer's hour is worth. The other
     fifteen are real settings with real effects, and putting a
     forecast's level-smoothing constant beside the minimum wage would
     make the page unreadable for the nine people in ten who never
     need it. */
  const fields = d.fields || {};
  const inputs = {};

  const numberField = (key, spec, host) => {
    const lab = el('label', null, spec.label || key);
    lab.htmlFor = `set-${key}`;
    const i = el('input');
    i.type = 'number';
    i.step = spec.kind === 'int' ? '1' : 'any';
    i.min = spec.lo;
    i.max = spec.hi;
    i.id = `set-${key}`;
    i.value = (d.settings || {})[key] ?? d.defaults[key];
    i.placeholder = String(d.defaults[key]);
    inputs[key] = i;
    host.append(lab);
    host.append(i);
    if (spec.help) host.append(el('p', 'drop-sub', spec.help));
  };

  const plain = Object.keys(fields).filter((k) => !fields[k].advanced);
  const deep = Object.keys(fields).filter((k) => fields[k].advanced);

  const pol = el('div', 'detail-block');
  pol.append(el('h3', null, 'How your figures are read'));
  plain.forEach((k) => numberField(k, fields[k], pol));
  form.append(pol);

  if (deep.length) {
    const more = el('details', 'advanced');
    const sum = el('summary', null,
      `Advanced — ${deep.length} more, grouped by what they move`);
    more.append(sum);
    more.append(el('p', 'drop-sub',
      'Each of these changes a figure somewhere. None of them is '
      + 'hidden because it is dangerous; they are here because the '
      + 'question each one asks only makes sense once you know which '
      + 'module is asking it.'));

    /* Grouped by the module each one moves, and labelled with that
       module's own name, so somebody who disagrees with a number on a
       screen can find the setting behind it by looking for the screen
       rather than by guessing at a term. */
    const titles = {};
    ((window.LANDED_MODULES || {}).modules || []).forEach((m) => {
      titles[m.id] = m.title;
    });
    const groups = {};
    deep.forEach((k) => {
      const g = fields[k].module || 'other';
      (groups[g] = groups[g] || []).push(k);
    });
    Object.keys(groups).sort().forEach((g) => {
      const block = el('div', 'detail-block');
      block.append(el('h3', null, titles[g] || said(g)));
      groups[g].forEach((k) => numberField(k, fields[k], block));
      more.append(block);
    });
    form.append(more);
  }

  /* Your data, out, in the shape it came in. Worth being on the
     settings screen rather than buried: a business about to put its
     stock, its cash position and its payroll into somebody else's
     software should be able to see the way out before it needs it. */
  const out = el('div', 'detail-block');
  out.append(el('h3', null, 'Your data'));
  out.append(el('p', 'drop-sub',
    'Everything this business has, as the same workbook the import '
    + 'reads \u2014 so it opens in Excel, and it imports back. Nothing '
    + 'here is locked in.'));
  const dl = el('a', 'btn', 'Download everything');
  dl.href = '/api/export.xlsx';
  dl.setAttribute('download', '');
  out.append(dl);
  form.append(out);

  const err = el('p', 'gate-error');
  err.hidden = true;
  form.append(err);
  const save = el('button', 'primary', 'Save');
  save.type = 'submit';
  form.append(save);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    save.disabled = true;
    save.textContent = 'Saving…';
    err.hidden = true;
    const settings = {};
    Object.entries(inputs).forEach(([k, i]) => { settings[k] = i.value; });
    if (ccy.value.trim()) settings.currency = ccy.value.trim().toUpperCase();
    try {
      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.value.trim(), settings }),
      });
      const { data } = await readResponse(r, 'Saving');
      if (!data.ok) throw new Error((data.problems || ['refused']).join('; '));
      /* Thresholds change what every screen says, so reload rather than
         leave the old verdicts on screen beside the new rules. */
      await boot();
      go('settings');
    } catch (ex) {
      err.hidden = false;
      err.textContent = ex.message;
      save.disabled = false;
      save.textContent = 'Save';
    }
  });

  host.append(form);
}

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

  /* Whatever the server says is in the file. This was a list of
     seven labels written here against eighteen counts sent from
     there, so most sheets a person filled in were never confirmed
     back to them -- and a sheet added later was invisible until
     somebody remembered to edit this array too. */
  const rows = (d.summary || []).map((r) => [r.label, r.rows]);

  host.append(table(
    [['What', ''], ['Rows', 'num']],
    rows.map(([label, n]) => [cell(label, 'lead'), cell(qty(n), 'num')])));

  if (d.value) {
    host.append(el('p', 'note',
      `Opening stock at cost: ${S.data ? S.data.currency : 'PKR'} `
      + `${money(d.value)}. Sense-check that against your own figure `
      + 'before writing.'));
  }

  againstRecorded(host, d.against_recorded);

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

/* What committing will do to what is already recorded.

   The server has answered this since the receipt-correction work went
   in -- counts per table, what changed, the recorded figure beside the
   incoming one -- and nothing on the screen read it. A person saw how
   many rows were in their file and pressed the button.

   Three fates, and the difference between them is the whole point:

     update   the recorded row changes to match the file
     added    new rows go in, the ones already there are left alone
     correct  the file disagrees with something that already happened,
              and committing will not resolve it

   The third is the one worth a person's attention. A receipt moved
   stock and a valuation took its cost; a later file saying it was
   really ninety does not unhappen that, so it needs an entry of its
   own. Said plainly here, because "3 changed" next to a Commit button
   implies the commit deals with them. */

const FATE = {
  update: ['Updates', 'Committing changes these to match your file.'],
  skip: ['Adds what is new',
         'Committing adds the rows that are not recorded yet and '
         + 'leaves the rest as they are.'],
  correct: ['Needs a correction',
            'These already happened: the stock moved and the valuation '
            + 'took the cost. Committing records nothing new for them '
            + 'and does not change them. Correcting one is its own '
            + 'entry, from the receipt.'],
};

function againstRecorded(host, a) {
  if (!a || !a.totals) return;

  const head = el('div', 'section-head');
  head.append(el('h2', null, 'Against what is already recorded'));
  host.append(head);

  /* .note, not .drop-sub. drop-sub is `margin: 0 auto; max-width:
     54ch` -- built to sit centred under the drop zone -- so used in a
     1,194px report it centres a 389px column of text under a
     left-aligned heading. It reads as a layout fault. Seen in a
     browser; the line below had the same problem and was there
     already. */
  const line = el('p',
    a.needs_correction ? 'note note-warn' : 'note', a.headline || '');
  host.append(line);

  /* Counted separately rather than as one "changed" figure, because
     the four mean four different things to the person deciding. */
  /* The house tile, not a new one. These already carry their own
     hairline as an outline rather than letting a container background
     show through the grid gap -- which rendered as a grey slab
     whenever the last row was short, twice in this file. */
  const tiles = el('div', 'tiles');
  [['New', a.totals.new, 'not recorded yet'],
   ['Updates', (a.totals.changed || 0) - (a.needs_correction || 0),
    'recorded rows your file changes'],
   ['Needs a correction', a.needs_correction || 0,
    'already happened; committing will not change them'],
   ['Already recorded', a.totals.unchanged, 'nothing to do'],
  ].forEach(([label, n, why]) => {
    const t = el('div', 'tile');
    if (label === 'Needs a correction' && n > 0) t.dataset.attention = '1';
    t.append(el('div', 'tile-v num', qty(n || 0)));
    t.append(el('div', 'tile-k', label));
    t.append(el('div', 'note-quiet', why));
    tiles.append(t);
  });
  host.append(tiles);

  /* Only the sheets where something is actually going to happen. A
     table of twenty-four rows reading zero, zero, zero is a way of
     hiding the two that matter. */
  const busy = (a.tables || []).filter(
    (t) => t.new > 0 || t.changed > 0);
  if (!busy.length) return;

  host.append(table(
    [['Sheet', ''], ['New', 'num'], ['Changed', 'num'],
     ['Already there', 'num'], ['On commit', '']],
    busy.map((t) => {
      const [name] = FATE[t.action] || ['Updates'];
      return [
        cell(t.label, 'lead'),
        cell(qty(t.new), 'num'),
        cell(qty(t.changed), t.action === 'correct' && t.changed
          ? 'num bad' : 'num'),
        cell(qty(t.unchanged), 'num'),
        cell(name, t.action === 'correct' ? 'warn' : ''),
      ];
    })));

  /* The three fates explained once, under the table, and only the
     ones that are actually on screen.

     They were a sentence in every row first. Twenty-one rows of
     "Committing changes these to match your file" squeezed the column
     to ninety pixels, and the Goods received row -- whose sentence is
     the long one, and the one that matters -- wrapped into a
     four-hundred-pixel ribbon of two-word lines. Seen in a browser.
     Saying a thing once is also just better than saying it twenty-one
     times. */
  const fates = [...new Set(busy.map((t) => t.action))];
  fates.forEach((action) => {
    const [name, why] = FATE[action] || [];
    if (!name) return;
    const n = el('p', action === 'correct' ? 'note note-warn' : 'note');
    n.append(el('b', null, `${name}. `));
    n.append(document.createTextNode(why));
    host.append(n);
  });

  /* Before and after, for the rows that changed. A count tells
     somebody something moved; this tells them whether it is the thing
     they meant to change. */
  const shown = busy.filter((t) => (t.examples || []).length);
  shown.forEach((t) => {
    const h = el('div', 'section-head');
    h.append(el('h3', null, `${t.label}: what changed`));
    if (t.changed > t.examples.length) {
      h.append(el('span', 'asof',
        `showing ${t.examples.length} of ${t.changed}`));
    }
    host.append(h);
    host.append(table(
      [['Which row', ''], ['Recorded', ''], ['In your file', '']],
      t.examples.map((x) => [
        cell(x.key, 'lead'),
        cell(fmtVals(x.recorded), ''),
        cell(fmtVals(x.incoming), 'warn'),
      ])));
  });
}

/* A row of values as one readable line. Empty and null are not the
   same as zero and must not print as it: "not entered" and "entered as
   nothing" are different facts, and this screen is where somebody
   decides whether a figure is wrong. */
function fmtVals(vals) {
  if (!Array.isArray(vals)) return String(vals == null ? '—' : vals);
  return vals.map((v) => {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'number') return qty(v);
    return String(v);
  }).join(' · ');
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

function wireFilters() {
  const q = $('q-stock');
  if (!q || q.dataset.wired) return;
  q.dataset.wired = '1';

  /* Typing filters as you type, but not on every keystroke: at 104
     lines it is instant, and at ten thousand it would not be. */
  let t = null;
  q.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => { S.q = q.value; renderStock(); }, 120);
  });
  ['f-state', 'f-class'].forEach((id) => {
    $(id).addEventListener('change', () => {
      S[id.replace('-', '_')] = $(id).value;
      renderStock();
    });
  });
  $('clear-filters').addEventListener('click', () => {
    S.q = ''; S.f_state = ''; S.f_class = ''; S.view_stock = 'all';
    S.sort = null;
    q.value = '';
    $('f-state').value = '';
    $('f-class').value = '';
    renderStock();
  });
}

let importWired = false;

/* ---------------------------------------------------- the model

   What the workbook is, on the screen where somebody is about to fill
   one in. Three files and a list of every sheet.

   The list comes from the server because the description of the
   sheets and the code that reads them have to be one thing. The
   template this product used to offer was built by a different file
   and shared six sheet names out of twenty-four: downloading it,
   filling it in and uploading it produced "These sheets are missing:
   Items, Stock" on the first thing a customer ever did. */

const MODEL_FILES = [
  ['/api/template.xlsx', 'Blank template',
   'Every sheet, no rows. Fill in what you have.'],
  ['/api/sample.xlsx', 'Sample data',
   'The same sheets with an invented importer in them. Upload it to '
   + 'see every screen working.'],
  ['/api/export.xlsx', 'Everything you have',
   'Your own figures, in the same shape. It imports back, so nothing '
   + 'here is locked in.'],
];

async function renderModel() {
  const files = $('model-files');
  const host = $('model-table');
  if (!files || !host) return;

  files.textContent = '';
  MODEL_FILES.forEach(([href, title, note]) => {
    const card = el('a', 'file-card');
    card.href = href;
    card.setAttribute('download', '');
    card.append(el('strong', null, title));
    card.append(el('span', null, note));
    files.append(card);
  });

  if (host.dataset.filled) return;
  let d;
  try {
    const r = await fetch('/api/model');
    d = await r.json();
    if (!r.ok) throw new Error('The model could not be read.');
  } catch (e) {
    host.textContent = '';
    host.append(empty('The model could not be read', e.message));
    return;
  }

  host.textContent = '';
  host.append(table(
    [['Sheet', ''], ['', ''], ['What it is', ''],
     ['What it switches on', ''], ['Where it lands', '']],
    (d.sheets || []).map((m) => [
      cell(m.sheet, 'lead'),
      cell(m.required ? 'needed' : '', 'dim'),
      cell(m.what),
      cell(m.unlocks, 'dim'),
      cell(m.tables.join(', ') || '—', 'dim'),
    ])));
  host.dataset.filled = '1';
}

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
