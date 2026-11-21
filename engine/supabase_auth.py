"""Deciding who is making a request, when Supabase Auth issued the token.

Named for Supabase rather than called `auth` because `engine/auth.py`
already exists and does something different: it is the self-hosted
scheme from the desktop product -- its own users table, Argon2id
passwords, its own session tokens. Nothing imports it now, and nothing
can: every policy in supabase/migrations reads `auth.uid()`, and the
schema has no users table of its own. Two authentication systems in one
repository is a trap worth naming rather than leaving to be discovered.


Everything in supabase/migrations rests on one value: the user id the
policies read through `auth.uid()`. This module is where that value
comes from, and it exists as its own file because the ways of getting
it wrong all look like working code.

A JSON Web Token carries its own claim about who it belongs to, signed.
Reading the claim is one line and is what a hurried version does; the
signature is the only part that makes the claim worth anything. A token
whose payload is decoded without checking the signature is a token an
attacker writes themselves, and the resulting request is served happily,
as whoever they said they were.

Supabase signs with one of two schemes and a project can be on either:

  * asymmetric (ES256 or RS256), where the public keys are published at
    the project's JWKS endpoint and verification is local; and
  * the legacy symmetric secret (HS256), where the only safe check is to
    ask the Auth server, because verifying locally would mean holding a
    secret that also *mints* tokens.

Which one a project uses is not something this code can assume, so it
asks, caches the answer, and handles both. Anything else -- no
signature, an algorithm nobody offered, a token for another audience --
is refused rather than guessed at.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

try:
    import jwt
    from jwt import PyJWKClient
except ImportError:  # pragma: no cover - the import guard test covers it
    jwt = None
    PyJWKClient = None


class AuthError(Exception):
    """The token is not acceptable. The reason is deliberately vague to
    the caller's caller: an error that distinguishes 'no such user' from
    'wrong password' is an account-enumeration oracle."""


# Algorithms this service will verify. `none` is absent, obviously, but
# so is every symmetric algorithm: a token arriving as HS256 when the
# project publishes an RSA public key is the key-confusion attack, where
# the attacker signs with the public key as the HMAC secret. Pinning the
# list is what stops it, so the list is never built from the token.
ASYMMETRIC = ("ES256", "RS256", "ES384", "RS384", "ES512", "RS512")


@dataclass(frozen=True)
class Claims:
    """Who the request is from, once that has actually been established."""

    user_id: str
    email: str
    expires_at: int
    session_id: str = ""

    @property
    def seconds_left(self) -> int:
        return max(0, self.expires_at - int(time.time()))


class Verifier:
    """Turns an access token into claims, or raises.

    One instance per process. It holds a JWKS client, which caches the
    project's public keys and refetches them when a token arrives with a
    key id it has not seen -- that is how a key rotation is picked up
    without a deploy.
    """

    def __init__(self, project_url: str, api_key: str = "", http=None,
                 clock=time.time):
        self.project_url = (project_url or "").rstrip("/")
        # Needed only on the legacy-symmetric path, where the token has
        # to be checked with the Auth server. It was being sent empty,
        # which that endpoint rejects.
        self.api_key = api_key or ""
        self._http = http                 # injected in tests
        self._clock = clock
        self._jwks = None
        self._jwks_tried = False
        self._lock = threading.Lock()

    # -------------------------------------------------------------- keys

    @property
    def jwks_url(self) -> str:
        return f"{self.project_url}/auth/v1/.well-known/jwks.json"

    def _jwk_client(self):
        """The JWKS client, built once.

        Built lazily and behind a lock: building it eagerly would mean a
        network call at import, which turns an unreachable Supabase into
        an app that will not start rather than an app that can tell you
        why it is not working.
        """
        if self._jwks_tried:
            return self._jwks
        with self._lock:
            if self._jwks_tried:
                return self._jwks
            try:
                self._jwks = PyJWKClient(
                    self.jwks_url, cache_keys=True, lifespan=600
                )
            except Exception:
                self._jwks = None
            self._jwks_tried = True
        return self._jwks

    # ------------------------------------------------------------ verify

    def verify(self, token: str) -> Claims:
        if not token or not isinstance(token, str):
            raise AuthError("no token")
        if jwt is None:  # pragma: no cover
            raise AuthError("PyJWT is not installed")

        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise AuthError("malformed token") from exc

        alg = header.get("alg")
        if alg in ASYMMETRIC:
            return self._verify_locally(token, alg)
        if alg == "HS256":
            # The shared secret both signs and verifies, so a service
            # that holds it can mint tokens for any user. This one does
            # not hold it; it asks the Auth server instead.
            return self._verify_remotely(token)
        raise AuthError(f"unacceptable algorithm: {alg!r}")

    def _verify_locally(self, token: str, alg: str) -> Claims:
        # Checked here and not only at the dispatch above. A mutation test
        # showed the pinned `algorithms` list below was doing no work,
        # because the caller had already filtered -- which means a later
        # refactor of the caller could remove the only real guard without
        # a single test turning red. This method now refuses on its own.
        if alg not in ASYMMETRIC:
            raise AuthError(f"not an asymmetric algorithm: {alg!r}")
        client = self._jwk_client()
        if client is None:
            raise AuthError("no signing keys available")
        try:
            key = client.get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                key,
                algorithms=list(ASYMMETRIC),   # pinned, never from the token
                audience="authenticated",
                options={"require": ["exp", "sub"], "verify_exp": True},
            )
        except Exception as exc:
            raise AuthError("token rejected") from exc
        return self._claims(payload)

    def _verify_remotely(self, token: str) -> Claims:
        """Ask the Auth server who this is.

        The reply is authoritative in a way local parsing is not: it also
        reflects a session that has been signed out or a user who has
        been deleted since the token was minted, neither of which changes
        the signature on a token that is still inside its lifetime.
        """
        http = self._http
        if http is None:  # pragma: no cover - exercised with an injected client
            import httpx

            http = httpx.Client(timeout=8.0)
        # The Authorization header carries the user's JWT and nothing
        # else. A publishable or secret key cannot go there -- they are
        # not JWTs -- so the project key travels in `apikey`, which is
        # where it belongs under both the old and the new key scheme.
        r = http.get(
            f"{self.project_url}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}",
                     "apikey": self.api_key},
        )
        if r.status_code != 200:
            raise AuthError("token rejected")
        user = r.json()
        # The server confirmed the token; the lifetime still comes from
        # the token itself, unverified-but-now-vouched-for.
        payload = jwt.decode(token, options={"verify_signature": False})
        return Claims(
            user_id=str(user.get("id") or payload.get("sub") or ""),
            email=str(user.get("email") or payload.get("email") or ""),
            expires_at=int(payload.get("exp") or 0),
            session_id=str(payload.get("session_id") or ""),
        )

    def _claims(self, payload: dict) -> Claims:
        sub = payload.get("sub")
        if not sub:
            raise AuthError("token has no subject")
        role = payload.get("role")
        if role not in (None, "authenticated"):
            # A service-role token is signed by the same project and will
            # verify perfectly. It must never be accepted as a user: it
            # is the key that bypasses every policy in the schema.
            raise AuthError(f"not a user token: role={role!r}")
        return Claims(
            user_id=str(sub),
            email=str(payload.get("email") or ""),
            expires_at=int(payload.get("exp") or 0),
            session_id=str(payload.get("session_id") or ""),
        )
