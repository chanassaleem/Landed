/* The platform: thirteen modules, one screen that draws any of them.
 *
 * Thirteen hand-written screens would be thirteen places to fix a
 * heading, and the twelve that are not stock would each be a table
 * with a different idea of how to print a number. So a module is
 * declared here -- its columns, the figures worth a tile, the notes
 * that have to be read -- and one renderer draws it from whatever
 * /api/module/<id> returns.
 *
 * Three things it deliberately does not do.
 *
 * It does not format money, quantities or months. app.js already
 * decides how a figure reads, including the crore-and-lakh grouping
 * that follows the country pack, and a second formatter here would
 * drift from the first the day somebody changed a country.
 *
 * It does not keep its own list of modules. The rail is built from
 * /api/modules, which is built from engine/platform.py, so a module
 * cannot appear in one and not the other.
 *
 * It does not decide what a module is allowed to show. A module the
 * role may not open never reaches the browser, and the API refuses it
 * again if asked directly. Hiding a rail entry is not a permission.
 */

(function () {
  'use strict';

  /* Bound when first used, not when this file is evaluated.
     platform.js is loaded before app.js -- it only defines things, and
     app.js calls into it from boot() -- so window.LANDED does not
     exist yet at this point and destructuring it here threw before a
     single screen was drawn. */
  let L, $, el, money, qty, pct, months, said;

  function bind() {
    if (L) return true;
    if (!window.LANDED) return false;
    L = window.LANDED;
    ({ $, el, money, qty, pct, months, said } = L);
    return true;
  }

  /* Formatters, by name, so a column spec reads as what the figure is
     rather than as a function reference. */
  const FMT = {
    text: (v) => (v === null || v === undefined || v === '' ? '—' : String(v)),
    said: (v) => (v ? said(v) : '—'),
    money: (v) => (v === null || v === undefined ? '—' : money(v)),
    qty: (v) => (v === null || v === undefined ? '—' : qty(v)),
    num: (v) => (v === null || v === undefined ? '—'
      : Number(v).toLocaleString()),
    pct: (v) => (v === null || v === undefined ? '—' : pct(v)),
    months: (v) => (v === null || v === undefined ? '—' : months(v)),
    hours: (v) => (v === null || v === undefined ? '—'
      : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 })}h`),
    days: (v) => (v === null || v === undefined ? '—'
      : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}d`),
    date: (v) => {
      if (!v) return '—';
      const d = new Date(v);
      if (Number.isNaN(d.getTime())) return String(v);
      return d.toLocaleDateString(undefined,
        { day: 'numeric', month: 'short', year: '2-digit' });
    },
    /* A verdict, printed as a verdict. These come out of the engine as
       upper-case enum values, which is not how anybody writes. */
    state: (v) => (v ? said(String(v).toLowerCase()) : '—'),
    bool: (v) => (v ? 'Yes' : 'No'),
    /* A list the engine wrote, as a sentence. Each cash week already
       carries what is in it -- the letter of credit, the salaries, the
       rent -- and a week that dips without saying why is a number
       somebody has to go and investigate by hand. */
    join: (v) => (Array.isArray(v) && v.length ? v.join(' · ') : '—'),
  };

  const NUMERIC = new Set(['money', 'qty', 'num', 'pct', 'months', 'hours',
    'days']);

  /* -------------------------------------------------------- the modules
     Each column is [key, label, format]. Each tile is [key, label,
     format] read from `totals`. `note` names the keys whose text has
     to be shown above the table rather than buried -- a caveat the
     engine attached is part of the answer, not decoration. */
  const SPEC = {
    stock: {
      cols: [
        ['item_key', 'SKU', 'text'],
        ['description', 'Item', 'text'],
        ['location_id', 'Branch', 'text'],
        ['on_hand', 'On hand', 'qty'],
        ['months_cover', 'Cover', 'months'],
        ['demand_class', 'Pattern', 'said'],
        ['status', 'State', 'state'],
        ['stock_value', 'Value', 'money'],
      ],
      tiles: [
        ['stock_value', 'On the shelf', 'money'],
        ['excess_value', 'Standing still', 'money'],
        ['order_value', 'Policy wants', 'money'],
        ['imports_avoided', 'Movable instead', 'money'],
        ['lines', 'Lines', 'num'],
        ['branches', 'Branches', 'num'],
      ],
      notes: ['blocked_note'],
    },
    buy: {
      cols: [
        ['item_key', 'SKU', 'text'],
        ['description', 'Item', 'text'],
        ['location_id', 'Branch', 'text'],
        ['quantity', 'Order', 'qty'],
        ['value', 'Value', 'money'],
        ['days_of_cover_left', 'Cover left', 'days'],
        ['verdict', 'Verdict', 'state'],
        ['place_by', 'Place by', 'date'],
      ],
      tiles: [
        ['requested', 'Policy wants', 'money'],
        ['place_now', 'Place now', 'money'],
        ['scheduled', 'Later weeks', 'money'],
        ['unfunded', 'Cannot fund', 'money'],
        ['by_transfer', 'Move instead', 'money'],
        ['clinical_unfunded', 'Critical unfunded', 'money'],
      ],
      notes: ['caveat', 'engine_caveat', 'reason'],
    },
    freight: {
      cols: [
        ['reference', 'Shipment', 'text'],
        ['supplier', 'Supplier', 'text'],
        ['mode', 'Mode', 'said'],
        ['eta', 'Due', 'date'],
        ['status', 'Status', 'said'],
        ['goods_value', 'Goods', 'money'],
        ['landed_costs', 'Landed on top', 'money'],
        ['uplift_pct', 'Uplift', 'pct'],
      ],
      tiles: [
        ['goods_value', 'Goods value', 'money'],
        ['landed_costs', 'Freight, duty, clearing', 'money'],
        ['uplift_pct', 'On top of the invoice', 'pct'],
        ['shipments', 'Shipments', 'num'],
      ],
    },
    demand: {
      cols: [
        ['item_key', 'SKU', 'text'],
        ['location_id', 'Branch', 'text'],
        ['demand_class', 'Pattern', 'said'],
        ['method', 'Method', 'said'],
        ['mean_monthly_demand', 'Sells a month', 'qty'],
        ['next_month', 'Next month', 'qty'],
        ['next_month_upper', 'Upper', 'qty'],
        ['mape', 'Error', 'pct'],
      ],
      tiles: [
        ['lines', 'Lines forecast', 'num'],
        ['measured', 'Accuracy measured', 'num'],
        ['seasonal', 'Seasonality fitted', 'num'],
      ],
    },
    cash: {
      cols: [
        ['week', 'Week', 'num'],
        ['starts', 'From', 'date'],
        ['lines', 'What is in it', 'join'],
        ['opening', 'Opening', 'money'],
        ['receipts', 'In', 'money'],
        ['payments', 'Out', 'money'],
        ['net', 'Net', 'money'],
        ['closing', 'Closing', 'money'],
      ],
      tiles: [
        ['lowest_balance', 'Lowest point', 'money'],
        ['lowest_week', 'In week', 'num'],
        ['weeks_negative', 'Weeks under', 'num'],
        ['headroom', 'Free for orders', 'money'],
        ['receipts', 'Expected in', 'money'],
        ['payments', 'Committed out', 'money'],
      ],
      notes: ['caveats'],
    },
    money: {
      cols: [
        ['customer_name', 'Customer', 'text'],
        ['invoice_count', 'Invoices', 'num'],
        ['outstanding', 'Owed', 'money'],
        ['overdue', 'Overdue', 'money'],
        ['worst_days_overdue', 'Worst', 'days'],
        ['weighted_avg_days_overdue', 'Typically late', 'days'],
        ['financing_cost_annual', 'Costs a year', 'money'],
        ['share_of_book', 'Share', 'pct'],
      ],
      tiles: [
        ['outstanding', 'Owed to you', 'money'],
        ['overdue', 'Past due', 'money'],
        ['dso_days', 'Collection days', 'days'],
        ['financing_cost', 'The delay costs', 'money'],
        ['cash_released_at_target', 'Freed at target', 'money'],
        ['intercompany_excluded', 'Inter-company left out', 'money'],
      ],
    },
    sell: {
      cols: [
        ['invoice_no', 'Invoice', 'text'],
        ['customer', 'Customer', 'text'],
        ['invoice_date', 'Raised', 'date'],
        ['due_date', 'Due', 'date'],
        ['amount', 'Amount', 'money'],
        ['amount_paid', 'Paid', 'money'],
        ['outstanding', 'Open', 'money'],
      ],
      tiles: [
        ['billed', 'Billed', 'money'],
        ['collected', 'Collected', 'money'],
        ['outstanding', 'Still open', 'money'],
        ['invoices', 'Invoices', 'num'],
      ],
      notes: ['note'],
    },
    customers: {
      cols: [
        ['code', 'Code', 'text'],
        ['name', 'Customer', 'text'],
        ['city', 'City', 'text'],
        ['payment_terms_days', 'Terms', 'days'],
        ['credit_limit', 'Limit', 'money'],
        ['outstanding', 'Owes', 'money'],
        ['contacts', 'Contacts', 'num'],
        ['last_contact', 'Last spoke', 'date'],
      ],
      tiles: [
        ['customers', 'Customers', 'num'],
        ['suppliers', 'Suppliers', 'num'],
        ['over_limit', 'Over their limit', 'num'],
        ['outstanding', 'Owed in total', 'money'],
      ],
      /* The one row that should never be a surprise. */
      flag: (r) => (r.over_limit ? 'st-out' : ''),
    },
    equipment: {
      cols: [
        ['machine_key', 'Machine', 'text'],
        ['model', 'Model', 'text'],
        ['customer_name', 'Placed with', 'text'],
        ['capital_cost', 'Capital', 'money'],
        ['net_recovered', 'Recovered', 'money'],
        ['recovery_pct', 'Recovered', 'pct'],
        ['payback_months', 'Pays back in', 'months'],
        ['status', 'Verdict', 'state'],
      ],
      tiles: [
        ['capital_deployed', 'Capital out there', 'money'],
        ['capital_recovered', 'Earned back', 'money'],
        ['recovery_pct', 'Recovered', 'pct'],
        ['downtime_margin_lost', 'Lost to downtime', 'money'],
        ['placements', 'Placements', 'num'],
      ],
    },
    service: {
      cols: [
        ['machine_key', 'Machine', 'text'],
        ['jobs', 'Jobs', 'num'],
        ['labour_hours', 'Hours worked', 'hours'],
        ['labour_hours_unbilled', 'Not billed', 'hours'],
        ['parts_uncharged', 'Parts given away', 'money'],
        ['repeat_visits', 'Second visits', 'num'],
        ['warranty_jobs', 'Under warranty', 'num'],
        ['value_lost', 'Value lost', 'money'],
      ],
      tiles: [
        ['value_lost', 'Given away', 'money'],
        ['labour_lost', 'Unbilled labour', 'money'],
        ['parts_lost', 'Uncharged parts', 'money'],
        ['recovery_rate', 'Recovered', 'pct'],
        ['repeat_visit_rate', 'Second visits', 'pct'],
        ['downtime_days', 'Days down', 'days'],
      ],
    },
    intelligence: {
      cols: [
        ['opportunity_id', 'Reference', 'text'],
        ['customer_name', 'Customer', 'text'],
        ['stage', 'Stage', 'said'],
        ['value', 'Value', 'money'],
        ['odds', 'Odds', 'pct'],
        ['weighted', 'Weighted', 'money'],
        ['expected_close', 'Closes', 'date'],
        ['supply', 'Can you supply it', 'said'],
      ],
      tiles: [
        ['open_value', 'In the pipeline', 'money'],
        ['weighted_value', 'Weighted', 'money'],
        ['win_rate', 'Win rate', 'pct'],
        ['cost_per_win', 'Cost per win', 'money'],
        ['spend', 'Spent', 'money'],
        ['at_risk_supply', 'Cannot supply', 'money'],
      ],
      notes: ['caveat', 'reason'],
    },
    quality: {
      cols: [
        ['severity', 'How bad', 'state'],
        ['dataset', 'Where', 'said'],
        ['title', 'What', 'text'],
        ['effect', 'What it does to the numbers', 'text'],
        ['fix', 'How to fix it', 'text'],
        ['count', 'Rows', 'num'],
      ],
      tiles: [
        ['blocks', 'Makes a figure wrong', 'num'],
        ['warns', 'Worth looking at', 'num'],
        ['notes', 'Worth knowing', 'num'],
        ['total', 'In all', 'num'],
      ],
      notes: ['verdict'],
      /* A blocking fault means a number on another screen is wrong,
         not merely unwelcome, so it is marked the way an overdrawn
         customer is. */
      flag: (r) => (r.severity === 'blocks' ? 'st-out' : ''),
    },
    people: {
      key: 'slips',
      cols: [
        ['name', 'Name', 'text'],
        ['department', 'Department', 'said'],
        ['gross_monthly', 'Gross', 'money'],
        ['tax_monthly', 'Tax', 'money'],
        ['eobi_employee', 'EOBI', 'money'],
        ['pf_employee', 'Provident', 'money'],
        ['net_monthly', 'Takes home', 'money'],
        ['employer_cost', 'Costs you', 'money'],
      ],
      tiles: [
        ['headcount', 'On payroll', 'num'],
        ['gross_monthly', 'Gross a month', 'money'],
        ['tax_monthly', 'Tax withheld', 'money'],
        ['net_monthly', 'Paid out', 'money'],
        ['employer_cost_monthly', 'Costs you a month', 'money'],
        ['employer_cost_annual', 'Costs you a year', 'money'],
      ],
    },
  };

  /* --------------------------------------------------------- the screen */

  function host() {
    let view = $('v-m');
    if (view) return view;
    view = el('section', 'view');
    view.id = 'v-m';
    const wrap = el('div', 'wrap');
    wrap.id = 'm-wrap';
    view.append(wrap);
    $('main').append(view);
    return view;
  }

  function tile(label, value) {
    const t = el('div', 'tile');
    t.append(el('div', 'tile-v num', value));
    t.append(el('div', 'tile-k', label));
    return t;
  }

  function note(text, kind) {
    const n = el('div', `note${kind ? ` note-${kind}` : ''}`);
    n.append(el('span', '', text));
    return n;
  }

  function plainTable(spec, rows) {
    const t = el('table');
    const head = el('tr');
    spec.cols.forEach(([, label, fmt]) => {
      head.append(el('th', NUMERIC.has(fmt) ? 'num' : '', label));
    });
    const thead = el('thead');
    thead.append(head);
    t.append(thead);

    const body = el('tbody');
    rows.forEach((r) => {
      const tr = el('tr');
      if (spec.flag) {
        const f = spec.flag(r);
        if (f) tr.classList.add(f);
      }
      spec.cols.forEach(([key, label, fmt]) => {
        const td = el('td', NUMERIC.has(fmt) ? 'num' : '',
          (FMT[fmt] || FMT.text)(r[key]));
        td.dataset.label = label;
        tr.append(td);
      });
      body.append(tr);
    });
    t.append(body);
    return t;
  }

  /* `missing` is the whole reason an empty module is not a bug. It
     names the record types this module needs and this business has
     none of, so the answer is "bring in X", not "no data". */
  function waiting(mod, missing) {
    const WORD = {
      items: 'items', demand: 'sales history', invoices: 'invoices',
      customers: 'customers', jobs: 'service jobs',
      placements: 'placed equipment', commitments: 'committed payments',
      accounts: 'a trial balance', employees: 'employees',
      opportunities: 'tenders or opportunities',
      marketing_spend: 'marketing spend', shipments: 'shipments',
      orders: 'purchase orders',
    };
    const names = missing.map((m) => WORD[m] || m);
    const last = names.pop();
    const list = names.length ? `${names.join(', ')} and ${last}` : last;
    return L.empty(`${mod.title} is waiting for figures`,
      `This needs ${list}. Bring them in on the Import screen, or add `
      + 'them here once there is something to add.');
  }

  function statements(data) {
    const out = el('div');
    if (!data.statements) {
      out.append(L.empty('No accounts loaded',
        'A trial balance brings the statements to life: profit, the '
        + 'balance sheet, and a cash flow worked out from both rather '
        + 'than typed in.'));
      return out;
    }
    const s = data.statements;

    /* The checks lead. A cash flow that does not tie is not a
       presentational problem -- it means a working-capital movement has
       been missed -- so whether the books hold together is the first
       thing on the screen, not a footnote under it. */
    const checks = el('div', 'checks');
    (data.checks || []).forEach((c) => {
      const row = el('div', `check ${c.passed ? 'ok' : 'bad'}`);
      row.append(el('span', 'check-mark', c.passed ? '✓' : '✕'));
      const body = el('div', 'check-body');
      body.append(el('div', 'check-name', c.name));
      body.append(el('div', 'check-why', c.meaning || ''));
      if (!c.passed) {
        body.append(el('div', 'check-gap',
          `Out by ${money(c.difference)}.`));
      }
      row.append(body);
      checks.append(row);
    });
    out.append(checks);

    const tiles = el('div', 'tiles');
    [['revenue', 'Revenue'], ['gross_profit', 'Gross profit'],
      ['operating_profit', 'Operating profit'],
      ['profit_after_tax', 'Profit after tax'],
      ['cash_closing', 'Cash at the end'],
      ['working_capital', 'Working capital']].forEach(([k, label]) => {
      tiles.append(tile(label, money(s[k])));
    });
    [['gross_margin', 'Gross margin'], ['net_margin', 'Net margin'],
      ['current_ratio', 'Current ratio'], ['gearing', 'Gearing']]
      .forEach(([k, label]) => {
        const v = s[k];
        tiles.append(tile(label, v === null || v === undefined ? '—'
          : (k === 'current_ratio'
            ? `${Number(v).toFixed(2)}×` : pct(v))));
      });
    out.append(tiles);

    [['pnl_rows', 'Profit and loss'], ['bs_rows', 'Balance sheet'],
      ['operating_lines', 'Cash from operations'],
      ['investing_lines', 'Cash from investing'],
      ['financing_lines', 'Cash from financing']].forEach(([k, label]) => {
      const rows = s[k] || [];
      if (!rows.length) return;
      const sec = el('div', 'section');
      const head = el('div', 'section-head');
      head.append(el('h2', '', label));
      sec.append(head);
      const panel = el('div', 'panel');
      panel.append(plainTable({
        cols: [['label', 'Line', 'text'], ['amount', 'Amount', 'money']],
      }, rows.map((r) => (Array.isArray(r)
        ? { label: said(r[0]), amount: r[1] }
        : { label: said(r.line || r.label || r.name), amount:
            r.amount === undefined ? r.closing : r.amount }))));
      sec.append(panel);
      out.append(sec);
    });
    return out;
  }

  async function open(mod) {
    if (!bind()) return;
    const wrap = host();
    document.querySelectorAll('.view').forEach((v) => {
      v.classList.toggle('on', v.id === 'v-m');
    });
    document.querySelectorAll('.rail a').forEach((a) => {
      if (a.dataset.module === mod.id) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    const line = $('money-line');
    if (line) line.hidden = true;
    history.replaceState(null, '', `#m/${mod.id}`);

    wrap.textContent = '';
    const sec = el('div', 'section');
    const head = el('div', 'section-head');
    head.append(el('h2', '', mod.title));
    head.append(el('p', '', mod.blurb));
    sec.append(head);
    wrap.append(sec);

    const body = el('div');
    body.append(el('p', 'loading', 'Working it out…'));
    sec.append(body);

    let payload;
    try {
      const res = await fetch(`/api/module/${mod.id}`);
      const text = await res.text();
      payload = text ? JSON.parse(text) : {};
      if (!res.ok) {
        body.textContent = '';
        body.append(L.empty('That module could not be read',
          payload.detail || payload.error
          || `The server answered ${res.status}.`));
        return;
      }
    } catch (e) {
      body.textContent = '';
      body.append(L.empty('That module could not be read',
        'The server did not answer with figures. Try again in a moment.'));
      return;
    }

    body.textContent = '';

    /* The add buttons go above everything, including the "waiting for
       figures" state -- that state is exactly when somebody wants to
       put the first record in, and hiding the way to do it until
       there is already data is the wrong way round. */
    if (window.LANDED_RECORDS) {
      const bar = window.LANDED_RECORDS.actionsFor(mod.id,
        () => open(mod));
      if (bar) body.append(bar);
    }

    if ((payload.missing || []).length) {
      body.append(waiting(mod, payload.missing));
      return;
    }
    const data = payload.data || {};

    if (mod.id === 'books') { body.append(statements(data)); return; }

    const spec = SPEC[mod.id];
    if (!spec) {
      body.append(L.empty('Nothing to draw yet',
        'This module has figures but no table defined for them.'));
      return;
    }

    /* The branch selector narrows the stock screen, client-side, by
       filtering rows. It does not narrow a module screen, and it must
       not pretend to: these tiles are engine totals -- collection
       days, the financing cost of the ageing book, a payback rate --
       and none of them can be recomputed from a filtered list of
       rows. Filtering the rows and leaving the tiles alone would put
       one branch's lines under the whole group's figures, which is
       worse than not filtering at all.

       So it says which it is. Narrowing a module properly means the
       server doing it, and that is work that has not been done. */
    const branch = ($('branch') || {}).value;
    if (branch && branch !== 'ALL') {
      body.append(note(mod.group_level
        ? `These figures cover the whole business. ${branch} cannot be `
          + 'separated out here: a statement belongs to a company, and a '
          + 'transfer has two ends.'
        : `These figures cover every branch, not just ${branch}. The `
          + 'branch selector narrows the stock screen only.', 'warn'));
    }

    (spec.notes || []).forEach((key) => {
      const v = data[key];
      if (!v) return;
      (Array.isArray(v) ? v : [v]).forEach((t) => {
        if (t) body.append(note(String(t)));
      });
    });

    /* Provenance, where a figure's meaning depends on where it came
       from. A collection figure computed off invoices and one computed
       off audited revenue are not the same claim. */
    if (data.revenue_basis) {
      body.append(note(data.revenue_basis === 'books'
        ? 'Collection days are measured against revenue in your trial '
          + 'balance.'
        : 'Collection days are measured against invoices raised, because '
          + 'no trial balance is loaded.', 'quiet'));
    }
    if (data.table && data.table.name) {
      body.append(note(`Rates: ${data.table.name}. ${data.table.source
        || ''}`, 'quiet'));
    }
    if (data.needs_balance) {
      body.append(note('No bank balance entered, so this opens from '
        + 'nothing. Add one and the weeks become real.', 'warn'));
    }
    if (data.error) { body.append(note(data.error, 'warn')); }

    const totals = data.totals || {};
    const tiles = el('div', 'tiles');
    let any = false;
    (spec.tiles || []).forEach(([key, label, fmt]) => {
      if (!(key in totals)) return;
      const v = totals[key];
      if (v === null || v === undefined) return;
      any = true;
      tiles.append(tile(label, (FMT[fmt] || FMT.text)(v)));
    });
    if (any) body.append(tiles);

    const rows = data[spec.key || 'rows'] || [];
    const panel = el('div', 'panel');
    if (!rows.length) {
      panel.append(L.empty('Nothing here yet',
        'The module is reading your figures and finding none of this '
        + 'kind.'));
    } else {
      panel.append(plainTable(spec, rows));
    }
    body.append(panel);

    if (data.breakdown && data.breakdown.length) {
      const sec2 = el('div', 'section');
      const h = el('div', 'section-head');
      h.append(el('h2', '', 'What the costs were, and how each was spread'));
      h.append(el('p', '', 'A shipping line bills by volume and customs '
        + 'by value, so each cost is allocated on its own basis. Spread '
        + 'the same way, one of them would be wrong.'));
      sec2.append(h);
      const p2 = el('div', 'panel');
      p2.append(plainTable({
        cols: [['kind', 'Cost', 'said'], ['allocate_by', 'Spread by', 'said'],
          ['amount', 'Amount', 'money']],
      }, data.breakdown));
      sec2.append(p2);
      body.append(sec2);
    }

    if (data.lines && data.lines.length) {
      const sec3 = el('div', 'section');
      const h = el('div', 'section-head');
      h.append(el('h2', '', 'What each line actually cost'));
      h.append(el('p', '', 'The landed figure is what the item really '
        + 'cost you. The gap between it and the invoice price is what '
        + 'pricing off the invoice gets wrong.'));
      sec3.append(h);
      const p3 = el('div', 'panel');
      p3.append(plainTable({
        cols: [['sku', 'SKU', 'text'], ['description', 'Item', 'text'],
          ['shipment', 'Shipment', 'text'], ['batch', 'Batch', 'text'],
          ['expiry_date', 'Expires', 'date'], ['qty', 'Qty', 'qty'],
          ['fob_unit_cost', 'Invoice each', 'money'],
          ['landed_unit_cost', 'Landed each', 'money']],
      }, data.lines));
      sec3.append(p3);
      body.append(sec3);
    }
  }


  /* ----------------------------------------------------------- people

     Who is in this organisation and who has been asked in. Members and
     pending invites are one list on the screen because they are one
     question: who has access. Somebody invited yesterday who has not
     signed up yet is part of the answer.
  */

  function peopleHost() {
    let view = $('v-people');
    if (view) return $('people-wrap');
    view = el('section', 'view');
    view.id = 'v-people';
    const wrap = el('div', 'wrap');
    wrap.id = 'people-wrap';
    view.append(wrap);
    $('main').append(view);
    return wrap;
  }

  async function renderPeople() {
    if (!bind()) return;
    const wrap = peopleHost();
    document.querySelectorAll('.view').forEach((v) => {
      v.classList.toggle('on', v.id === 'v-people');
    });
    document.querySelectorAll('.rail a').forEach((a) => {
      if (a.dataset.view === 'people') a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    const line = $('money-line');
    if (line) line.hidden = true;
    history.replaceState(null, '', '#people');

    wrap.textContent = '';
    const sec = el('div', 'section');
    const head = el('div', 'section-head');
    head.append(el('h2', '', 'People'));
    head.append(el('p', '', 'Who can open this business, what each of '
      + 'them sees, and who has been asked in.'));
    sec.append(head);
    wrap.append(sec);

    const body = el('div');
    body.append(el('p', 'loading', 'Reading…'));
    sec.append(body);

    let d;
    try {
      const res = await fetch('/api/users');
      d = await res.json();
      if (!res.ok || d.error) {
        body.textContent = '';
        body.append(L.empty('The people list could not be read',
          d.detail || d.error || `The server answered ${res.status}.`));
        return;
      }
    } catch (e) {
      body.textContent = '';
      body.append(L.empty('The people list could not be read',
        'The server did not answer. Try again in a moment.'));
      return;
    }

    body.textContent = '';
    const branch = (ids) => (!ids || !ids.length ? 'Every branch'
      : ids.map((i) => (d.branches[i] || {}).code || '?').join(', '));

    if (d.can_invite) body.append(inviteForm(d));

    const members = el('div', 'section');
    const mh = el('div', 'section-head');
    mh.append(el('h2', '', 'In this business'));
    members.append(mh);
    const mp = el('div', 'panel');
    mp.append(plainTable({
      cols: [['who', 'Who', 'text'], ['role', 'Role', 'said'],
        ['where', 'Branches', 'text'], ['since', 'Since', 'date'],
        ['act', '', 'text']],
    }, d.members.map((m) => ({
      who: m.is_you ? `${m.email} (you)` : `Member ${m.user_id.slice(0, 8)}`,
      role: m.role,
      where: branch(m.locations),
      since: m.since,
      act: '',
    }))));
    members.append(mp);

    /* Addresses live in auth.users, which a tenant cannot read, so a
       colleague shows as their role and branches rather than by name.
       Said plainly rather than shown as a blank somebody has to
       wonder about. */
    if (d.members.some((m) => !m.is_you)) {
      members.append(note('Colleagues show by role rather than by name: '
        + 'email addresses are held by the sign-in service, which a '
        + 'business cannot read from inside its own data.', 'quiet'));
    }
    body.append(members);

    if (d.can_change) {
      const rows = d.members.filter((m) => !m.is_you);
      if (rows.length) body.append(changeRows(rows, d, branch));
    }

    const inv = el('div', 'section');
    const ih = el('div', 'section-head');
    ih.append(el('h2', '', 'Asked in, not yet arrived'));
    inv.append(ih);
    const ip = el('div', 'panel');
    if (!d.invites.length) {
      ip.append(L.empty('Nobody is waiting',
        'An invite is claimed when that address signs up.'));
    } else {
      const t = el('table');
      const thead = el('thead');
      const hr = el('tr');
      ['Email', 'Role', 'Branches', 'Expires', ''].forEach((h) => {
        hr.append(el('th', '', h));
      });
      thead.append(hr);
      t.append(thead);
      const tb = el('tbody');
      d.invites.forEach((i) => {
        const tr = el('tr');
        tr.append(el('td', '', i.email));
        tr.append(el('td', '', said(i.role)));
        tr.append(el('td', '', branch(i.locations)));
        tr.append(el('td', '', i.expired ? 'Expired'
          : FMT.date(i.expires)));
        const act = el('td');
        if (d.can_invite) {
          const b = el('button', '', 'Withdraw');
          b.type = 'button';
          b.addEventListener('click', async () => {
            b.disabled = true;
            b.textContent = 'Withdrawing…';
            await fetch(`/api/invite/${i.id}/revoke`, { method: 'POST' });
            renderPeople();
          });
          act.append(b);
        }
        tr.append(act);
        tb.append(tr);
      });
      t.append(tb);
      ip.append(t);
    }
    inv.append(ip);
    body.append(inv);

    const roles = el('div', 'section');
    const rh = el('div', 'section-head');
    rh.append(el('h2', '', 'What each role can open'));
    rh.append(el('p', '', 'Picking a role is picking which screens '
      + 'somebody sees. The gate is on the server as well, so a role '
      + 'that cannot open the books cannot read them by asking.'));
    roles.append(rh);
    const rp = el('div', 'panel');
    rp.append(plainTable({
      cols: [['id', 'Role', 'said'], ['blurb', 'What they get', 'text'],
        ['count', 'Modules', 'num']],
    }, d.roles.map((r) => ({ ...r, count: r.modules.length }))));
    roles.append(rp);
    body.append(roles);
  }

  function inviteForm(d) {
    const sec = el('div', 'section');
    const head = el('div', 'section-head');
    head.append(el('h2', '', 'Ask somebody in'));
    head.append(el('p', '', 'No email is sent. Send them the address '
      + 'yourself; when they sign up with this exact email they land '
      + 'in this business with the role you picked.'));
    sec.append(head);

    const form = el('form', 'invite-form');
    const email = el('input');
    email.type = 'email';
    email.required = true;
    email.placeholder = 'name@company.com';
    email.setAttribute('aria-label', 'Email');

    const role = el('select');
    role.setAttribute('aria-label', 'Role');
    d.roles.forEach((r) => {
      const o = el('option', '', `${said(r.id)} — ${r.blurb}`);
      o.value = r.id;
      role.append(o);
    });
    role.value = 'staff';

    const go = el('button', 'primary', 'Send the invite');
    go.type = 'submit';

    const problems = el('div', 'modal-problems');
    problems.hidden = true;

    form.append(email);
    form.append(role);
    form.append(go);
    sec.append(form);
    sec.append(problems);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      problems.hidden = true;
      problems.textContent = '';
      go.disabled = true;
      go.textContent = 'Sending…';
      try {
        const res = await fetch('/api/invite', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: email.value.trim(),
            role: role.value }),
        });
        const out = await res.json();
        if (!res.ok || out.ok === false) {
          (out.problems || [out.error || 'That was refused.'])
            .forEach((m) => problems.append(el('p', '', m)));
          problems.hidden = false;
          go.disabled = false;
          go.textContent = 'Send the invite';
          return;
        }
      } catch (err) {
        problems.append(el('p', '', 'The server did not answer.'));
        problems.hidden = false;
        go.disabled = false;
        go.textContent = 'Send the invite';
        return;
      }
      renderPeople();
    });
    return sec;
  }

  function changeRows(rows, d, branch) {
    const sec = el('div', 'section');
    const head = el('div', 'section-head');
    head.append(el('h2', '', 'Change what somebody can do'));
    head.append(el('p', '', 'Your own membership is not here. The only '
      + 'owner of a business demoting themselves loses it, with nobody '
      + 'left who can let them back in.'));
    sec.append(head);
    const panel = el('div', 'panel');
    const list = el('div', 'change-list');
    rows.forEach((m) => {
      const row = el('div', 'change-row');
      row.append(el('span', '',
        `Member ${m.user_id.slice(0, 8)} · ${branch(m.locations)}`));
      const sel = el('select');
      sel.setAttribute('aria-label', 'Role');
      d.roles.forEach((r) => {
        const o = el('option', '', said(r.id));
        o.value = r.id;
        sel.append(o);
      });
      sel.value = m.role;
      const save = el('button', '', 'Change');
      save.type = 'button';
      save.addEventListener('click', async () => {
        save.disabled = true;
        await fetch(`/api/users/${m.user_id}/role`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: sel.value }),
        });
        renderPeople();
      });
      const drop = el('button', '', 'Remove');
      drop.type = 'button';
      drop.addEventListener('click', async () => {
        drop.disabled = true;
        await fetch(`/api/users/${m.user_id}`, { method: 'DELETE' });
        renderPeople();
      });
      row.append(sel);
      row.append(save);
      row.append(drop);
      list.append(row);
    });
    panel.append(list);
    sec.append(panel);
    return sec;
  }

  /* ----------------------------------------------------------- the rail */

  /* Stock keeps its own screen: the money band, the named views, the
     detail panel and the four operations. Replacing that with a plain
     table for the sake of symmetry would be a downgrade.
     
     It still has to carry what the module API knows and /api/state
     does not -- chiefly the surplus that cannot be moved, which is
     the difference between "no transfers today" and "the two limits
     cross and nothing can ever move". */
  const BESPOKE = { stock: 'stock' };

  async function decorateStock() {
    const panel = $('views');
    if (!panel || panel.dataset.noted === '1') return;
    try {
      const res = await fetch('/api/module/stock');
      if (!res.ok) return;
      const d = (await res.json()).data || {};
      if (!d.blocked_note) return;
      panel.parentNode.insertBefore(note(d.blocked_note, 'warn'), panel);
      panel.dataset.noted = '1';
    } catch (e) { /* the note is extra; the screen stands without it */ }
  }

  async function buildRail() {
    if (!bind()) return null;
    let listed;
    try {
      const res = await fetch('/api/modules');
      if (!res.ok) return null;
      listed = await res.json();
    } catch (e) { return null; }

    const group = $('rail-items');
    group.textContent = '';

    const add = (label, view, onClick) => {
      const a = el('a');
      a.href = `#${view}`;
      a.dataset.view = view;
      a.title = label;
      a.append(L.icon(L.ICON[view] || L.ICON.items));
      a.append(el('span', 'rail-label', label));
      a.addEventListener('click', (e) => {
        e.preventDefault();
        if (onClick) onClick(); else L.go(view);
      });
      group.append(a);
    };

    add('Home', 'home');
    add('Today', 'today');

    const bySection = {};
    listed.modules.forEach((m) => {
      (bySection[m.section] = bySection[m.section] || []).push(m);
    });

    listed.sections.forEach((s) => {
      const mods = bySection[s.id] || [];
      if (!mods.length) return;
      group.append(el('div', 'sep'));
      const label = el('div', 'rail-section');
      label.append(el('span', 'rail-label', s.label));
      group.append(label);

      mods.forEach((m) => {
        const a = el('a');
        a.href = `#m/${m.id}`;
        a.dataset.module = m.id;
        a.title = m.state === 'locked'
          ? `${m.title} — not licensed`
          : m.title;
        a.append(L.icon(L.ICON[m.id] || L.ICON.items));
        a.append(el('span', 'rail-label', m.title));
        if (m.state !== 'ready') {
          a.append(el('span', 'rail-dot', m.state === 'locked' ? '·' : '○'));
        }
        a.addEventListener('click', (e) => {
          e.preventDefault();
          /* Stock keeps its own screen. It is the one module with a
             bespoke interface -- the money band, the named views, the
             detail panel -- and replacing that with a plain table to
             be consistent would be a downgrade for the sake of
             symmetry. */
          if (BESPOKE[m.id]) {
            L.go(BESPOKE[m.id]);
            if (m.id === 'stock') decorateStock();
            return;
          }
          open(m);
        });
        group.append(a);
      });
    });

    group.append(el('div', 'sep'));
    add('People', 'people', renderPeople);
    add('Import', 'import');
    add('Settings', 'settings');

    window.LANDED_MODULES = listed;
    return listed;
  }

  window.LANDED_PLATFORM = { buildRail, open, decorateStock, renderPeople,
    SPEC, FMT, plainTable, tile, note };
}());
