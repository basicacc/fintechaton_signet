from pca import auth

SECRET = b"s" * 32


def test_round_trip_and_expiry():
    tok = auth.issue(["bank"], SECRET, ttl=60, now=1_000)
    assert auth.roles_of(tok, SECRET, now=1_030) == {"bank"}
    assert auth.roles_of(tok, SECRET, now=1_061) == set()  # expired


def test_tampering_and_wrong_secret_rejected():
    tok = auth.issue(["client"], SECRET)
    assert auth.roles_of(tok.replace("client", "regulator"), SECRET) == set()
    assert auth.roles_of(tok, b"other-secret" * 3) == set()
    assert auth.roles_of("garbage", SECRET) == set()
    assert auth.roles_of(None, SECRET) == set()


def test_unknown_roles_refused():
    import pytest
    with pytest.raises(ValueError):
        auth.issue(["admin"], SECRET)


def test_client_tokens_carry_a_signed_subject():
    tok = auth.issue(["client"], SECRET, subject="zhang-hao")
    assert auth.claims(tok, SECRET) == ({"client"}, "zhang-hao")
    assert auth.claims(tok.replace("zhang-hao", "chen-mei"), SECRET) == (set(), None)  # can't switch client
    assert auth.claims(auth.issue(["bank"], SECRET), SECRET) == ({"bank"}, None)
    import pytest
    with pytest.raises(ValueError):
        auth.issue(["client"], SECRET, subject="a.b")
