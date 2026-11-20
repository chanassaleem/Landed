"""Demo web shell over the S&OP engine.

This is NOT the product. The product is a desktop application where data never
leaves the machine. This exists so the engine can be shown over a link, and it
runs on the sample pack by default.

The shell computes nothing. Every figure comes from engine.report.build, which
the CLI calls too, so the dashboard and the terminal cannot disagree.

Two deliberate choices in here.

Uploaded data is never mixed with sample data. If you upload any file, only
your files are used and the datasets you did not send are simply absent, with
their panels saying so. Backfilling the gaps with invented numbers would
produce a dashboard that looks complete and is partly fiction.

An uploaded file is stored as one snapshot per (entity, location), not one per
file. The snapshot key is (entity, location, report type, as-of date) and a
re-upload supersedes its key. Storing a three-branch file as a single snapshot
would mean Lahore re-sending its own export superseded Karachi's data too.

Persistence note for the hosted demo: Render's free tier has an ephemeral
filesystem, so the DuckDB file is recreated when the instance restarts. The
desktop build points SOP_DB_PATH at a durable location instead.
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from engine import __version__, alias, parse, report, samples, scope, workbook
from engine.db.store import DuplicateUpload, Store
from engine.samples import DATASETS, ORDER
from engine.schema import Thresholds

STATIC = Path(__file__).parent / "static"
DB_PATH = os.environ.get("SOP_DB_PATH", ":memory:")

app = FastAPI(title="S&OP Insight", version=__version__)
store = Store(DB_PATH)

# Report type stored against each dataset's snapshots.
# A dataset can be addressed by its id ("demand") or by its filename stem
# ("demand_history"), because the file you download is named the second way and
# that is the URL anyone will reach for.
ALIASES = {ds.filename.replace(".csv", ""): ds.id for ds in DATASETS.values()}
ALIASES.update({ds.id: ds.id for ds in DATASETS.values()})


def _dataset_id(raw: str) -> Optional[str]:
    return ALIASES.get(raw) or ALIASES.get(raw.replace("_template", ""))


REPORT_TYPE = {
    "inventory": "inventory",
    "demand": "demand",
    "receivables": "receivables",
    "service": "service",
    "machines": "placements",
    "commitments": "commitments",
    "trial_balance": "trial_balance",
    "employees": "payroll",
    "opportunities": "pipeline",
    "marketing_spend": "marketing_spend",
}


# ------------------------------------------------------------- helpers


def _as_of(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date() if raw else samples.AS_OF


def _f(raw: str, default: float) -> float:
    try:
        return float(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _slices(dataset_id: str, text: str) -> Dict[Tuple[str, str], bytes]:
    """Split a file into one CSV per (entity, location) for storage.

    Done on the raw rows rather than the parsed records so the stored bytes are
    the customer's own text, which is what an audit trail is for.
    """
    header = DATASETS[dataset_id].header
    grouped: Dict[Tuple[str, str], List[dict]] = defaultdict(list)

    for row in csv.DictReader(io.StringIO(text)):
        if not any((v or "").strip() for v in row.values()):
            continue
        entity = (row.get("entity_id") or "").strip() or "default"
        location = (row.get("location_id") or "").strip() or "default"
        grouped[(entity, location)].append(row)

    out: Dict[Tuple[str, str], bytes] = {}
    for key, rows in grouped.items():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})
        out[key] = buf.getvalue().encode("utf-8")
    return out


def _zip(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _csv_response(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------------------------------------------- routes


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/meta")
def meta() -> dict:
    """What the shell needs to render navigation honestly.

    Unbuilt modules are listed as unbuilt with a reason. Showing them with
    placeholder numbers is how a demo becomes a lie you later have to defend.
    """
    return {
        "version": __version__,
        "persistent": DB_PATH != ":memory:",
        "stats": store.stats(),
        "as_of_default": samples.AS_OF.isoformat(),
        "domains": [
            {
                "id": "overview", "name": "Overview",
                "modules": [
                    {"id": "dashboard", "name": "Dashboard", "built": True},
                    {"id": "actions", "name": "Actions", "built": True},
                ],
            },
            {
                "id": "stock", "name": "Stock",
                "modules": [
                    {"id": "stock", "name": "Inventory", "built": True},
                    {"id": "transfers", "name": "Transfers", "built": True},
                    {"id": "purchasing", "name": "Purchasing", "built": True},
                    {"id": "forecast", "name": "Forecast", "built": True},
                ],
            },
            {
                "id": "money", "name": "Money",
                "modules": [
                    {"id": "receivables", "name": "Receivables", "built": True},
                    {"id": "cash", "name": "Cash forecast", "built": True},
                    {"id": "payables", "name": "Payables", "built": False,
                     "why": "Not started. Committed outflows are covered by "
                            "commitments.csv and feed the cash view; a full "
                            "payables ledger with ageing needs an export no "
                            "customer has given us yet."},
                ],
            },
            {
                "id": "finance", "name": "Finance",
                "modules": [
                    {"id": "pnl", "name": "Profit and loss", "built": True},
                    {"id": "balance", "name": "Balance sheet", "built": True},
                    {"id": "cashflow", "name": "Cash flow", "built": True},
                    {"id": "payroll", "name": "Payroll", "built": True},
                ],
            },
            {
                "id": "marketing", "name": "Marketing",
                "modules": [
                    {"id": "pipeline", "name": "Pipeline", "built": True},
                ],
            },
            {
                "id": "equipment", "name": "Equipment",
                "modules": [
                    {"id": "machines", "name": "Placements", "built": True},
                    {"id": "service", "name": "Service", "built": True},
                ],
            },
            {
                "id": "data", "name": "Data",
                "modules": [
                    {"id": "pack", "name": "Data pack", "built": True},
                    {"id": "sanity", "name": "Data sanity", "built": True},
                    {"id": "history", "name": "History", "built": True},
                    {"id": "audit", "name": "Audit", "built": True},
                ],
            },
            {
                "id": "settings", "name": "Settings",
                "modules": [
                    {"id": "org", "name": "Group and branches", "built": True},
                    {"id": "thresholds", "name": "Thresholds", "built": True},
                    {"id": "mapping", "name": "Item and customer mapping", "built": True},
                    {"id": "users", "name": "Users and roles", "built": False,
                     "why": "The password handling and database-enforced tenant "
                            "isolation are built and tested, and not yet wired "
                            "into this shell. Until they are, anything uploaded "
                            "here is visible to anyone with the link."},
                ],
            },
        ],
    }


@app.get("/api/datasets")
def datasets() -> dict:
    """The column registry, so the UI documents the files from one source."""
    return {
        "order": list(ORDER),
        "datasets": [
            {
                "id": ds.id,
                "filename": ds.filename,
                "title": ds.title,
                "bucket": ds.bucket,
                "description": ds.description,
                "columns": [
                    {
                        "name": c.name,
                        "example": c.example,
                        "note": c.note,
                        "required": c.required,
                    }
                    for c in ds.columns
                ],
                "sample_rows": len(ds.rows()),
            }
            for ds in (DATASETS[k] for k in ORDER)
        ],
    }


@app.get("/api/sample/{name}.csv")
def sample_file(name: str):
    dataset_id = _dataset_id(name)
    if dataset_id is None:
        return JSONResponse(
            {"error": f"unknown dataset {name!r}", "known": sorted(ALIASES)},
            status_code=404,
        )
    return _csv_response(
        samples.sample_csv(dataset_id), DATASETS[dataset_id].filename
    )


@app.get("/api/template/{name}.csv")
def template_file(name: str):
    dataset_id = _dataset_id(name)
    if dataset_id is None:
        return JSONResponse(
            {"error": f"unknown dataset {name!r}", "known": sorted(ALIASES)},
            status_code=404,
        )
    filename = DATASETS[dataset_id].filename.replace(".csv", "_template.csv")
    return _csv_response(samples.template_csv(dataset_id), filename)


@app.get("/api/pack.zip")
def pack_zip():
    return Response(
        content=_zip(samples.pack()),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="sop_insight_sample_pack.zip"'
        },
    )


_WORKBOOK: bytes | None = None


@app.get("/api/workbook.xlsx")
def workbook_download():   # not `workbook`: that is the module that reads it
    """The whole data model as one Excel file.

    Every field the engine reads, with the arithmetic that can be checked by
    eye written out beside it: white columns are the import, grey are worked
    out. Built on first request and kept, because it is the same file every
    time and building it takes a second.
    """
    global _WORKBOOK
    if _WORKBOOK is None:
        import sys as _sys
        import tempfile
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parent.parent
        if str(root) not in _sys.path:
            _sys.path.insert(0, str(root))
        from tools.build_workbook import main as build_workbook

        with tempfile.TemporaryDirectory() as tmp:
            out = _Path(tmp) / "sop-insight-data-model.xlsx"
            build_workbook(str(out))
            _WORKBOOK = out.read_bytes()
    return Response(
        content=_WORKBOOK,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition":
                'attachment; filename="sop-insight-data-model.xlsx"'
        },
    )


@app.get("/api/templates.zip")
def templates_zip():
    return Response(
        content=_zip(samples.pack(templates=True)),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="sop_insight_templates.zip"'
        },
    )


@app.get("/api/readme.md")
def readme():
    return Response(content=samples.notes_md(), media_type="text/markdown")


@app.post("/api/analyse")
async def analyse(
    inventory: UploadFile | None = File(default=None),
    demand: UploadFile | None = File(default=None),
    receivables: UploadFile | None = File(default=None),
    service: UploadFile | None = File(default=None),
    machines: UploadFile | None = File(default=None),
    commitments: UploadFile | None = File(default=None),
    trial_balance: UploadFile | None = File(default=None),
    employees: UploadFile | None = File(default=None),
    opportunities: UploadFile | None = File(default=None),
    marketing_spend: UploadFile | None = File(default=None),
    workbook_file: UploadFile | None = File(default=None, alias="workbook"),
    as_of: str = Form(default=""),
    target_cover: str = Form(default=""),
    excess_months: str = Form(default=""),
    cost_of_capital: str = Form(default=""),
    target_dso: str = Form(default=""),
    labour_rate: str = Form(default=""),
    payback_target: str = Form(default=""),
    min_transfer_value: str = Form(default=""),
    monthly_revenue: str = Form(default=""),
    opening_balance: str = Form(default=""),
    collection_rate: str = Form(default=""),
    cash_buffer: str = Form(default=""),
    forecast_horizon: str = Form(default=""),
    entity: str = Form(default=""),
    location: str = Form(default=""),
    persist: str = Form(default="1"),
) -> JSONResponse:
    try:
        on = _as_of(as_of)
        uploads = {
            "inventory": inventory,
            "demand": demand,
            "receivables": receivables,
            "service": service,
            "machines": machines,
            "commitments": commitments,
            "trial_balance": trial_balance,
            "employees": employees,
            "opportunities": opportunities,
            "marketing_spend": marketing_spend,
        }
        sent = {k: v for k, v in uploads.items() if v is not None}

        texts: Dict[str, str] = {}
        filenames: Dict[str, str] = {}
        carried: Dict[str, str] = {}
        workbook_notes: List[str] = []

        if workbook_file is not None:
            # One file instead of six. The workbook is the data model, so it
            # already holds every sheet, and its Settings sheet holds the
            # assumptions: both travel together or the cash view comes back
            # without the opening balance that was sitting in the file.
            raw = await workbook_file.read()
            try:
                read = workbook.read(raw)
                texts = read["texts"]
                workbook_notes = read["notes"]
                carried = workbook.settings(raw)
            except workbook.WorkbookError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            source = "workbook"
            name = workbook_file.filename or "data model.xlsx"
            filenames = {k: f"{name} ({workbook.SHEETS[k]})" for k in texts}
        elif sent:
            source = "uploaded"
            for key, upload in sent.items():
                raw = await upload.read()
                texts[key] = raw.decode("utf-8-sig", errors="replace")
                filenames[key] = upload.filename or DATASETS[key].filename
        else:
            source = "sample"
            for key in ORDER:
                texts[key] = samples.sample_csv(key)
                filenames[key] = DATASETS[key].filename

        records: Dict[str, list] = {k: [] for k in ORDER}
        quality: Dict[str, dict] = {}

        for key, text in texts.items():
            missing = parse.missing_columns(key, text)
            recs, rejected = parse.parse(key, text)
            records[key] = recs
            quality[key] = {
                "filename": filenames[key],
                "rows_read": len(recs) + len(rejected),
                "rows_used": len(recs),
                "missing_columns": missing,
                "rejected": [
                    {"row": n, "reason": why} for n, why, _ in rejected[:200]
                ],
                "rejected_total": len(rejected),
            }

        # A file whose required columns are absent is refused rather than
        # computed on. Letting it through produces a dashboard where every item
        # reads as "no history" and the cash figures are zero, which looks
        # complete and is fiction. The usual cause is a wrong file in a slot.
        unusable = {
            key: q["missing_columns"]
            for key, q in quality.items()
            if q["missing_columns"]
        }
        if unusable:
            names = ", ".join(
                f'{quality[k]["filename"]} (missing {", ".join(cols)})'
                for k, cols in sorted(unusable.items())
            )
            return JSONResponse(
                {
                    "error": (
                        f"These files are missing required columns, so they were "
                        f"not used: {names}. Download the matching template if "
                        f"you need the exact headers."
                    ),
                    "quality": quality,
                },
                status_code=400,
            )

        if not any(records[k] for k in ORDER):
            return JSONResponse(
                {
                    "error": "Nothing usable was found in the files supplied.",
                    "quality": quality,
                },
                status_code=400,
            )

        # Aliases are applied on the way in, so every module downstream sees
        # one customer where the files held two spellings. Applied rather than
        # written back, because the files are immutable snapshots of what the
        # customer's system exported and the mapping is a decision about how
        # to read them.
        mapped = alias.apply(
            items=records["inventory"],
            demand=records["demand"],
            invoices=records["receivables"],
            jobs=records["service"],
            placements=records["machines"],
            item_map=store.alias_map("item"),
            customer_map=store.alias_map("customer"),
            machine_map=store.alias_map("machine"),
        )

        # A value typed on screen wins; the workbook fills in what was left
        # blank, so uploading it on its own is enough. Without this the cash
        # view came back with no opening balance even though the number was
        # sitting on the Settings sheet of the file just uploaded.
        def _carried(value: str, field: str) -> str:
            return value if str(value).strip() else carried.get(field, "")

        if not str(as_of).strip() and carried.get("as_of"):
            on = _as_of(carried["as_of"])

        target_cover = _carried(target_cover, "target_cover")
        excess_months = _carried(excess_months, "excess_months")
        cost_of_capital = _carried(cost_of_capital, "cost_of_capital")
        target_dso = _carried(target_dso, "target_dso")
        labour_rate = _carried(labour_rate, "labour_rate")
        payback_target = _carried(payback_target, "payback_target")
        min_transfer_value = _carried(min_transfer_value, "min_transfer_value")
        monthly_revenue = _carried(monthly_revenue, "monthly_revenue")
        opening_balance = _carried(opening_balance, "opening_balance")
        cash_buffer = _carried(cash_buffer, "cash_buffer")
        collection_rate = _carried(collection_rate, "collection_rate")
        forecast_horizon = _carried(forecast_horizon, "forecast_horizon")

        thresholds = Thresholds(
            target_cover_months=_f(target_cover, 3.0),
            excess_cover_months=_f(excess_months, 6.0),
            cost_of_capital_annual=_f(cost_of_capital, 0.22),
            target_dso_days=int(_f(target_dso, 60)),
            labour_rate_per_hour=_f(labour_rate, 2500.0),
            payback_target_months=int(_f(payback_target, 24)),
            min_transfer_value=_f(min_transfer_value, 25_000.0),
            cash_buffer=_f(cash_buffer, 2_000_000.0),
            forecast_horizon_months=int(_f(forecast_horizon, 6)),
        )
        revenue = _f(monthly_revenue, 0.0) or None
        # Left as None when blank rather than defaulted to zero: a zero
        # opening balance is a claim about the bank account, and a blank one
        # is an absence of information. The modules behave differently.
        balance = (
            _f(opening_balance, -1.0) if str(opening_balance).strip() else None
        )
        if balance is not None and balance < 0:
            balance = None
        rate = _f(collection_rate, 1.0)

        # Scope is applied to the facts, before any module runs, rather than
        # to the rows afterwards in the page. Filtering rows in the browser
        # narrowed the table and left every figure above it reading the whole
        # group, and figures like safety stock and DSO cannot be recovered by
        # adding up rows that have already been through the engine.
        group_facts = {
            "items": mapped["items"],
            "demand": mapped["demand"],
            "invoices": mapped["invoices"],
            "jobs": mapped["jobs"],
            "placements": mapped["placements"],
            "commitments": records["commitments"],
            "accounts": records["trial_balance"],
            "employees": records["employees"],
            "opportunities": records["opportunities"],
            "marketing_spend": records["marketing_spend"],
        }
        the_scope = scope.read(entity, location)
        narrowed = scope.apply(the_scope, **group_facts)

        payload = report.build(
            **narrowed["records"],
            thresholds=thresholds,
            as_of=on,
            monthly_revenue=revenue,
            opening_balance=balance,
            collection_rate=rate,
            scope=the_scope,
            group=group_facts,
            scope_counts=narrowed["counts"],
        )

        # A scope that matches nothing is a dead end: every screen reads empty
        # and nothing says why. Refusing it with the reason is more use.
        if the_scope.active and not any(
            narrowed["records"][k] for k in narrowed["records"]
        ):
            return JSONResponse(
                {
                    "error": (
                        f"Nothing in the data matches "
                        f"{the_scope.label()}. Check the company and branch "
                        f"codes against what the files actually contain."
                    ),
                    "scope": {"entity": the_scope.entity,
                              "location": the_scope.location},
                },
                status_code=400,
            )

        if workbook_notes:
            payload["workbook_notes"] = workbook_notes

        raw_results = payload.get("_results") or []
        raw_order_values = payload.get("_order_values") or {}

        # ------------------------------------------------------ persistence

        snapshot_ids: List[str] = []
        run_id: Optional[str] = None
        duplicates: List[str] = []

        if persist == "1":
            try:
                if records["inventory"]:
                    store.upsert_items(records["inventory"])

                for key in ORDER:
                    if key not in texts or not records[key]:
                        continue
                    by_loc = _slices(key, texts[key])
                    for (entity, location), body in sorted(by_loc.items()):
                        # Not every record has a branch. A trial balance
                        # belongs to a company, and marketing spend is paid
                        # by one: asking those for a location_id raised an
                        # AttributeError that cost the whole save, while the
                        # answer on screen looked fine.
                        rows_here = [
                            r for r in records[key]
                            if r.entity_id == entity
                            and getattr(r, "location_id", "") in
                                (location, "" if location == "default" else None)
                        ]
                        try:
                            sid = store.add_snapshot(
                                report_type=REPORT_TYPE[key],
                                as_of=on,
                                entity_id=entity,
                                location_id=location,
                                source_filename=filenames[key],
                                source_bytes=body,
                                row_count=len(rows_here),
                                rows_rejected=quality[key]["rejected_total"],
                                status="partial"
                                if quality[key]["rejected_total"]
                                else "valid",
                            )
                        except DuplicateUpload as dup:
                            duplicates.append(f"{key} {entity}/{location}: {dup}")
                            continue

                        snapshot_ids.append(sid)
                        if key == "inventory":
                            store.add_inventory_facts(sid, rows_here)
                        elif key == "demand":
                            store.add_demand_facts(sid, rows_here)

                # A run is recorded whenever the engine produced results,
                # not only when new files were filed. Changing a threshold
                # and recalculating files no snapshots -- the content is
                # identical -- but it is still a different answer, and a
                # history that drops it cannot explain why a figure moved.
                if raw_results:
                    tsid = store.save_thresholds(thresholds, "group")
                    run_id = store.save_metric_run(
                        raw_results,
                        as_of=on,
                        threshold_set_id=tsid,
                        snapshot_ids=snapshot_ids,
                        order_values=raw_order_values,
                        entity_id="group",
                        location_id="all",
                    )
            except Exception as exc:  # persistence must never lose an answer
                payload["persist_error"] = f"{type(exc).__name__}: {exc}"

        payload = report.public(payload)
        payload["source"] = source
        payload["quality"] = quality
        payload["datasets_supplied"] = sorted(texts)
        payload["datasets_missing"] = [k for k in ORDER if k not in texts]
        payload["run_id"] = run_id
        payload["snapshot_ids"] = snapshot_ids
        payload["duplicates"] = duplicates
        payload["persistent"] = DB_PATH != ":memory:"
        payload["aliases_applied"] = mapped["applied"]
        payload["config"] = read_config()
        return JSONResponse(payload)

    except Exception as exc:
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"}, status_code=400
        )


# ------------------------------------------------------------ configuration
#
# The schema has carried entity, location, customer and alias since the first
# commit and nothing ever wrote to them but the stubs an upload creates. These
# endpoints are what make a company and a branch into objects somebody can
# name, and an alias into a decision rather than an observation.


@app.get("/api/config")
def read_config() -> dict:
    return {
        "group": store.group(),
        "entities": store.entities(),
        "locations": store.locations(),
        "aliases": {
            kind: store.aliases(kind) for kind in ("customer", "item", "machine")
        },
    }


@app.post("/api/config/group")
def write_group(
    name: str = Form(...),
    country: str = Form(default="PK"),
    currency: str = Form(default="PKR"),
) -> JSONResponse:
    try:
        saved = store.save_group(name, country, currency)
        # The client keeps one copy of the configuration and repaints from it,
        # so every write here answers with the whole of it. Returning only the
        # part that changed left the group name off the bar until a reload.
        return JSONResponse({"group": saved, "config": read_config()})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/config/entity")
def write_entity(
    entity_id: str = Form(...),
    legal_name: str = Form(...),
    ntn: str = Form(default=""),
    is_separate_company: str = Form(default="1"),
    base_currency: str = Form(default="PKR"),
) -> JSONResponse:
    try:
        saved = store.save_entity(
            entity_id, legal_name, ntn,
            is_separate_company in ("1", "true", "yes", "on"), base_currency,
        )
        return JSONResponse({"entity": saved, "config": read_config()})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/config/location")
def write_location(
    location_id: str = Form(...),
    entity_id: str = Form(...),
    name: str = Form(...),
    city: str = Form(default=""),
    is_warehouse: str = Form(default="1"),
) -> JSONResponse:
    try:
        saved = store.save_location(
            location_id, entity_id, name, city,
            is_warehouse in ("1", "true", "yes", "on"),
        )
        return JSONResponse({"location": saved, "config": read_config()})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/config/location/delete")
def remove_location(location_id: str = Form(...)) -> JSONResponse:
    try:
        store.delete_location(location_id)
        return JSONResponse({"removed": location_id, "config": read_config()})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/config/alias")
def write_alias(
    target_type: str = Form(...),
    raw_value: str = Form(...),
    target_key: str = Form(...),
    source_entity_id: str = Form(default=""),
) -> JSONResponse:
    try:
        saved = store.save_alias(target_type, raw_value, target_key, source_entity_id)
        return JSONResponse({"alias": saved, "config": read_config()})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/config/alias/delete")
def remove_alias(alias_id: str = Form(...)) -> JSONResponse:
    store.delete_alias(alias_id)
    return JSONResponse({"removed": alias_id, "config": read_config()})


@app.get("/api/history")
def history_runs() -> dict:
    return {"runs": store.runs(limit=30), "snapshots": store.snapshots(limit=60)}


@app.get("/api/audit")
def audit() -> dict:
    return {"entries": store.audit_log(limit=120), "stats": store.stats()}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
