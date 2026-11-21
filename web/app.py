"""Landed — the web layer.

Deliberately thin. It serves one page, answers one data request, hands
back the Excel model, and says whether the database is healthy. Every
figure it returns was computed by `engine/`, which knows nothing about
HTTP and is tested on its own; this file only decides what reaches the
screen.

What used to be here was the S&OP Insight demo shell: twenty views over
a scratch DuckDB, no tenancy, sample data baked in. It was the right
thing for a tool that analysed a file someone handed it, and the wrong
thing for a product with customers, so it has gone rather than been
adapted. The engine it drove came across untouched.
"""

from __future__ import annotations

import io
import pathlib
import tempfile

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

from engine.supabase_auth import AuthError, Claims, Verifier
from web import api, session

__version__ = "0.4.0"

STATIC = pathlib.Path(__file__).parent / "static"

_verifier: Verifier | None = None
_auth: session.Auth | None = None


def auth() -> session.Auth:
    """The Auth client, built on first use from the environment."""
    global _auth
    if _auth is None:
        import os

        _auth = session.Auth(os.environ.get("SUPABASE_URL", ""),
                             session.public_key())
    return _auth


def verifier() -> Verifier:
    global _verifier
    if _verifier is None:
        import os

        _verifier = Verifier(os.environ.get("SUPABASE_URL", ""),
                             session.public_key())
    return _verifier


def base_url(request: Request) -> str:
    """The address this deployment is reachable at, from outside.

    Needed because a confirmation link has to be built before the
    browser that will follow it exists. Taken from configuration first,
    then Render's own variable, and only then from the request -- which
    behind a proxy reports http even when the world speaks to it over
    https, and a redirect_to that does not match the allow-list exactly
    is discarded without a word.
    """
    import os

    for name in ("LANDED_BASE_URL", "RENDER_EXTERNAL_URL"):
        if os.environ.get(name):
            return os.environ[name].rstrip("/")
    url = str(request.base_url).rstrip("/")
    if url.startswith("http://") and "localhost" not in url \
            and "127.0.0.1" not in url:
        url = "https://" + url[len("http://"):]
    return url


def who(request: Request) -> Claims | None:
    """The signed-in user, or None. Never raises.

    Used by routes that behave differently for a visitor rather than
    refusing them -- the page itself, mainly, which has to render a
    sign-in screen rather than a 401 nobody can read.
    """
    token = request.cookies.get(session.ACCESS)
    if not token:
        return None
    try:
        return verifier().verify(token)
    except AuthError:
        return None


def require(request: Request) -> Claims:
    """The signed-in user, or 401.

    Every route that touches tenant data depends on this. Note what it
    returns: claims proven against the project's signing keys, not a
    user id read out of a cookie. The difference is whether a request
    can choose whose data it sees.
    """
    claims = who(request)
    if claims is None:
        raise HTTPException(status_code=401, detail="sign in")
    return claims

app = FastAPI(title="Landed", version=__version__, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health() -> dict:
    """Is the process up? Nothing more.

    Separate from the database check on purpose: when Postgres is
    unreachable you still want this to answer, so the platform keeps the
    instance alive long enough for you to read why.
    """
    return {"status": "ok", "version": __version__}


@app.get("/health/supabase")
def health_supabase(response: Response) -> dict:
    """Is the database reachable, and are its policies actually on?

    The second half is the one worth having. A Postgres with row-level
    security switched off answers every ordinary health check perfectly
    while serving every tenant's rows to anyone who asks, so this probe
    reads a protected table as a user who belongs to no organisation and
    expects to be told nothing.

    Returns 503 when either half fails, so a platform health check and a
    person reading the JSON reach the same conclusion.
    """
    from engine.db.supabase import NotConfigured, database

    try:
        db = database()
    except NotConfigured as exc:
        response.status_code = 503
        return {"ok": False, "configured": False, "detail": str(exc)}

    report = db.check().as_dict()
    report["configured"] = True
    if db.config.warnings:
        report["warnings"] = db.config.warnings
    if not report["ok"]:
        response.status_code = 503
    return report


@app.get("/health/auth")
def health_auth() -> dict:
    """Is sign-in switched on, and if not, what exactly is missing?

    This endpoint exists because of the shape of the failure it reports.
    With no anon key the app does not break -- it quietly drops the gate
    and serves sample data, which looks identical to a build where the
    login was never written. Naming the missing variable turns twenty
    minutes of wondering into one request.

    It reports which variables are set, never their values.
    """
    import os

    url = os.environ.get("SUPABASE_URL", "")
    key = session.public_key()
    missing = []
    if not url:
        missing.append("SUPABASE_URL")
    if not key:
        # Named with the current key first. Supabase is retiring the anon
        # key in favour of the publishable one, and a project created
        # now has only the latter -- so telling someone to go and find
        # an anon key sends them looking for something that is not there.
        missing.append("SUPABASE_PUBLISHABLE_KEY (or SUPABASE_ANON_KEY)")
    return {
        "sign_in_enabled": not missing,
        "missing": missing,
        "project": url or None,
        "detail": (
            "Sign-in is on."
            if not missing
            else "Sign-in is off, so the app is open and showing sample "
                 f"data. Set {' and '.join(missing)} in the environment "
                 "and redeploy."
        ),
        "version": __version__,
    }


@app.get("/api/config")
def config() -> dict:
    """What the page needs to know before it renders. No secrets."""
    import os

    return {
        "auth_configured": bool(os.environ.get("SUPABASE_URL")
                                and session.public_key()),
        "version": __version__,
    }


@app.post("/auth/sign-in")
async def sign_in(request: Request) -> JSONResponse:
    body = await request.json()
    a = auth()
    if not a.configured:
        return JSONResponse({"error": "auth is not configured"}, 503)
    try:
        tokens = a.sign_in(str(body.get("email", "")).strip(),
                           str(body.get("password", "")))
    except session.SignInFailed:
        # One message for every cause. Telling the person which half was
        # wrong tells anyone who asks whether an email has an account.
        return JSONResponse({"error": "That email and password do not match."},
                            401)
    r = JSONResponse({"ok": True})
    session.set_session(r, tokens)
    return r


@app.post("/auth/sign-up")
async def sign_up(request: Request) -> JSONResponse:
    body = await request.json()
    a = auth()
    if not a.configured:
        return JSONResponse({"error": "auth is not configured"}, 503)
    password = str(body.get("password", ""))
    if len(password) < 10:
        # Checked here as well as at Supabase so the person is told
        # before the round trip, in the same words either way.
        return JSONResponse(
            {"error": "Use at least 10 characters."}, 400)
    try:
        tokens = a.sign_up(str(body.get("email", "")).strip(), password,
                           redirect_to=f"{base_url(request)}/auth/landing")
    except session.SignInFailed:
        return JSONResponse({"error": "That did not work. Try another email."},
                            400)
    if tokens is None:
        return JSONResponse({"ok": True, "confirm": True})
    r = JSONResponse({"ok": True, "confirm": False})
    session.set_session(r, tokens)
    return r


# The types Supabase sends on a confirmation link. Pinned rather than
# passed through: this value goes straight to the Auth server, and an
# allow-list is cheaper than reasoning about what else it would accept.
CONFIRM_TYPES = {"signup", "email", "magiclink", "recovery", "invite",
                 "email_change"}


@app.get("/auth/confirm")
def confirm(request: Request) -> Response:
    """The link in a confirmation email lands here.

    Redeemed server-side, so the session arrives as a cookie and no
    token is ever in a URL. On success the person is simply signed in
    and sent to the app -- there is no reason to make somebody who has
    just proved they own the address type their password again.
    """
    token_hash = request.query_params.get("token_hash", "")
    kind = request.query_params.get("type", "signup")
    if not token_hash or kind not in CONFIRM_TYPES:
        return RedirectResponse("/sign-in?problem=link", status_code=303)

    try:
        tokens = auth().verify_otp(token_hash, kind)
    except session.SignInFailed:
        # Almost always an expired or already-used link. Say which,
        # rather than "invalid", because the two have different fixes
        # and the person can act on one of them.
        return RedirectResponse("/sign-in?problem=expired", status_code=303)

    r = RedirectResponse("/", status_code=303)
    session.set_session(r, tokens)
    return r


@app.get("/auth/landing", response_class=HTMLResponse)
def landing() -> FileResponse:
    """Where Supabase's own confirmation link comes back to.

    The built-in email template cannot be edited without custom SMTP,
    so the link goes to Supabase, which verifies the token and then
    redirects here with the session in the URL fragment. This page
    hands those tokens to the server and removes them from the address
    bar. It is the lesser of the two routes -- /auth/confirm never puts
    a token in a URL at all -- and it is the one that works on a
    project using the default template.
    """
    return FileResponse(STATIC / "landing.html")


@app.post("/auth/session")
async def adopt_session(request: Request) -> JSONResponse:
    """Take tokens from the landing page and turn them into a cookie.

    Every token is verified before anything is set. This endpoint is
    public and accepts a token from whoever posts one, so trusting it
    would mean anybody could hand it a string and be issued a session
    cookie for whichever user they named. The signature is the only
    thing that makes the claim worth anything, and it is checked here
    exactly as it is checked on every other request.
    """
    body = await request.json()
    access = str(body.get("access_token", ""))
    refresh = str(body.get("refresh_token", ""))
    try:
        claims = verifier().verify(access)
    except AuthError:
        return JSONResponse({"error": "that link did not work"}, 401)

    r = JSONResponse({"ok": True})
    session.set_session(
        r, session.Tokens(access, refresh, max(claims.seconds_left, 60)))
    return r


@app.post("/auth/sign-out")
def sign_out(request: Request) -> JSONResponse:
    token = request.cookies.get(session.ACCESS)
    if token:
        auth().sign_out(token)
    r = JSONResponse({"ok": True})
    session.clear_session(r)
    return r


@app.get("/api/me")
def me(request: Request) -> JSONResponse:
    """Who is signed in, and do they have an organisation yet?

    The second half decides which screen the browser shows next, so it
    is answered here rather than inferred from an empty data response --
    a new account and a broken query would otherwise look the same.
    """
    claims = who(request)
    if claims is None:
        return JSONResponse({"signed_in": False})
    orgs = []
    try:
        from engine.db.supabase import NotConfigured, database

        with database().as_user(claims.user_id) as conn:
            orgs = [
                {"id": str(i), "name": n}
                for i, n in conn.execute(
                    "select id, name from public.organisations order by name"
                ).fetchall()
            ]
    except NotConfigured:
        pass
    except Exception:
        # A database that is down must not look like a signed-out user,
        # or the page bounces them to the login screen for a fault that
        # has nothing to do with their session.
        return JSONResponse(
            {"signed_in": True, "email": claims.email, "organisations": None,
             "detail": "the database is unreachable"})
    return JSONResponse({"signed_in": True, "email": claims.email,
                         "organisations": orgs})


@app.post("/api/organisation")
async def create_organisation(request: Request,
                              claims: Claims = Depends(require)) -> JSONResponse:
    """Start an organisation. The creator becomes its owner.

    The membership is not written here: a trigger in migration 0001 does
    it, inside the same transaction as the insert. Doing it in two
    statements from the application would leave an organisation with no
    members whenever the second one failed, and the owner locked out of
    the thing they just made.
    """
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "Give the business a name."}, 400)
    from engine.db.supabase import database

    with database().as_user(claims.user_id) as conn:
        row = conn.execute(
            "insert into public.organisations (name, created_by) "
            "values (%s, auth.uid()) returning id, name",
            (name,),
        ).fetchone()
    return JSONResponse({"id": str(row[0]), "name": row[1]})


# An import is two requests on purpose: see what it will do, then do
# it. The file is sent twice rather than parked on the server between
# them -- server-side state would need a lifetime, a cleanup and a
# story about what happens when two tabs upload at once, and re-reading
# a spreadsheet costs milliseconds.

MAX_UPLOAD = 12 * 1024 * 1024


async def _read_upload(request: Request) -> bytes:
    form = await request.form()
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(400, "No file was sent.")
    data = await up.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(
            413, f"That file is larger than {MAX_UPLOAD // (1024*1024)} MB.")
    return data


@app.post("/api/import/preview")
async def import_preview(request: Request,
                         claims: Claims = Depends(require)) -> JSONResponse:
    """Read the workbook and report what it would do. Writes nothing."""
    from engine.importer import BadWorkbook, plan

    data = await _read_upload(request)
    try:
        p = plan(data)
    except BadWorkbook as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    return JSONResponse(p.as_dict())


@app.post("/api/import/commit")
async def import_commit(request: Request,
                        claims: Claims = Depends(require)) -> JSONResponse:
    """Write it. One transaction, so a failure leaves nothing behind."""
    from engine.db.supabase import NotConfigured, database
    from engine.importer import (BadWorkbook, NoOrganisation,
                                 apply, plan)

    data = await _read_upload(request)
    try:
        p = plan(data)
    except BadWorkbook as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    if not p.ok:
        return JSONResponse(
            {"ok": False,
             "error": "This workbook still has problems that must be fixed.",
             **p.as_dict()}, 400)

    try:
        with database().as_user(claims.user_id) as conn:
            made = apply(conn, p)
    except NoOrganisation as exc:
        return JSONResponse({"ok": False, "error": str(exc),
                             "needs_organisation": True}, 409)
    except NotConfigured:
        return JSONResponse({"ok": False, "error": "No database is "
                             "configured for this deployment."}, 503)
    except Exception as exc:
        # The message, not just the class name.
        #
        # This handler used to report "TypeError" and nothing else,
        # which is the same as reporting nothing: it names the shape of
        # the problem and withholds the problem. The first real import
        # failed on it and neither of us could say why.
        #
        # The text is redacted because an exception from the database
        # layer can carry a connection string, and a connection string
        # carries a password.
        import logging
        import traceback

        from engine.db.supabase import redact

        logging.getLogger("landed.import").exception("import failed")
        detail = redact(f"{type(exc).__name__}: {exc}")[:400]
        return JSONResponse(
            {"ok": False,
             "error": f"Nothing was written. The import failed: {detail}",
             "where": redact(traceback.format_exc().strip().splitlines()[-2]
                             if traceback.format_exc().strip() else "")[:200],
             }, 400)
    return JSONResponse({"ok": True, "written": made})


# ------------------------------------------------- money owed, equipment
#
# Separate requests from /api/state, not folded into it. They are
# different modules with different entitlements, and a business that
# does not place machines should not pay the cost of that query on
# every load of the stock screen.


def _th(conn):
    row = conn.execute("select settings from public.organisations "
                       "order by created_at limit 1").fetchone()
    return thresholds_for(row[0] if row else {})


@app.get("/api/owed")
def owed(claims: Claims = Depends(require)) -> JSONResponse:
    """What customers owe, aged, with the intercompany part set aside."""
    from datetime import date as _d

    from engine.db.snapshot import read_invoices
    from engine.db.supabase import database
    from engine.modules import receivables

    try:
        with database().as_user(claims.user_id) as conn:
            invoices = read_invoices(conn)
            out = receivables.evaluate(invoices, _th(conn), _d.today())
            rows = [
                {"invoice": i.invoice_no, "customer": i.customer_name,
                 "due": i.due_date.isoformat(),
                 "days_overdue": max(0, (_d.today() - i.due_date).days),
                 "amount": i.amount, "paid": i.amount_paid,
                 "open": round(i.amount - i.amount_paid, 2),
                 "public_sector": i.is_public_sector,
                 "intercompany": i.is_intercompany}
                for i in invoices if i.amount > i.amount_paid
            ]
    except Exception as exc:
        return _op_error(exc)

    rows.sort(key=lambda r: -r["days_overdue"])
    return JSONResponse({
        "ok": True,
        "outstanding": out.outstanding,
        "overdue": out.overdue,
        "intercompany_excluded": out.intercompany_excluded,
        "dso_days": out.dso_days,
        "target_dso_days": out.target_dso_days,
        "cash_released_at_target": out.cash_released_at_target,
        "buckets": out.buckets,
        "invoices": rows,
    })


@app.get("/api/equipment")
def equipment(claims: Claims = Depends(require)) -> JSONResponse:
    """Placed machines, and what the workshop gave away this year."""
    from datetime import date as _d

    from engine.db.snapshot import read_jobs, read_machines
    from engine.db.supabase import database
    from engine.modules import machines as machines_mod
    from engine.modules import service

    try:
        with database().as_user(claims.user_id) as conn:
            th = _th(conn)
            placements = read_machines(conn)
            jobs = read_jobs(conn)
            judged = machines_mod.evaluate(placements, th, _d.today())
            work = service.evaluate(jobs, th, _d.today())
    except Exception as exc:
        return _op_error(exc)

    return JSONResponse({
        "ok": True,
        "machines": [
            {"key": m.machine_key, "model": m.model,
             "customer": m.customer_name,
             "capital": m.capital_cost,
             "recovered": m.net_recovered,
             "recovery_pct": m.recovery_pct,
             "months_elapsed": m.months_elapsed,
             "payback_months": m.payback_months,
             "months_left": m.months_remaining_on_contract,
             "utilisation": m.utilisation,
             "downtime_days": m.downtime_days_ytd,
             "service_cost": m.service_cost_to_date,
             "shortfall": m.unrecoverable_shortfall,
             "status": getattr(m.status, "value", str(m.status)),
             "why": m.reason}
            for m in judged
        ],
        "service": {
            "jobs": work.jobs, "open": work.jobs_open,
            "hours": work.labour_hours,
            "hours_unbilled": work.labour_hours_unbilled,
            "labour_value_lost": work.labour_value_lost,
            "parts_cost": work.parts_cost,
            "parts_uncharged": work.parts_uncharged,
            "parts_value_lost": work.parts_value_lost,
        },
    })


# --------------------------------------------------------- settings
#
# What a business can change about how its own figures are read. Held
# in organisations.settings, which is jsonb, so a new knob needs no
# migration -- and read back into Thresholds, which is where the
# engine already takes every number it uses.

# Only these, and only as numbers. A settings blob written straight
# into Thresholds would let anything in the column become anything in
# the engine, which is a wide door for a feature this small.
SETTABLE = {
    "target_cover_months": (0.25, 36.0),
    "excess_cover_months": (0.5, 60.0),
    "cost_of_capital_annual": (0.0, 1.0),
    "cash_buffer": (0.0, 1e12),
}


def thresholds_for(settings: dict):
    """Thresholds with this organisation's own numbers in them."""
    from engine.schema import Thresholds

    clean = {}
    for key, (lo, hi) in SETTABLE.items():
        if key not in (settings or {}):
            continue
        try:
            v = float(settings[key])
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            clean[key] = v
    # Nonsense is refused rather than clamped: an excess limit below
    # the reorder level means every line is at once too little and too
    # much, and silently swapping them would be the software deciding
    # what the business meant.
    if clean.get("excess_cover_months", 6.0) <= clean.get(
            "target_cover_months", 3.0):
        clean.pop("excess_cover_months", None)
        clean.pop("target_cover_months", None)
    return Thresholds(**clean)


@app.get("/api/settings")
def read_settings(claims: Claims = Depends(require)) -> JSONResponse:
    from engine.db.supabase import database

    with database().as_user(claims.user_id) as conn:
        row = conn.execute(
            "select name, country, currency, settings from "
            "public.organisations order by created_at limit 1").fetchone()
        branches = [
            {"code": c, "name": n, "city": city or ""}
            for c, n, city in conn.execute(
                "select code, name, city from public.locations "
                "where is_active order by code").fetchall()]
    if not row:
        return JSONResponse({"error": "no organisation"}, 409)
    return JSONResponse({
        "name": row[0], "country": row[1], "currency": row[2],
        "settings": row[3] or {},
        "defaults": {k: getattr(thresholds_for({}), k) for k in SETTABLE},
        "limits": {k: list(v) for k, v in SETTABLE.items()},
        "branches": branches,
        "email": claims.email,
    })


@app.post("/api/settings")
async def write_settings(request: Request,
                         claims: Claims = Depends(require)) -> JSONResponse:
    from engine.db.supabase import database

    body = await request.json()
    name = str(body.get("name", "")).strip()
    incoming = body.get("settings") or {}

    keep, rejected = {}, []
    for key, (lo, hi) in SETTABLE.items():
        if key not in incoming or incoming[key] in ("", None):
            continue
        try:
            v = float(incoming[key])
        except (TypeError, ValueError):
            rejected.append(f"{key} is not a number")
            continue
        if not (lo <= v <= hi):
            rejected.append(f"{key} has to be between {lo:g} and {hi:g}")
            continue
        keep[key] = v

    target = keep.get("target_cover_months", 3.0)
    excess = keep.get("excess_cover_months", 6.0)
    if excess <= target:
        rejected.append(
            "the standing-still limit has to be above the cover target, or "
            "every line is at once too little and too much")
    if rejected:
        return JSONResponse({"ok": False, "problems": rejected}, 400)

    import json as _json

    with database().as_user(claims.user_id) as conn:
        conn.execute(
            "update public.organisations set settings = %s"
            + (", name = %s" if name else "")
            + " where id = (select id from public.organisations "
              "order by created_at limit 1)",
            (_json.dumps(keep), name) if name else (_json.dumps(keep),))
    return JSONResponse({"ok": True, "settings": keep})


# ------------------------------------------------------- operations
#
# The four things that happen to stock in a day. Each is a row in the
# ledger and nothing else, so each is a POST that either writes one or
# says plainly why it will not.


def _ops(claims: Claims):
    from engine.db.supabase import database

    return database().as_user(claims.user_id)


def _op_error(exc) -> JSONResponse:
    from engine.operations import NotAllowed

    if isinstance(exc, NotAllowed):
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    from engine.db.supabase import redact

    return JSONResponse(
        {"ok": False,
         "error": f"Nothing was recorded: {redact(str(exc))[:200]}"}, 400)


@app.post("/api/movement")
async def movement(request: Request,
                   claims: Claims = Depends(require)) -> JSONResponse:
    """Received, sold, issued, returned, written off."""
    from engine.operations import record

    b = await request.json()
    try:
        with _ops(claims) as conn:
            out = record(conn, str(b.get("sku", "")), str(b.get("branch", "")),
                         str(b.get("kind", "")), b.get("qty"),
                         unit_cost=b.get("unit_cost"),
                         note=str(b.get("note", "")))
    except Exception as exc:
        return _op_error(exc)
    return JSONResponse({"ok": True, **out})


@app.post("/api/adjust")
async def adjust_count(request: Request,
                       claims: Claims = Depends(require)) -> JSONResponse:
    """A stock take. Takes the count, not the difference."""
    from engine.operations import adjust

    b = await request.json()
    try:
        with _ops(claims) as conn:
            out = adjust(conn, str(b.get("sku", "")), str(b.get("branch", "")),
                         b.get("counted"), note=str(b.get("note", "")))
    except Exception as exc:
        return _op_error(exc)
    return JSONResponse({"ok": True, **out})


@app.post("/api/transfer")
async def transfer_stock(request: Request,
                         claims: Claims = Depends(require)) -> JSONResponse:
    """Between two branches: two rows, both or neither."""
    from engine.operations import transfer

    b = await request.json()
    try:
        with _ops(claims) as conn:
            out = transfer(conn, str(b.get("sku", "")),
                           str(b.get("from", "")), str(b.get("to", "")),
                           b.get("qty"), note=str(b.get("note", "")))
    except Exception as exc:
        return _op_error(exc)
    return JSONResponse({"ok": True, **out})


@app.get("/api/line/history")
def line_history(request: Request, sku: str, branch: str,
                 claims: Claims = Depends(require)) -> JSONResponse:
    """What has happened to this line. The panel asks for it on open."""
    from engine.operations import history

    try:
        with _ops(claims) as conn:
            return JSONResponse({"ok": True,
                                 "movements": history(conn, sku, branch)})
    except Exception as exc:
        return _op_error(exc)


@app.get("/api/state")
def state(request: Request) -> dict:
    """Everything the screen needs, in one response.

    One request rather than six, because every view is a different cut
    of the same question. Six requests would be six chances for the cuts
    to disagree with each other on screen.

    Who is asking decides what comes back. A visitor gets the sample
    importer, which is how the product can be looked at before anyone
    has an account. Someone signed in gets their own rows -- including
    when there are none, which is the whole point: showing a new
    customer a fictional company's healthy numbers and calling it their
    dashboard is the worst of the three outcomes, because it is the one
    they might believe.
    """
    from datetime import date as _date

    claims = who(request)
    if claims is None:
        return api.state_payload()

    try:
        from engine.db.snapshot import read
        from engine.db.supabase import NotConfigured, database

        with database().as_user(claims.user_id) as conn:
            snap = read(conn, _date.today())
            row = conn.execute(
                "select settings from public.organisations "
                "order by created_at limit 1").fetchone()
            th = thresholds_for(row[0] if row else {})
    except NotConfigured:
        return api.state_payload()
    except Exception:
        # A database that is down must say so rather than quietly
        # falling back to the sample importer, which would show a
        # signed-in person someone else's figures and no warning.
        return {"error": "the database is unreachable", "source": "Error",
                "stock": [], "shipments": [], "decisions": [],
                "branches": ["ALL"], "line": {}, "currency": "PKR",
                "asof": _date.today().isoformat()}

    if snap.is_empty:
        return api.empty_payload(_date.today(), snap.currency)

    return api.state_payload(as_of=_date.today(), items=snap.items,
                             demand=snap.demand, source="Your data",
                             currency=snap.currency, thresholds=th)


@app.get("/api/workbook.xlsx")
def workbook() -> Response:
    """The whole model as a spreadsheet.

    Still here, and still worth having: it is how a business sees the
    arithmetic before it trusts the software, and how one with a year of
    history gets that history in without typing it twice.
    """
    from tools import build_workbook

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "landed-model.xlsx"
        build_workbook.main(path)
        body = path.read_bytes()

    return Response(
        content=body,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": 'attachment; filename="landed-model.xlsx"'},
    )


@app.get("/sign-in", response_class=HTMLResponse)
def sign_in_page(request: Request) -> Response:
    """Already signed in? Then this page is not what they wanted."""
    if who(request) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "sign-in.html")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    """The app, or the gate.

    Decided on the server. Serving the app shell and letting it discover
    it has no session means a signed-out visitor watches the interface
    appear and then get taken away, and for a moment the page is asking
    the browser to render a tenant's screen with no tenant behind it.

    When auth is not configured at all the app still opens, on sample
    data, so the thing can be looked at before Supabase exists.
    """
    if auth().configured and who(request) is None:
        return RedirectResponse("/sign-in", status_code=303)
    return FileResponse(STATIC / "index.html")
