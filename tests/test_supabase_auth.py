"""Attacks on the token verifier.

Every policy in the schema is reached through one value, the user id
this module extracts. So these are not tests that a valid token works --
that is the easy half -- they are the forgeries, and each one is a way
somebody becomes another tenant if the check is missing.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from engine.supabase_auth import AuthError, Claims, Verifier

PROJECT = "https://example.supabase.co"


# ------------------------------------------------------------- a keypair

@pytest.fixture(scope="module")
def keys():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = priv.public_key()
    pub_pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return {"private": pem, "public": pub_pem, "public_key": pub}


@pytest.fixture
def verifier(keys, monkeypatch):
    """A verifier whose JWKS lookup returns our test key."""
    v = Verifier(PROJECT)

    class FakeClient:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": keys["public_key"]})()

    monkeypatch.setattr(v, "_jwk_client", lambda: FakeClient())
    return v


def token(keys, alg="ES256", **over):
    body = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "email": "a@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
        "session_id": "sess-1",
    }
    body.update(over)
    key = keys["private"] if alg.startswith(("ES", "RS")) else "secret"
    return jwt.encode(body, key, algorithm=alg)


# --------------------------------------------------------- the happy path

def test_a_properly_signed_token_yields_its_subject(verifier, keys):
    c = verifier.verify(token(keys))
    assert c.user_id == "11111111-1111-1111-1111-111111111111"
    assert c.email == "a@example.com"
    assert c.seconds_left > 3500


# ------------------------------------------------------------- forgeries

def test_an_unsigned_token_is_refused(verifier, keys):
    """alg=none is the oldest JWT attack and still the first one tried."""
    bad = jwt.encode({"sub": "attacker", "exp": int(time.time()) + 99},
                     key="", algorithm="none")
    with pytest.raises(AuthError):
        verifier.verify(bad)


def test_a_token_signed_with_the_public_key_as_an_hmac_secret_is_refused(
    verifier, keys
):
    """Key confusion.

    The project publishes an EC public key. An attacker takes that public
    key -- it is public -- and uses it as the shared secret for an HS256
    token. A verifier that reads the algorithm out of the token and then
    asks for 'the key' hands the public key to an HMAC check, which
    passes, and the attacker is whoever they claimed to be. Pinning the
    algorithm list to the asymmetric ones is what stops it.
    """
    # PyJWT refuses to *build* this -- it guards against asymmetric keys
    # used as HMAC secrets -- so the attacker's token is assembled by
    # hand, which is what an attacker would do anyway.
    import base64
    import hashlib
    import hmac
    import json

    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({
        "sub": "attacker", "aud": "authenticated", "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }).encode())
    signing_input = head + b"." + body
    sig = b64(hmac.new(keys["public"].encode(), signing_input,
                       hashlib.sha256).digest())
    forged = (signing_input + b"." + sig).decode()

    # Sanity, checked with raw HMAC rather than PyJWT: the forgery really
    # does carry a correct HS256 signature under that secret, so this is
    # a live attack and not a typo. (PyJWT refuses to verify it too --
    # it rejects asymmetric material used as an HMAC secret on decode as
    # well as on encode -- which is a second line of defence, not the
    # one being tested here.)
    assert hmac.compare_digest(
        sig,
        b64(hmac.new(keys["public"].encode(), signing_input,
                     hashlib.sha256).digest()),
    )

    # The route a real request takes: the algorithm is symmetric, so it
    # must never reach the local JWKS path at all.
    with pytest.raises(AuthError):
        verifier._verify_locally(forged, "HS256")


def test_a_token_signed_by_a_different_key_is_refused(verifier):
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    bad = jwt.encode(
        {"sub": "attacker", "aud": "authenticated", "role": "authenticated",
         "exp": int(time.time()) + 3600},
        other, algorithm="ES256")
    with pytest.raises(AuthError):
        verifier.verify(bad)


def test_an_expired_token_is_refused(verifier, keys):
    with pytest.raises(AuthError):
        verifier.verify(token(keys, exp=int(time.time()) - 10))


def test_a_token_without_an_expiry_is_refused(verifier, keys):
    """A token that never expires is a password that cannot be changed."""
    body = {"sub": "x", "aud": "authenticated", "role": "authenticated"}
    with pytest.raises(AuthError):
        verifier.verify(jwt.encode(body, keys["private"], algorithm="ES256"))


def test_a_token_for_another_audience_is_refused(verifier, keys):
    with pytest.raises(AuthError):
        verifier.verify(token(keys, aud="some-other-service"))


def test_a_service_role_token_is_never_accepted_as_a_user(verifier, keys):
    """The service role bypasses every policy in supabase/migrations.

    It is signed by the same project, so it verifies perfectly. If it is
    then handed to `as_user` as a subject, the request runs with a role
    that has no row-level security at all. It is refused on the role
    claim, not on the signature, because the signature is genuine.
    """
    with pytest.raises(AuthError) as e:
        verifier.verify(token(keys, role="service_role"))
    assert "service_role" in str(e.value)


def test_rubbish_is_refused_without_raising_something_odd(verifier):
    for junk in ["", "   ", "not.a.token", "a.b", None, 12345, "..."]:
        with pytest.raises(AuthError):
            verifier.verify(junk)


# ------------------------------------------- the legacy symmetric project

class FakeHTTP:
    def __init__(self, status, body=None):
        self.status, self.body, self.calls = status, body or {}, []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        return type("R", (), {
            "status_code": self.status, "json": lambda s=self: self.body,
        })()


def test_an_hs256_project_is_checked_against_the_auth_server():
    """No local verification is attempted: that would need the secret
    that also mints tokens."""
    http = FakeHTTP(200, {"id": "22222222-2222-2222-2222-222222222222",
                          "email": "b@example.com"})
    v = Verifier(PROJECT, http=http)
    tok = jwt.encode(
        {"sub": "22222222-2222-2222-2222-222222222222", "email": "b@example.com",
         "exp": int(time.time()) + 600}, "the-secret", algorithm="HS256")
    c = v.verify(tok)
    assert c.user_id == "22222222-2222-2222-2222-222222222222"
    assert http.calls[0][0].endswith("/auth/v1/user")
    assert http.calls[0][1]["Authorization"] == f"Bearer {tok}"


def test_an_hs256_token_the_auth_server_rejects_is_refused():
    """Covers a signed-out session and a deleted user: both still carry
    a valid signature inside the token's lifetime."""
    v = Verifier(PROJECT, http=FakeHTTP(401))
    tok = jwt.encode({"sub": "x", "exp": int(time.time()) + 600},
                     "the-secret", algorithm="HS256")
    with pytest.raises(AuthError):
        v.verify(tok)


def test_no_signing_keys_means_refusal_not_acceptance(keys, monkeypatch):
    """If the JWKS endpoint cannot be reached, every token is refused.

    The tempting failure mode is to let requests through while the key
    server is down, so the product stays up. That turns a network blip
    into an open door.
    """
    v = Verifier(PROJECT)
    monkeypatch.setattr(v, "_jwk_client", lambda: None)
    with pytest.raises(AuthError):
        v.verify(token(keys))


def test_a_symmetric_algorithm_is_refused_before_any_key_is_fetched(keys):
    """Pins the guard itself, not the library underneath it.

    The key-confusion test above passes even with every guard in this
    module deleted, because PyJWT independently refuses asymmetric key
    material used as an HMAC secret. That is a good second line of
    defence and a bad test: it stays green while this module's own check
    rots. So this one proves the check by its effect -- the symmetric
    token is turned away before the JWKS client is ever consulted.
    """
    v = Verifier(PROJECT)

    def explode():
        raise AssertionError(
            "reached the key lookup with a symmetric algorithm")

    v._jwk_client = explode
    with pytest.raises(AuthError, match="asymmetric"):
        v._verify_locally("a.b.c", "HS256")


def test_the_project_key_travels_in_the_apikey_header_not_authorization():
    """A publishable or secret key is not a JWT and cannot go in the
    Authorization header -- that one carries the user's token. The key
    was previously sent as an empty string, which the endpoint rejects,
    so every legacy-symmetric project would have failed to verify.
    """
    http = FakeHTTP(200, {"id": "44444444-4444-4444-4444-444444444444",
                          "email": "c@example.com"})
    v = Verifier(PROJECT, api_key="sb_publishable_example", http=http)
    tok = jwt.encode({"sub": "44444444-4444-4444-4444-444444444444",
                      "exp": int(time.time()) + 600}, "s", algorithm="HS256")
    v.verify(tok)
    _, headers = http.calls[0]
    assert headers["apikey"] == "sb_publishable_example"
    assert headers["Authorization"] == f"Bearer {tok}"
