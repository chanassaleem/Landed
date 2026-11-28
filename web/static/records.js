/* Popout windows: adding a record without losing the screen you are on.
 *
 * Every record in this product used to arrive one of two ways -- a
 * spreadsheet import, or not at all. That is fine for the first
 * afternoon and wrong forever after: a customer rings, a machine is
 * placed, an invoice is raised, and re-importing a workbook to record
 * one of them is not a workflow anybody follows.
 *
 * A dialog rather than a page for a reason. The thing being added is
 * almost always in service of what is already on screen -- a contact
 * while looking at a customer, a commitment while looking at the cash
 * weeks -- and navigating away to a form loses the context that
 * prompted it. The screen behind stays put and reloads from the server
 * afterwards rather than guessing at the new figure, because a screen
 * that works out its own new total is a screen that can disagree with
 * the database.
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

  /* --------------------------------------------------------- the forms
     Each field is [name, label, type, options]. `req` marks what the
     server will refuse without. Nothing here validates a business rule
     -- the server owns those, and a second copy in the browser is a
     second thing to keep in step. */
  const FORMS = {
    /* The two that put a wrong figure right. Both are opened from the
       row that is wrong, with the record's identity filled in and
       locked, because the screen already knows it and retyping it is
       how a correction ends up filed against the wrong line. */
    shipment_basis: {
      title: 'Enter a missing measurement',
      blurb: 'Weight and volume are what freight is divided by. Until '
        + 'every line on a shipment has the one its charges use, the '
        + 'whole charge lands on the lines that do \u2014 so those '
        + 'are valued too high and the rest too low.',
      post: '/api/shipment/basis',
      fields: [
        ['shipment', 'Shipment', 'text', {}],
        ['sku', 'Item', 'text', {}],
        ['batch', 'Batch', 'text', {}],
        ['weight', 'Weight, kg', 'number',
          { hint: 'From the packing list or the weighbridge ticket. '
            + 'Enter 0 if this line really weighs nothing to the '
            + 'carrier \u2014 a blank is an unanswered question, a '
            + 'zero is an answer.' }],
        ['volume', 'Volume, cbm', 'number', {}],
        ['why', 'Where the figure comes from', 'text',
          { req: true, hint: 'This moves money between items, so the '
            + 'note is what explains the old split afterwards.' }],
      ],
    },
    receipt_correction: {
      title: 'Correct a receipt',
      blurb: 'The original stays exactly as it was filed. This adds a '
        + 'correction beside it and reverses the movement the first '
        + 'one made, so the ledger still only ever grows and anyone '
        + 'can see what was filed as well as what replaced it.',
      post: '/api/receipt/correct',
      fields: [
        ['grn', 'Receipt', 'text', {}],
        ['sku', 'Item', 'text', {}],
        ['batch', 'Batch', 'text', {}],
        ['qty', 'How many really arrived', 'number',
          { hint: 'Leave blank to keep the recorded quantity.' }],
        ['unit_cost', 'Cost each', 'number',
          { hint: 'In the currency the receipt is in, which is the '
            + 'order\u2019s where there was one. Blank keeps it.' }],
        ['fx', 'Exchange rate', 'number',
          { hint: 'The rate this consignment was really bought at. The '
            + 'operation has always accepted a corrected rate and this '
            + 'form did not offer it, so a rate entered wrongly could '
            + 'not be put right from the application at all \u2014 and '
            + 'a wrong rate moves the landed cost by the whole '
            + 'difference between two currencies.' }],
        ['expiry', 'Expires', 'date', {}],
        ['why', 'Why', 'text',
          { req: true, hint: 'In three months this is the only thing '
            + 'that will say whether it was a recount, a breakage or a '
            + 'supplier\u2019s revised invoice.' }],
      ],
    },
    item: {
      title: 'Add an item',
      blurb: 'A line you buy and sell. The lead time is what makes its '
        + 'reorder point mean anything, so it is worth being honest about.',
      post: '/api/record/item',
      fields: [
        ['sku', 'Code', 'text', { req: true, hint: 'Your own code for it.' }],
        ['description', 'What it is', 'text', { req: true }],
        ['category', 'Category', 'text', {}],
        ['uom', 'Sold as', 'text', { value: 'each' }],
        ['barcode', 'Barcode', 'text', {}],
        ['lead_time_days', 'Lead time, days', 'number',
          { hint: 'Order to arrival, door to door.' }],
        ['lead_time_days_std', 'How much it varies, days', 'number',
          { hint: 'A 75-day lead that slips three weeks is normal here. '
            + 'Say so, and the safety stock accounts for it.' }],
        ['is_clinical_critical', 'Clinically critical', 'check',
          { hint: 'Raises the service target. Never lowers it.' }],
      ],
    },
    /* The three that put committed money on the books: what you have
       ordered, what is carrying it, and what the carrier is charging
       for it. Each used to arrive only as a row in the import
       workbook, which is a workflow for the first load of history
       and not for a buyer who has just put the phone down. */
    order: {
      title: 'Raise a purchase order',
      blurb: 'What you have committed to buy. Ordering reads this to '
        + 'work out what is still outstanding, so an order that is '
        + 'not recorded is stock the system thinks you still need.',
      post: '/api/order',
      fields: [
        ['po_number', 'Order number', 'text',
          { req: true, hint: 'Yours, not the supplier\u2019s. It has '
            + 'to be unique here, and it is what a receipt is filed '
            + 'against.' }],
        ['supplier', 'Supplier code', 'text',
          { hint: 'Leave blank for a local cash purchase with no '
            + 'supplier record.' }],
        ['branch', 'For which branch', 'text', { req: true }],
        ['currency', 'Priced in', 'text',
          { hint: 'Blank means your own currency.' }],
        ['fx_rate', 'At what rate', 'number',
          { hint: 'The rate this order was bought at. Needed whenever '
            + 'the currency is not your own \u2014 without it there is '
            + 'no way to tell one currency from another further down.' }],
        ['expected_at', 'Expected', 'date', {}],
        ['note', 'Note', 'text', {}],
        ['lines', 'What you are ordering', 'lines', {
          req: true,
          hint: 'One line per item. An item can appear once on an '
            + 'order; two prices for the same item means two orders.',
          columns: [['sku', 'Item code', 'text'],
            ['qty', 'Quantity', 'number'],
            ['unit_price', 'Price each', 'number']],
        }],
      ],
    },
    shipment: {
      title: 'Book a shipment',
      blurb: 'What is carrying the goods. Receipts hang off it, and '
        + 'so do the charges that become their landed cost.',
      post: '/api/record/shipment',
      fields: [
        ['reference', 'Reference', 'text', { req: true }],
        ['supplier_code', 'Supplier code', 'text',
          { hint: 'Leave blank for a consolidation carrying several '
            + 'suppliers\u2019 goods \u2014 which is also what makes '
            + 'it valid for any order\u2019s receipt.' }],
        ['mode', 'How', 'select', {
          options: [['sea', 'Sea'], ['air', 'Air'], ['road', 'Road'],
            ['courier', 'Courier']],
        }],
        ['status', 'Where it is', 'select', {
          options: [['draft', 'Not shipped yet'],
            ['in_transit', 'In transit'], ['arrived', 'Arrived'],
            ['cleared', 'Cleared'], ['closed', 'Closed']],
        }],
        ['etd', 'Left on', 'date', {}],
        ['eta', 'Due', 'date', {}],
      ],
    },
    shipment_cost: {
      title: 'Add a charge to a shipment',
      blurb: 'Freight, duty, clearing and the rest. Each is divided '
        + 'across the goods on its own basis, and together they are '
        + 'the difference between a supplier\u2019s price and what '
        + 'the stock actually cost you.',
      post: '/api/record/shipment_cost',
      fields: [
        ['shipment_reference', 'Shipment', 'text', { req: true }],
        ['kind', 'What for', 'select', {
          req: true,
          options: [['freight', 'Freight'], ['duty', 'Duty'],
            ['clearing', 'Clearing'], ['inland', 'Inland transport'],
            ['bank', 'Bank charges'], ['insurance', 'Insurance'],
            ['other', 'Something else']],
        }],
        ['allocate_by', 'Divided by', 'select', {
          req: true,
          options: [['value', 'Value of the goods'],
            ['weight', 'Weight'], ['volume', 'Volume'],
            ['qty', 'Quantity']],
          hint: 'How this charge is split across the lines on the '
            + 'shipment. Duty usually goes by value, freight by '
            + 'weight or volume, clearing by quantity.',
        }],
        ['amount', 'Amount', 'number', { req: true }],
        ['currency', 'In what currency', 'text', { req: true, value: 'PKR' }],
        ['fx_rate', 'At what rate', 'number', { req: true, value: 1 }],
        ['note', 'Note', 'text', {}],
      ],
    },
    customer: {
      title: 'Add a customer',
      blurb: 'Who you sell to, their terms, and the limit you want to be '
        + 'warned about before the next order rather than after.',
      post: '/api/record/customer',
      fields: [
        ['code', 'Code', 'text', { req: true }],
        ['name', 'Name', 'text', { req: true }],
        ['city', 'City', 'text', {}],
        ['payment_terms_days', 'Terms, days', 'number', { value: 30 }],
        ['credit_limit', 'Credit limit', 'number', {}],
        ['is_public_sector', 'Public sector', 'check',
          { hint: 'Public hospitals pay late as a matter of course. '
            + 'Mixed into one collection figure, they hide whether the '
            + 'private book is healthy.' }],
        ['is_intercompany', 'Another company you own', 'check',
          { hint: 'Left out of every group total, because a sale to '
            + 'yourself is not money owed.' }],
        ['contact_name', 'Contact', 'text', {}],
        ['contact_phone', 'Phone', 'text', {}],
        ['contact_email', 'Email', 'text', {}],
      ],
    },
    supplier: {
      title: 'Add a supplier',
      post: '/api/record/supplier',
      fields: [
        ['code', 'Code', 'text', { req: true }],
        ['name', 'Name', 'text', { req: true }],
        ['country', 'Country', 'text', { hint: 'Two letters.' }],
        ['currency', 'Invoices you in', 'text', { value: 'USD' }],
        ['lead_time_days', 'Usual lead time, days', 'number', {}],
      ],
    },
    location: {
      title: 'Add a branch',
      blurb: 'A place that holds stock. Stock is counted per branch, and '
        + 'so is demand, which is what lets one branch supply another.',
      post: '/api/record/location',
      fields: [
        ['code', 'Code', 'text', { req: true, hint: 'Short. KHI, LHE.' }],
        ['name', 'Name', 'text', { req: true }],
        ['city', 'City', 'text', {}],
        ['is_warehouse', 'Warehouse', 'check', {}],
      ],
    },
    commitment: {
      title: 'Add a committed payment',
      blurb: 'Money already promised. Without these the cash weeks are '
        + 'fiction: the letter of credit settles and the salaries go out '
        + 'whatever the stock position says.',
      post: '/api/record/commitment',
      fields: [
        ['label', 'What it is', 'text', { req: true }],
        ['kind', 'Kind', 'select', {
          req: true,
          options: [['lc_settlement', 'Letter of credit'],
            ['loan_repayment', 'Loan repayment'], ['payroll', 'Payroll'],
            ['rent', 'Rent'], ['tax', 'Tax'], ['supplier', 'Supplier'],
            ['other', 'Something else']],
        }],
        ['due_date', 'Due', 'date', { req: true }],
        ['amount', 'Amount', 'number', { req: true }],
        ['is_recurring', 'Happens again', 'check', {}],
        ['recurs_every_days', 'Every, days', 'number',
          { value: 30, hint: 'Held once and spread across the weeks, so '
            + 'changing the rent changes every week at once.' }],
      ],
    },
    cash_balance: {
      title: 'Record the bank balance',
      blurb: 'What the cash weeks open from. Without it they open from '
        + 'nothing, and a cash view that opens at zero describes a '
        + 'business about to miss payroll.',
      post: '/api/record/cash_balance',
      fields: [
        ['as_at', 'As at', 'date', { req: true }],
        ['balance', 'Balance', 'number', { req: true }],
        ['account_label', 'Account', 'text', { value: 'Main account' }],
      ],
    },
    opportunity: {
      title: 'Add a tender or opportunity',
      blurb: 'The only part of the business that is a claim about the '
        + 'future. Naming the item lets it be checked against the shelf.',
      post: '/api/record/opportunity',
      fields: [
        ['reference', 'Reference', 'text', { req: true }],
        ['customer_code', 'Customer code', 'text', {}],
        ['stage', 'Stage', 'select', {
          req: true,
          options: [['lead', 'Lead'], ['qualified', 'Qualified'],
            ['quoted', 'Quoted'], ['tender_submitted', 'Tender submitted'],
            ['won', 'Won'], ['lost', 'Lost']],
        }],
        ['value', 'Value', 'number', { req: true }],
        ['expected_close', 'Expected to close', 'date', {}],
        ['item_sku', 'Item code', 'text',
          { hint: 'So the pipeline can be checked against the warehouse.' }],
        ['quantity', 'How many', 'number', {}],
        ['owner_name', 'Who is working it', 'text', {}],
        ['source', 'Came from', 'text', {}],
      ],
    },
    interaction: {
      title: 'Record a dealing',
      blurb: 'What was said, attached to who it was about. This is the '
        + 'half of a CRM anybody actually uses.',
      post: '/api/record/interaction',
      fields: [
        ['customer_code', 'Customer code', 'text', { req: true }],
        ['happened_on', 'When', 'date', { req: true }],
        ['channel', 'How', 'select', {
          options: [['note', 'A note'], ['call', 'Call'], ['visit', 'Visit'],
            ['email', 'Email'], ['whatsapp', 'WhatsApp'],
            ['meeting', 'Meeting']],
        }],
        ['summary', 'What happened', 'textarea', { req: true }],
        ['next_step', 'Next step', 'text', {}],
        ['next_step_on', 'By', 'date', {}],
      ],
    },
    employee: {
      title: 'Add an employee',
      blurb: 'Pay is split because the deductions are: tax is on the '
        + 'taxable part, EOBI is on the minimum wage, and medical is '
        + 'exempt up to a tenth of basic.',
      post: '/api/record/employee',
      fields: [
        ['employee_no', 'Number', 'text', { req: true }],
        ['name', 'Name', 'text', { req: true }],
        ['department', 'Department', 'text', {}],
        ['basic_monthly', 'Basic a month', 'number', { req: true }],
        ['allowances_monthly', 'Allowances a month', 'number', {}],
        ['medical_monthly', 'Medical a month', 'number', {}],
        ['employer_pf_rate', 'Provident, employer', 'number',
          { hint: 'As a fraction. 0.05 is five per cent.' }],
        ['employee_pf_rate', 'Provident, employee', 'number', {}],
        ['joined', 'Joined', 'date', {}],
      ],
    },
  };

  /* Which forms belong on which module screen. A module with nothing
     here has no record a person would reasonably type in one at a
     time -- a forecast is computed, not entered. */
  const ON_MODULE = {
    stock: ['item', 'location'],
    /* Ordering is where somebody looks at what policy says to buy,
       so it is where they raise the order for it. */
    buy: ['order'],
    /* Landing owns the shipment and what the carrier charges for it.
       Both here rather than split, because a charge arrives on the
       same invoice as the shipment it belongs to. */
    freight: ['shipment', 'shipment_cost'],
    cash: ['commitment', 'cash_balance'],
    customers: ['customer', 'supplier', 'interaction'],
    people: ['employee'],
    intelligence: ['opportunity'],
  };

  /* -------------------------------------------------------- the dialog */

  let open_ = null;

  function close() {
    if (!open_) return;
    const { root, restore } = open_;
    root.remove();
    document.removeEventListener('keydown', open_.onKey, true);
    open_ = null;
    if (restore && restore.focus) restore.focus();
  }

  function field(spec) {
    const [name, label, type, opt = {}] = spec;
    const wrap = el('div', `fld fld-${type}`);
    const id = `f-${name}`;

    if (type === 'check') {
      const row = el('label', 'fld-check');
      const box = el('input');
      box.type = 'checkbox';
      box.id = id;
      box.name = name;
      row.append(box);
      row.append(el('span', '', label));
      wrap.append(row);
      if (opt.hint) wrap.append(el('p', 'fld-hint', opt.hint));
      return wrap;
    }

    /* A repeating set of rows, for a record that is a header and its
       lines. An order is the only one so far, and it is the reason
       an order could not go through this builder before: a purchase
       order with no lines is not an order, and nothing here could
       collect more than one value for a field.

       Starts with one empty row, because a form that opens with
       nothing to type into reads as a form that is not ready. */
    if (type === 'lines') {
      wrap.classList.add('fld-lines');
      wrap.append(el('label', '', label + (opt.req ? ' *' : '')));
      if (opt.hint) wrap.append(el('p', 'fld-hint', opt.hint));

      const table = el('table', 'line-table');
      const head = el('tr');
      (opt.columns || []).forEach(([, text]) => head.append(el('th', '', text)));
      head.append(el('th', '', ''));
      /* Node.append returns undefined, so this cannot be chained. */
      const thead = el('thead');
      thead.append(head);
      table.append(thead);
      const tbody = el('tbody');
      table.append(tbody);

      const addRow = () => {
        const tr = el('tr');
        (opt.columns || []).forEach(([key, text, kind]) => {
          const td = el('td');
          const f = el('input');
          f.type = kind === 'number' ? 'number' : 'text';
          if (kind === 'number') f.step = 'any';
          f.dataset.line = key;
          f.setAttribute('aria-label', text);
          f.placeholder = text;
          td.append(f);
          tr.append(td);
        });
        const td = el('td', 'line-drop');
        const drop = el('button', 'linkish', 'Remove');
        drop.type = 'button';
        drop.addEventListener('click', () => {
          /* Never the last one: an order with no rows cannot be
             typed into, and the server would refuse it anyway. */
          if (tbody.children.length > 1) tr.remove();
          else tr.querySelectorAll('input').forEach((x) => { x.value = ''; });
        });
        td.append(drop);
        tr.append(td);
        tbody.append(tr);
        return tr;
      };
      addRow();

      wrap.append(table);
      const more = el('button', 'btn', 'Add another line');
      more.type = 'button';
      more.addEventListener('click', () => {
        addRow().querySelector('input').focus();
      });
      wrap.append(more);
      wrap.dataset.lines = name;
      return wrap;
    }

    const lab = el('label', '', label + (opt.req ? ' *' : ''));
    lab.htmlFor = id;
    wrap.append(lab);

    let input;
    if (opt.locked) {
      // Read-only rather than disabled: a disabled input is left out
      // of the form's own data, and these are the fields that say
      // which record is being changed.
      input = el('input');
      input.type = 'text';
      input.readOnly = true;
      input.id = id;
      input.name = name;
      input.value = opt.value === undefined ? '' : String(opt.value);
      input.classList.add('fld-locked');
      wrap.append(input);
      if (opt.hint) wrap.append(el('p', 'fld-hint', opt.hint));
      return wrap;
    }
    if (type === 'select') {
      input = el('select');
      (opt.options || []).forEach(([v, text]) => {
        const o = el('option', '', text);
        o.value = v;
        input.append(o);
      });
    } else if (type === 'textarea') {
      input = el('textarea');
      input.rows = 3;
    } else {
      input = el('input');
      input.type = type === 'number' ? 'number' : type;
      if (type === 'number') input.step = 'any';
      if (type === 'text') input.autocomplete = 'off';
    }
    input.id = id;
    input.name = name;
    if (opt.req) input.required = true;
    if (opt.value !== undefined) input.value = opt.value;
    wrap.append(input);
    if (opt.hint) wrap.append(el('p', 'fld-hint', opt.hint));
    return wrap;
  }

  /* `given` prefills fields and locks the ones that say which record
     this is about. An exception on a screen already knows the
     shipment, the SKU and the batch; asking somebody to retype them
     into a form opened from that very row is how a correction ends up
     filed against the wrong line. */
  function show(key, after, given) {
    if (!bind()) return;
    const spec = FORMS[key];
    if (!spec) return;
    close();
    const fixed = given || {};

    const restore = document.activeElement;
    const root = el('div', 'modal');
    const scrim = el('div', 'modal-scrim');
    scrim.addEventListener('click', close);
    root.append(scrim);

    const card = el('div', 'modal-card');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-label', spec.title);

    const head = el('div', 'modal-head');
    head.append(el('h2', '', spec.title));
    const x = el('button', 'iconbtn');
    x.type = 'button';
    x.setAttribute('aria-label', 'Close');
    x.textContent = '✕';
    x.addEventListener('click', close);
    head.append(x);
    card.append(head);

    if (spec.blurb) card.append(el('p', 'modal-blurb', spec.blurb));

    const form = el('form', 'modal-form');
    const problems = el('div', 'modal-problems');
    problems.hidden = true;
    form.append(problems);

    const grid = el('div', 'fld-grid');
    spec.fields.forEach((f) => {
      const [name, label, type, opt = {}] = f;
      if (!(name in fixed)) { grid.append(field(f)); return; }
      /* Shown, not hidden. Somebody about to change a figure on a
         historical record should be able to see which record, and a
         form that silently carries an identity in a hidden input is
         one nobody can check before they press the button. */
      grid.append(field([name, label, type,
        { ...opt, value: fixed[name], locked: true }]));
    });
    form.append(grid);

    const foot = el('div', 'modal-foot');
    const cancel = el('button', '', 'Cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', close);
    const save = el('button', 'primary', 'Save');
    save.type = 'submit';
    foot.append(cancel);
    foot.append(save);
    form.append(foot);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      problems.hidden = true;
      problems.textContent = '';
      save.disabled = true;
      save.textContent = 'Saving…';

      const body = {};
      spec.fields.forEach(([name, , type, opt]) => {
        if (type === 'lines') {
          const host = form.querySelector(`[data-lines="${name}"]`);
          if (!host) return;
          /* A row where nothing was typed is somebody who pressed
             "add another line" and changed their mind, not an order
             for nothing. Dropped here rather than sent for the
             server to refuse. */
          body[name] = [...host.querySelectorAll('tbody tr')]
            .map((tr) => {
              const row = {};
              (opt.columns || []).forEach(([key, , kind]) => {
                const f = tr.querySelector(`[data-line="${key}"]`);
                const v = (f && f.value.trim()) || '';
                if (v !== '') row[key] = kind === 'number' ? Number(v) : v;
              });
              return row;
            })
            .filter((row) => Object.keys(row).length);
          return;
        }
        const input = form.querySelector(`[name="${name}"]`);
        if (!input) return;
        if (type === 'check') { body[name] = input.checked; return; }
        const v = input.value.trim();
        if (v === '') return;
        body[name] = type === 'number' ? Number(v) : v;
      });

      let out;
      try {
        const res = await fetch(spec.post, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const text = await res.text();
        out = text ? JSON.parse(text) : {};
        if (!res.ok || out.ok === false) {
          const list = out.problems || [out.error || out.detail
            || `The server answered ${res.status}.`];
          list.forEach((m) => problems.append(el('p', '', m)));
          problems.hidden = false;
          save.disabled = false;
          save.textContent = 'Save';
          return;
        }
      } catch (err) {
        problems.append(el('p', '', 'The server did not answer. Nothing '
          + 'was saved.'));
        problems.hidden = false;
        save.disabled = false;
        save.textContent = 'Save';
        return;
      }

      close();
      /* Reload from the server rather than splicing the new row in.
         A screen that works out its own new total is a screen that can
         disagree with the database. */
      if (after) after(out);
    });

    card.append(form);
    root.append(card);
    document.body.append(root);

    /* Focus is trapped because a dialog that leaks focus to the page
       behind it is unusable with a keyboard and invisible to a screen
       reader. */
    const focusable = () => Array.from(card.querySelectorAll(
      'input, select, textarea, button')).filter((n) => !n.disabled);
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key !== 'Tab') return;
      const nodes = focusable();
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    open_ = { root, restore, onKey };

    const first = focusable()[0];
    if (first) first.focus();
  }

  /* An "Add" control for a module screen, or nothing when that module
     holds nothing a person types one at a time. */
  function actionsFor(moduleId, after) {
    if (!bind()) return null;
    const keys = ON_MODULE[moduleId];
    if (!keys || !keys.length) return null;
    const bar = el('div', 'actions');
    keys.forEach((k) => {
      const b = el('button', '', FORMS[k].title);
      b.type = 'button';
      b.addEventListener('click', () => show(k, after));
      bar.append(b);
    });
    return bar;
  }

  window.LANDED_RECORDS = { show, close, actionsFor, FORMS, ON_MODULE };
}());
