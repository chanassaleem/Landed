"""Signing in, and holding the session afterwards.

The browser never sees an access token. It posts an email and a password
to this app, this app exchanges them with Supabase Auth, and the tokens
come back here and stay here -- written into cookies the browser will
send but no script can read.

The usual arrangement is the opposite: the Supabase client runs in the
page, keeps the session in localStorage, and attaches the token to each
request. It is less code. It also means any script that ends up running
on the page -- an injected one, a compromised dependency, a bad
extension -- can read a working token for a business's inventory and
cash position and send it anywhere. An httpOnly cookie cannot be read
by script at all, so the same bug yields nothing.

The cost of the cookie is cross-site request forgery: a cookie is sent
by the browser whether or not the page that triggered the request is
ours. SameSite=Lax removes the cross-site POST, which is the shape that
matters here, and every state-changing route is a POST for that reason.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

ACCESS = "landed_at"
REFRESH = "landed_rt"

# The browser-safe key, under either of its names.
#
# Supabase replaced the anon key with a publishable key -- same low
# privileges, same role, so the policies in supabase/migrations behave
# identically -- and is retiring the old one. A project may be on
# either, and a project that has just been created is on the new one,
# so both names are read and the newer is preferred. The value is never
# logged: it is public by design, but a key in a log line is still a key
# somebody has to think about.
KEY_VARS = ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY")


def public_key(env=None) -> str:
    import os

    env = os.environ if env is None else env
    for name in KEY_VARS:
        if env.get(name):
            return env[name]
    return ""

# A refresh costs a round trip, so it is not done on every request; it is
# done when the token is close enough to expiry that the next one might
# land after it. Sixty seconds is comfortably more than a slow request.
REFRESH_WITHIN = 60


class SignInFailed(Exception):
    """Credentials rejected, or Supabase said no.

    Deliberately one exception for every cause. 'No such account' and
    'wrong password' as separate messages let anyone test whether an
    email has an account here, one request at a time.
    """


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str
    expires_in: int


class Auth:
    """Talks to the project's Auth server. Holds no secrets of its own.

    Only the anon key is used. It is public by design: every request it
    makes is still subject to row-level security, which is the whole
    reason the schema puts its trust in policies rather than in the
    application remembering to filter.
    """

    def __init__(self, project_url: str, anon_key: str, http=None):
        self.url = (project_url or "").rstrip("/")
        self.anon = anon_key or ""
        self._http = http

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon)

    def _client(self):
        return self._http or httpx.Client(timeout=12.0)

    def _headers(self) -> dict:
        return {"apikey": self.anon, "Content-Type": "application/json"}

    def _tokens(self, data: dict) -> Tokens:
        at = data.get("access_token")
        if not at:
            # Sign-up with email confirmation on returns a user and no
            # session. That is success, not failure, and the caller has
            # to tell the person to go and check their email rather than
            # show them a wrong password message.
            raise SignInFailed("no session returned")
        return Tokens(at, data.get("refresh_token", ""),
                      int(data.get("expires_in") or 3600))

    def sign_in(self, email: str, password: str) -> Tokens:
        r = self._client().post(
            f"{self.url}/auth/v1/token?grant_type=password",
            headers=self._headers(),
            json={"email": email, "password": password},
        )
        if r.status_code != 200:
            raise SignInFailed("rejected")
        return self._tokens(r.json())

    def sign_up(self, email: str, password: str,
                redirect_to: str = "") -> Tokens | None:
        """Create the account, and say where its confirmation link lands.

        `redirect_to` goes in the query string, not the body. GoTrue
        ignores it in the body and silently falls back to the project's
        Site URL, which defaults to localhost -- so the symptom is a
        confirmation email pointing at a machine that is not running,
        with nothing anywhere reporting a mistake.

        It is also ignored unless the exact URL is allow-listed under
        Authentication -> URL Configuration -> Redirect URLs, with the
        same silent fallback. Both halves have to be right.
        """
        url = f"{self.url}/auth/v1/signup"
        if redirect_to:
            # Deliberately a bare path with no query string of its own:
            # a redirect_to carrying its own parameters gets truncated
            # at the first one by the time it reaches the email.
            url += f"?redirect_to={quote(redirect_to, safe='')}"
        r = self._client().post(
            url,
            headers=self._headers(),
            json={"email": email, "password": password},
        )
        if r.status_code not in (200, 201):
            raise SignInFailed("rejected")
        try:
            return self._tokens(r.json())
        except SignInFailed:
            return None          # created, but needs email confirmation

    def verify_otp(self, token_hash: str, kind: str = "signup") -> Tokens:
        """Redeem the token from a confirmation email, here on the server.

        The default Supabase link goes to Supabase, which confirms the
        account and then redirects to the site with the session in the
        URL -- in the fragment, or as a code to exchange. Both put a
        credential in a place that is kept: browser history, the
        referrer header, a shoulder, a screenshot in a bug report.

        A token_hash link avoids all of it. The email carries a one-use
        hash, this exchanges it for a session out of band, and the
        browser is sent on to a plain URL with a cookie it cannot read.
        It also works when the email is opened in a different browser
        from the one that signed up, which the code-exchange flow does
        not: that one needs a verifier held by the original browser, so
        signing up on a laptop and opening the email on a phone fails.
        """
        r = self._client().post(
            f"{self.url}/auth/v1/verify",
            headers=self._headers(),
            json={"type": kind, "token_hash": token_hash},
        )
        if r.status_code != 200:
            raise SignInFailed("link rejected")
        return self._tokens(r.json())

    def refresh(self, refresh_token: str) -> Tokens:
        r = self._client().post(
            f"{self.url}/auth/v1/token?grant_type=refresh_token",
            headers=self._headers(),
            json={"refresh_token": refresh_token},
        )
        if r.status_code != 200:
            raise SignInFailed("refresh rejected")
        return self._tokens(r.json())

    def sign_out(self, access_token: str) -> None:
        """Best effort. The cookies are cleared either way.

        If this call fails the local session is still gone, which is what
        the person asked for; the token then expires on its own. Leaving
        the cookies in place because a network call failed would mean a
        sign-out button that sometimes does not sign you out.
        """
        try:
            self._client().post(
                f"{self.url}/auth/v1/logout",
                headers={**self._headers(),
                         "Authorization": f"Bearer {access_token}"},
            )
        except Exception:
            pass


# Where a session cookie may go out without the Secure flag. Nowhere
# but a developer's own machine, and named rather than inferred.
INSECURE_OK = ("dev", "development", "local", "test")


def secure_cookies() -> bool:
    """Whether to mark cookies Secure.

    Secure unless this is explicitly a development environment.

    It used to be the other way round: Secure only when LANDED_ENV said
    production. LANDED_ENV was not set in render.yaml, so the deployed
    application issued its session cookies without the flag over HTTPS
    -- and the failure was silent, because a cookie without Secure
    works perfectly. Nothing breaks, nothing logs, and the only sign is
    that the session token will also travel over plain http if anything
    ever downgrades the connection.

    A default that fails open is the wrong default for a credential.
    Forgetting to set a variable now costs a developer a login on
    localhost, which they notice in seconds, rather than costing a
    customer a cookie nobody notices at all.
    """
    return os.environ.get("LANDED_ENV", "").lower() not in INSECURE_OK


def set_session(response, tokens: Tokens) -> None:
    common = dict(httponly=True, secure=secure_cookies(), samesite="lax",
                  path="/")
    response.set_cookie(ACCESS, tokens.access, max_age=tokens.expires_in,
                        **common)
    if tokens.refresh:
        # Thirty days: long enough that a person is not signed out every
        # week, short enough that an abandoned laptop stops working.
        response.set_cookie(REFRESH, tokens.refresh, max_age=30 * 24 * 3600,
                            **common)


def clear_session(response) -> None:
    for name in (ACCESS, REFRESH):
        response.delete_cookie(name, path="/")


def needs_refresh(expires_at: int | None,
                  now: float | None = None) -> bool:
    """Whether to renew, given what the verifier made of the token.

    `expires_at` comes from claims that have been checked against the
    project's signing keys, and None means the token did not verify at
    all -- expired, malformed, or signed by something else.

    The first version of this read `exp` out of the token itself
    without verifying it. That was wrong twice over. It put a second,
    weaker opinion about expiry next to the verifier's, which is how
    two readings of one fact drift apart; and it treated anything it
    could not parse as due for renewal, so a session whose access
    token the verifier was perfectly happy with got a round trip to
    Supabase on every request, and when that failed, cleared. Ask the
    thing that already knows.

    A refresh costs a round trip, so not on every request -- only when
    the token is close enough to expiry that the next one might land
    after it.
    """
    if expires_at is None:
        return True
    return expires_at - (now if now is not None else time.time()) \
        <= REFRESH_WITHIN
