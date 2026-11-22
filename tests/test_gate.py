"""The sign-in flow, from the outside.

The verifier is tested separately against forgeries. These are about the
wiring around it: that a cookie cannot be read by script, that a visitor
cannot reach tenant routes, that a wrong password and an unknown email
are indistinguishable, and that the app still opens when there is no
Supabase at all.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web import session


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    webapp._auth = None
    webapp._verifier = None
    yield TestClient(webapp.app)
    webapp._auth = None
    webapp._verifier = None


class FakeAuth(session.Auth):
    """Stands in for Supabase Auth. Accepts one account."""

    def __init__(self, **kw):
        super().__init__("https://example.supabase.co", "anon-key")
        self.signed_out = []

    def sign_in(self, email, password):
        if (email, password) != ("real@example.com", "correct-horse"):
            raise session.SignInFailed("rejected")
        return session.Tokens("access-token", "refresh-token", 3600)

    def sign_up(self, email, password):
        return session.Tokens("new-token", "new-refresh", 3600)

    def sign_out(self, token):
        self.signed_out.append(token)


@pytest.fixture
def signed_in(client, monkeypatch):
    """A client holding a real session cookie."""
    fake = FakeAuth()
    monkeypatch.setattr(webapp, "auth", lambda: fake)
    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {
            "verify": staticmethod(lambda t: __import__(
                "engine.supabase_auth", fromlist=["Claims"]).Claims(
                    user_id="33333333-3333-3333-3333-333333333333",
                    email="real@example.com",
                    expires_at=int(time.time()) + 3600))
        })())
    r = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "correct-horse"})
    assert r.status_code == 200
    return client, fake


# ------------------------------------------------------------ the cookie

def test_the_session_cookie_cannot_be_read_by_script(client, monkeypatch):
    """HttpOnly is the whole reason the token lives on the server.

    Without it a single injected script reads a working token for a
    business's inventory and cash position and posts it anywhere.
    """
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "correct-horse"})
    assert r.status_code == 200
    raw = r.headers.get_list("set-cookie")
    access = [c for c in raw if c.startswith(f"{session.ACCESS}=")]
    assert access, raw
    assert "HttpOnly" in access[0]
    assert "SameSite=lax" in access[0].replace("samesite", "SameSite")


def test_no_token_ever_reaches_the_response_body(client, monkeypatch):
    """Not in the JSON either -- a body is readable by script, so putting
    the token there would undo the cookie."""
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "correct-horse"})
    assert "access-token" not in r.text
    assert "refresh-token" not in r.text


def test_cookies_are_marked_secure_in_production(monkeypatch):
    monkeypatch.setenv("LANDED_ENV", "production")
    assert session.secure_cookies() is True
    monkeypatch.setenv("LANDED_ENV", "")
    assert session.secure_cookies() is False


# ------------------------------------------------------------ the answers

def test_a_wrong_password_and_an_unknown_email_read_identically(
    client, monkeypatch
):
    """Otherwise the form is an oracle for whether an address has an
    account here, one request at a time."""
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    a = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "wrong"})
    b = client.post("/auth/sign-in", json={"email": "nobody@example.com",
                                           "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_a_short_password_is_refused_before_the_round_trip(client, monkeypatch):
    called = []

    class Watch(FakeAuth):
        def sign_up(self, email, password):
            called.append(email)
            return super().sign_up(email, password)

    monkeypatch.setattr(webapp, "auth", lambda: Watch())
    r = client.post("/auth/sign-up", json={"email": "a@b.com", "password": "short"})
    assert r.status_code == 400
    assert not called


# ------------------------------------------------------------- the guard

def test_a_visitor_is_sent_to_the_gate_not_shown_the_shell(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sign-in"


def test_a_visitor_cannot_create_an_organisation(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/api/organisation", json={"name": "Acme"})
    assert r.status_code == 401


def test_a_tampered_cookie_is_not_a_session(client, monkeypatch):
    """The cookie holds a real JWT, so an attacker editing it has to
    forge a signature. The verifier is what notices; this proves the
    route actually consults it rather than reading the cookie."""
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    client.cookies.set(session.ACCESS, "made.up.token")
    r = client.get("/api/me")
    assert r.json() == {"signed_in": False}
    r = client.post("/api/organisation", json={"name": "Acme"})
    assert r.status_code == 401


def test_signing_in_then_out_leaves_no_session(signed_in):
    client, fake = signed_in
    assert client.get("/api/me").json()["signed_in"] is True
    client.post("/auth/sign-out")
    assert fake.signed_out == ["access-token"]
    assert client.get("/api/me").json()["signed_in"] is False


def test_the_gate_redirects_someone_already_signed_in(signed_in):
    client, _ = signed_in
    r = client.get("/sign-in", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ------------------------------------------------- before Supabase exists

def test_the_app_still_opens_with_no_auth_configured(monkeypatch):
    """So the thing can be looked at and demonstrated before anyone has
    created a project."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    webapp._auth = None
    webapp._verifier = None
    c = TestClient(webapp.app)
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/api/state").status_code == 200
    webapp._auth = None


def test_sign_in_says_so_when_auth_is_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    webapp._auth = None
    c = TestClient(webapp.app)
    r = c.post("/auth/sign-in", json={"email": "a@b.com", "password": "x" * 12})
    assert r.status_code == 503
    webapp._auth = None


# ------------------------------------------------- saying so when it is off

def test_health_auth_names_the_missing_variable(monkeypatch):
    """The failure this reports is a silent one: with no anon key the
    app does not break, it drops the gate and serves sample data, which
    looks exactly like a build where login was never written."""
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    webapp._auth = None
    d = TestClient(webapp.app).get("/health/auth").json()
    assert d["sign_in_enabled"] is False
    assert d["missing"] == ["SUPABASE_PUBLISHABLE_KEY (or SUPABASE_ANON_KEY)"]
    assert "SUPABASE_PUBLISHABLE_KEY" in d["detail"]
    webapp._auth = None


def test_health_auth_never_reports_a_key_value(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "super-secret-anon-value")
    webapp._auth = None
    r = TestClient(webapp.app).get("/health/auth")
    assert r.json()["sign_in_enabled"] is True
    assert "super-secret-anon-value" not in r.text
    webapp._auth = None


def test_config_tells_the_page_whether_to_offer_a_login(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    webapp._auth = None
    c = TestClient(webapp.app)
    assert c.get("/api/config").json()["auth_configured"] is False
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    assert c.get("/api/config").json()["auth_configured"] is True
    webapp._auth = None


def test_the_version_is_reported_so_a_stale_deploy_is_visible(monkeypatch):
    """A deploy that fails leaves the previous image serving. Everything
    answers normally and the new feature is simply absent, which reads
    as a bug in the feature rather than a deploy that never landed.
    """
    r = TestClient(webapp.app).get("/health")
    assert r.json()["version"] == webapp.__version__
    assert TestClient(webapp.app).get("/health/auth").json()["version"] == \
        webapp.__version__


# ------------------------------------------------------ the new key scheme

def test_either_key_name_switches_sign_in_on(monkeypatch):
    """Supabase is retiring the anon key for a publishable one.

    A project created today has only the new key, so reading just
    SUPABASE_ANON_KEY would leave sign-in off with no way to switch it
    on. An older project has only the old one. Both must work.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    for name in ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY"):
        for other in ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY"):
            monkeypatch.delenv(other, raising=False)
        monkeypatch.setenv(name, "sb_publishable_example")
        webapp._auth = None
        d = TestClient(webapp.app).get("/health/auth").json()
        assert d["sign_in_enabled"] is True, f"{name} was not read"
    webapp._auth = None


def test_the_publishable_key_wins_when_both_are_present(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "legacy-jwt")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_new")
    assert session.public_key() == "sb_publishable_new"


def test_what_is_missing_names_the_current_key_first(monkeypatch):
    """Sending someone to look for an anon key in a project that has
    none is a wasted trip through the dashboard."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    webapp._auth = None
    d = TestClient(webapp.app).get("/health/auth").json()
    assert d["missing"] == ["SUPABASE_PUBLISHABLE_KEY (or SUPABASE_ANON_KEY)"]
    webapp._auth = None


# ------------------------------------------------- the confirmation link

class ConfirmingAuth(FakeAuth):
    """Accepts one token hash, as Supabase would accept one link once."""

    def __init__(self, good="good-hash"):
        super().__init__()
        self.good, self.seen = good, []

    def verify_otp(self, token_hash, kind="signup"):
        self.seen.append((token_hash, kind))
        if token_hash != self.good:
            raise session.SignInFailed("link rejected")
        return session.Tokens("confirmed-token", "confirmed-refresh", 3600)


def test_a_good_confirmation_link_signs_the_person_straight_in(
    client, monkeypatch
):
    """They have just proved they own the address. Asking for the
    password again at that exact moment is a step for its own sake."""
    fake = ConfirmingAuth()
    monkeypatch.setattr(webapp, "auth", lambda: fake)
    r = client.get("/auth/confirm?token_hash=good-hash&type=signup",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert any(c.startswith(f"{session.ACCESS}=")
               for c in r.headers.get_list("set-cookie"))
    assert fake.seen == [("good-hash", "signup")]


def test_no_token_ever_appears_in_a_url(client, monkeypatch):
    """The whole reason for redeeming the link on the server.

    A session in a URL is kept: browser history, the referrer header,
    a screenshot in a bug report. The redirect after confirming must be
    a plain path.
    """
    monkeypatch.setattr(webapp, "auth", lambda: ConfirmingAuth())
    r = client.get("/auth/confirm?token_hash=good-hash&type=signup",
                   follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "token" not in r.headers["location"]
    assert "confirmed-token" not in r.text


def test_a_used_or_expired_link_says_which(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: ConfirmingAuth())
    r = client.get("/auth/confirm?token_hash=stale&type=signup",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sign-in?problem=expired"


def test_an_unknown_link_type_is_not_passed_to_the_auth_server(
    client, monkeypatch
):
    """The value goes straight on to Supabase, so it is allow-listed
    rather than forwarded and reasoned about."""
    fake = ConfirmingAuth()
    monkeypatch.setattr(webapp, "auth", lambda: fake)
    r = client.get("/auth/confirm?token_hash=good-hash&type=whatever",
                   follow_redirects=False)
    assert r.headers["location"] == "/sign-in?problem=link"
    assert fake.seen == []


def test_a_link_with_no_token_is_not_a_crash(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: ConfirmingAuth())
    r = client.get("/auth/confirm", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sign-in?problem=link"


# ------------------------------------------------ the default-template door

def test_the_session_endpoint_refuses_a_token_it_cannot_verify(
    client, monkeypatch
):
    """This endpoint is public and takes a token from whoever posts one.

    If it trusted what it was given, anybody could hand it a string
    naming any user and be issued a working session cookie for them.
    The signature check is the entire security of this route.
    """
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/auth/session",
                    json={"access_token": "made.up.token",
                          "refresh_token": "also-made-up"})
    assert r.status_code == 401
    assert not any(c.startswith(f"{session.ACCESS}=")
                   for c in r.headers.get_list("set-cookie"))


def test_a_verified_token_becomes_an_httponly_cookie(client, monkeypatch):
    import engine.supabase_auth as sa

    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    monkeypatch.setattr(webapp, "verifier", lambda: type("V", (), {
        "verify": staticmethod(lambda t: sa.Claims(
            user_id="55555555-5555-5555-5555-555555555555",
            email="d@example.com", expires_at=int(time.time()) + 3600))
    })())
    r = client.post("/auth/session",
                    json={"access_token": "real", "refresh_token": "r"})
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith(f"{session.ACCESS}=") and "HttpOnly" in c
               for c in cookies)


def test_sign_up_sends_the_redirect_as_a_query_parameter(client, monkeypatch):
    """GoTrue ignores redirect_to in the JSON body and falls back to the
    Site URL -- which defaults to localhost, so the only symptom is a
    confirmation email pointing at a machine that is not running."""
    seen = {}

    class Watch(FakeAuth):
        def sign_up(self, email, password, redirect_to=""):
            seen["redirect_to"] = redirect_to
            return None

    monkeypatch.setattr(webapp, "auth", lambda: Watch())
    monkeypatch.setenv("LANDED_BASE_URL", "https://landed.example.com")
    r = client.post("/auth/sign-up",
                    json={"email": "a@b.com", "password": "x" * 12})
    assert r.json()["confirm"] is True
    assert seen["redirect_to"] == "https://landed.example.com/auth/landing"


def test_the_redirect_url_carries_no_query_string_of_its_own(
    client, monkeypatch
):
    """GoTrue truncates a redirect_to at its first query parameter by
    the time it reaches the email, so the URL has to be a bare path."""
    seen = {}

    class Watch(FakeAuth):
        def sign_up(self, email, password, redirect_to=""):
            seen["redirect_to"] = redirect_to
            return None

    monkeypatch.setattr(webapp, "auth", lambda: Watch())
    client.post("/auth/sign-up", json={"email": "a@b.com", "password": "x" * 12})
    assert "?" not in seen["redirect_to"]


def test_the_base_url_is_https_behind_a_proxy(monkeypatch):
    """Render terminates TLS, so the request arrives as http. A
    redirect_to that does not match the allow-list exactly is discarded
    without a word, and http:// against an https:// entry does not
    match."""
    monkeypatch.delenv("LANDED_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://landed.onrender.com")
    c = TestClient(webapp.app)

    class Req:
        base_url = "http://landed.onrender.com/"

    assert webapp.base_url(Req()) == "https://landed.onrender.com"
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert webapp.base_url(Req()) == "https://landed.onrender.com"


# ---------------------------------------------- whose figures are on screen

def test_a_visitor_gets_the_sample_importer(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    d = client.get("/api/state").json()
    assert d["source"] == "Sample data"
    assert d["stock"]


def test_a_signed_in_person_never_gets_someone_elses_figures(
    signed_in, monkeypatch
):
    """The failure this prevents is the quiet one.

    Falling back to the sample importer when the database cannot be
    reached shows a signed-in customer a fictional company's healthy
    numbers, labelled as their dashboard, with nothing on screen saying
    otherwise. An error they can see is strictly better than figures
    they might believe.
    """
    client, _ = signed_in

    class Boom:
        def as_user(self, _uid):
            raise RuntimeError("no database")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: Boom())
    d = client.get("/api/state").json()
    assert d["source"] == "Error"
    assert d["stock"] == []
    assert "unreachable" in d["error"]


def test_an_empty_organisation_says_so_and_says_what_to_do(
    signed_in, monkeypatch
):
    from engine.db.snapshot import Snapshot

    client, _ = signed_in

    class Conn:
        """State also reads the organisation's own thresholds now, so
        the connection has to answer that query too."""

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k):
            class R:
                def fetchone(self_): return ({},)      # no custom settings
            return R()

    class DB:
        def as_user(self, _uid): return Conn()

    import engine.db.supabase as sup
    import engine.db.snapshot as snapmod
    monkeypatch.setattr(sup, "database", lambda: DB())
    monkeypatch.setattr(
        snapmod, "read",
        lambda conn, as_of, currency="PKR": Snapshot([], [], [], "PKR"))

    d = client.get("/api/state").json()
    assert d["source"] == "No data yet"
    assert d["stock"] == []
    assert sum(d["line"].values()) == 0
    # It has the same shape as a full payload, so the screen needs no
    # second code path and cannot drift from one.
    assert set(d) >= {"line", "stock", "shipments", "decisions", "branches"}
    assert len(d["decisions"]) == 1
    assert d["decisions"][0]["action"]


# ------------------------------------------------------------- importing

def _book():
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable,
                        str(root / "tools" / "make_import_workbook.py"), tmp],
                       check=True, cwd=root)
        return next(Path(tmp).glob("landed-import-meridian*.xlsx")).read_bytes()


def test_a_visitor_cannot_upload(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/api/import/preview",
                    files={"file": ("x.xlsx", b"PK\x03\x04", "application/x")})
    assert r.status_code == 401


def test_preview_reports_what_it_would_do_and_writes_nothing(
    signed_in, monkeypatch
):
    """The whole reason preview exists. An import is not undoable, so
    the first anyone hears of what it does must be before it does it."""
    client, _ = signed_in
    touched = []

    class DB:
        def as_user(self, _uid):
            touched.append(1)
            raise AssertionError("preview must not open a transaction")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/import/preview",
                    files={"file": ("book.xlsx", _book(),
                                    "application/vnd.openxmlformats-"
                                    "officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["ok"] is True
    assert d["counts"]["items"] == 60
    assert d["counts"]["branches"] == 4
    assert not touched


def test_a_file_that_is_not_a_workbook_is_refused_in_words(signed_in):
    client, _ = signed_in
    r = client.post("/api/import/preview",
                    files={"file": ("notes.xlsx", b"hello", "text/plain")})
    assert r.status_code == 400
    assert "workbook" in r.json()["error"].lower()


def test_an_empty_upload_is_refused(signed_in):
    client, _ = signed_in
    r = client.post("/api/import/preview",
                    files={"file": ("empty.xlsx", b"", "application/x")})
    assert r.status_code == 400


def test_commit_refuses_a_workbook_with_problems(signed_in, monkeypatch):
    """And refuses it before opening a transaction, so a broken file
    cannot leave anything half-written."""
    from io import BytesIO
    from openpyxl import load_workbook

    client, _ = signed_in
    wb = load_workbook(BytesIO(_book()))
    wb["Stock"].cell(2, 1, "GHOST-1")       # a SKU that is not on Items
    buf = BytesIO()
    wb.save(buf)

    class DB:
        def as_user(self, _uid):
            raise AssertionError("must not reach the database")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/import/commit",
                    files={"file": ("book.xlsx", buf.getvalue(),
                                    "application/x")})
    assert r.status_code == 400
    assert r.json()["ok"] is False


# -------------------------------------------------- the missing first step

def test_importing_with_no_organisation_says_so_in_words(signed_in, monkeypatch):
    """The failure the first real import hit.

    Every insert finds the caller's organisation with `select id from
    public.organisations limit 1`. With no organisation that returns
    nothing, the insert writes nothing, and the fetchone() after it
    hands back None. It reached the screen as the word "TypeError".
    """
    client, _ = signed_in

    class Conn:
        """A connection whose organisations table is empty for this
        caller -- which is what row-level security returns when the
        account belongs to none."""

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k):
            class R:
                def fetchone(self_): return None       # no organisation
            return R()

    class DB:
        def as_user(self, _uid): return Conn()

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/import/commit",
                    files={"file": ("book.xlsx", _book(), "application/x")})
    assert r.status_code == 409
    d = r.json()
    assert d["needs_organisation"] is True
    assert "organisation" in d["error"].lower()
    assert "TypeError" not in r.text


def test_a_failed_import_reports_the_message_not_just_the_class(
    signed_in, monkeypatch
):
    """Reporting only the exception class names the shape of the problem
    and withholds the problem."""
    client, _ = signed_in

    class DB:
        def as_user(self, _uid):
            raise RuntimeError("relation \"items\" does not exist")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/import/commit",
                    files={"file": ("book.xlsx", _book(), "application/x")})
    assert r.status_code == 400
    assert "does not exist" in r.json()["error"]


def test_a_failure_message_cannot_leak_a_password(signed_in, monkeypatch):
    """An exception out of the database layer can carry a connection
    string, and a connection string carries a password."""
    client, _ = signed_in

    class DB:
        def as_user(self, _uid):
            raise RuntimeError(
                "could not connect: postgres://user:hunter2@db.host:5432/x")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/import/commit",
                    files={"file": ("book.xlsx", _book(), "application/x")})
    assert "hunter2" not in r.text
    assert "********" in r.json()["error"]


# ------------------------------------------------------------- settings

def test_a_standing_still_limit_below_the_cover_target_is_refused(
    signed_in, monkeypatch
):
    """Every line would be at once too little and too much.

    Clamping it silently would be the software deciding what the
    business meant, so it is refused with the reason.
    """
    client, _ = signed_in

    class DB:
        def as_user(self, _uid):
            raise AssertionError("must not reach the database")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/settings", json={
        "settings": {"target_cover_months": 6, "excess_cover_months": 3}})
    assert r.status_code == 400
    assert any("above the cover target" in p for p in r.json()["problems"])


def test_settings_outside_their_range_are_refused_by_name(signed_in):
    """Refused, and the response says which field.

    The sentence a person reads names the setting the way the screen
    labels it; the field key comes back separately so the input can be
    marked and so this test matches on something steadier than
    wording.
    """
    client, _ = signed_in
    r = client.post("/api/settings",
                    json={"settings": {"cost_of_capital_annual": 40}})
    assert r.status_code == 400
    assert r.json()["rejected"] == ["cost_of_capital_annual"]
    assert any("between 0 and 1" in p for p in r.json()["problems"])


def test_an_unknown_setting_is_ignored_not_stored(signed_in, monkeypatch):
    """The blob goes into jsonb and comes back out into the engine's
    Thresholds. Anything the column accepts would otherwise become
    anything the engine uses."""
    import web.app as w

    seen = {}

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            seen["sql"], seen["args"] = sql, args
            class R:
                def fetchone(self_): return None
            return R()

    class DB:
        def as_user(self, _uid): return Conn()

    import engine.db.supabase as sup
    client, _ = signed_in
    monkeypatch.setattr(sup, "database", lambda: DB())
    # `minimum_wage_monthly` used to be the unknown key here. It is a
    # real setting now, which is exactly the drift this test exists to
    # catch, so the example is one the engine has no field for.
    r = client.post("/api/settings", json={"settings": {
        "excess_cover_months": 9, "profit_target_margin": 1}})
    assert r.status_code == 200
    assert r.json()["settings"] == {"excess_cover_months": 9.0}


def test_thresholds_for_uses_the_stored_numbers():
    from web.app import thresholds_for

    assert thresholds_for({}).excess_cover_months == 6.0
    assert thresholds_for({"excess_cover_months": 9}).excess_cover_months == 9.0
    # Rubbish in the column cannot reach the engine.
    assert thresholds_for({"excess_cover_months": "nine"}
                          ).excess_cover_months == 6.0
    assert thresholds_for({"excess_cover_months": 999}
                          ).excess_cover_months == 6.0


# ----------------------------------------------------------- operations

def test_operations_need_a_session(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    for path, body in (("/api/movement", {"sku": "X", "branch": "KHI",
                                          "kind": "sale", "qty": 1}),
                       ("/api/adjust", {"sku": "X", "branch": "KHI",
                                        "counted": 1}),
                       ("/api/transfer", {"sku": "X", "from": "KHI",
                                          "to": "LHR", "qty": 1})):
        assert client.post(path, json=body).status_code == 401


# -------------------------------------------------------- country packs

def test_a_country_pack_is_merged_not_imposed(signed_in, monkeypatch):
    """Applying a pack overwrites only what the pack covers. Anything
    already tuned by hand survives, because a default is a starting
    point and not a subscription."""
    written = {}

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            if "update" in sql:
                written["settings"] = args[0]
                written["currency"] = args[1]
            class R:
                def fetchone(self_):
                    # Already has a hand-tuned excess limit and a key
                    # no pack knows about.
                    return ({"excess_cover_months": 14, "mine": "keep"},)
            return R()

    class DB:
        def as_user(self, _uid): return Conn()

    import json

    import engine.db.supabase as sup
    client, _ = signed_in
    monkeypatch.setattr(sup, "database", lambda: DB())

    r = client.post("/api/settings/country", json={"country": "AE"})
    assert r.status_code == 200
    got = json.loads(written["settings"])
    assert written["currency"] == "AED"
    assert got["scale"] == "western"
    assert got["mine"] == "keep", "a setting the pack does not cover was lost"
    # The pack does cover this one, so it is overwritten -- and the
    # person can change it straight back.
    assert got["excess_cover_months"] == 6.0


def test_an_unknown_country_is_refused_by_name(signed_in):
    client, _ = signed_in
    r = client.post("/api/settings/country", json={"country": "ZZ"})
    assert r.status_code == 400
    assert "ZZ" in r.json()["error"]


def test_the_country_list_is_public_and_complete(client):
    from engine.country import PACKS

    d = client.get("/api/countries").json()
    assert len(d["countries"]) == len(PACKS)
    assert all(c["currency"] and c["name"] for c in d["countries"])


def test_currency_and_scale_can_be_set_on_their_own(signed_in, monkeypatch):
    written = {}

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            written["args"] = args
            class R:
                def fetchone(self_): return None
            return R()

    class DB:
        def as_user(self, _uid): return Conn()

    import json

    import engine.db.supabase as sup
    client, _ = signed_in
    monkeypatch.setattr(sup, "database", lambda: DB())
    r = client.post("/api/settings", json={
        "settings": {"currency": "aed", "scale": "western"}})
    assert r.status_code == 200
    assert r.json()["settings"]["currency"] == "AED"
    assert r.json()["settings"]["scale"] == "western"


def test_a_currency_that_is_not_three_letters_is_refused(signed_in):
    client, _ = signed_in
    r = client.post("/api/settings", json={"settings": {"currency": "RUPEES"}})
    assert r.status_code == 400
    assert "currency" in " ".join(r.json()["problems"])
