import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cio import auth
from tests.conftest import requires_sqlite

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _flow(settings):
    url, cookie = auth.begin_login(settings)
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["openid email profile"] and q["code_challenge_method"] == ["S256"]
    return q["state"][0], q["nonce"][0], cookie


def _http(token_claims):
    def handler(req):
        tok = jwt.encode(token_claims, KEY, algorithm="RS256")
        return httpx.Response(200, json={"id_token": tok, "access_token": "a"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _claims(settings, nonce_value, **over):
    c = {"iss": settings.zoho_accounts_url, "aud": settings.login_client_id, "exp": int(time.time()) + 300,
         "iat": int(time.time()), "nonce": nonce_value, "email": "Op@Europcar.com.br", "email_verified": True}
    c.update(over)
    return c


def test_oidc_valido(settings):
    state, nonce, cookie = _flow(settings)
    email = auth.finish_login(settings, "code", state, cookie, http=_http(_claims(settings, nonce)),
                              key_resolver=lambda t: KEY.public_key())
    assert email == "op@europcar.com.br"


@pytest.mark.parametrize("over", [{"aud": "outro"}, {"iss": "https://evil"}, {"nonce": "x"},
                                  {"email": "a@gmail.com"}, {"exp": 1}, {"email_verified": False}])
def test_oidc_rejeita_token_invalido(settings, over):
    state, nonce, cookie = _flow(settings)
    with pytest.raises(auth.LoginError):
        auth.finish_login(settings, "code", state, cookie, http=_http(_claims(settings, nonce, **over)),
                          key_resolver=lambda t: KEY.public_key())


def test_oidc_rejeita_state_e_assinatura(settings):
    state, nonce, cookie = _flow(settings)
    with pytest.raises(auth.LoginError):
        auth.finish_login(settings, "code", "outro-state", cookie, http=_http(_claims(settings, nonce)),
                          key_resolver=lambda t: KEY.public_key())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(auth.LoginError):
        auth.finish_login(settings, "code", state, cookie, http=_http(_claims(settings, nonce)),
                          key_resolver=lambda t: other.public_key())
    with pytest.raises(auth.LoginError):
        auth.finish_login(settings, "code", state, cookie[:-2] + "00", http=_http(_claims(settings, nonce)),
                          key_resolver=lambda t: KEY.public_key())


def test_producao_exige_segredo_e_https(monkeypatch, tmp_path):
    from cio.config import Settings
    monkeypatch.setenv("CIO_ENV", "production")
    monkeypatch.setenv("CIO_SECRET_KEY", "curta")
    with pytest.raises(RuntimeError):
        Settings().validate()
    monkeypatch.setenv("CIO_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("CIO_BASE_URL", "http://inseguro")
    with pytest.raises(RuntimeError):
        Settings().validate()


def test_dev_login_inexistente_em_producao(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from cio.app import create_app
    from cio.config import get_settings
    monkeypatch.setenv("CIO_ENV", "production")
    monkeypatch.setenv("CIO_SECRET_KEY", "y" * 40)
    monkeypatch.setenv("CIO_BASE_URL", "https://portal.exemplo")
    monkeypatch.setenv("CIO_DATA_DIR", str(tmp_path / "d"))
    c = TestClient(create_app(get_settings()), base_url="https://testserver")
    assert c.post("/auth/dev-login", data={"email": "ad@europcar.com.br"}).status_code in (404, 405)
    assert "Ambiente de teste" not in c.get("/login").text
