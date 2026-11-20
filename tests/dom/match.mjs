import { JSDOM, VirtualConsole } from "jsdom";
const BASE = "http://127.0.0.1:8099";
const vc = new VirtualConsole();
const errors = [];
vc.on("jsdomError", e => errors.push(e.message));

const html = await (await fetch(BASE + "/")).text();
function shim(w){
  w.fetch = async (i, init) => {
    const url = typeof i === "string" ? (i.startsWith("http") ? i : BASE + i) : i.url;
    if (init?.body && typeof init.body.entries === "function"){
      const fd = new FormData();
      for (const [k, v] of init.body.entries()){
        if (v && typeof v.arrayBuffer === "function"){
          const buf = await v.arrayBuffer();
          fd.append(k, new Blob([buf], {type: v.type || "text/csv"}), v.name || "u.csv");
        } else { fd.append(k, v); }
      }
      init = {...init, body: fd};
    }
    const r = await fetch(url, init);
    const b = await r.arrayBuffer();
    return {ok:r.ok, status:r.status,
      json: async () => JSON.parse(Buffer.from(b).toString("utf8")),
      text: async () => Buffer.from(b).toString("utf8")};
  };
  w.scrollTo = () => {};
}
const { window } = new JSDOM(html, {url: BASE+"/", runScripts:"dangerously",
  pretendToBeVisual:true, virtualConsole:vc, beforeParse:shim});
const wait = ms => new Promise(r => setTimeout(r, ms));
await wait(2200);

const checks = [];
const ok = (n, c, d="") => checks.push({n, pass:!!c, d});

// Every sample file must match its own slot, by header alone.
const ids = ["inventory","demand","receivables","service","machines","commitments"];
for (const id of ids){
  const text = await (await fetch(`${BASE}/api/sample/${id}.csv`)).text();
  const got = window.matchDataset(text);
  ok(`sample matches its own slot: ${id}`, got === id, `got ${got}`);
}
// Templates too.
for (const id of ids){
  const text = await (await fetch(`${BASE}/api/template/${id}.csv`)).text();
  ok(`template matches its slot: ${id}`, window.matchDataset(text) === id);
}
// A real-world export name is irrelevant; extra columns must still match.
const inv = await (await fetch(`${BASE}/api/sample/inventory.csv`)).text();
const lines = inv.split("\n");
const withExtra = [lines[0] + ",warehouse_bin,supplier_ref",
  ...lines.slice(1).filter(Boolean).map(l => l + ",A-12,SUP-9")].join("\n");
ok("extra columns still match", window.matchDataset(withExtra) === "inventory",
   `got ${window.matchDataset(withExtra)}`);

// Missing a required column must NOT match, so it lands in "unmatched"
// rather than being silently filed in the wrong slot.
const noCost = lines.map(l => {
  const f = l.split(",");
  f.splice(7, 1);          // unit_cost
  return f.join(",");
}).join("\n");
ok("missing a required column does not match", window.matchDataset(noCost) === null,
   `got ${window.matchDataset(noCost)}`);

// Nonsense must not match anything.
ok("unrelated csv does not match",
   window.matchDataset("foo,bar,baz\n1,2,3\n") === null);
ok("empty text does not match", window.matchDataset("") === null);

// Headers are matched case-insensitively and tolerate Excel quoting.
const upper = lines[0].toUpperCase() + "\n" + lines.slice(1).join("\n");
ok("header case is ignored", window.matchDataset(upper) === "inventory",
   `got ${window.matchDataset(upper)}`);
const quoted = '"' + lines[0].split(",").join('","') + '"\n' + lines.slice(1).join("\n");
ok("quoted headers are handled", window.matchDataset(quoted) === "inventory",
   `got ${window.matchDataset(quoted)}`);

// Staging through the real code path, with real File objects, because
// `staged` is a top-level let binding and not a window property.
const file = new window.File([inv], "StockOnHand_20261120 (3).csv", {type:"text/csv"});
ok("File.text is available", typeof file.text === "function");
await window.stageFiles([file]);
await wait(200);
const slots = window.document.getElementById("slots");
ok("slot shows the real filename", slots.innerHTML.includes("StockOnHand_20261120 (3).csv"),
   slots.querySelector(".slot.has .st")?.textContent?.trim());
ok("one slot marked ready", slots.querySelectorAll(".slot.has").length === 1,
   `${slots.querySelectorAll(".slot.has").length} ready`);
ok("upload button enabled", !window.document.getElementById("upload").disabled);
ok("upload button counts files",
   window.document.getElementById("upload").textContent.includes("1 file"),
   window.document.getElementById("upload").textContent);

// An unmatched file is reported rather than filed in the wrong slot.
const junk = new window.File(["foo,bar\n1,2\n"], "mystery.csv", {type:"text/csv"});
await window.stageFiles([junk]);
await wait(200);
ok("unmatched file is flagged to the user",
   window.document.getElementById("flash").textContent.includes("could not be matched"),
   window.document.getElementById("flash").textContent.trim().slice(0,80));
ok("unmatched file did not occupy a slot",
   slots.querySelectorAll(".slot.has").length === 1);

const failed = checks.filter(c => !c.pass);
failed.forEach(c => console.log(`FAIL  ${c.n}  ${c.d}`));
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
if (errors.length) console.log("errors:", errors.slice(0,5));
process.exit(failed.length ? 1 : 0);
