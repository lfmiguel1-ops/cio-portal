import httpx
import pytest

from cio.zoho import ZohoClient, ZohoError


def make_client(settings, handler):
    settings.ingest_client_id = "cid"
    settings.ingest_client_secret = "SEGREDO-NAO-VAZAR"
    settings.ingest_refresh_token = "REFRESH-NAO-VAZAR"
    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    return ZohoClient(settings, http=http, sleep=sleeps.append, base_delay=1), sleeps


def test_renova_token_expirado_401(settings):
    calls = {"token": 0, "api": 0}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/oauth/v2/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok{calls['token']}", "expires_in": 3600})
        calls["api"] += 1
        if req.headers["Authorization"] == "Zoho-oauthtoken tok1":
            return httpx.Response(401, json={"data": {"errorCode": "INVALID_OAUTHTOKEN"}})
        return httpx.Response(200, json={"data": {"content": "<p>ok</p>"}})

    c, _ = make_client(settings, handler)
    from cio.zoho import MailMessage
    out = c.get_content(MailMessage("a", "m", "f", "s", "x", 0, False))
    assert out == "<p>ok</p>" and calls == {"token": 2, "api": 2}


def test_retentativa_exponencial_em_503_e_429(settings):
    seq = iter([503, 429, 503, 200])

    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        code = next(seq)
        return httpx.Response(code, json={"data": []} if code == 200 else {})

    c, sleeps = make_client(settings, handler)
    assert list(c.search_messages("acc", "x@y", 0)) == []
    assert sleeps == [1, 2, 4]


def test_erro_nao_retentavel_e_sem_segredos(settings):
    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(404, json={"error": "nope"})

    c, sleeps = make_client(settings, handler)
    with pytest.raises(ZohoError) as e:
        list(c.search_messages("acc", "x@y", 0))
    assert e.value.status == 404 and sleeps == []
    assert "SEGREDO" not in str(e.value) and "REFRESH" not in str(e.value)


def test_falha_ao_renovar_token_nao_expoe_segredo(settings):
    def handler(req):
        return httpx.Response(400, json={"error": "invalid_code"})

    c, _ = make_client(settings, handler)
    with pytest.raises(ZohoError) as e:
        c.tokens.token()
    assert "invalid_code" in str(e.value) and "SEGREDO" not in str(e.value)


def test_rede_instavel_esgota_tentativas(settings):
    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        raise httpx.ConnectError("boom")

    c, sleeps = make_client(settings, handler)
    with pytest.raises(ZohoError):
        list(c.search_messages("acc", "x@y", 0))
    assert len(sleeps) == 5


def test_paginacao_busca_filtra_por_data(settings):
    pages = {1: [{"messageId": str(i), "receivedTime": str(1000 + i), "fromAddress": "X@Y", "subject": "s",
                  "folderId": "f"} for i in range(25)],
             26: [{"messageId": "99", "receivedTime": "5", "fromAddress": "x@y", "subject": "s", "folderId": "f"}]}

    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"data": pages.get(int(req.url.params["start"]), [])})

    c, _ = make_client(settings, handler)
    msgs = list(c.search_messages("acc", "x@y", 900))
    assert len(msgs) == 25 and msgs[0].sender == "x@y"  # "99" é anterior ao checkpoint
