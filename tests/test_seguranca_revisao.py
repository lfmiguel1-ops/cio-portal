"""Prioridade 4: autorização horizontal e vertical, revogação de sessões, CSRF em todas as rotas,
isolamento entre áreas, quarentena, proteção de tokens OAuth e falhas de ingestão.

As rotas são descobertas a partir da própria aplicação: rotas novas entram nos testes automaticamente.
"""
import logging
import re
import time

import httpx
import pytest

from tests.test_portal_security import csrf_of, ids_in

PUBLIC = {"/login", "/auth/start", "/auth/callback", "/saude", "/auth/dev-login"}


def _routes(app):
    for r in app.routes:
        methods = getattr(r, "methods", None)
        if not methods or r.path.startswith("/static"):
            continue
        for m in methods - {"HEAD"}:
            yield m, r.path


def _fill(path, portal, conn):
    rid = portal["reps"]["atd_int"]
    att = conn.insert("INSERT INTO attachments(report_id, filename, local_path) VALUES (?, 'a.csv', '')", (rid,))
    err = conn.insert("INSERT INTO ingest_errors(account_id, message_id, stage, error) VALUES "
                      "('acc1', 'm', 'processamento', 'x')")
    fid = conn.insert("INSERT INTO search_folders(name, q) VALUES ('P', 'alfa')")
    return (path.replace("{report_id}", str(rid)).replace("{att_id}", str(att)).replace("{slug}", "atendimento")
            .replace("{folder_id}", str(fid)).replace("{error_id}", str(err)))


def test_inventario_de_rotas_e_coberto(portal):
    """Garante que o teste enxerga todas as rotas (se alguém criar uma, ela entra nos testes abaixo)."""
    rotas = set(_routes(portal["app"]))
    assert ("POST", "/admin/grupos") in rotas and ("GET", "/r/{report_id}/arquivo") in rotas
    assert len(rotas) >= 35


def test_sem_login_nada_alem_das_rotas_publicas(portal, conn):
    c = portal["client"](None)
    for method, path in _routes(portal["app"]):
        if path in PUBLIC:
            continue
        r = c.request(method, _fill(path, portal, conn))
        assert r.status_code in (303, 401) and r.headers.get("location", "/login") == "/login", (method, path)


@pytest.mark.parametrize("who", ["op", "ge", "f1", "sem", "dir"])
def test_vertical_nao_admin_nunca_alcanca_rotas_de_administracao(portal, conn, who):
    c = portal["client"](who)
    tok = csrf_of(c)
    for method, path in _routes(portal["app"]):
        if not path.startswith("/admin"):
            continue
        r = c.request(method, _fill(path, portal, conn), data={"csrf_token": tok} if method == "POST" else None)
        assert r.status_code == 404, (who, method, path, r.status_code)
    assert "/admin" not in c.get("/").text


def test_csrf_obrigatorio_em_todos_os_posts(portal, conn):
    ad = portal["client"]("ad")
    for method, path in _routes(portal["app"]):
        if method != "POST" or path in PUBLIC:
            continue
        r = ad.post(_fill(path, portal, conn), data={"acao": "despublicar", "name": "x", "email": "x@europcar.com.br"})
        assert r.status_code == 403, (path, r.status_code)
    # token de OUTRA sessão também é recusado
    outro = csrf_of(portal["client"]("ad"))
    assert ad.post(f"/admin/r/{portal['reps']['atd_int']}", data={"acao": "despublicar",
                                                                  "csrf_token": outro}).status_code == 403


def test_horizontal_mesmo_nivel_grupos_diferentes(portal, conn):
    """Financeiro x Gestores e Franqueado 1 x Franqueado 2: mesmo 'nível', conteúdos isolados."""
    from tests.conftest import add_report, add_user
    add_user(conn, "fin@europcar.com.br", group="financeiro")
    fin = add_report(conn, "fx", "Caixa", "zeta financeira", "financeiro", groups=("financeiro",))
    ge, finc = portal["client"]("ge"), portal["client"]("fin")
    assert fin not in ids_in(ge.get("/historico").text) and fin in ids_in(finc.get("/historico").text)
    assert portal["reps"]["com_ges"] not in ids_in(finc.get("/historico").text)
    for who, alheio in (("f1", "fr2"), ("f2", "fr1")):
        c = portal["client"](who)
        for suf in ("", "/conteudo", "/download", "/arquivo"):
            assert c.get(f"/r/{portal['reps'][alheio]}{suf}").status_code == 404


def test_contadores_e_pastas_nao_revelam_conteudo_alheio(portal, conn):
    ad = portal["client"]("ad")
    ad.post("/admin/pastas", data={"name": "Tudo omega", "q": "omega", "active": "1", "csrf_token": csrf_of(ad)})
    f1 = portal["client"]("f1")
    pastas = f1.get("/pastas").text
    assert re.search(r"<strong>1</strong><span>Tudo omega", pastas)  # só o resultado da própria cidade
    areas = f1.get("/areas").text
    assert "Financeiro" not in areas and "Atendimento" not in areas  # áreas sem nada visível nem aparecem
    assert f1.get("/areas/atendimento").status_code == 404


def test_revogacao_de_sessoes(portal, conn, settings):
    ge = portal["client"]("ge")
    com = portal["reps"]["com_ges"]
    assert ge.get(f"/r/{com}").status_code == 200
    # troca de grupo vale na próxima requisição (sem novo login)
    conn.execute("UPDATE users SET group_id=(SELECT id FROM groups WHERE slug='geral') WHERE email='ge@europcar.com.br'")
    assert ge.get(f"/r/{com}").status_code == 404
    # rebaixar admin vale na hora
    ad = portal["client"]("ad")
    conn.execute("INSERT INTO users(email, role) VALUES ('ad2@europcar.com.br', 'admin')")
    conn.execute("UPDATE users SET role='operacional' WHERE email='ad@europcar.com.br'")
    assert ad.get("/admin").status_code == 404
    # desativar pela tela apaga as sessões
    ad2 = portal["client"]("ad2@europcar.com.br")
    op = portal["client"]("op")
    ad2.post("/admin/usuarios", data={"email": "op@europcar.com.br", "csrf_token": csrf_of(ad2)})
    assert conn.execute("SELECT count(*) FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE u.email='op@europcar.com.br'").fetchone()[0] == 0
    assert op.get("/").status_code == 303
    # cookie antigo não volta a valer depois do logout
    c = portal["client"]("dir")
    cookie = dict(c.cookies)
    c.post("/logout", data={"csrf_token": csrf_of(c)})
    novo = portal["client"](None)
    for k, v in cookie.items():
        novo.cookies.set(k, v)
    assert novo.get("/").status_code == 303
    # validade máxima da sessão
    f2 = portal["client"]("f2")
    conn.execute("UPDATE sessions SET expires_at=? WHERE user_id=(SELECT id FROM users WHERE "
                 "email='f2@europcar.com.br')", (time.time() - 1,))
    assert f2.get("/").status_code == 303


def test_isolamento_entre_areas_e_filtros(portal, conn):
    op = portal["client"]("op")
    # filtrar por área que a pessoa não vê não revela nada
    assert ids_in(op.get("/busca", params={"q": "segredo", "area": "comercial"}).text) == set()
    # desativar área esconde até de quem tinha o grupo certo; admin continua vendo
    conn.execute("UPDATE areas SET active=0 WHERE slug='atendimento'")
    assert op.get(f"/r/{portal['reps']['atd_int']}").status_code == 404
    assert portal["client"]("ad").get(f"/r/{portal['reps']['atd_int']}").status_code == 200


def test_quarentena_invisivel_em_todos_os_caminhos(portal, conn):
    q = portal["reps"]["quar"]
    for who in ("op", "ge", "dir", "f1"):
        c = portal["client"](who)
        for url in ("/", "/hoje", "/historico", "/busca?q=epsilon", "/busca?q=epsilon&status=quarentena",
                    "/areas", "/pastas"):
            body = c.get(url).text
            assert "conteudo epsilon" not in body and "Quarentena<" not in body and q not in ids_in(body), (who, url)
        for suf in ("", "/conteudo", "/download", "/arquivo"):
            assert c.get(f"/r/{q}{suf}").status_code == 404
    assert q in ids_in(portal["client"]("ad").get("/busca", params={"q": "epsilon"}).text)


SEGREDOS = {"ZOHO_INGEST_CLIENT_SECRET": "CS-ingest-NAO-VAZAR", "ZOHO_INGEST_REFRESH_TOKEN": "RT-NAO-VAZAR",
            "ZOHO_LOGIN_CLIENT_SECRET": "CS-login-NAO-VAZAR"}


def test_tokens_oauth_nao_aparecem_em_paginas_logs_ou_erros(portal, conn, settings, caplog):
    for k, v in SEGREDOS.items():
        setattr(settings, {"ZOHO_INGEST_CLIENT_SECRET": "ingest_client_secret",
                           "ZOHO_INGEST_REFRESH_TOKEN": "ingest_refresh_token",
                           "ZOHO_LOGIN_CLIENT_SECRET": "login_client_secret"}[k], v)
    settings.ingest_client_id = "cid"
    # páginas do admin e saúde
    ad = portal["client"]("ad")
    for url in ("/admin", "/admin/fontes", "/admin/regras", "/admin/usuarios", "/admin/grupos", "/admin/auditoria",
                "/saude", "/login"):
        body = ad.get(url).text
        assert not any(v in body for v in SEGREDOS.values()), url
    # URL de autorização do login só leva o client_id
    from cio import auth
    url, _ = auth.begin_login(settings)
    assert "client_secret" not in url and not any(v in url for v in SEGREDOS.values())
    # falha de renovação de token durante a ingestão: erro registrado, sem segredo em log/banco
    from cio.ingest import run_once
    from cio.zoho import ZohoClient

    def handler(req):
        return httpx.Response(401, json={"error": "invalid_client"})
    api = ZohoClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    caplog.set_level(logging.DEBUG)
    res = run_once(conn, api, settings)
    assert res.errors == 1
    erros = " ".join(r[0] for r in conn.execute("SELECT error FROM ingest_errors").fetchall())
    assert "invalid_client" in erros
    tudo = caplog.text + erros + repr(settings)
    assert not any(v in tudo for v in SEGREDOS.values())


def test_falhas_de_ingestao_nao_publicam_nem_perdem(conn, fake, settings):
    from cio.ingest import run_once
    from cio.zoho import ZohoError
    P = "🔵 "
    fake.add("a", P + "Zoho Desk – Resumo diário A", "<p>a</p>")
    # 1) erro ao obter o cabeçalho de autenticação: não publica, registra, tenta de novo
    original = fake.get_auth_header
    fake.get_auth_header = lambda msg: (_ for _ in ()).throw(ZohoError("HTTP 503", 503, True))
    r = run_once(conn, fake, settings)
    assert r.errors == 1 and conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 0
    fake.get_auth_header = original
    assert run_once(conn, fake, settings).new == 1
    # 2) falha no WorkDrive durante a gravação do original: nada pela metade, recupera no ciclo seguinte
    settings.workdrive_root_folder_id = "root"
    fake.add("b", P + "Zoho Desk – Resumo diário B", "<p>b</p>", received_ms=1_794_000_000_500)
    up = fake.upload
    fake.upload = lambda *a, **k: (_ for _ in ()).throw(ZohoError("HTTP 500 upload", 500, True))
    assert run_once(conn, fake, settings).errors == 1
    assert conn.execute("SELECT count(*) FROM reports WHERE zoho_message_id='b'").fetchone()[0] == 0
    fake.upload = up
    assert run_once(conn, fake, settings).new == 1
    assert conn.execute("SELECT count(*) FROM reports WHERE zoho_message_id='b'").fetchone()[0] == 1
    # 3) queda total da busca: nenhum registro novo, checkpoint preservado
    cp = conn.execute("SELECT checkpoint_ms FROM ingest_state").fetchone()[0]
    fake.fail_search = True
    assert run_once(conn, fake, settings).errors == 1
    assert conn.execute("SELECT checkpoint_ms FROM ingest_state").fetchone()[0] == cp
