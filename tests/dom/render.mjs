import { JSDOM, VirtualConsole } from "jsdom";

const BASE = process.env.BASE || "http://127.0.0.1:8099";
const errors = [];
const POSTS = [];
const vc = new VirtualConsole();
vc.on("jsdomError", e => errors.push("jsdomError: " + (e.stack || e.message)));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const html = await (await fetch(BASE + "/")).text();

// The page's boot script runs during parsing, so the shims have to be in
// place before the parser reaches it.
function shim(window){
  window.__fetchLog = [];
  window.fetch = async (input, init) => {
    const url = typeof input === "string"
      ? (input.startsWith("http") ? input : BASE + input)
      : input.url;
    // jsdom's FormData is not Node's. Node's fetch serialises its string
    // fields by luck but turns file entries into garbage, so an upload test
    // silently falls back to sample data and passes without uploading.
    // Rebuild it entry by entry.
    if (init?.body && typeof init.body.entries === "function"){
      const fd = new FormData();
      POSTS.push([...init.body.entries()]
        .map(([k, v]) => typeof v === "string" ? k : `${k}=<file>`).join(" "));
      for (const [k, v] of init.body.entries()){
        if (v && typeof v.arrayBuffer === "function"){
          const buf = await v.arrayBuffer();
          fd.append(k, new Blob([buf], {type: v.type || "text/csv"}),
                    v.name || "upload.csv");
        } else {
          fd.append(k, v);
        }
      }
      init = {...init, body: fd};
    }
    let res, body;
    try {
      res = await fetch(url, init);
      body = await res.arrayBuffer();
      window.__fetchLog.push(`${res.status} ${url.replace(BASE, "")}`);
    } catch (err) {
      window.__fetchLog.push(`THREW ${url.replace(BASE, "")} ${err.message}`);
      throw err;
    }
    return {
      ok: res.ok, status: res.status,
      json: async () => JSON.parse(Buffer.from(body).toString("utf8")),
      text: async () => Buffer.from(body).toString("utf8"),
    };
  };
  // Do NOT stub Blob or FormData: jsdom provides both, and replacing Blob
  // turns every uploaded file into "[object Object]" on the wire, so the
  // server silently falls back to sample data and upload tests pass
  // without having uploaded anything.
  window.scrollTo = () => {};
  // jsdom implements no layout, so it has no scrollIntoView. The page is
  // right to call it; stub it rather than guarding the app for a test.
  window.Element.prototype.scrollIntoView = () => {};
  if (!window.matchMedia) window.matchMedia =
    q => ({matches:false, addEventListener(){}, media:q});
}

const dom = new JSDOM(html, {
  url: BASE + "/",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse: shim,
});
const { window } = dom;

const wait = ms => new Promise(r => setTimeout(r, ms));
const $ = id => window.document.getElementById(id);
const text = id => ($(id)?.textContent || "").replace(/\s+/g, " ").trim();

/* Boot is three fetches and an analyse, and how long that takes varies with
   the machine. Waiting a fixed 2.5s passed most runs and failed some, which
   is the worst of both: a flake that looks like a bug. Wait for the state
   the assertions need instead. */
async function waitFor(what, ready, ms = 20000){
  const t0 = Date.now();
  while (Date.now() - t0 < ms){
    try { if (ready()) return true; } catch { /* still painting */ }
    await wait(100);
  }
  throw new Error(`timed out after ${ms}ms waiting for ${what}`);
}

try {
  await waitFor("the first analyse to paint", () =>
    $("vitals")?.children.length >= 6
    && /PKR/.test(($("headline")?.textContent || ""))
    && window.document.querySelectorAll(".navitem").length >= 10);
} catch (e) {
  console.log("BOOT DIAGNOSTIC");
  console.log("  vitals tiles :", $("vitals")?.children.length);
  console.log("  headline     :", ($("headline")?.textContent || "").slice(0, 120));
  console.log("  nav items    :", window.document.querySelectorAll(".navitem").length);
  console.log("  flash        :", ($("flash")?.textContent || "").trim().slice(0, 160));
  console.log("  run button   :", ($("run")?.textContent || "").trim());
  console.log("  errors       :", errors.length ? errors.join(" | ").slice(0, 400) : "none");
  console.log("  fetches seen :", (window.__fetchLog || []).join(", "));
  throw e;
}

const checks = [];
const ok = (name, cond, detail = "") => checks.push({name, pass: !!cond, detail});

// paintRail rebuilds the rail on every paint, so a nav button must be
// re-queried at the moment it is clicked rather than held in a variable.
// Waits for the view to actually appear instead of guessing a delay.
/* A scope change is a question to the engine, not a filter on what we hold,
   so it costs a round trip. Waiting a fixed 250ms for it left later sections
   asserting against data that was still scoped to the previous branch. */
async function setScope(id, value){
  const el = $(id);
  el.value = value;
  el.dispatchEvent(new window.Event("change", {bubbles:true}));
  const key = id === "f_entity" ? "entity" : "location";
  await waitFor(`scope ${key}=${value}`, () => {
    const inUrl = new URLSearchParams(window.location.search).get(key) || "all";
    const running = ($("run")?.textContent || "").includes("Running");
    return inUrl === value && !running;
  }, 15000);
  await wait(250);
}

async function goTo(view, ms = 300){
  for (let tries = 0; tries < 20; tries++){
    const btn = window.document.querySelector(`.navitem[data-go="${view}"]`);
    if (btn){
      btn.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
      break;
    }
    await wait(100);
  }
  for (let tries = 0; tries < 20; tries++){
    const sec = $("v-" + view);
    if (sec && !sec.hidden) break;
    await wait(100);
  }
  await wait(ms);
  return $("v-" + view);
}

// --- boot
ok("rail rendered", $("rail").children.length >= 4, `${$("rail").children.length} groups`);
ok("nav has no competitor labels", !$("rail").innerHTML.match(/Cin7|Xero|Employment Hero/));
ok("as-of prefilled", $("asof").value === "2026-11-20", $("asof").value);
ok("headline replaced", !text("headline").startsWith("Press Analyse"), text("headline").slice(0,90));
ok("headline has figures", /PKR/.test(text("headline")));
ok("vitals rendered", $("vitals").children.length >= 6, `${$("vitals").children.length} tiles`);
ok("top actions rendered", $("acts-top").querySelectorAll(".act").length >= 3,
   `${$("acts-top").querySelectorAll(".act").length} cards`);
ok("ageing bars rendered", $("aging").querySelectorAll(".barrow").length >= 4);
ok("state bars rendered", $("statebars").querySelectorAll(".barrow").length >= 3);
ok("branches table rendered", $("t-locations").querySelectorAll("tbody tr").length === 3,
   `${$("t-locations").querySelectorAll("tbody tr").length} rows`);

// --- navigate every built view
const views = ["actions","stock","transfers","receivables","service","machines",
  "purchasing","cash","forecast","pack","sanity","history","audit"];
for (const v of views){
  ok(`nav button exists: ${v}`,
     !!window.document.querySelector(`.navitem[data-go="${v}"]`));
  const sec = await goTo(v, v === "history" || v === "audit" ? 700 : 180);
  ok(`view visible: ${v}`, sec && !sec.hidden);
  ok(`url carries view: ${v}`, window.location.search.includes("view=" + v),
     window.location.search);
}

// --- stock view detail
await goTo("stock", 250);
const rowCount = () => $("t-stock").querySelectorAll("tbody tr").length;
ok("stock rows rendered", rowCount() === 36, `${rowCount()} rows`);
ok("stock has data-labels", !!$("t-stock").querySelector("td[data-label]"));
ok("stock lead cell marked", !!$("t-stock").querySelector("td.lead"));
ok("stock pills rendered", $("stock-pills").querySelectorAll(".pill").length >= 5);

// filter by a status pill
const critPill = [...$("stock-pills").querySelectorAll(".pill")]
  .find(p => p.dataset.val === "CRITICAL");
ok("critical pill exists", !!critPill);
critPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("pill filtered the table", rowCount() === 3, `${rowCount()} rows after CRITICAL`);
ok("pill wrote to the url", window.location.search.includes("status=CRITICAL"));
ok("chip appeared", $("stock-chips").querySelectorAll(".chip").length === 1);

// remove via chip
$("stock-chips").querySelector(".chip button")
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("chip removal restored rows", rowCount() === 36, `${rowCount()} rows`);
ok("url cleaned up", !window.location.search.includes("status="), window.location.search);

// sorting
// renderTable replaces innerHTML, so the header button must be re-queried
// after every render rather than held in a variable.
const clickSort = async () => {
  const b = $("t-stock").querySelector('th button[data-sort="on_hand"]');
  ok("sortable header present", !!b);
  b.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(220);
};
await clickSort();
ok("sort wrote to url", window.location.search.includes("sort=on_hand"));
const firstDesc = $("t-stock").querySelector("tbody tr td.lead")?.textContent.trim();
await clickSort();
ok("second click flips direction", window.location.search.includes("dir=asc"));
const firstAsc = $("t-stock").querySelector("tbody tr td.lead")?.textContent.trim();
ok("sort order actually changed", firstDesc !== firstAsc, `${firstDesc} -> ${firstAsc}`);

// The Display menu now holds density, grouping, sort and page size.
const openDisplay = async () => {
  const b = window.document.querySelector('[data-menu="disp-stock"]');
  b.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(140);
};
const displayBtn = window.document.querySelector('[data-menu="disp-stock"]');
ok("one Display control replaces the separate menus", !!displayBtn);
ok("no separate density control remains",
   !window.document.querySelector("[data-density-set]"));
await openDisplay();
ok("display sheet opened", $("disp-stock") && !$("disp-stock").hidden);
for (const section of ["Density","Group by","Sort by","Columns","Rows per page"]){
  ok(`display has a ${section} section`, $("disp-stock").textContent.includes(section));
}

$("disp-stock").querySelector('[data-set="density"][data-val="compact"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(200);
ok("density applied to body", window.document.body.dataset.density === "compact");
ok("density in url", window.location.search.includes("density=compact"));

// columns
await openDisplay();
const headCount = () => $("t-stock").querySelectorAll("thead th").length;
const before = headCount();
$("disp-stock").querySelector('[data-col="abc"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(200);
ok("hiding a column removed it", headCount() === before - 1, `${before} -> ${headCount()}`);
ok("hidden column in url", window.location.search.includes("hide=abc"));

// sort affordance is visible before you click
ok("every sortable column advertises itself",
   $("t-stock").querySelectorAll("th .ar.idle, th .ar.on").length >= 5,
   `${$("t-stock").querySelectorAll("th .ar").length} markers`);
ok("column resize handles present",
   $("t-stock").querySelectorAll("th [data-grip]").length >= 5);

// grouping
await openDisplay();
$("disp-stock").querySelector('[data-set="group"][data-val="status"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(250);
const groupRows = () => $("t-stock").querySelectorAll("tr.grouprow").length;
ok("grouping produced group headers", groupRows() >= 3, `${groupRows()} groups`);
ok("group headers carry a count",
   $("t-stock").querySelector("tr.grouprow .gcount") !== null);
ok("group headers carry a money aggregate",
   ($("t-stock").querySelector("tr.grouprow .gtotal")?.textContent || "").includes("PKR"));
ok("grouping in url", window.location.search.includes("group=status"));

const bodyBefore = $("t-stock").querySelectorAll("tbody tr").length;
$("t-stock").querySelector("tr.grouprow")
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("a group collapses",
   $("t-stock").querySelectorAll("tbody tr").length < bodyBefore,
   `${bodyBefore} -> ${$("t-stock").querySelectorAll("tbody tr").length}`);
$("t-stock").querySelector("tr.grouprow")
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);

// paging: turn grouping off, set a small page
await openDisplay();
$("disp-stock").querySelector('[data-set="group"][data-val=""]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("grouping removed", groupRows() === 0);
await openDisplay();
$("disp-stock").querySelector('[data-set="size"][data-val="25"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(250);
ok("page size limits the rows", $("t-stock").querySelectorAll("tbody tr").length === 25,
   `${$("t-stock").querySelectorAll("tbody tr").length} rows`);
ok("a pager appeared", $("stock-foot").textContent.includes("Showing"),
   $("stock-foot").textContent.slice(0, 60));
const nextBtn = $("stock-foot").querySelector('[data-page="next"]');
ok("next page button exists", !!nextBtn);
nextBtn.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(250);
ok("paging moved", window.location.search.includes("page=1"));
ok("second page has the remainder",
   $("t-stock").querySelectorAll("tbody tr").length === 11,
   `${$("t-stock").querySelectorAll("tbody tr").length} rows`);

// a filter change must send you back to page one
// Pills are re-rendered on every paint, so each click must re-query.
const clickPill = async (key, val) => {
  const b = [...$("stock-pills").querySelectorAll(".pill")]
    .find(p => p.dataset.pill === key && p.dataset.val === val);
  ok(`pill present: ${key}=${val}`, !!b);
  if (b){ b.dispatchEvent(new window.MouseEvent("click", {bubbles:true})); await wait(250); }
};
await clickPill("status", "CRITICAL");
ok("filtering resets the page", !window.location.search.includes("page=1"),
   window.location.search);
await clickPill("status", "CRITICAL");
ok("the filter came back off", !window.location.search.includes("status="),
   window.location.search);

// selection and the action bar
const pick = $("t-stock").querySelector("[data-pick]");
ok("rows are selectable", !!pick);
pick.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("selection bar appeared", $("stock-foot").querySelector(".selbar") !== null);
ok("selection bar shows a count",
   ($("stock-foot").querySelector(".selbar b")?.textContent || "") === "1");
ok("selection reaches the status bar", $("statusbar").textContent.includes("Selected"));
const pickAll = $("pickall");
ok("select-all exists", !!pickAll);
pickAll.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(250);
ok("select-all selected everything",
   ($("stock-foot").querySelector(".selbar b")?.textContent || "") === "36",
   $("stock-foot").querySelector(".selbar b")?.textContent);
$("stock-foot").querySelector('[data-sel="clear"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("selection cleared", $("stock-foot").querySelector(".selbar") === null);

// the row detail panel
const leadCell = $("t-stock").querySelector("tbody tr td.lead");
ok("a lead cell exists to click", !!leadCell);
leadCell.closest("tr").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(250);
ok("detail panel opened", window.document.body.classList.contains("detail-open"));
ok("detail panel has a title", text("detail-title").length > 2, text("detail-title"));
ok("detail panel lists fields",
   $("detail-body").querySelectorAll("dt").length >= 8,
   `${$("detail-body").querySelectorAll("dt").length} fields`);
ok("detail panel shows the reason prose",
   $("detail-body").querySelector(".why") !== null);
ok("detail row is in the url", window.location.search.includes("row="));
$("detail-close").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(220);
ok("detail panel closed", !window.document.body.classList.contains("detail-open"));

// the filter command input
const qbox = $("q");
ok("filter command input exists", !!qbox);
qbox.dispatchEvent(new window.FocusEvent("focusin", {bubbles:true}));
await wait(200);
ok("focusing offers suggestions", !$("sugg").hidden,
   `${$("sugg").querySelectorAll(".sopt").length} suggestions`);
ok("suggestions span several filter categories",
   new Set([...$("sugg").querySelectorAll(".sopt")]
     .map(b => b.textContent.split(":")[0].trim())).size >= 2);
const stateSugg = [...$("sugg").querySelectorAll(".sopt")]
  .find(b => b.textContent.includes("Runs out"));
ok("a state suggestion is offered", !!stateSugg);
if (stateSugg){
  stateSugg.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(250);
  ok("applying a suggestion filters the table",
     $("t-stock").querySelectorAll("tbody tr").length === 3,
     `${$("t-stock").querySelectorAll("tbody tr").length} rows`);
  ok("the filter badge counts what is on",
     ($("viewbar").querySelector(".fcount")?.textContent || "0") !== "0",
     $("viewbar").querySelector(".fcount")?.textContent);
  const chipX = $("stock-chips").querySelector(".chip button");
  ok("a chip appeared for the applied suggestion", !!chipX,
     `url=${window.location.search} chips=${$("stock-chips").innerHTML.slice(0,120)}`);
  if (chipX){
    chipX.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
    await wait(220);
    // Page size is still 25 from the paging test, so a restored full set
    // shows a full page rather than all 36 rows.
    ok("removing the chip cleared the filter",
       !window.location.search.includes("status="), window.location.search);
    ok("removing the chip restored the rows",
       $("t-stock").querySelectorAll("tbody tr").length === 25,
       `${$("t-stock").querySelectorAll("tbody tr").length} rows`);
  }
}

// "/" focuses the filter from anywhere
window.document.body.focus();
const slash = new window.KeyboardEvent("keydown", {key:"/", bubbles:true});
window.document.dispatchEvent(slash);
await wait(150);
ok("slash focuses the filter", window.document.activeElement?.id === "q",
   window.document.activeElement?.id);
window.document.dispatchEvent(new window.KeyboardEvent("keydown",
  {key:"Escape", bubbles:true}));
await wait(150);

// the status bar
ok("status bar reports rows", $("statusbar").textContent.includes("Rows"));
ok("status bar reports the as-of date", $("statusbar").textContent.includes("As of"));
ok("status bar reports the data source",
   /Sample data|Your files/.test($("statusbar").textContent));
ok("status bar reports the store mode",
   /Scratch database|Saved to disk/.test($("statusbar").textContent));
ok("status bar reports data sanity",
   /data faults|data notes|No data faults/.test($("statusbar").textContent),
   $("statusbar").textContent.slice(0, 120));

// scope select
ok("branch select exists", !!$("f_location"));
await setScope("f_location", "LHR");
ok("scope filter applied", rowCount() === 12, `${rowCount()} rows for LHR`);

// Pill counts must narrow with the scope, or they contradict the table.
window.history.pushState({}, "", "?view=stock");
window.dispatchEvent(new window.Event("popstate"));
await wait(250);
const totalFromPills = () =>
  [...$("stock-pills").querySelectorAll('.pill[data-pill="status"] .c')]
    .reduce((a, el) => a + Number(el.textContent), 0);
const allPillTotal = totalFromPills();
ok("pills account for every row before scoping", allPillTotal === 36, String(allPillTotal));
await setScope("f_location", "LHR");
ok("pill counts narrow with the branch filter", totalFromPills() === 12,
   `${totalFromPills()} after scoping to LHR`);
ok("the table agrees with the pills",
   $("t-stock").querySelectorAll("tbody tr").length === 12);
// And the figures above the table narrow too, which is the whole point of
// scoping on the server: a tile that still read the group was the bug.
ok("the branch's stock value is below the group's",
   $("v-stock").textContent.includes("of 12") ||
   $("stock-count").textContent.includes("12"),
   $("stock-count")?.textContent);
await setScope("f_location", "all");

// --- transfers
await goTo("transfers", 250);
ok("transfers rows rendered", $("t-tx").querySelectorAll("tbody tr").length >= 8,
   `${$("t-tx").querySelectorAll("tbody tr").length} transfers`);
ok("transfers readout has money", /PKR/.test(text("tx-readout")), text("tx-readout").slice(0,80));

// --- receivables
await goTo("receivables", 250);
ok("receivables customers rendered", $("t-ar").querySelectorAll("tbody tr").length >= 6,
   `${$("t-ar").querySelectorAll("tbody tr").length} customers`);
ok("ar vitals rendered", $("ar-vitals").children.length >= 6);
ok("dso shows a dash without revenue", text("ar-vitals").includes("enter monthly revenue"),
   "DSO tile note");

// --- machines. The branch filter is deliberately global, so clear it first;
// it stays visible as a chip rather than filtering a view silently.
ok("scope filter is visible as a chip", $("mx-chips") !== null);
window.history.pushState({}, "", "?view=machines");
window.dispatchEvent(new window.Event("popstate"));
await wait(300);
await goTo("machines", 250);
ok("machines rendered", $("t-mx").querySelectorAll("tbody tr").length === 8,
   `${$("t-mx").querySelectorAll("tbody tr").length} placements`);
ok("ramping status is shown as too-early", $("t-mx").innerHTML.includes("Too early to judge"));

// --- data pack
await goTo("pack", 400);
// Nothing staged yet, so the slots are one line rather than ten empty
// cards listing what you have not done.
ok("the empty state is one line, not ten empty slots",
   $("slots").querySelectorAll(".slot").length === 0
   && /covers 10 sheets/.test(text("slots")),
   text("slots").slice(0, 90));
ok("a column spec for every dataset",
   $("specs").querySelectorAll("table.cols").length === 10);
ok("upload disabled with nothing staged", $("upload").disabled);
// An inline display beats [hidden], which is how two dead buttons stayed on
// screen after being told to hide. Computed style, not the attribute.
ok("the staged actions are not shown at all with nothing staged",
   window.getComputedStyle($("stagedactions")).display === "none",
   window.getComputedStyle($("stagedactions")).display);
// Ten cards each saying nothing is wrong is the dump this screen had.
ok("a clean load is one line, not one card per sheet",
   $("quality").querySelectorAll(".note").length === 0
   && /Currently loaded/.test(text("quality")),
   text("quality").slice(0, 90));
ok("pack download links present", !!window.document.querySelector('a[href="/api/pack.zip"]'));

// --- unbuilt module
const pendBtn = window.document.querySelector(".navitem.off[data-pending]");
ok("unbuilt module listed", !!pendBtn);
pendBtn.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(150);
ok("pending view shown", !$("v-pending").hidden);
ok("pending explains why", text("pend-why").length > 20, text("pend-why").slice(0,70));

// --- history / audit content
await goTo("history", 900);
ok("runs table populated", $("t-runs").querySelectorAll("tbody tr").length >= 1,
   `${$("t-runs").querySelectorAll("tbody tr").length} runs`);
ok("snapshots table populated", $("t-snaps").querySelectorAll("tbody tr").length >= 5,
   `${$("t-snaps").querySelectorAll("tbody tr").length} snapshots`);

await goTo("audit", 900);
ok("audit stats rendered", $("audit-stats").children.length >= 6);
ok("audit log populated", $("t-audit").querySelectorAll("tbody tr").length >= 3);

// --- data sanity
await goTo("sanity", 300);
ok("sanity view visible", !$("v-sanity").hidden);
ok("sanity vitals rendered", $("sn-vitals").children.length === 4);
ok("clean sample pack reports nothing wrong",
   text("sn-readout").includes("contradicts anything else"),
   text("sn-readout").slice(0, 80));
ok("sanity explains that silence is not correctness",
   $("sn-list").textContent.includes("not the same as being right"));
ok("parser rejections section rendered", $("sn-rejected").textContent.length > 20);

// --- the three new modules. These need an opening balance, so set it in
// settings and re-run, which also exercises the settings round trip.
window.history.pushState({}, "", "?view=purchasing");
window.dispatchEvent(new window.Event("popstate"));
await wait(200);
$("t_bal").value = "18000000";
$("run").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await waitFor("the re-run to finish", () =>
  !($("run")?.textContent || "").includes("Running"));
await wait(300);

// The headline is written by renderDashboard, so it has to be read on the
// dashboard rather than from whichever view happens to be open.
await goTo("dashboard", 300);
ok("headline becomes the fundable question",
   /can be placed this week/.test(text("headline")), text("headline").slice(0,80));
ok("dashboard surfaces the unfunded order book",
   $("acts-top").textContent.includes("cannot be funded"));

await goTo("purchasing", 300);

// purchasing
ok("purchasing rows rendered", $("t-po").querySelectorAll("tbody tr").length >= 10,
   `${$("t-po").querySelectorAll("tbody tr").length} lines`);
ok("purchasing vitals rendered", $("po-vitals").children.length === 6);
ok("purchasing readout has money", /PKR/.test(text("po-readout")));
ok("purchasing states its heuristic",
   text("po-caveat").includes("rather than a mathematical optimum"));
ok("transfer-instead decisions shown", $("t-po").innerHTML.includes("Transfer instead"));
ok("unfundable decisions shown", $("t-po").innerHTML.includes("Cannot fund"));
ok("clinical items are marked", $("t-po").innerHTML.includes("clinical"));

const poRows = () => $("t-po").querySelectorAll("tbody tr").length;
const poAll = poRows();
const unfundPill = [...$("po-pills").querySelectorAll(".pill")]
  .find(p => p.dataset.val === "UNFUNDED");
ok("unfunded pill exists", !!unfundPill);
if (unfundPill){
  unfundPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(220);
  ok("verdict pill filters purchasing", poRows() < poAll, `${poAll} -> ${poRows()}`);
  ok("verdict filter in url", window.location.search.includes("verdict=UNFUNDED"));
  unfundPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(200);
}

// cash. It opens as a chart, so the table has to be asked for.
await goTo("cash", 300);
ok("cash opens as a chart without being asked",
   $("v-cash").querySelector(".altview svg .line") !== null);
ok("opening as a chart leaves the url clean",
   !window.location.search.includes("vtype="), window.location.search);
$("viewbar").querySelector('[data-set="vtype"][data-val="table"]')
  ?.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(350);
ok("thirteen cash weeks rendered", $("t-cash").querySelectorAll("tbody tr").length === 13,
   `${$("t-cash").querySelectorAll("tbody tr").length} weeks`);
ok("cash vitals rendered", $("cash-vitals").children.length === 8);
ok("cash readout mentions the floor", /floor/.test(text("cash-readout")),
   text("cash-readout").slice(0,90));
ok("weekly explanations rendered", $("cash-lines").children.length >= 5,
   `${$("cash-lines").children.length} weeks explained`);
ok("cash states its assumptions",
   text("cash-caveat").includes("New purchase orders are not included"));
ok("balance sparkbars drawn", $("cash-foot").querySelectorAll("span[title]").length === 13);

// forecast
await goTo("forecast", 300);
ok("forecast rows rendered", $("t-fc").querySelectorAll("tbody tr").length === 36,
   `${$("t-fc").querySelectorAll("tbody tr").length} forecasts`);
ok("forecast vitals rendered", $("fc-vitals").children.length === 6);
ok("sparklines drawn", $("t-fc").querySelectorAll("svg polyline").length > 10,
   `${$("t-fc").querySelectorAll("svg polyline").length} sparklines`);
ok("confidence band drawn", $("t-fc").querySelectorAll("svg polygon").length > 10);
ok("seasonality reported as not fitted on 12 months",
   $("fc-vitals").textContent.includes("Seasonality fitted"));
ok("unmeasured accuracy is shown as unmeasured",
   $("t-fc").innerHTML.includes("unmeasured"));
const fcAll = $("t-fc").querySelectorAll("tbody tr").length;
const sbaPill = [...$("fc-pills").querySelectorAll(".pill")]
  .find(p => p.dataset.val === "sba");
ok("method pill exists", !!sbaPill);
if (sbaPill){
  sbaPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(220);
  ok("method pill filters forecasts",
     $("t-fc").querySelectorAll("tbody tr").length < fcAll);
  sbaPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(200);
}

// no module is still listed as unbuilt except payables
const stillSoon = [...window.document.querySelectorAll(".navitem.off")]
  .map(b => b.dataset.pending);
// Payables and Users are both deliberately unbuilt, and both say why.
ok("exactly two modules remain unbuilt",
   stillSoon.length === 2 && stillSoon.includes("payables") && stillSoon.includes("users"),
   stillSoon.join(","));

// --- data sanity against deliberately broken data, through the UI.
// A clean pack proves the panel renders; it does not prove the checks fire.
const invText = await (await fetch(`${BASE}/api/sample/inventory.csv`)).text();
const invLines = invText.split("\n");
const broken = invLines.map((line, i) => {
  if (i === 0 || !line.trim()) return line;
  const f = line.split(",");
  // One branch exporting in thousands: the commonest real corruption.
  if (f[1] === "LHR") f[7] = "5.00";
  return f.join(",");
}).join("\n");

await goTo("pack", 300);
const demText = await (await fetch(`${BASE}/api/sample/demand_history.csv`)).text();
await window.stageFiles([
  new window.File([broken], "stock.csv", {type:"text/csv"}),
  new window.File([demText], "demand.csv", {type:"text/csv"}),
]);
await wait(300);
ok("two files staged for the broken-data run",
   $("slots").querySelectorAll(".slot.has").length === 2,
   `${$("slots").querySelectorAll(".slot.has").length} staged; flash=${
     ($("flash").textContent||"").trim().slice(0,70)}`);
ok("upload button enabled for the broken run", !$("upload").disabled);
$("upload").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(60);
const spun = ($("upload").textContent || "").includes("Running");
await wait(3400);
ok("the upload actually started", spun,
   `button said "${($("upload").textContent||"").trim()}" just after the click`);
ok("the broken upload was accepted",
   $("statusbar").textContent.includes("Your files"),
   `flash=${($("flash").textContent||"").trim().slice(0,110)}`);

await goTo("sanity", 350);
ok("a scale fault is reported", $("sn-list").textContent.includes("factor of a hundred"),
   $("sn-list").textContent.slice(0, 90));
ok("the fault says what it does to the numbers",
   $("sn-list").textContent.includes("exporting in thousands"));
ok("the fault names the affected rows",
   $("sn-list").querySelector(".find .rows") !== null);
ok("blocking faults are counted", Number(text("sn-vitals").match(/\d+/)?.[0]) >= 1,
   text("sn-vitals").slice(0, 60));
ok("the status bar flags the fault",
   $("statusbar").textContent.includes("affect the figures"),
   $("statusbar").textContent.slice(0, 110));

await goTo("dashboard", 350);
ok("the dashboard pins the data fault above everything else",
   $("acts-top").querySelector(".act h3")?.textContent.includes("data fault"),
   $("acts-top").querySelector(".act h3")?.textContent);

const sevPill = [...$("sn-pills").querySelectorAll(".pill")];
await goTo("sanity", 300);
const blocksPill = [...$("sn-pills").querySelectorAll(".pill")]
  .find(b => b.dataset.val === "blocks");
ok("severity pills rendered", !!blocksPill);
if (blocksPill){
  const allFinds = $("sn-list").querySelectorAll(".find").length;
  blocksPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(250);
  ok("severity pill filters the findings",
     $("sn-list").querySelectorAll(".find").length <= allFinds);
}

// ============================================================
// The three-tier bar, app-level scope, Settings and view types
//
// Earlier sections leave filters in the URL, and the URL is the state,
// so this block starts from a clean one rather than inheriting them.
// ============================================================

// The broken-data run above uploaded inventory and demand only, which
// supersedes those two domains and leaves the rest of the pack behind. The
// whole pack goes up here so the screens below have a complete dataset --
// and so the six-file upload is itself exercised once.
await goTo("pack", 300);
const PACK = [
  ["inventory.csv",      "inventory"],
  ["demand_history.csv", "demand"],
  ["receivables.csv",    "receivables"],
  ["service_jobs.csv",   "service"],
  ["machines.csv",       "machines"],
  ["commitments.csv",    "commitments"],
  ["trial_balance.csv",  "trial_balance"],
  ["employees.csv",      "employees"],
  ["opportunities.csv",  "opportunities"],
  ["marketing_spend.csv","marketing_spend"],
];
const packFiles = [];
for (const [file] of PACK){
  const t = await (await fetch(`${BASE}/api/sample/${file}`)).text();
  packFiles.push(new window.File([t], file, {type:"text/csv"}));
}
await window.stageFiles(packFiles);
await wait(400);
ok("the whole pack stages in one go",
   $("slots").querySelectorAll(".slot.has").length === 10,
   `${$("slots").querySelectorAll(".slot.has").length} staged`);
$("upload").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(5000);
ok("the whole pack was accepted",
   !($("flash").textContent || "").includes("Not accepted"),
   ($("flash").textContent || "").trim().slice(0, 90));

window.history.pushState({}, "", "/");
window.dispatchEvent(new window.Event("popstate"));
await wait(600);
ok("every domain is back after the full upload",
   $("vitals").children.length >= 6, `${$("vitals").children.length} tiles`);

// ==============================================================
// Finance, payroll and pipeline
// ==============================================================

{
  await goTo("pnl", 400);
  ok("the profit and loss reads as a sentence",
     /revenue/.test(text("pnl-readout")) && /PKR/.test(text("pnl-readout")),
     text("pnl-readout").slice(0, 80));
  ok("the statement has its subtotals",
     /Gross profit/.test(text("pnl-statement"))
     && /Operating profit/.test(text("pnl-statement"))
     && /Profit after tax/.test(text("pnl-statement")));
  ok("every account is listed under it",
     $("t-pnl").querySelectorAll("tbody tr").length >= 5,
     `${$("t-pnl").querySelectorAll("tbody tr").length} accounts`);

  await goTo("balance", 400);
  ok("the balance sheet has all three sides",
     /Assets/.test(text("bs-statement")) && /Liabilities/.test(text("bs-statement"))
     && /Equity/.test(text("bs-statement")));
  ok("it balances, so it says nothing is wrong",
     ($("bs-note").textContent || "").trim() === "",
     text("bs-note").slice(0, 60));
  ok("the ratios are shown", /Current ratio/.test(text("bs-vitals"))
     && /Gearing/.test(text("bs-vitals")));

  await goTo("cashflow", 400);
  ok("the cash flow has its three sections",
     /Cash from operations/.test(text("cf-statement"))
     && /Cash from investing/.test(text("cf-statement"))
     && /Cash from financing/.test(text("cf-statement")));
  ok("it starts at profit and adds back what never moved cash",
     /Profit after tax/.test(text("cf-statement"))
     && /Depreciation/.test(text("cf-statement")));
  ok("working capital movements are named, not netted",
     /Receivables up/.test(text("cf-statement"))
     && /Stock up/.test(text("cf-statement")));
  ok("the checks are on the screen, not in a footnote",
     $("cf-checks").querySelectorAll(".check").length === 4,
     `${$("cf-checks").querySelectorAll(".check").length} checks`);
  ok("all four pass on the sample data",
     $("cf-checks").querySelectorAll(".check.bad").length === 0);
  ok("each check says what it means",
     [...$("cf-checks").querySelectorAll(".check p")]
       .every(el => el.textContent.length > 30));

  await goTo("payroll", 400);
  ok("the payroll costs more than it pays",
     /cost PKR/.test(text("pay-readout")), text("pay-readout").slice(0, 70));
  ok("everybody is listed",
     $("t-pay").querySelectorAll("tbody tr").length === 8,
     `${$("t-pay").querySelectorAll("tbody tr").length} people`);
  ok("it names the law it used",
     /Finance Act 2026/.test(text("pay-source")), text("pay-source").slice(0, 70));
  ok("EOBI is explained as being on the minimum wage",
     /minimum wage/.test(text("pay-vitals")));
  ok("what is not modelled is said out loud",
     /[Bb]onuses/.test(text("pay-caveat")));
  const payRows = $("t-pay").querySelectorAll("tbody tr").length;
  const deptPill = [...$("pay-pills").querySelectorAll(".pill")][0];
  if (deptPill){
    deptPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
    await wait(400);
    ok("filtering by department narrows the payroll",
       $("t-pay").querySelectorAll("tbody tr").length < payRows);
    deptPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
    await wait(400);
  }

  await goTo("pipeline", 400);
  ok("the pipeline is weighted, and says so",
     /weighted/.test(text("pl-readout")), text("pl-readout").slice(0, 80));
  ok("weighted is never called revenue",
     /never be added to it/.test(text("pl-caveat")));
  ok("a deal that cannot be supplied in time is flagged",
     /cannot supply in time/.test($("t-pl").textContent),
     $("t-pl").textContent.slice(0, 120));
  ok("the supply column is on the table by default",
     /Can we supply it/.test($("v-pipeline").textContent));
  ok("it is checked against the cash too",
     /Beyond the cash/.test(text("pl-vitals")));
  ok("spend is priced against wins",
     /a win/.test(text("pl-spend")) || /Nothing recorded/.test(text("pl-spend")),
     text("pl-spend").slice(-60));
}


// One global bar above the navigation; the screen's own two inside the
// content column, where they belong to whatever is selected on the left.
ok("the group bar is global", window.document.querySelectorAll(".topbars .bar").length === 1,
   `${window.document.querySelectorAll(".topbars .bar").length} in the header`);
ok("the screen's bars are in the content column",
   $("viewbars")?.closest("main") !== null
   && $("actionbar").closest("#viewbars") !== null
   && $("viewbar").closest("#viewbars") !== null);
ok("the navigation comes before them",
   window.document.querySelector(".rail").compareDocumentPosition($("viewbars"))
     & window.Node.DOCUMENT_POSITION_FOLLOWING);
ok("tier one holds the scope", $("scope").querySelector("#f_entity") !== null);
ok("tier one holds the as-of date", $("asof") !== null);
ok("tier two is the action bar", $("actionbar") !== null);
ok("tier three is the view bar", $("viewbar") !== null);

window.history.pushState({}, "", "?view=stock");
window.dispatchEvent(new window.Event("popstate"));
await wait(300);
ok("scope is no longer repeated inside the view",
   $("stock-tools").innerHTML.trim() === "", `"${$("stock-tools").innerHTML.trim().slice(0,40)}"`);
ok("the action bar offers recalculate and import",
   /Recalculate/.test($("actionbar").textContent) && /Import/.test($("actionbar").textContent),
   $("actionbar").textContent.slice(0, 70));
ok("the view bar holds the search box", $("viewbar").querySelector("#q") !== null);
ok("the view bar holds the Display control",
   $("viewbar").querySelector('[data-menu="disp-stock"]') !== null);

// scope set once, honoured everywhere
const allStockRows = $("t-stock").querySelectorAll("tbody tr").length;
await setScope("f_entity", "DX");
ok("company scope applies", window.location.search.includes("entity=DX"));
const dxRows = $("t-stock").querySelectorAll("tbody tr").length;
ok("company scope narrows the stock table", dxRows > 0 && dxRows < allStockRows,
   `${dxRows} of ${allStockRows} rows`);
await goTo("machines", 300);
const dxMx = $("t-mx").querySelectorAll("tbody tr").length;
ok("the same scope is inherited by another view without being reapplied",
   dxMx > 0 && $("f_entity").value === "DX",
   `${dxMx} placements, scope ${$("f_entity").value}`);
ok("the branch list narrows to the chosen company",
   [...$("scope").querySelectorAll("#f_location option")].length === 3,
   `${[...$("scope").querySelectorAll("#f_location option")].length} options`);
await setScope("f_entity", "all");

// view types
window.history.pushState({}, "", "?view=stock");
window.dispatchEvent(new window.Event("popstate"));
await wait(300);
const vt = sel => $("viewbar").querySelector(`[data-set="vtype"][data-val="${sel}"]`);
ok("stock offers three representations",
   $("viewbar").querySelectorAll('[data-set="vtype"]').length === 3);
vt("chart").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(300);
ok("chart view in the url", window.location.search.includes("vtype=chart"));
ok("a chart was drawn", $("v-stock").querySelector(".altview svg .bar") !== null,
   `${$("v-stock").querySelectorAll(".altview svg .bar").length} bars`);
ok("the chart has a legend", $("v-stock").querySelector(".altview .legend") !== null);
ok("the chart explains what it shows",
   ($("v-stock").querySelector(".altview .chartnote")?.textContent || "").length > 40);
ok("the table is hidden while the chart shows",
   $("t-stock").closest(".tscroll").hidden === true);

vt("board").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(300);
ok("board view drew columns",
   $("v-stock").querySelectorAll(".altview .board .col").length >= 3,
   `${$("v-stock").querySelectorAll(".altview .board .col").length} columns`);
ok("board cards carry a value",
   $("v-stock").querySelector(".altview .card .amt") !== null);
const card = $("v-stock").querySelector(".altview .card");
card.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(300);
ok("a board card opens the detail panel",
   window.document.body.classList.contains("detail-open"));
$("detail-close").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(200);

vt("table").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(300);
ok("back to the table", $("t-stock").closest(".tscroll").hidden === false);
ok("the table is the default so it leaves the url",
   !window.location.search.includes("vtype="), window.location.search);

// cash defaults to the chart, because a balance over time is a shape
await goTo("cash", 350);
ok("the cash chart is drawn on a clean url",
   $("v-cash").querySelector(".altview svg .line") !== null,
   `url=${window.location.search} altlen=${
     ($("v-cash").querySelector(".altview")?.innerHTML||"").length}`);
{
  const knows = !text("cash-vitals").startsWith("Opening balance—")
    && !/Opening balance\s*—/.test(text("cash-vitals"));
  ok("the floor is drawn exactly when the balance is known",
     !!$("v-cash").querySelector(".altview svg .floor") === knows,
     `known=${knows}`);
}

// receivables and forecast charts
await goTo("receivables", 300);
$("viewbar").querySelector('[data-set="vtype"][data-val="chart"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(300);
ok("the ageing profile draws as bars",
   $("v-receivables").querySelectorAll(".altview svg .bar").length >= 4,
   `${$("v-receivables").querySelectorAll(".altview svg .bar").length} buckets, url=${
     window.location.search}, rows=${text("ar-count")}`);

await goTo("forecast", 300);
$("viewbar").querySelector('[data-set="vtype"][data-val="chart"]')
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(300);
ok("the forecast draws a line with its uncertainty band",
   $("v-forecast").querySelector(".altview svg .line") !== null
   && $("v-forecast").querySelector(".altview svg .band") !== null);

// ==============================================================
// Tiers two and three belong to the sidebar selection
// ==============================================================

{
  const bars = new Map();
  const views = ["dashboard","actions","stock","transfers","purchasing","forecast",
    "receivables","cash","machines","service","pack","sanity","history","audit",
    "org","thresholds","mapping"];
  for (const v of views){
    await goTo(v, 250);
    bars.set(v, $("actionbar").textContent.replace(/\s+/g, " ").trim());
  }
  ok("every screen has an action bar of its own",
     new Set(bars.values()).size === views.length,
     `${new Set(bars.values()).size} distinct of ${views.length}`);
  ok("no action bar is empty", [...bars.values()].every(b => b.length > 0),
     [...bars].filter(([, b]) => !b).map(([v]) => v).join(","));

  // The domain's actions are inherited; the module's are its own.
  ok("a data screen can recalculate and import",
     /Recalculate/.test(bars.get("stock")) && /Import/.test(bars.get("stock")));
  ok("settings screens do not offer an import",
     !/Import/.test(bars.get("org")), bars.get("org"));
  ok("the data pack does not offer to go to itself",
     !/Import files/.test(bars.get("pack")), bars.get("pack"));
  ok("stock leads to the two screens that answer it",
     /Move instead of buying/.test(bars.get("stock"))
     && /What to order/.test(bars.get("stock")));
  ok("purchasing names what funds it",
     /What funds it/.test(bars.get("purchasing")));
  ok("cash names both sides of itself",
     /What comes in/.test(bars.get("cash"))
     && /What it has to fund/.test(bars.get("cash")));

  // A one-click lens in tier two toggles the same state a pill does.
  // From a clean url: the view-type section above leaves stock on a chart,
  // and a row count taken while the table is hidden is whatever was last
  // drawn into it.
  window.history.pushState({}, "", "?view=stock");
  window.dispatchEvent(new window.Event("popstate"));
  await wait(700);
  const beforeRows = $("t-stock").querySelectorAll("tbody tr").length;
  const lens = $("actionbar").querySelector('[data-quick="status"]');
  ok("stock offers a one-click lens", !!lens);
  if (lens){
    lens.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
    await wait(400);
    const after = $("t-stock").querySelectorAll("tbody tr").length;
    ok("the lens filters the table", after < beforeRows, `${after} of ${beforeRows}`);
    ok("the lens writes to the url", window.location.search.includes("status="));
    const again = $("actionbar").querySelector('[data-quick="status"]');
    ok("the lens shows itself as on", again?.getAttribute("aria-pressed") === "true");
    ok("the pill agrees with the lens",
       [...$("stock-pills").querySelectorAll(".pill")]
         .some(x => x.dataset.val === "CRITICAL"
                 && x.getAttribute("aria-pressed") === "true"),
       [...$("stock-pills").querySelectorAll(".pill")]
         .map(x => `${x.dataset.val}=${x.getAttribute("aria-pressed")}`).join(" "));
    again.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
    await wait(400);
    ok("clicking it again takes it off",
       $("t-stock").querySelectorAll("tbody tr").length === beforeRows,
       window.location.search);
  }

  // Tier three names what it searches, per screen.
  const placeholders = {};
  for (const v of ["stock","receivables","service","sanity"]){
    await goTo(v, 250);
    placeholders[v] = $("q")?.getAttribute("placeholder") || "";
  }
  ok("the search box says what it searches",
     /items/.test(placeholders.stock)
     && /customers/.test(placeholders.receivables)
     && /findings/.test(placeholders.sanity),
     JSON.stringify(placeholders));
  ok("sanity does not inherit the previous screen's search box",
     placeholders.sanity !== placeholders.service,
     `${placeholders.sanity} vs ${placeholders.service}`);
}

// ==============================================================
// Scope is a question to the engine, not a filter on the answer
//
// The bug these cover: picking a branch narrowed the table and left every
// figure above it reading the whole group.
// ==============================================================

// Start from a clean url: the view-type section above leaves stock showing a
// chart, and renderAlt hides the table rather than emptying it, so a row
// count taken in chart mode is whatever was last drawn.
window.history.pushState({}, "", "?view=stock");
window.dispatchEvent(new window.Event("popstate"));
await wait(700);
await setScope("f_entity", "all");
const groupStock = $("t-stock").querySelectorAll("tbody tr").length;
ok("the stock table is live before the scope checks", groupStock === 36,
   `${groupStock} rows`);
await setScope("f_entity", "DX");
const dxStock = $("t-stock").querySelectorAll("tbody tr").length;
ok("scoping narrows the table", dxStock < groupStock, `${dxStock} of ${groupStock}`);

// removing the chip has to re-ask the engine, not just repaint
const sChip = $("stock-chips").querySelector('[data-unchip="entity"]');
ok("the scope appears as a removable chip", !!sChip,
   $("stock-chips").textContent.slice(0, 70));
if (sChip){
  sChip.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await waitFor("the group to come back", () =>
    $("t-stock").querySelectorAll("tbody tr").length === groupStock, 15000);
  ok("removing the chip restores the group", true);
}

// and so does Clear all, which only repainted at first
await setScope("f_entity", "DX");
const aPill = [...$("stock-pills").querySelectorAll(".pill")].find(x => x.dataset.val);
if (aPill){
  aPill.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(350);
}
const clearAll = $("stock-chips").querySelector("[data-clearfilters]");
ok("clear all is offered once two things are on", !!clearAll);
if (clearAll){
  clearAll.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await waitFor("clear all to restore the group", () =>
    $("t-stock").querySelectorAll("tbody tr").length === groupStock, 15000);
  ok("clear all restores the group too", true);
}

// the back button can step across a scope boundary
await setScope("f_entity", "MS");
const msStock = $("t-stock").querySelectorAll("tbody tr").length;
window.history.back();
await waitFor("the back button to re-fetch", () =>
  $("t-stock").querySelectorAll("tbody tr").length !== msStock, 15000);
ok("the back button re-fetches rather than repainting stale figures",
   $("t-stock").querySelectorAll("tbody tr").length === groupStock,
   `${$("t-stock").querySelectorAll("tbody tr").length} vs group ${groupStock}`);

// the two modules that cannot honestly be narrowed say so
await setScope("f_entity", "DX");
await goTo("cash", 400);
ok("cash admits it is group level under a scope",
   /Group level/.test($("cash-note").textContent)
   && /bank balance/.test($("cash-note").textContent),
   $("cash-note").textContent.slice(0, 80));
await goTo("transfers", 400);
ok("transfers admits it is group level under a scope",
   /Group level/.test($("tx-note").textContent),
   $("tx-note").textContent.slice(0, 80));
ok("transfers keeps only the moves this scope is party to",
   [...$("t-tx").querySelectorAll("tbody tr")].length > 0);
await setScope("f_entity", "all");
await goTo("cash", 350);
ok("the group-level notice goes away without a scope",
   ($("cash-note").textContent || "").trim() === "",
   $("cash-note").textContent.slice(0, 60));

// ---------------------------------------------------- Settings: group
await goTo("org", 350);
ok("the group screen is reachable", !$("v-org").hidden);
ok("running an analysis configures nothing",
   $("ent-list").textContent.includes("No companies configured"),
   $("ent-list").textContent.slice(0, 70));
ok("the codes the data mentioned are listed apart from configuration",
   $("ent-list").textContent.includes("Seen in your data"),
   $("ent-list").textContent.slice(0, 120));
ok("a discovered company offers to be set up",
   $("ent-list").querySelector(".setrow.found [data-edit-entity]") !== null);
ok("discovered branches are listed apart too",
   $("loc-list").textContent.includes("Seen in your data"));
ok("nothing is claimed as a configured branch",
   $("loc-list").textContent.includes("No branches configured"));
ok("the action bar offers the set-up actions",
   /Add a company/.test($("actionbar").textContent));

$("g_name").value = "Diagnostix Group";
$("g_save").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(500);
ok("the group name reaches tier one", text("orgname") === "Diagnostix Group",
   `"${text("orgname")}"`);

$("e_id").value = "DX";
$("e_name").value = "Diagnostix (Pvt) Ltd";
$("e_ntn").value = "1234567-8";
$("e_save").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(500);
ok("a company was saved", $("ent-list").textContent.includes("Diagnostix (Pvt) Ltd"),
   $("ent-list").textContent.slice(0, 80));
ok("the form cleared after saving", $("e_id").value === "");
ok("the branch form offers the new company",
   [...$("l_entity").options].some(o => o.value === "DX"));

$("l_id").value = "KHI";
$("l_entity").value = "DX";
$("l_name").value = "Karachi warehouse";
$("l_city").value = "Karachi";
$("l_save").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(500);
ok("a branch was saved", $("loc-list").textContent.includes("Karachi warehouse"));
ok("the branch shows its company by name",
   $("loc-list").textContent.includes("Diagnostix (Pvt) Ltd"));

// a branch on a company that does not exist must be refused
$("l_id").value = "XXX";
$("l_name").value = "Nowhere";
const sel2 = $("l_entity");
const ghost = window.document.createElement("option");
ghost.value = "GHOST"; ghost.textContent = "GHOST";
sel2.appendChild(ghost); sel2.value = "GHOST";
$("l_save").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(500);
ok("an unknown company is refused with a reason",
   $("flash").textContent.includes("no company"), $("flash").textContent.slice(0, 80));

// editing loads the row back into the form
window.document.querySelector("[data-edit-location]")
  .dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(250);
ok("editing a branch fills the form", $("l_name").value === "Karachi warehouse",
   $("l_name").value);

// ------------------------------------------------- Settings: thresholds
await goTo("thresholds", 300);
ok("every threshold is on its own screen",
   $("thr-list").querySelectorAll(".thr").length >= 11,
   `${$("thr-list").querySelectorAll(".thr").length} thresholds`);
ok("each threshold explains what it moves",
   [...$("thr-list").querySelectorAll(".thr .ex")].every(e => e.textContent.length > 40));
ok("the cash floor is called out as consequential",
   $("thr-list").textContent.includes("decides which purchase orders get placed"));
ok("the threshold inputs are the ones the analyse call reads",
   $("t_buffer") !== null && $("t_cover") !== null && $("t_bal") !== null);

// ---------------------------------------------------- Settings: mapping
await goTo("mapping", 300);
ok("the mapping screen is reachable", !$("v-mapping").hidden);
ok("nothing is mapped on a clean pack",
   $("map-list").textContent.includes("Nothing is mapped"),
   $("map-list").textContent.slice(0, 60));

$("a_raw").value = "PIMS-2";
$("a_target").value = "PIMS";
$("a_save").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(500);
ok("a mapping was saved", $("map-list").textContent.includes("PIMS-2"),
   $("map-list").textContent.slice(0, 70));
ok("the mapping records who set it",
   $("map-list").textContent.includes("set by"));

// and it is applied on the next run, not just recorded
$("run").dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
await wait(3000);
await goTo("mapping", 300);
const delMap = window.document.querySelector("[data-del-alias]");
ok("a mapping can be removed", !!delMap);
if (delMap){
  delMap.dispatchEvent(new window.MouseEvent("click", {bubbles:true}));
  await wait(450);
  ok("removing it leaves nothing mapped",
     $("map-list").textContent.includes("Nothing is mapped"));
}

// --- back button
await goTo("stock", 250);
const beforeBack = window.location.search;
window.history.back();
await wait(350);
ok("back button changes state", window.location.search !== beforeBack,
   `${beforeBack} -> ${window.location.search}`);

// --- report
const failed = checks.filter(c => !c.pass);
for (const c of checks) {
  if (!c.pass) console.log(`FAIL  ${c.name}${c.detail ? "  [" + c.detail + "]" : ""}`);
}
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
if (failed.length) console.log("POSTs:\n  " + POSTS.join("\n  "));
if (errors.length){
  console.log(`\n${errors.length} runtime errors:`);
  errors.slice(0, 12).forEach(e => console.log("  " + e.slice(0, 400)));
} else {
  console.log("no runtime JS errors");
}
process.exit(failed.length || errors.length ? 1 : 0);
