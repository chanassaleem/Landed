"""Run the app against a local Postgres, signed in, for looking at it.

Development only, and it says so loudly on every page it serves. Two
things are stubbed that must never be stubbed anywhere else:

  * the token verifier always returns the same signed-in person, so
    there is no Supabase project to configure to see a screen;
  * the seed is the fixture from tests/test_module_data.py, so the
    figures on screen are the ones the tests assert on. Looking at a
    screen and reading its test should not require believing that two
    separate piles of invented data agree.

The policies are NOT stubbed. It connects as `authenticated` with real
claims, so a screen that works here works for a real signed-in user,
and one that needs the table owner fails here too.

    DATABASE_URL='postgres://postgres@/landed?host=/tmp&port=5441' \
        python tools/dev_server.py 8099
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("DATABASE_URL") or os.environ.get("LANDED_TEST_DSN")
if not DSN:
    raise SystemExit("Set DATABASE_URL to a local Postgres first.")
os.environ["DATABASE_URL"] = DSN
os.environ["LANDED_TEST_DSN"] = DSN


def seed():
    """Build the schema and the demo organisation, and return its owner."""
    import tests.test_module_data as T

    gen = T.seeded.__wrapped__()
    con, owner, org = next(gen)
    return con, owner, org


def main() -> int:
    # Secure cookies are the default; this is the one place they are
    # not wanted, because a Secure cookie is not sent over plain http
    # and the sign-in would look broken on localhost.
    os.environ.setdefault("LANDED_ENV", "dev")

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    con, owner, _org = seed()

    import web.app as webapp
    from engine.supabase_auth import Claims
    from web import session

    email = "owner@seed.test"

    webapp.verifier = lambda: type("V", (), {
        "verify": staticmethod(lambda token: Claims(
            user_id=str(owner), email=email,
            expires_at=int(time.time()) + 86400)),
    })()

    class DevAuth(session.Auth):
        def __init__(self):
            super().__init__("https://dev.invalid", "dev-anon-key")

        def sign_in(self, _email, _password):
            return session.Tokens("dev-access", "dev-refresh", 86400)

    webapp.auth = lambda: DevAuth()

    import uvicorn

    print(f"→ http://127.0.0.1:{port}  (signed in as {email})")
    uvicorn.run(webapp.app, host="127.0.0.1", port=port, log_level="warning")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
