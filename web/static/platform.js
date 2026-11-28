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
  let L, $, el, money, qty, pct, months, said, GROUPING;

  function bind() {
    if (L) return true;
    if (!window.LANDED) return false;
    L = window.LANDED;
    ({ $, el, money, qty, pct, months, said, GROUPING } = L);
    return true;
  }

  /* Formatters, by name, so a column spec reads as what the figure is
     rather than as a function reference. */
  const FMT = {
    text: (v) => (v === null || v === undefined || v === '' ? '—' : String(v)),
    said: (v) => (v ? said(v) : '—'),
    money: (v) => (v === null || v === undefined ? '—' : money(v)),
    qty: (v) => (v === null || v === undefined ? '—' : qty(v)),
    /* The same grouping as every other figure. See GROUPING in
       app.js: taking it from the viewer's browser drew the same
       number two ways on one screen. */
    num: (v) => (v === null || v === undefined ? '—'
      : Number(v).toLocaleString(GROUPING)),
    pct: (v) => (v === null || v === undefined ? '—' : pct(v)),
    months: (v) => (v === null || v === undefined ? '—' : months(v)),
    hours: (v) => (v === null || v === undefined ? '—'
      : `${Number(v).toLocaleString(GROUPING,
        { maximumFractionDigits: 1 })}h`),
    days: (v) => (v === null || v === undefined ? '—'
      : `${Number(v).toLocaleString(GROUPING,
        { maximumFractionDigits: 0 })}d`),
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

  /* Dates are left-aligned like text but must not wrap: "Sep 21, 26"
     broken over three lines makes a row twice as tall as its
     neighbours and the column impossible to scan. */
  const TIGHT = new Set(['date']);

  /* -------------------------------------------------------- the modules
     Each column is [key, label, format]. Each tile is [key, label,
     format] read from `totals`. `note` names the keys whose text has
     to be shown above the table rather than buried -- a caveat the
     engine attached is part of the answer, not decoration. */
  const SPEC = {
    stock: {
      /* A stock line has movements behind it and operations that write
         to them, so its rows open. Nothing else does yet. */
      opens: true,
      /* Every figure derived from the landed cost, not just the cost
         itself. A margin computed off a provisional cost is exactly
         as provisional, and the margin is the figure somebody prices
         against. See migration 0024.
         Value at cost and margin held are left unmarked on purpose:
         they are totals over on-hand quantity, where the skew tends
         to wash out across lines, and marking every money column on
         the row would make the mark mean nothing. */
      mark: (row, col) => {
        if (!row.cost_provisional) return null;
        if (!['unit_cost', 'margin', 'margin_pct'].includes(col.key)) {
          return null;
        }
        const of = row.cost_provisional_of, n = row.cost_receipts;
        const part = (of && n && of < n)
          ? `${of} of the ${n} receipts behind this cost came in`
          : 'This cost came in';
        /* The reason the view actually gives, not a guess at it:
           "no volume recorded" and "weight missing on 1 of 2 lines"
           need different work, and sending everybody to the freight
           screen to find out which wastes the trip. */
        /* A dash, because the view's reasons are fragments -- "no
           volume recorded", "weight missing on 1 of 2 lines" -- and
           no conjunction reads correctly in front of all of them. */
        const why = row.cost_why ? ` \u2014 ${row.cost_why}` : '';
        return `Provisional. ${part} on a shipment with a charge that `
          + `could not be divided${why}. The figure will move once the `
          + 'missing measurement is entered, on the Landing screen.';
      },
      cols: [
        ['item_key', 'SKU', 'text'],
        ['description', 'Item', 'text'],
        ['location_id', 'Branch', 'text'],
        ['on_hand', 'On hand', 'qty'],
        ['uom', 'Unit', 'text'],
        ['months_cover', 'Cover', 'months'],
        ['status', 'State', 'state'],
        ['unit_cost', 'Landed each', 'money'],
        ['list_price', 'Sells for', 'money'],
        ['margin', 'Margin each', 'money'],
        ['margin_pct', 'Margin', 'pct'],
        ['stock_value', 'Value at cost', 'money'],
        ['line_margin', 'Margin held', 'money'],
        ['fits', 'Used on', 'text'],
        ['category', 'Category', 'text'],
        ['demand_class', 'Pattern', 'said'],
        ['abc', 'Class', 'text'],
        ['mean_monthly_demand', 'Sells a month', 'qty'],
        ['reorder_point', 'Reorder at', 'qty'],
        ['suggested_order_qty', 'Order now', 'qty'],
        ['order_value', 'Order value', 'money'],
        ['excess_value', 'Standing still', 'money'],
        ['lead_time_days', 'Lead time', 'days'],
        ['lead_time_days_std', 'Lead varies', 'days'],
        ['open_po', 'On order', 'qty'],
        ['position', 'Position', 'qty'],
        ['safety_stock', 'Safety stock', 'qty'],
        ['service_target', 'Service target', 'pct'],
        ['days_to_expiry', 'Expires in', 'days'],
        ['is_clinical_critical', 'Critical', 'bool'],
        ['barcode', 'Barcode', 'text'],
        ['reason', 'Why', 'text'],
      ],
      /* Twenty-eight columns is too many to show and not too many to
         have. Eight are on by default -- the ones somebody reads
         every morning -- and the rest are a tick away in the column
         picker, remembered per person. */
      show: ['item_key', 'description', 'location_id', 'on_hand',
             'months_cover', 'status', 'unit_cost', 'stock_value'],
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every line at every branch.' },
        { id: 'decide', label: 'Needs a decision',
          blurb: 'Running out, due a reorder, standing still or '
            + 'expiring. The lines that want somebody today.',
          keep: (r) => ['critical', 'reorder', 'excess', 'expiry_risk']
            .includes(String(r.status).toLowerCase()) },
        { id: 'short', label: 'Running out',
          blurb: 'At or below the reorder point.',
          keep: (r) => ['critical', 'reorder']
            .includes(String(r.status).toLowerCase()) },
        { id: 'idle', label: 'Standing still',
          blurb: 'More cover than the standing-still limit allows.',
          keep: (r) => String(r.status).toLowerCase() === 'excess' },
        { id: 'expiring', label: 'Expiring',
          blurb: 'More on the shelf than will be used before it '
            + 'expires.',
          keep: (r) => String(r.status).toLowerCase() === 'expiry_risk' },
        { id: 'critical', label: 'Clinically critical',
          blurb: 'Lines flagged critical, whatever their state. A '
            + 'stockout here stops a hospital testing.',
          keep: (r) => !!r.is_clinical_critical },
        { id: 'priced', label: 'Priced',
          blurb: 'Lines with a selling price, so the margin is real.',
          keep: (r) => r.list_price !== null
                       && r.list_price !== undefined },
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every line policy asked for.' },
        { id: 'now', label: 'Place this week',
          blurb: 'Funded, and short enough that waiting costs more '
            + 'than ordering.',
          keep: (r) => String(r.verdict).toLowerCase() === 'place_now' },
        { id: 'refused', label: 'Cash will not fund',
          blurb: 'Policy wants it and the next thirteen weeks cannot '
            + 'pay for it. Finance it, split it, or accept the '
            + 'stockout deliberately.',
          keep: (r) => String(r.verdict).toLowerCase() === 'unfunded' },
        { id: 'transfer', label: 'Move instead',
          blurb: 'Another branch holds it above its own policy level, '
            + 'so this needs no import at all.',
          keep: (r) => String(r.verdict).toLowerCase() === 'by_transfer' },
      ],
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every shipment, landed or not.' },
        { id: 'landed', label: 'Costed',
          blurb: 'Goods recorded against them, so the uplift on each '
            + 'line is real.',
          keep: (r) => r.goods_value > 0 },
        { id: 'stranded', label: 'No goods yet',
          blurb: 'Carrying costs with nothing to spread them across. '
            + 'Either still at sea, or the receipt has not been '
            + 'entered.',
          keep: (r) => !(r.goods_value > 0) },
        { id: 'water', label: 'In transit',
          blurb: 'Shipped and not yet arrived.',
          keep: (r) => String(r.status).toLowerCase() !== 'arrived' },
      ],
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
        ['shipments_landed', 'Shipments with goods', 'num'],
        ['awaiting_goods', 'Awaiting goods', 'num'],
        ['unallocated', 'Reached no item', 'money'],
      ],
      /* Which charge went nowhere, and what to go and do about it.
         The totals above say how much; this says which, and the
         action is the sentence somebody can act on. */
      panels: [{
        key: 'needs_measuring',
        title: 'Lines with no measurement for the freight to divide by',
        cols: [
          ['shipment', 'Shipment', 'text'],
          ['sku', 'Item', 'text'],
          ['batch', 'Batch', 'text'],
          ['qty', 'Quantity', 'qty'],
          ['missing', 'What is missing', 'text'],
        ],
        act: {
          label: 'Enter it',
          form: 'shipment_basis',
          identify: (r) => ({ shipment: r.shipment, sku: r.sku,
            batch: r.batch || '' }),
        },
      }, {
        key: 'problems',
        title: 'Charges that have not reached an item',
        cols: [
          ['shipment', 'Shipment', 'text'],
          ['kind', 'Charge', 'said'],
          ['billed', 'Billed', 'money'],
          ['allocate_by', 'Divided by', 'said'],
          ['blocked_by', 'What is missing', 'text'],
          ['action', 'What to do', 'text'],
        ],
      }],
      notes: ['unallocated_note', 'awaiting_note'],
    },
    demand: {
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every line with a forecast.' },
        { id: 'measured', label: 'Accuracy known',
          blurb: 'Enough history to measure the error on a holdout, '
            + 'so the safety stock behind it means something.',
          keep: (r) => r.mape !== null && r.mape !== undefined },
        { id: 'unmeasured', label: 'Too little history',
          blurb: 'Forecast from what there is, with no honest way to '
            + 'say how wrong it might be. Decide these by hand.',
          keep: (r) => r.mape === null || r.mape === undefined },
        { id: 'lumpy', label: 'Lumpy or intermittent',
          blurb: 'Sells in bursts, so an average is the wrong tool '
            + 'and the method changes accordingly.',
          keep: (r) => ['lumpy', 'intermittent', 'erratic']
            .includes(String(r.demand_class).toLowerCase()) },
      ],
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'All thirteen weeks.' },
        { id: 'under', label: 'Below the floor',
          blurb: 'Weeks where the closing balance breaks the buffer. '
            + 'These are what Ordering is refusing orders against.',
          keep: (r) => r.closing < 0 },
        { id: 'out', label: 'Money going out',
          blurb: 'Weeks with a net outflow.',
          keep: (r) => r.net < 0 },
      ],
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every customer with a balance.' },
        { id: 'overdue', label: 'Overdue',
          blurb: 'Past their own terms.',
          keep: (r) => r.overdue > 0 },
        { id: 'late', label: 'Habitually late',
          blurb: 'Late on average rather than late this once. The '
            + 'ones to check before extending more credit.',
          keep: (r) => r.weighted_avg_days_overdue > 30 },
        { id: 'big', label: 'Worst exposure',
          blurb: 'More than a tenth of the open book sitting with one '
            + 'customer.',
          keep: (r) => r.share_of_book > 0.1 },
      ],
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every invoice raised.' },
        { id: 'open', label: 'Not yet paid',
          blurb: 'Still outstanding, whatever the terms say.',
          keep: (r) => r.outstanding > 0 },
        { id: 'part', label: 'Part paid',
          blurb: 'Something came in and something did not.',
          keep: (r) => r.amount_paid > 0 && r.outstanding > 0 },
      ],
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Everyone you deal with.' },
        { id: 'owing', label: 'Owing you',
          blurb: 'Carrying a balance right now.',
          keep: (r) => r.outstanding > 0 },
        { id: 'over', label: 'Over their limit',
          blurb: 'Past the credit limit you set. Worth knowing before '
            + 'the next order, not after the invoice ages.',
          keep: (r) => r.credit_limit > 0 && r.outstanding > r.credit_limit },
        { id: 'quiet', label: 'Not heard from',
          blurb: 'No dealing recorded against them.',
          keep: (r) => !r.last_contact },
      ],
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
      flag: (r) => (r.over_limit ? 'flagged' : ''),
    },
    equipment: {
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every machine placed.' },
        { id: 'behind', label: 'Will not repay',
          blurb: 'At the current pull, the capital does not come back '
            + 'inside the contract. Renegotiate the volume or recover '
            + 'the instrument.',
          keep: (r) => ['behind', 'at_risk', 'never']
            .includes(String(r.status).toLowerCase()) },
        { id: 'unrecovered', label: 'Under half recovered',
          blurb: 'Capital still sitting in somebody else\'s building.',
          keep: (r) => r.recovery_measured
            && r.recovery_pct !== null && r.recovery_pct < 0.5 },
        { id: 'unmeasured', label: 'Nothing to go on',
          blurb: 'No reagent sale is linked to these placements, so '
            + 'their recovery is unknown rather than zero. Link the '
            + 'reagents each model takes and the figures follow from '
            + 'sales already in the ledger.',
          keep: (r) => !r.recovery_measured },
      ],
      /* An unmeasured placement's recovery is not a low figure, it is
         an absent one. Shown marked rather than hidden: a blank where
         a percentage belongs invites somebody to assume it is zero,
         and zero is the one reading that sends them to a hospital to
         retrieve a working analyser. */
      mark: (row, col) => {
        if (row.recovery_measured) {
          if (col.key === 'net_recovered'
              && row.unattributed_reagent_sales > 0) {
            const n = row.unattributed_reagent_sales;
            return `Understated. ${n} reagent sale`
              + `${n === 1 ? '' : 's'} fit this machine and another at `
              + 'the same branch, so they could not be attributed to '
              + 'one and are excluded. Splitting them would be a guess.';
          }
          return null;
        }
        if (!['net_recovered', 'recovery_pct', 'payback_months']
            .includes(col.key)) {
          return null;
        }
        return 'Not measured. No reagent sale is linked to this '
          + 'placement, so its recovery is unknown rather than zero. '
          + 'Link the reagents this model takes to it.';
      },
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
      notes: ['unmeasured_note'],
    },
    service: {
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every machine with jobs against it.' },
        { id: 'unbilled', label: 'Work never billed',
          blurb: 'Hours or parts that went out and were never charged '
            + 'for.',
          keep: (r) => r.labour_hours_unbilled > 0
            || r.parts_uncharged > 0 },
        { id: 'repeat', label: 'Coming back',
          blurb: 'Repeat visits, which means the first one did not fix '
            + 'the fault.',
          keep: (r) => r.repeat_visits > 0 },
      ],
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
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every open tender and account.' },
        { id: 'risk', label: 'Cannot supply',
          blurb: 'Won or not, the stock will not be there in time. '
            + 'Lost for want of goods rather than for want of price.',
          keep: (r) => !!r.supply && String(r.supply).toLowerCase()
            .includes('cannot') },
        { id: 'likely', label: 'Better than even',
          blurb: 'Odds above half, by what has actually closed.',
          keep: (r) => r.odds > 0.5 },
      ],
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
        ['at_risk_supply_value', 'Cannot supply', 'money'],
      ],
      notes: ['caveat', 'reason'],
    },
    quality: {
      /* Where each kind of finding is put right. The engine's own
         dataset names, mapped once here rather than guessed at by a
         person reading a sentence about demand_history.csv. */
      /* The screen where this is fixed, which is not always the
         screen the figure is wrong on. A landed cost resting on an
         undivided charge is an inventory finding, but the missing
         weight is entered on Landing -- so the dataset alone sends
         people to the wrong place and they come back.

         By code first, then by dataset for everything with no
         special case. */
      fixAt: (r) => {
        const byCode = {
          cost_provisional: 'm/freight',
          placement_unmeasured: 'm/equipment',
        }[String(r.code || '')];
        if (byCode) return byCode;
        return {
          inventory: 'm/stock',
          demand: 'm/demand',
          receivables: 'm/money',
          commitments: 'm/cash',
          machines: 'm/equipment',
          service: 'm/service',
        }[String(r.dataset || '').toLowerCase()];
      },
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Every check that found something.' },
        { id: 'blocks', label: 'Makes a figure wrong',
          blurb: 'A number on another screen is wrong because of this. '
            + 'Fix these before acting on anything.',
          keep: (r) => String(r.severity).toLowerCase() === 'blocks' },
        { id: 'warns', label: 'Worth looking at',
          blurb: 'Not wrong, but not safe to lean on either.',
          keep: (r) => String(r.severity).toLowerCase() === 'warns' },
      ],
      cols: [
        ['severity', 'How bad', 'state'],
        ['dataset', 'Where', 'said'],
        ['title', 'What', 'text'],
        ['state', 'Who has it', 'said'],
        ['assigned_to_email', 'With', 'text'],
        ['effect', 'What it does to the numbers', 'text'],
        ['fix', 'How to fix it', 'text'],
        ['count', 'Rows', 'num'],
      ],
      tiles: [
        /* Needing action first, because it is the one anybody acts
           on. A list of twelve that includes four nobody has to do
           anything about reads as twelve jobs. */
        ['needing_action', 'Left to pick up', 'num'],
        ['blocks', 'Makes a figure wrong', 'num'],
        ['warns', 'Worth looking at', 'num'],
        ['assigned', 'Somebody is on', 'num'],
        ['accepted', 'Accepted as is', 'num'],
        ['total', 'In all', 'num'],
      ],
      /* A list of findings becomes a list of work here. Two verbs,
         because they say different things: somebody is on it, or
         the business has looked and this is how things are. */
      rowActions: [
        { label: (r) => (r.state === 'assigned' ? 'Hand back' : 'Take'),
          title: 'Who is on this',
          run: (r) => settleCheck(r, r.state === 'assigned'
            ? 'handback' : 'take') },
        { label: (r) => (r.state === 'accepted' ? 'Reopen' : 'Accept'),
          title: 'This is how things are',
          run: (r) => settleCheck(r, r.state === 'accepted'
            ? 'reopen' : 'accept') },
      ],
      /* An accepted finding is still listed -- hiding it is how it
         is forgotten -- but it is marked, so the eye skips it the
         way it should. */
      mark: (row, col) => {
        if (row.state === 'accepted' && col.key === 'title') {
          return `Accepted as how things are${
            row.disposition_note ? `: ${row.disposition_note}` : ''}. `
            + 'It comes back on its own if the check ever names a row '
            + 'this acceptance did not cover.';
        }
        return null;
      },
      notes: ['verdict'],
      /* A blocking fault means a number on another screen is wrong,
         not merely unwelcome, so it is marked the way an overdrawn
         customer is. */
      flag: (r) => (r.severity === 'blocks' ? 'flagged' : ''),
    },
    people: {
      views: [
        { id: 'all', label: 'Everything',
          blurb: 'Everyone on the payroll.' },
        { id: 'taxed', label: 'Paying tax',
          blurb: 'Above the threshold this year.',
          keep: (r) => r.tax_monthly > 0 },
        { id: 'untaxed', label: 'Below the threshold',
          blurb: 'Earning under the point where income tax starts.',
          keep: (r) => !(r.tax_monthly > 0) },
      ],
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

  /* Correcting a fact about an item. Quantities are not here: they
     come from the ledger, and a cell that typed over one would put a
     number on screen that no movement supports. */
  async function saveItem(row, field, value) {
    const sku = row.item_key || row.sku;
    if (!sku) return false;
    try {
      const res = await fetch(`/api/item/${encodeURIComponent(sku)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value }),
      });
      const out = await res.json();
      if (!res.ok || out.ok === false) {
        const why = (out.problems || ['That change was refused.'])[0];
        if (window.LANDED_PLATFORM) {
          const bar = $('m-wrap');
          if (bar) {
            const n = note(why, 'warn');
            bar.prepend(n);
            setTimeout(() => n.remove(), 6000);
          }
        }
        return false;
      }
      return true;
    } catch (e) {
      return false;
    }
  }

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

  /* Assign, hand back, accept or reopen one finding.
   *
   * Taking it is a click: it is yours, and nothing else needs
   * saying. Accepting asks why, because "we know" with no reason is
   * a finding somebody silences and nobody can audit afterwards --
   * and the reason is the only thing that tells the next person
   * whether the acceptance still holds.
   */
  async function settleCheck(row, what) {
    let body = { code: row.code };
    let url = '/api/check/assign';
    if (what === 'take') {
      const me = await whoAmI();
      if (!me) return;
      body.to = me;
    } else if (what === 'handback') {
      body.to = '';
    } else if (what === 'reopen') {
      url = '/api/check/reopen';
    } else {
      const why = window.prompt(
        'Why is this how things are? Whoever reads this next has '
        + 'only your answer to go on.');
      if (!why || !why.trim()) return;
      url = '/api/check/accept';
      body.why = why.trim();
    }
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const text = await res.text();
      const d = text ? JSON.parse(text) : {};
      if (!res.ok || d.ok === false) {
        window.alert(d.error || `The server answered ${res.status}.`);
        return;
      }
    } catch (e) {
      window.alert('The server did not answer. Nothing was changed.');
      return;
    }
    /* Redrawn from the server rather than patched here: accepting a
       finding changes three tiles and may leave it open anyway, if
       it names a row the acceptance did not cover. */
    const mod = (window.LANDED_MODULES || {}).modules
      ? (window.LANDED_MODULES.modules || [])
        .find((m) => m.id === 'quality')
      : null;
    if (mod) open(mod);
  }

  let me_ = null;

  async function whoAmI() {
    if (me_) return me_;
    try {
      const r = await fetch('/api/me');
      const d = await r.json();
      me_ = d.user_id || null;
    } catch (e) { me_ = null; }
    if (!me_) window.alert('Could not tell who you are signed in as.');
    return me_;
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
      head.append(el('th',
        NUMERIC.has(fmt) ? 'num' : (TIGHT.has(fmt) ? 'tight' : ''), label));
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
        const td = el('td',
          NUMERIC.has(fmt) ? 'num' : (TIGHT.has(fmt) ? 'tight' : ''),
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

  /* Remembered across modules, so picking a branch on one screen and
     moving to the next keeps it. Not stored between sessions: a scope
     somebody set last week and has forgotten is how a figure gets
     quoted out of context. */
  let branch = '';

  function scopeBar(mod, payload) {
    const bar = el('div', 'scope');

    if (!mod.can_narrow && !payload.can_narrow) {
      /* The module's own reason, from the registry. One sentence used
         to serve all six -- a statement, a bank balance and a
         transfer -- so on any given screen at least two thirds of it
         was about a different screen, and Checks explained itself in
         terms of bank balances. */
      bar.append(el('span', 'scope-note',
        `Whole business. ${mod.whole_business
          || 'This one cannot be shown for a single branch.'}`));
      return bar;
    }

    const list = (window.LANDED_MODULES || {}).branches || [];
    if (!list.length) return bar;

    bar.append(el('span', 'scope-k', 'Branch'));
    const sel = el('select', 'picker');
    sel.setAttribute('aria-label', 'Branch');
    const all = el('option', '', 'Every branch');
    all.value = '';
    sel.append(all);
    list.forEach((b) => {
      const o = el('option', '', `${b.code} — ${b.name}`);
      o.value = b.code;
      sel.append(o);
    });
    sel.value = payload.branch || '';
    sel.addEventListener('change', () => {
      branch = sel.value;
      open(mod);
    });
    bar.append(sel);

    bar.append(el('span', 'scope-note', payload.branch
      ? 'Every figure here was worked out from this branch\u2019s rows '
        + 'alone, not filtered from the group total.'
      : 'Every branch.'));
    return bar;
  }


  /* ------------------------------------------- how a screen is worked

     Fourteen screens of figures, and nothing saying where to start on
     any of them. Most software of this kind is bought by one person
     and used by another, and the second person is the one who opens a
     reorder list for the first time with no idea which end of it to
     work from.

     So each module states two things: the steps somebody works
     through, in order, and why the screen is worth the time at all.
     Both come from the registry, so they sit next to the module's
     dependencies and its engine function rather than in the browser
     where they would drift.

     Open the first time a person opens that module, shut after that,
     and the choice is remembered. Collapsed by default would mean
     nobody ever read it; open by default would push the figures below
     the fold for somebody who has read it forty times. */

  function howToWorkIt(mod) {
    const steps = mod.process || [];
    if (!steps.length && !mod.why) return null;

    const key = `landed.how.${mod.id}`;
    let seen = false;
    try { seen = localStorage.getItem(key) === 'seen'; } catch (e) { /**/ }

    const d = el('details', 'how');
    /* Open on a first visit, so somebody meeting a screen is told
       how it is worked -- but not on a phone, where the five steps
       and the paragraph under them fill the whole first screen and
       push the figures below the fold twice over. Measured at 430px:
       about a thousand pixels of explanation before a single row of
       stock, on each of fourteen modules.

       860px is the breakpoint the stylesheet already uses to stop
       laying this out beside the content. Somebody on a phone is
       nearly always checking a figure rather than learning the
       screen, and the summary line says plainly that the
       explanation is a tap away. */
    const narrow = (() => {
      try { return window.matchMedia('(max-width: 860px)').matches; }
      catch (e) { return false; }
    })();
    d.open = !seen && !narrow;
    const sum = el('summary', '', 'How this screen is worked');
    d.append(sum);

    d.addEventListener('toggle', () => {
      try {
        if (!d.open) localStorage.setItem(key, 'seen');
      } catch (e) { /* the disclosure still works without remembering */ }
    });

    const inner = el('div', 'how-body');
    if (steps.length) {
      /* Numbered because they are a sequence. The guidance against
         decorative numbering is about lists that are not ordered;
         this one is, and the order is the content. */
      const ol = el('ol', 'how-steps');
      steps.forEach((t) => ol.append(el('li', '', t)));
      inner.append(ol);
    }
    if (mod.why) {
      const why = el('div', 'how-why');
      why.append(el('h4', '', 'Why it matters'));
      why.append(el('p', '', mod.why));
      inner.append(why);
    }
    d.append(inner);
    return d;
  }

  async function open(mod, { view } = {}) {
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
    /* The router already put us here, so this only tidies a call
       that came from a button rather than the address. */
    const want = view ? `#m/${mod.id}?view=${view}` : `#m/${mod.id}`;
    if (location.hash !== want) history.replaceState(null, '', want);

    wrap.textContent = '';
    const sec = el('div', 'section');
    const head = el('div', 'section-head');
    head.append(el('h2', '', mod.name));
    head.append(el('p', '', `${mod.title}. ${mod.blurb}`));
    sec.append(head);
    /* After the head, not inside it: the head is a baseline-aligned
       flex row for the title and its sentence, and a block of steps
       dropped into it lands beside the title instead of under it. */
    const how = howToWorkIt(mod);
    if (how) sec.append(how);
    wrap.append(sec);

    const body = el('div');
    body.append(el('p', 'loading', 'Working it out…'));
    sec.append(body);

    let payload;
    try {
      const url = branch
        ? `/api/module/${mod.id}?branch=${encodeURIComponent(branch)}`
        : `/api/module/${mod.id}`;
      const res = await fetch(url);
      const text = await res.text();
      payload = text ? JSON.parse(text) : {};
      if (!res.ok) {
        body.textContent = '';
        /* A session that has run out is not a module that could not
           be read, and it must not read like one. The access token
           lasts an hour and is renewed from the refresh cookie before
           it expires, so a 401 here means that failed too -- the
           refresh token has gone, or somebody signed out in another
           tab.

           It used to show the server's own word, "sign in", as the
           body of "That module could not be read", which leaves
           somebody staring at a broken screen with two words on it
           and nowhere to go. The forms have said this properly for
           months; this path had not caught up. */
        if (res.status === 401) {
          const card = L.empty('Your session has run out',
            'Nothing is wrong with the figures \u2014 this screen just '
            + 'cannot ask for them any more. Sign in again and you '
            + 'will come straight back here.');
          const back = el('a', 'btn primary', 'Sign in again');
          /* Carrying where they were, so signing in returns them to
             this screen rather than to the front page. */
          back.href = `/sign-in?next=${encodeURIComponent(
            location.hash || `#m/${mod.id}`)}`;
          card.append(back);
          body.append(card);
          return;
        }
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
      body.append(scopeBar(mod, payload));
      body.append(waiting(mod, payload.missing));
      return;
    }
    const data = payload.data || {};

    /* The scope line goes in before anything that can return early.
       Placed after the generic table instead, it covered twelve
       modules and missed the statements -- which is the one that most
       needs to say "whole business", because a balance sheet for one
       branch is a thing somebody could believe in. A screen silent
       about its scope can be misread in either direction. */
    body.append(scopeBar(mod, payload));

    if (mod.id === 'books') { body.append(statements(data)); return; }

    const spec = SPEC[mod.id];
    if (!spec) {
      body.append(L.empty('Nothing to draw yet',
        'This module has figures but no table defined for them.'));
      return;
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

    /* Exceptions, before the table they are exceptions to.
     *
     * A module that finds something wrong has, until now, had two
     * ways to say so: a total that looks odd, and a sentence in a
     * note. Neither names the row. "PKR 560,000 reached no item" is a
     * figure that worries somebody without telling them which of six
     * charges on which of five shipments, or what to do about it --
     * and working that out meant knowing that a charge is divided by
     * a basis and that a basis of zero cannot be divided by.
     *
     * Declared like cols and tiles so any module can have one, and
     * the module's own server code decides what belongs in it. Rows
     * marked `expected` are shown quietly: a shipment still at sea
     * has no received lines yet, and flagging that as though somebody
     * forgot something trains people to ignore the flags. */
    (spec.panels || []).forEach((pan) => {
      const found = data[pan.key] || [];
      if (!found.length) return;
      const wrap = el('div', 'panel exceptions');
      const head = el('div', 'section-head');
      const live = found.filter((r) => !r.expected);
      head.append(el('h3', null, pan.title));
      head.append(el('span', 'asof', live.length
        ? `${live.length} to deal with`
        : 'nothing needing attention'));
      wrap.append(head);
      const t = el('table');
      const thead = el('thead');
      const hr = el('tr');
      pan.cols.forEach(([, label, fmt]) => {
        hr.append(el('th', fmt === 'money' || fmt === 'num' ? 'num' : '',
          label));
      });
      if (pan.act && window.LANDED_RECORDS) hr.append(el('th', 'act-cell'));
      thead.append(hr);
      t.append(thead);
      const tb = el('tbody');
      live.forEach((r) => {
        const tr = el('tr');
        pan.cols.forEach(([k, , fmt]) => {
          const v = r[k];
          tr.append(el('td', fmt === 'money' || fmt === 'num' ? 'num' : '',
            v === null || v === undefined || v === ''
              ? '\u2014' : (FMT[fmt] || FMT.text)(v)));
        });
        /* The fix, beside the thing that is wrong.
         *
         * The panel could explain what was missing and the person
         * then had to find the right screen, the right shipment and
         * the right line to put it in. The form opens from this row
         * with the record already named and locked, which is also
         * how a correction stops being filed against the wrong line.
         */
        if (pan.act && window.LANDED_RECORDS) {
          const cell = el('td', 'act-cell');
          const b = el('button', 'btn', pan.act.label);
          b.type = 'button';
          b.addEventListener('click', () => window.LANDED_RECORDS.show(
            r.act || pan.act.form,
            () => L.go(location.hash.slice(1)),
            pan.act.identify(r)));
          cell.append(b);
          tr.append(cell);
        }
        tb.append(tr);
      });
      t.append(tb);
      if (live.length) wrap.append(t);

      /* The expected ones in a line, not a row each.
       *
       * A shipment at sea with four charges on it produced four
       * identical rows saying nothing has been booked in against it,
       * and the one charge that had actually gone astray was fourth
       * in the list. Eight rows of the same sentence is how a person
       * learns to skip the whole panel.
       *
       * They are still counted and still totalled, because the money
       * is real and will land somewhere. They are just not eight
       * separate things to read. */
      const waiting = found.filter((r) => r.expected);
      if (waiting.length) {
        const ships = [...new Set(waiting.map((r) => r.shipment))];
        const total = waiting.reduce((n, r) => n + (r.billed || 0), 0);
        wrap.append(note(
          `${waiting.length} other charge${waiting.length === 1 ? '' : 's'}`
          + ` (${FMT.money(total)}) sit on ${ships.length} shipment`
          + `${ships.length === 1 ? '' : 's'} with nothing booked in `
          + `against ${ships.length === 1 ? 'it' : 'them'} yet: `
          + `${ships.slice(0, 4).join(', ')}`
          + `${ships.length > 4 ? ', and others' : ''}. That is the `
          + 'normal state of a shipment still on its way; the charges '
          + 'divide across the goods once they are received.', 'quiet'));
      }
      body.append(wrap);
    });

    const rows = data[spec.key || 'rows'] || [];
    if (!rows.length) {
      const panel = el('div', 'panel');
      panel.append(L.empty('Nothing here yet',
        'The module is reading your figures and finding none of this '
        + 'kind.'));
      body.append(panel);
    } else if (window.LANDED_GRID) {
      const cols = spec.cols.map(([k, label, f]) => (
        { key: k, label, fmt: f }));
      /* A module that names its default columns starts with those
         hidden-by-default; one that does not shows everything it
         declares, which is what every module but stock does. */
      const hide = spec.show
        ? cols.filter((c) => !spec.show.includes(c.key)).map((c) => c.key)
        : [];
      const stored = (() => {
        try {
          return JSON.parse(
            localStorage.getItem(`landed.grid.${mod.id}`));
        } catch (e) { return null; }
      })();
      if (!stored && hide.length) {
        try {
          localStorage.setItem(`landed.grid.${mod.id}`,
            JSON.stringify({ hidden: hide }));
        } catch (e) { /* the grid still works without remembering */ }
      }
      body.append(window.LANDED_GRID.render({
        key: mod.id,
        cols,
        rows,
        fmt: FMT,
        numeric: NUMERIC,
        views: spec.views || [],
        /* Opened at a particular view -- from the money band, or from
           a link somebody saved -- the grid starts there instead of
           drawing everything first and filtering a beat later. */
        view: view && (spec.views || []).some((v) => v.id === view)
          ? view : undefined,
        editable: data.editable || [],
        flag: spec.flag,
        mark: spec.mark,
        rowActions: spec.rowActions || [],
        onEdit: (row, field, value) => saveItem(row, field, value),
        /* Only where the row is a stock line, because the drawer shows
           a line's movements and offers the four operations that write
           to the ledger. A customer or a payroll row has neither. */
        onOpen: spec.opens
          ? (row) => L.openDetail(row, null)
          /* Or, where the module says so, the screen that fixes this
             row. A check reading "demand history for items that are
             not in the stock file" leaves somebody to work out which
             of nineteen screens that means, and a finding nobody can
             act on in one step is a finding they read once and never
             act on at all. */
          : (spec.fixAt ? (row) => {
            const to = spec.fixAt(row);
            if (to) location.hash = `#${to}`;
          } : null),
      }));
    } else {
      const panel = el('div', 'panel');
      panel.append(plainTable(spec, rows));
      body.append(panel);
    }

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

  /* --------------------------------------------------------- hover card

     One element, moved, rather than one inside each rail item. The
     rail clips its overflow -- that is what makes it collapse to a
     strip of icons -- so a tip nested inside an item is cut off at
     the edge. A single fixed-position card parented to the body
     escapes that, and there is only ever one visible.

     Shown on focus as well as hover: the rail is reachable by
     keyboard, and an explanation only a mouse can get at is not an
     explanation. */

  let tipEl = null;

  function tip(target, heading, body) {
    if (!tipEl) {
      tipEl = el('div', 'tip');
      tipEl.setAttribute('role', 'tooltip');
      document.body.append(tipEl);
    }
    tipEl.textContent = '';
    tipEl.append(el('b', '', heading));
    if (body) tipEl.append(el('span', '', body));
    const box = target.getBoundingClientRect();
    tipEl.style.top = `${Math.round(box.top)}px`;
    tipEl.style.left = `${Math.round(box.right + 10)}px`;
    tipEl.dataset.on = '1';
    /* Nudged up if it would run off the bottom. Measured after it is
       shown, because an element with display:none has no height. */
    const own = tipEl.getBoundingClientRect();
    if (own.bottom > window.innerHeight - 8) {
      tipEl.style.top =
        `${Math.round(window.innerHeight - own.height - 8)}px`;
    }
  }

  function untip() {
    if (tipEl) tipEl.dataset.on = '0';
  }

  function explains(node, heading, body) {
    node.addEventListener('mouseenter', () => tip(node, heading, body));
    node.addEventListener('focus', () => tip(node, heading, body));
    node.addEventListener('mouseleave', untip);
    node.addEventListener('blur', untip);
  }

  /* ----------------------------------------------------------- the rail */

  /* Stock keeps its own screen: the money band, the named views, the
     detail panel and the four operations. Replacing that with a plain
     table for the sake of symmetry would be a downgrade.
     
     It still has to carry what the module API knows and /api/state
     does not -- chiefly the surplus that cannot be moved, which is
     the difference between "no transfers today" and "the two limits
     cross and nothing can ever move". */
  /* Stock used to route to its own hand-built screen. It is the
     screen somebody spends the day in, which is exactly why it wants
     the grid -- twenty-eight columns to choose from, named views,
     resizing and editing -- rather than a fixed eight-column table.
     The hand-built screens are still there and still reachable:
     Today for the decisions and the money band, Import for a file. */
  const BESPOKE = {};

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
        a.append(L.icon(L.ICON[m.id] || L.ICON.items));
        a.append(el('span', 'rail-label', m.name));
        explains(a, m.title, m.state === 'locked'
          ? `${m.blurb} Not licensed for this business.`
          : m.blurb);
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
    explains, SPEC, FMT, plainTable, tile, note };
}());
