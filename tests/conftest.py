"""What is true for every test in this suite.

One thing so far: this is a development environment, and it says so.

Secure cookies are the default now, and a Secure cookie is not sent
over plain http. The test client speaks http, so without this the
session cookie is set and never comes back, and every test that signs
in fails with something that looks nothing like its cause -- a 200
where a 303 was expected, a signed_in that is False straight after
signing in.

Setting it here rather than in each test is the honest shape: the
suite is not production, and that is a fact about the suite.
"""

from __future__ import annotations

import os

os.environ.setdefault("LANDED_ENV", "dev")
