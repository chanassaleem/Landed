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
    """Read the workbook and report what it would do. Writes nothing.

    Two halves. The plan is what is in the file and whether it is
    coherent; the inspection is how it compares to what is already
    recorded. The second is what somebody actually wants before
    pressing commit, because the file they are holding is very often
    one they have already imported -- a second click, or a corrected
    version with two edits buried in five thousand unchanged rows.

    The comparison needs the database, so it is skipped when the plan
    is not worth committing anyway, and a failure to produce it never
    blocks the import: the commit's own conflict clauses are what
    actually prevent a duplicate, and this is advice.
    """
    from engine.importer import BadWorkbook, inspect, plan

    data = await _read_upload(request)
    try:
        p = plan(data)
    except BadWorkbook as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)

    payload = p.as_dict()
    if p.ok:
        from engine.db.supabase import NotConfigured, database

        try:
            with database().as_user(claims.user_id) as conn:
                payload["against_recorded"] = inspect(conn, p)
        except NotConfigured:
            payload["against_recorded"] = None
        except Exception as exc:
            payload["against_recorded"] = None
            payload["against_recorded_error"] = str(exc)[:200]
    return JSONResponse(payload)


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
#
# Four of the engine's twenty-three numbers used to be here, which
# meant the other nineteen were settable in the dataclass and
# unreachable from the product -- a forecast's smoothing constants, the
# payroll minimum wage, the cash horizon. They are all here now, each
# with the module it moves, so a person can find the one that explains
# a figure they disagree with.
#
# `kind` is not decoration. Thresholds holds ints for the fields that
# index or iterate -- cash_horizon_weeks reaches a range(), target_dso
# days a timedelta -- and a float arriving there is a TypeError deep in
# a module rather than a rejection at the edge. The old table held only
# floats, so this never came up and would have the first time one of
# the week counts was exposed.


class Setting:
    """One settable number: its bounds, its type, and what it moves."""

    __slots__ = ("lo", "hi", "kind", "label", "help", "module", "advanced")

    def __init__(self, lo, hi, kind=float, label="", help="",
                 module="", advanced=True):
        self.lo, self.hi, self.kind = lo, hi, kind
        self.label, self.help, self.module = label, help, module
        self.advanced = advanced

    def clean(self, raw):
        """The value, or a sentence saying why not. Never a clamp.

        Clamping is the software deciding what the business meant. A
        person who types 400% for the cost of capital has made a
        mistake worth telling them about, and silently storing 100%
        hides it until a figure they cannot explain shows up.
        """
        try:
            v = self.kind(float(raw))
        except (TypeError, ValueError):
            return None, f"{self.label or 'that'} is not a number"
        if not (self.lo <= v <= self.hi):
            return None, (f"{self.label or 'that'} has to be between "
                          f"{self.lo:g} and {self.hi:g}")
        return v, None


SETTABLE: dict[str, Setting] = {
    # ---------------------------------------------------- stock policy
    "target_cover_months": Setting(
        0.25, 36.0, float, "Cover you aim to hold", module="stock",
        advanced=False,
        help="Months of demand a branch should have on the shelf."),
    "excess_cover_months": Setting(
        0.5, 60.0, float, "Standing-still limit", module="stock",
        advanced=False,
        help="Cover above which stock counts as money standing still."),
    "min_transfer_value": Setting(
        0.0, 1e12, float, "Smallest transfer worth making", module="stock",
        help="A move has paperwork and breakage risk, so below this the "
             "engine does not suggest it."),
    "sender_min_cover_months": Setting(
        0.0, 36.0, float, "Floor a sending branch keeps", module="stock",
        help="A branch is never stripped below this to rescue another."),
    "abc_a_cutoff": Setting(
        0.5, 0.99, float, "Where the A class ends", module="stock",
        help="Share of consumption value that counts as the A lines."),
    "abc_b_cutoff": Setting(
        0.6, 0.999, float, "Where the B class ends", module="stock"),
    "clinical_critical_floor": Setting(
        0.5, 0.9999, float, "Service level for critical lines",
        module="stock",
        help="Clinical criticality raises a service target and never "
             "lowers it."),
    # ------------------------------------------------------- forecasting
    "forecast_horizon_months": Setting(
        1, 36, int, "How far ahead to forecast", module="demand"),
    "min_periods_for_seasonality": Setting(
        6, 120, int, "History needed before fitting seasonality",
        module="demand",
        help="Two full cycles. Below this a seasonal pattern is a fit to "
             "noise that will read as insight."),
    "holt_alpha": Setting(
        0.01, 1.0, float, "Level smoothing", module="demand",
        help="Higher reacts faster to the last few months and is noisier."),
    "holt_beta": Setting(
        0.01, 1.0, float, "Trend smoothing", module="demand"),
    "croston_alpha": Setting(
        0.01, 1.0, float, "Smoothing for intermittent lines",
        module="demand",
        help="Used by the Syntetos-Boylan method on lines that sell in "
             "bursts."),
    # -------------------------------------------------------------- cash
    "cost_of_capital_annual": Setting(
        0.0, 1.0, float, "What money costs you a year", module="cash",
        advanced=False,
        help="Prices the delay in the ageing book and the stock you hold."),
    "cash_buffer": Setting(
        0.0, 1e15, float, "Cash you will not spend down to", module="cash",
        advanced=False,
        help="No order is ever recommended that eats into this."),
    "cash_horizon_weeks": Setting(
        4, 52, int, "Weeks the cash view covers", module="cash"),
    "overdue_catchup_weeks": Setting(
        1, 26, int, "Weeks to spread the overdue book across",
        module="cash",
        help="An invoice already past its customer's own average "
             "lateness is spread rather than expected this week."),
    "lc_payment_lag_days": Setting(
        0, 180, int, "Days before an order's money leaves", module="cash",
        help="A letter of credit is opened at close to full value days "
             "after the order, not on arrival."),
    # ------------------------------------------------------- receivables
    "target_dso_days": Setting(
        1, 365, int, "Collection days you aim for", module="money",
        advanced=False,
        help="What the ageing book would release if you hit it."),
    # ------------------------------------------- service and equipment
    "labour_rate_per_hour": Setting(
        0.0, 1e7, float, "An engineer's hour", module="service",
        advanced=False,
        help="Prices the hours worked that nobody billed."),
    "payback_target_months": Setting(
        1, 120, int, "Payback you expect on a placement",
        module="equipment", advanced=False),
    "placement_min_months_to_judge": Setting(
        1, 36, int, "Before a placement is judged", module="equipment",
        help="A rate extrapolated from a few weeks of pull is noise, so "
             "a new machine reads as ramping up rather than failing."),
    # ----------------------------------------------------------- payroll
    "minimum_wage_monthly": Setting(
        0.0, 1e7, float, "Monthly minimum wage", module="people",
        advanced=False,
        help="Provincial, reset each year, and EOBI is charged on it "
             "rather than on pay -- so one wrong figure moves every "
             "employee's contribution at once."),
    "social_security_wage_ceiling": Setting(
        0.0, 1e7, float, "Social security wage ceiling", module="people",
        help="Zero applies no ceiling, which overstates the cost rather "
             "than understating it."),
}

# Not numbers, so not range-checked: each is one of a known set.
SETTABLE_TEXT = {
    "currency": lambda v: (len(v) == 3 and v.isalpha(), v.upper()),
    "scale": lambda v: (v in ("indian", "western"), v.lower()),
    "country": lambda v: (len(v) == 2 and v.isalpha(), v.upper()),
}


def thresholds_for(settings: dict):
    """Thresholds with this organisation's own numbers in them."""
    from engine.schema import Thresholds

    clean = {}
    for key, spec in SETTABLE.items():
        if key not in (settings or {}):
            continue
        value, _ = spec.clean(settings[key])
        if value is not None:
            clean[key] = value
    # Nonsense is refused rather than clamped: an excess limit below
    # the reorder level means every line is at once too little and too
    # much, and silently swapping them would be the software deciding
    # what the business meant.
    if clean.get("excess_cover_months", 6.0) <= clean.get(
            "target_cover_months", 3.0):
        clean.pop("excess_cover_months", None)
        clean.pop("target_cover_months", None)
    return Thresholds(**clean)


@app.post("/api/settings/country")
async def apply_country(request: Request,
                        claims: Claims = Depends(require)) -> JSONResponse:
    """Apply a country pack.

    A starting point, written in rather than referenced. Changing a
    pack in the code afterwards must never silently change a figure on
    somebody's screen -- they took a default, they did not subscribe
    to one. Every value it writes stays editable.
    """
    import json as _json

    from engine.country import PACKS, as_settings
    from engine.db.supabase import database

    body = await request.json()
    code = str(body.get("country", "")).upper()
    if code not in PACKS:
        return JSONResponse({"ok": False, "error": f"No pack for {code!r}."},
                            400)

    fresh = as_settings(code)
    with database().as_user(claims.user_id) as conn:
        row = conn.execute(
            "select settings from public.organisations "
            "order by created_at limit 1").fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "no organisation"}, 409)
        # Merged, not replaced. Anything already tuned by hand survives
        # applying a pack; only what the pack covers is overwritten.
        merged = dict(row[0] or {})
        merged.update(fresh)
        conn.execute(
            "update public.organisations set settings = %s, currency = %s, "
            "country = %s where id = (select id from public.organisations "
            "order by created_at limit 1)",
            (_json.dumps(merged), fresh["currency"], code))
    return JSONResponse({"ok": True, "applied": fresh})


@app.get("/api/countries")
def countries() -> JSONResponse:
    """Every pack, for the dropdown. Public: it is a list of countries."""
    from engine.country import listing

    return JSONResponse({"countries": listing()})


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
        "fields": {
            k: {"lo": s.lo, "hi": s.hi,
                "kind": "int" if s.kind is int else "float",
                "label": s.label, "help": s.help,
                "module": s.module, "advanced": s.advanced}
            for k, s in SETTABLE.items()
        },
        # Kept for anything already reading it. The shape above carries
        # the same bounds plus what each number is for, which is what
        # makes twenty-three of them navigable rather than a wall.
        "limits": {k: [s.lo, s.hi] for k, s in SETTABLE.items()},
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

    # Two lists, because they answer different questions. `problems`
    # is what the person reads; `rejected` is which field it was, so a
    # screen can mark the input and a caller can match on something
    # more stable than a sentence.
    keep, rejected, bad_fields = {}, [], []
    for key, spec in SETTABLE.items():
        if key not in incoming or incoming[key] in ("", None):
            continue
        value, why = spec.clean(incoming[key])
        if why:
            rejected.append(why)
            bad_fields.append(key)
            continue
        keep[key] = value

    for key, check in SETTABLE_TEXT.items():
        if key not in incoming or not incoming[key]:
            continue
        ok, cleaned = check(str(incoming[key]).strip())
        if not ok:
            rejected.append(f"{key} is not one this app knows")
            bad_fields.append(key)
            continue
        keep[key] = cleaned

    target = keep.get("target_cover_months", 3.0)
    excess = keep.get("excess_cover_months", 6.0)
    if excess <= target:
        rejected.append(
            "the standing-still limit has to be above the cover target, or "
            "every line is at once too little and too much")
        bad_fields += ["target_cover_months", "excess_cover_months"]
    if rejected:
        return JSONResponse(
            {"ok": False, "problems": rejected,
             "rejected": sorted(set(bad_fields))}, 400)

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
            settings = row[0] if row else {}
            th = thresholds_for(settings)
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

    money_scale = (settings or {}).get("scale", "indian")
    currency = (settings or {}).get("currency") or snap.currency
    if snap.is_empty:
        return api.empty_payload(_date.today(), currency, money_scale)

    return api.state_payload(as_of=_date.today(), items=snap.items,
                             demand=snap.demand, source="Your data",
                             currency=currency, thresholds=th,
                             scale=money_scale)


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


# ───────────────────────────────────────────────────────── the platform
#
# One endpoint lists the modules and one runs a module. Both read the
# registry in engine/platform.py rather than a list of their own, so a
# module cannot exist in the rail and not in the API, or the reverse.


def _standing(conn, claims) -> tuple[str, set[str], dict]:
    """This person's role here, what the organisation has licensed, and
    its settings.

    Role comes from the membership rather than from anything the
    browser sent. A role in a request body is a role the browser can
    edit, and the modules this gates include payroll and the
    statements.

    An organisation with no entitlement rows at all is treated as
    having licensed everything. That is deliberate for now: billing
    does not exist, so an empty table means nobody has sold them
    anything yet rather than that they bought nothing, and the
    alternative is a product that shows a new account thirteen locked
    doors.
    """
    row = conn.execute(
        "select m.role from public.memberships m"
        "  join public.organisations o on o.id = m.org_id"
        " where m.user_id = auth.uid() and m.status = 'active'"
        " order by o.created_at limit 1"
    ).fetchone()
    role = row[0] if row else "staff"

    licensed = {
        r[0] for r in conn.execute(
            "select module from public.entitlements"
            " where status in ('active','trial')"
            "   and (valid_until is null or valid_until >= current_date)"
        ).fetchall()
    }
    settings = conn.execute(
        "select settings from public.organisations order by created_at limit 1"
    ).fetchone()
    return role, licensed, (settings[0] if settings and settings[0] else {})


@app.get("/api/modules")
def list_modules(claims: Claims = Depends(require)) -> JSONResponse:
    """The rail: every module, with the state that explains it.

    Four states, and the middle two are the point. `ready` has what it
    needs. `waiting` is licensed and permitted but has no figures yet,
    and says which record types are missing. `locked` is not licensed.
    A module the role may not open is not in the response at all --
    somebody who may not read the books should not be told the books
    exist.
    """
    from engine.db.supabase import NotConfigured, database
    from engine.platform import SECTIONS, catalogue

    try:
        with database().as_user(claims.user_id) as conn:
            role, licensed, _ = _standing(conn, claims)
            from engine.db import snapshot
            present = snapshot.present(conn)
            branches = [
                {"code": c, "name": n}
                for c, n in conn.execute(
                    "select code, name from public.locations"
                    " where is_active order by code").fetchall()
            ]
    except NotConfigured:
        return JSONResponse({"error": "the database is not configured"}, 503)
    except Exception as exc:
        return JSONResponse(
            {"error": "the database is unreachable", "detail": str(exc)[:200]},
            503)

    return JSONResponse({
        "role": role,
        "branches": branches,
        "sections": [{"id": i, "label": l} for i, l in SECTIONS],
        "modules": catalogue(
            present=present, role=role,
            licensed=licensed if licensed else None),
        "present": sorted(present),
    })


@app.get("/api/home")
def read_home(branch: str = "",
              claims: Claims = Depends(require)) -> JSONResponse:
    """Every module the role may see, in one figure each.

    One request rather than fourteen, and one Facts instance rather
    than fourteen, because Facts reads each record type once and
    lazily -- so the whole home screen costs roughly what the most
    expensive single module costs, not fourteen times it.

    A module that throws is reported as a module that threw. Fourteen
    runners over whatever happens to be in the database is exactly the
    situation where one bad record should not blank the home screen,
    and a card that says it could not be read is more useful than a
    card that is quietly missing.
    """
    from engine.db.supabase import NotConfigured, database
    from engine.platform import BY_ID, SECTIONS, for_role
    from web.facts import Facts
    from web.modules import headline, run

    try:
        with database().as_user(claims.user_id) as conn:
            role, licensed, settings = _standing(conn, claims)
            from engine.db import snapshot
            present = snapshot.present(conn)
            wanted = (branch or "").strip().upper()
            allowed = [m for m in for_role(role)
                       if not licensed or m.id in licensed]
            facts_all = Facts(
                conn,
                thresholds=thresholds_for(settings),
                currency=str(settings.get("currency") or "PKR"),
                branch=None,
            )
            facts_one = Facts(
                conn,
                thresholds=thresholds_for(settings),
                currency=str(settings.get("currency") or "PKR"),
                branch=wanted or None,
            ) if wanted else facts_all

            cards = []
            for m in allowed:
                card = {
                    "id": m.id, "name": m.name, "title": m.title,
                    "blurb": m.blurb, "section": m.section,
                    "group_level": m.group_level,
                    "missing": list(m.missing(present)),
                }
                if m.missing(present):
                    card["state"] = "waiting"
                    cards.append(card)
                    continue
                facts = facts_all if m.group_level else facts_one
                try:
                    payload = run(m.id, facts)
                    card["head"] = headline(m.id, payload.get("data"))
                    card["state"] = "ready" if card["head"] else "waiting"
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    card["state"] = "broken"
                    card["detail"] = str(exc)[:160]
                cards.append(card)
    except NotConfigured:
        return JSONResponse({"error": "the database is not configured"}, 503)
    except Exception as exc:
        return JSONResponse(
            {"error": "the database is unreachable", "detail": str(exc)[:200]},
            503)

    return JSONResponse({
        "currency": str(settings.get("currency") or "PKR"),
        "branch": wanted or None,
        "sections": [{"id": i, "label": l} for i, l in SECTIONS],
        "cards": cards,
    })


@app.get("/api/module/{module_id}")
def read_module(module_id: str, branch: str = "",
                claims: Claims = Depends(require)) -> JSONResponse:
    """One module's figures.

    The role gate is here rather than only in the rail, because a rail
    that hides a module is a hidden link and not a permission. The
    policies refuse the rows underneath as well, so this is the second
    of two gates and not the only one.
    """
    from engine.db.supabase import NotConfigured, database
    from engine.platform import BY_ID, for_role
    from web.facts import Facts
    from web.modules import run

    if module_id not in BY_ID:
        raise HTTPException(404, f"There is no {module_id} module.")

    try:
        with database().as_user(claims.user_id) as conn:
            role, licensed, settings = _standing(conn, claims)
            if module_id not in {m.id for m in for_role(role)}:
                raise HTTPException(
                    403, f"A {role} cannot open this module.")
            if licensed and module_id not in licensed:
                raise HTTPException(
                    402, f"This organisation has not licensed {module_id}.")
            # A group-level module ignores the branch rather than
            # pretending to honour it. There is no Karachi balance
            # sheet unless somebody keeps one, one bank balance serves
            # the whole business, and a transfer has two ends -- so
            # narrowing those would not make them more accurate, it
            # would make them wrong. The response says which happened,
            # so the screen can state its scope instead of implying
            # one.
            module = BY_ID[module_id]
            wanted = (branch or "").strip().upper()
            applied = wanted if (wanted and not module.group_level) else ""
            facts = Facts(
                conn,
                thresholds=thresholds_for(settings),
                currency=str(settings.get("currency") or "PKR"),
                branch=applied or None,
            )
            payload = run(module_id, facts)
            payload["branch"] = applied or None
            payload["branch_asked"] = wanted or None
            payload["can_narrow"] = not module.group_level
    except NotConfigured:
        return JSONResponse({"error": "the database is not configured"}, 503)
    except HTTPException:
        raise
    except Exception as exc:
        # The message, not the class. Twice now a failure has reached a
        # person as the name of a Python exception, which tells them
        # nothing and tells us only slightly more.
        return JSONResponse(
            {"error": "that module could not be read",
             "detail": str(exc)[:300]}, 500)
    return JSONResponse(payload)


# ───────────────────────────────────────────────── people in the business
#
# An organisation has had one member since migration 0001: whoever
# created it. These five routes are the rest of it.
#
# No email is sent. Sending one needs the Supabase admin key, which is
# server-side only and would make inviting somebody depend on a
# credential this app deliberately does not hold at request time. So an
# invite is a row, and the person inviting shares the sign-up link
# themselves -- which is also honest about what happens next, because
# the invite is claimed by a trigger when that address signs up. The
# response says so rather than letting somebody wait for a mail that
# was never going to arrive.

ROLES = ("owner", "manager", "staff", "accountant")

ROLE_BLURB = {
    "owner": "Everything, including the books, payroll and who else is here.",
    "manager": "Stock, buying, cash, customers and the pipeline. Not the "
               "books or payroll.",
    "accountant": "The books, payroll, cash and what you are owed. Not the "
                  "pipeline.",
    "staff": "Stock, shipments, customers and service. No money.",
}


@app.get("/api/users")
def list_users(claims: Claims = Depends(require)) -> JSONResponse:
    """Who is in this organisation, and who has been asked.

    Members and pending invites in one response because they are one
    list on the screen: somebody invited yesterday who has not signed
    up yet is part of the answer to "who has access".
    """
    from engine.db.supabase import NotConfigured, database
    from engine.platform import for_role

    try:
        with database().as_user(claims.user_id) as conn:
            role, _licensed, _settings = _standing(conn, claims)
            branches = {
                str(i): {"code": c, "name": n}
                for i, c, n in conn.execute(
                    "select id, code, name from public.locations"
                    " where is_active order by code").fetchall()
            }
            members = [
                {
                    "user_id": str(r[0]),
                    "role": r[1],
                    # Addresses come from auth.users, which a tenant
                    # cannot read, so a member who is not the caller
                    # shows as their role and branches until they are
                    # invited again. Said plainly rather than shown as
                    # a blank.
                    "email": claims.email if str(r[0]) == str(
                        claims.user_id) else None,
                    "is_you": str(r[0]) == str(claims.user_id),
                    "locations": [str(x) for x in (r[2] or [])],
                    "status": r[3],
                    "since": r[4].isoformat() if r[4] else None,
                }
                for r in conn.execute(
                    "select user_id, role, location_ids, status, created_at"
                    "  from public.memberships order by created_at"
                ).fetchall()
            ]
            invites = [
                {
                    "id": str(r[0]), "email": r[1], "role": r[2],
                    "locations": [str(x) for x in (r[3] or [])],
                    "invited": r[4].isoformat() if r[4] else None,
                    "expires": r[5].isoformat() if r[5] else None,
                    "expired": r[6],
                }
                for r in conn.execute(
                    "select id, email, role, location_ids, created_at,"
                    "       expires_at, expires_at <= clock_timestamp()"
                    "  from public.invites"
                    " where accepted_at is null and revoked_at is null"
                    " order by created_at desc"
                ).fetchall()
            ]
    except NotConfigured:
        return JSONResponse({"error": "the database is not configured"}, 503)
    except Exception as exc:
        return JSONResponse(
            {"error": "the people list could not be read",
             "detail": str(exc)[:200]}, 503)

    return JSONResponse({
        "role": role,
        "can_invite": role in ("owner", "manager"),
        "can_change": role == "owner",
        "members": members,
        "invites": invites,
        "branches": branches,
        "roles": [
            {"id": r, "blurb": ROLE_BLURB[r],
             "modules": [m.id for m in for_role(r)]}
            for r in ROLES
        ],
    })


@app.post("/api/invite")
async def create_invite(request: Request,
                        claims: Claims = Depends(require)) -> JSONResponse:
    """Ask somebody into this organisation, with a role and a branch scope.

    The address is folded here as well as constrained in the schema,
    because the fold is what makes the invite findable when they sign
    up -- GoTrue does not normalise it, so an invite to `Ahmed@x.test`
    matched against a stored `Ahmed@x.test` would be dead on arrival
    with no error anywhere to explain why they have a login and no
    organisation.
    """
    from engine.db.supabase import database

    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    role = str(body.get("role", "")).strip().lower()
    locations = body.get("locations") or []

    problems = []
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        problems.append("That does not look like an email address.")
    if role not in ROLES:
        problems.append(f"Role has to be one of {', '.join(ROLES)}.")
    if not isinstance(locations, list):
        problems.append("Branches have to be a list.")
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, 400)

    with database().as_user(claims.user_id) as conn:
        # Checked here so the answer is a sentence rather than a
        # constraint name. The policies refuse it as well, which is
        # what actually enforces it.
        standing, _licensed, _settings = _standing(conn, claims)
        if standing not in ("owner", "manager"):
            return JSONResponse(
                {"ok": False,
                 "problems": [f"A {standing} cannot invite people."]}, 403)
        if role == "owner" and standing != "owner":
            return JSONResponse(
                {"ok": False,
                 "problems": ["Only an owner can make somebody an owner."]},
                403)
        try:
            row = conn.execute(
                "insert into public.invites (org_id, email, role,"
                " location_ids, invited_by)"
                " values ((select id from public.organisations"
                "           order by created_at limit 1), %s, %s, %s,"
                "         auth.uid())"
                " returning id, expires_at",
                (email, role, locations),
            ).fetchone()
        except Exception as exc:
            text = str(exc)
            if "invites_live_idx" in text:
                return JSONResponse(
                    {"ok": False,
                     "problems": [f"{email} has already been invited and "
                                  f"has not answered yet."]}, 409)
            return JSONResponse(
                {"ok": False, "problems": ["That invite could not be "
                                           "written."],
                 "detail": text[:200]}, 400)

    return JSONResponse({
        "ok": True, "id": str(row[0]),
        "expires": row[1].isoformat() if row[1] else None,
        # The one thing somebody has to be told, because nothing else
        # will tell them.
        "note": f"No email was sent. Ask {email} to sign up at this "
                f"address with that exact email, and they will land in "
                f"this organisation.",
    })


@app.post("/api/invite/{invite_id}/revoke")
def revoke_invite(invite_id: str,
                  claims: Claims = Depends(require)) -> JSONResponse:
    """Withdraw an invite that has not been accepted.

    Revoked rather than deleted, so the record of having invited
    somebody survives withdrawing it -- and the live-invite index is
    scoped to unrevoked rows, so the same address can be invited again
    afterwards.
    """
    from engine.db.supabase import database

    with database().as_user(claims.user_id) as conn:
        got = conn.execute(
            "update public.invites set revoked_at = clock_timestamp()"
            " where id = %s and accepted_at is null and revoked_at is null"
            " returning email",
            (invite_id,),
        ).fetchone()
    if not got:
        return JSONResponse(
            {"ok": False,
             "error": "That invite is not open, or is not yours to withdraw."},
            404)
    return JSONResponse({"ok": True, "email": got[0]})


@app.post("/api/users/{user_id}/role")
async def change_member(user_id: str, request: Request,
                        claims: Claims = Depends(require)) -> JSONResponse:
    """Change somebody's role or which branches they see.

    An owner cannot act on their own membership: the policies refuse
    it, because the only owner of an organisation demoting themselves
    loses it permanently with nobody left who can let them back in. The
    refusal is translated into a sentence here.
    """
    from engine.db.supabase import database

    body = await request.json()
    role = str(body.get("role", "")).strip().lower()
    locations = body.get("locations")
    if role and role not in ROLES:
        return JSONResponse(
            {"ok": False,
             "problems": [f"Role has to be one of {', '.join(ROLES)}."]}, 400)

    if str(user_id) == str(claims.user_id):
        return JSONResponse(
            {"ok": False,
             "problems": ["You cannot change your own role. Ask another "
                          "owner, so an organisation can never be left "
                          "with nobody who can administer it."]}, 400)

    sets, args = [], []
    if role:
        sets.append("role = %s")
        args.append(role)
    if isinstance(locations, list):
        sets.append("location_ids = %s")
        args.append(locations)
    if not sets:
        return JSONResponse(
            {"ok": False, "problems": ["Nothing to change."]}, 400)

    with database().as_user(claims.user_id) as conn:
        got = conn.execute(
            f"update public.memberships set {', '.join(sets)}"
            " where user_id = %s returning role, location_ids",
            (*args, user_id),
        ).fetchone()
    if not got:
        return JSONResponse(
            {"ok": False,
             "problems": ["That change was refused. Only an owner can "
                          "change a membership."]}, 403)
    return JSONResponse({"ok": True, "role": got[0],
                         "locations": [str(x) for x in (got[1] or [])]})


@app.delete("/api/users/{user_id}")
def remove_member(user_id: str,
                  claims: Claims = Depends(require)) -> JSONResponse:
    """Take somebody out of this organisation.

    Their account survives; only the membership goes. Nothing they
    wrote is removed, because a ledger that loses rows when a person
    leaves is not a ledger.
    """
    from engine.db.supabase import database

    if str(user_id) == str(claims.user_id):
        return JSONResponse(
            {"ok": False,
             "error": "You cannot remove yourself. Ask another owner."}, 400)

    with database().as_user(claims.user_id) as conn:
        got = conn.execute(
            "delete from public.memberships where user_id = %s"
            " returning role", (user_id,),
        ).fetchone()
    if not got:
        return JSONResponse(
            {"ok": False,
             "error": "That person was not removed. Only an owner can."}, 403)
    return JSONResponse({"ok": True, "was": got[0]})


# ──────────────────────────────────────────────── adding one record
#
# Everything in this product arrived one of two ways until now: a
# spreadsheet import, or not at all. That is fine for the first
# afternoon and wrong forever after -- a customer rings, a machine is
# placed, a letter of credit is agreed, and re-importing a workbook to
# record one of them is not a workflow anybody follows.
#
# One declarative table rather than a route per record. Each entry
# names the columns it accepts and how to clean them, so adding a
# record type is a dict entry and not another forty lines that have
# their own ideas about whitespace and missing values. The policies
# decide who may write; this decides what a column can contain.


# The labels in RECORDS below appear inside a sentence -- "X is
# needed", "X has to be a date" -- so they are nouns. The labels a
# person reads on the form itself are in web/static/records.js and are
# free to be conversational: "What it is" is a good field label and a
# bad sentence subject, and "What it is is needed" was what came out
# of sharing one string between the two.


def _txt(v, limit=200):
    s = str(v or "").strip()
    return s[:limit] or None


def _upper(v, limit=40):
    s = _txt(v, limit)
    return s.upper() if s else None


def _lower(v, limit=200):
    s = _txt(v, limit)
    return s.lower() if s else None


def _number(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError("has to be a number")


def _whole(v):
    n = _number(v)
    return None if n is None else int(n)


def _rate(v):
    """A fraction, not a percentage.

    Somebody typing 5 for five per cent is the commonest way to get a
    payroll figure wrong by twenty times, so anything above 1 is
    refused rather than quietly divided by a hundred -- which would be
    the software deciding what they meant.
    """
    n = _number(v)
    if n is None:
        return None
    if not (0 <= n <= 1):
        raise ValueError("is a fraction between 0 and 1, so five per "
                         "cent is 0.05")
    return n


def _flag(v):
    return bool(v)


def _day(v):
    if v in (None, ""):
        return None
    from datetime import date as _date

    try:
        return _date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ValueError("has to be a date, as YYYY-MM-DD")


def _one_of(*allowed):
    def check(v):
        s = _lower(v, 40)
        if s is None:
            return None
        if s not in allowed:
            raise ValueError(f"has to be one of {', '.join(allowed)}")
        return s
    return check


# name -> (table, {column: (label, cleaner, required)}, extra SQL bits)
RECORDS: dict[str, dict] = {
    "item": {
        "table": "public.items",
        "cols": {
            "sku": ("Code", _txt, True),
            "description": ("A description", _txt, True),
            "category": ("Category", _txt, False),
            "uom": ("Unit", _txt, False),
            "barcode": ("Barcode", _txt, False),
            "lead_time_days": ("Lead time", _number, False),
            "lead_time_days_std": ("Lead-time variation", _number, False),
            "is_clinical_critical": ("Clinically critical", _flag, False),
        },
        "unique": "sku",
    },
    "location": {
        "table": "public.locations",
        "cols": {
            "code": ("Code", _upper, True),
            "name": ("Name", _txt, True),
            "city": ("City", _txt, False),
            "is_warehouse": ("Warehouse", _flag, False),
        },
        "unique": "code",
    },
    "supplier": {
        "table": "public.suppliers",
        "cols": {
            "code": ("Code", _upper, True),
            "name": ("Name", _txt, True),
            "country": ("Country", _upper, False),
            "currency": ("Currency", _upper, False),
            "lead_time_days": ("Lead time", _number, False),
        },
        "unique": "code",
    },
    "customer": {
        "table": "public.customers",
        "cols": {
            "code": ("Code", _upper, True),
            "name": ("Name", _txt, True),
            "city": ("City", _txt, False),
            "payment_terms_days": ("Terms", _whole, False),
            "credit_limit": ("Credit limit", _number, False),
            "is_public_sector": ("Public sector", _flag, False),
            "is_intercompany": ("Inter-company", _flag, False),
            "contact_name": ("Contact", _txt, False),
            "contact_phone": ("Phone", _txt, False),
            "contact_email": ("Email", _lower, False),
        },
        "unique": "code",
    },
    "commitment": {
        "table": "public.commitments",
        "cols": {
            "label": ("A label", _txt, True),
            "kind": ("Kind", _one_of("lc_settlement", "loan_repayment",
                                     "payroll", "rent", "tax", "supplier",
                                     "other"), True),
            "due_date": ("Due", _day, True),
            "amount": ("Amount", _number, True),
            "is_recurring": ("Recurring", _flag, False),
            "recurs_every_days": ("Recurrence", _whole, False),
        },
    },
    "cash_balance": {
        "table": "public.cash_balances",
        "cols": {
            "as_at": ("A date", _day, True),
            "balance": ("Balance", _number, True),
            "account_label": ("Account", _txt, False),
        },
        # A balance for a day already recorded is a correction, not a
        # second account.
        "upsert": ("org_id, as_at, account_label", ("balance",)),
    },
    "employee": {
        "table": "public.employees",
        "cols": {
            "employee_no": ("Number", _txt, True),
            "name": ("Name", _txt, True),
            "department": ("Department", _txt, False),
            "basic_monthly": ("Basic", _number, True),
            "allowances_monthly": ("Allowances", _number, False),
            "medical_monthly": ("Medical", _number, False),
            "employer_pf_rate": ("Provident, employer", _rate, False),
            "employee_pf_rate": ("Provident, employee", _rate, False),
            "joined": ("Joined", _day, False),
        },
        "unique": "employee_no",
    },
    "opportunity": {
        "table": "public.opportunities",
        "cols": {
            "reference": ("Reference", _txt, True),
            "stage": ("Stage", _one_of("lead", "qualified", "quoted",
                                       "tender_submitted", "won", "lost"),
                      True),
            "value": ("Value", _number, True),
            "expected_close": ("Expected to close", _day, False),
            "quantity": ("Quantity", _number, False),
            "owner_name": ("Owner", _txt, False),
            "source": ("Source", _txt, False),
        },
        # Looked up by the code a person knows rather than by an id
        # they would have to find first.
        "lookup": {
            "customer_code": ("customer_id", "public.customers", "code"),
            "item_sku": ("item_id", "public.items", "sku"),
        },
        "unique": "reference",
    },
    "interaction": {
        "table": "public.interactions",
        "cols": {
            "happened_on": ("A date", _day, True),
            "channel": ("Channel", _one_of("note", "call", "visit", "email",
                                       "whatsapp", "meeting"), False),
            "summary": ("A summary", _txt, True),
            "next_step": ("Next step", _txt, False),
            "next_step_on": ("Next-step date", _day, False),
        },
        "lookup": {
            "customer_code": ("customer_id", "public.customers", "code"),
        },
        "author": "created_by",
    },
}


@app.post("/api/record/{kind}")
async def add_record(kind: str, request: Request,
                     claims: Claims = Depends(require)) -> JSONResponse:
    """Write one record, or say in words why not.

    Every refusal is a sentence naming the field. A person filling in a
    form should never be shown a constraint name, and a unique
    violation in particular has an obvious meaning worth stating --
    that code is already in use -- which `items_org_id_sku_key` does
    not convey.
    """
    from engine.db.supabase import NotConfigured, database

    spec = RECORDS.get(kind)
    if spec is None:
        raise HTTPException(404, f"There is no {kind} record to add.")

    body = await request.json()
    values, problems = {}, []
    for col, (label, clean, required) in spec["cols"].items():
        raw = body.get(col)
        try:
            got = clean(raw)
        except ValueError as exc:
            problems.append(f"{label} {exc}.")
            continue
        if got is None and required:
            problems.append(f"{label} is needed.")
            continue
        if got is not None:
            values[col] = got
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, 400)

    try:
        with database().as_user(claims.user_id) as conn:
            org = conn.execute(
                "select id from public.organisations"
                " order by created_at limit 1").fetchone()
            if not org:
                return JSONResponse(
                    {"ok": False,
                     "problems": ["Name your business first."]}, 409)
            values["org_id"] = org[0]

            # Codes a person knows, turned into ids they do not have to.
            for field, (col, table, key) in (spec.get("lookup") or {}).items():
                raw = _txt(body.get(field))
                if not raw:
                    continue
                found = conn.execute(
                    f"select id from {table} where upper({key}) = %s",
                    (raw.upper(),)).fetchone()
                if not found:
                    return JSONResponse(
                        {"ok": False,
                         "problems": [f"No record here has the code "
                                      f"{raw}."]}, 400)
                values[col] = found[0]

            if spec.get("author"):
                values[spec["author"]] = claims.user_id

            cols = list(values)
            marks = ", ".join(["%s"] * len(cols))
            sql = (f"insert into {spec['table']} ({', '.join(cols)})"
                   f" values ({marks})")
            if spec.get("upsert"):
                target, updates = spec["upsert"]
                sets = ", ".join(f"{c} = excluded.{c}" for c in updates)
                sql += f" on conflict ({target}) do update set {sets}"
            sql += " returning id"
            try:
                row = conn.execute(sql, tuple(values[c] for c in cols)
                                   ).fetchone()
            except Exception as exc:
                text = str(exc)
                if "duplicate key" in text or "unique" in text.lower():
                    field = spec.get("unique")
                    shown = values.get(field) if field else None
                    return JSONResponse(
                        {"ok": False,
                         "problems": [f"{shown} is already in use."
                                      if shown else
                                      "That record already exists."]}, 409)
                if "violates row-level security" in text:
                    return JSONResponse(
                        {"ok": False,
                         "problems": ["Your role cannot add this."]}, 403)
                if "violates check constraint" in text:
                    return JSONResponse(
                        {"ok": False,
                         "problems": ["One of those values is not one "
                                      "this app allows."]}, 400)
                return JSONResponse(
                    {"ok": False, "problems": ["That could not be saved."],
                     "detail": text[:200]}, 400)
    except NotConfigured:
        return JSONResponse({"error": "the database is not configured"}, 503)

    return JSONResponse({"ok": True, "id": str(row[0]), "kind": kind})


@app.get("/api/export.xlsx")
def export_workbook(claims: Claims = Depends(require)) -> Response:
    """This organisation's own data, in the shape the import reads.

    A business about to put its stock, its cash position and its
    payroll into somebody else's software asks, reasonably, how it
    gets them out again. "Export to CSV" is a thin answer when the
    thing being exported is twenty tables that reference each other,
    so the export is the import workbook: same sheets, same headers,
    same codes in the same columns.

    Which makes the claim checkable rather than reassuring. An export
    that can be imported has been checked, and
    tests/test_export.py round-trips it: export, import, and the
    preview reports that every row is already recorded.

    Read through the signed-in person's own connection, so it holds
    exactly what they are allowed to see. Built as the service role it
    would be a quiet way around every policy in the schema.
    """
    from datetime import date as _date

    from engine import exporter
    from engine.db.supabase import NotConfigured, database

    try:
        with database().as_user(claims.user_id) as conn:
            body = exporter.to_bytes(conn)
            name = conn.execute(
                "select name from public.organisations"
                " order by created_at limit 1").fetchone()
    except NotConfigured:
        return JSONResponse({"error": "the database is not configured"}, 503)
    except Exception as exc:
        return JSONResponse(
            {"error": "the export could not be built",
             "detail": str(exc)[:300]}, 500)

    slug = "".join(
        ch if ch.isalnum() else "-"
        for ch in (name[0] if name else "landed").lower()).strip("-")
    stamp = _date.today().isoformat()
    return Response(
        content=body,
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"),
        headers={
            "Content-Disposition":
                f'attachment; filename="{slug or "landed"}-{stamp}.xlsx"',
        },
    )


@app.post("/api/item/{sku}")
async def edit_item(sku: str, request: Request,
                    claims: Claims = Depends(require)) -> JSONResponse:
    """Correct a fact about an item, in place.

    The question this answers is where data gets entered on a stock
    screen, and the answer is that there are two different acts and
    they must not share a control.

    Correcting a lead time, a description or a selling price is
    editing something somebody wrote down. It has no history worth
    keeping and no arithmetic behind it, so it is typed into the cell.

    Recording that forty units went out is a ledger event. It can
    never be edited, only reversed, because the stock figure is the
    sum of those events and nothing else. Those go through the
    operations -- receive, sell, count, transfer -- which write a row
    rather than change one.

    So `on_hand` and `unit_cost` are not in this list and never will
    be. Typing over either would put a number on screen that no
    movement supports, and the first person to reconcile would find
    stock that exists in the product and nowhere else.
    """
    from engine.db.supabase import database
    from web.modules import EDITABLE_ITEM_FIELDS

    body = await request.json()
    cleaners = {
        "description": _txt, "category": _txt, "uom": _txt,
        "barcode": _txt, "lead_time_days": _number,
        "lead_time_days_std": _number, "is_clinical_critical": _flag,
        "list_price": _number, "is_generic": _flag,
    }

    values, problems = {}, []
    for field, raw in body.items():
        if field not in EDITABLE_ITEM_FIELDS:
            problems.append(
                f"{field} is not something you can type over here."
                if field in ("on_hand", "unit_cost", "position")
                else f"{field} is not a field on an item.")
            continue
        try:
            values[field] = cleaners[field](raw)
        except ValueError as exc:
            problems.append(f"{field} {exc}.")
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, 400)
    if not values:
        return JSONResponse(
            {"ok": False, "problems": ["Nothing to change."]}, 400)

    sets = ", ".join(f"{k} = %s" for k in values)
    with database().as_user(claims.user_id) as conn:
        got = conn.execute(
            f"update public.items set {sets} where sku = %s returning sku",
            (*values.values(), sku),
        ).fetchone()
    if not got:
        return JSONResponse(
            {"ok": False,
             "problems": [f"No item here has the code {sku}, or your "
                          f"role cannot change it."]}, 404)
    return JSONResponse({"ok": True, "sku": got[0], "changed": values})
