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
                             os.environ.get("SUPABASE_ANON_KEY", ""))
    return _auth


def verifier() -> Verifier:
    global _verifier
    if _verifier is None:
        import os

        _verifier = Verifier(os.environ.get("SUPABASE_URL", ""))
    return _verifier


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
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    missing = [n for n, v in (("SUPABASE_URL", url),
                              ("SUPABASE_ANON_KEY", key)) if not v]
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
                                and os.environ.get("SUPABASE_ANON_KEY")),
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
        tokens = a.sign_up(str(body.get("email", "")).strip(), password)
    except session.SignInFailed:
        return JSONResponse({"error": "That did not work. Try another email."},
                            400)
    if tokens is None:
        return JSONResponse({"ok": True, "confirm": True})
    r = JSONResponse({"ok": True, "confirm": False})
    session.set_session(r, tokens)
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


@app.get("/api/state")
def state() -> dict:
    """Everything the screen needs, in one response.

    One request rather than six, because every view is a different cut
    of the same question. Six requests would be six chances for the cuts
    to disagree with each other on screen.
    """
    return api.state_payload()


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
