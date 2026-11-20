"""Critérios de aceite de isolamento: usuário não autorizado não localiza, pré-visualiza nem baixa
documento restrito, mesmo conhecendo URL/ID."""
import re
import time

import pytest


def ids_in(html: str) -> set[int]:
    return {int(x) for x in re.findall(r'href="/r/(\d+)"', html)}


def test_sem_login_redireciona(portal):
    c = portal["client"](None)
    for path in ("/", "/busca?q=x", f"/r/{portal['reps']['atd_int']}", "/admin"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_email_do_dominio_fora_da_lista_nao_entra(portal):
    c = portal["client"](None)
    r = c.post("/auth/dev-login", data={"email": "naolistado@europcar.com.br"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?erro=")
    assert c.get("/").status_code == 303


@pytest.mark.parametrize("who,expected", [
    ("op", {"atd_int", "todos"}),
    ("ge", {"atd_int", "atd_ges", "com_ges", "todos"}),
    ("sem", {"todos"}),
    ("f1", {"fr1", "todos"}),
    ("f2", {"fr2", "todos"}),
    ("dir", {"atd_int", "atd_ges", "com_ges", "fin_int", "fr1", "todos"}),
    ("ad", {"atd_int", "atd_ges", "com_ges", "fin_int", "quar", "restr", "fr1", "fr2", "todos"}),
])
def test_historico_mostra_somente_permitidos(portal, who, expected):
    html = portal["client"](who).get("/historico").text
    assert ids_in(html) == {portal["reps"][k] for k in expected}


@pytest.mark.parametrize("who,expected", [
    ("op", set()),
    ("ge", {"com_ges"}),
    ("ad", {"com_ges", "quar", "restr"}),
])
def test_busca_nao_vaza_trechos(portal, who, expected):
    r = portal["client"](who).get("/busca", params={"q": "segredo"})
    assert ids_in(r.text) == {portal["reps"][k] for k in expected}
    if who == "op":
        # nenhum trecho dos documentos proibidos aparece
        for word in ("gama", "epsilon", "zeta", "segredo</mark>"):
            assert word not in r.text


@pytest.mark.parametrize("who", ["op", "sem", "f1"])
@pytest.mark.parametrize("rep", ["atd_ges", "com_ges", "fin_int", "quar", "restr", "fr2"])
def test_acesso_direto_por_id_retorna_404(portal, who, rep):
    c = portal["client"](who)
    rid = portal["reps"][rep]
    for suffix in ("", "/conteudo", "/download", "/anexo/1"):
        assert c.get(f"/r/{rid}{suffix}").status_code == 404, (who, rep, suffix)


def test_inexistente_e_proibido_respondem_igual(portal):
    c = portal["client"]("op")
    a = c.get(f"/r/{portal['reps']['restr']}")
    b = c.get("/r/999999")
    assert a.status_code == b.status_code == 404 and a.text == b.text


def test_autorizado_le_e_baixa(portal):
    c = portal["client"]("op")
    rid = portal["reps"]["atd_int"]
    assert "Resumo Desk" in c.get(f"/r/{rid}").text
    body = c.get(f"/r/{rid}/conteudo")
    assert body.status_code == 200 and "alfa" in body.text
    assert "sandbox" in body.headers["content-security-policy"]
    dl = c.get(f"/r/{rid}/download")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/octet-stream"
    assert dl.headers["content-disposition"].startswith("attachment")


def test_cabecalhos_de_seguranca(portal):
    r = portal["client"]("op").get("/")
    h = r.headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src 'self'" in h[
        "content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"


def test_cookie_de_sessao_seguro(portal):
    c = portal["client"](None)
    r = c.post("/auth/dev-login", data={"email": "op@europcar.com.br"})
    cookie = r.headers["set-cookie"].lower()
    assert "__host-sid=" in cookie and "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_operacional_nao_acessa_admin(portal):
    c = portal["client"]("ge")
    assert c.get("/admin").status_code == 404
    assert c.post(f"/admin/r/{portal['reps']['quar']}", data={"acao": "publicar"}).status_code == 404
    assert c.get("/admin/grupos").status_code == 404


def test_csrf_obrigatorio(portal):
    c = portal["client"]("ad")
    rid = portal["reps"]["quar"]
    assert c.post(f"/admin/r/{rid}", data={"acao": "publicar", "area_id": 1}).status_code == 403
    assert c.post("/logout").status_code == 403
    assert c.get("/").status_code == 200  # sessão continua válida


def csrf_of(c):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/").text).group(1)


def test_admin_publica_e_auditoria_registra(portal, conn):
    ad, op = portal["client"]("ad"), portal["client"]("op")
    rid = portal["reps"]["quar"]
    assert rid not in ids_in(op.get("/historico").text)
    atd = conn.execute("SELECT id FROM areas WHERE slug='atendimento'").fetchone()[0]
    geral = conn.execute("SELECT id FROM groups WHERE slug='geral'").fetchone()[0]
    sem_grupo = ad.post(f"/admin/r/{rid}", data={"acao": "publicar", "area_id": atd, "csrf_token": csrf_of(ad)})
    assert sem_grupo.status_code == 403  # publicar exige ao menos um grupo
    r = ad.post(f"/admin/r/{rid}", data={"acao": "publicar", "area_id": atd, "grupos": geral,
                                          "csrf_token": csrf_of(ad)})
    assert r.status_code == 303
    assert rid in ids_in(op.get("/historico").text)
    actions = [a for (a,) in conn.execute("SELECT action FROM audit_log")]
    assert "admin_publicar" in actions and "login" in actions


def test_logout_invalida_sessao(portal):
    c = portal["client"]("op")
    assert c.post("/logout", data={"csrf_token": csrf_of(c)}).status_code == 303
    assert c.get("/").status_code == 303


def test_sessao_expira_por_inatividade_e_usuario_desativado(portal, conn, settings):
    c = portal["client"]("op")
    conn.execute("UPDATE sessions SET last_seen_at=?", (time.time() - settings.session_idle_hours * 3600 - 5,))
    assert c.get("/").status_code == 303
    c2 = portal["client"]("ge")
    conn.execute("UPDATE users SET active=0 WHERE email='ge@europcar.com.br'")
    assert c2.get("/").status_code == 303


@pytest.mark.parametrize("q", ['"', 'NEAR(a b)', "a OR b", "*", "a AND", "subject:x", "'; DROP TABLE reports;--",
                               "\u0000", "ç" * 300])
def test_busca_resiste_a_sintaxe_maliciosa(portal, q):
    r = portal["client"]("ge").get("/busca", params={"q": q})
    assert r.status_code == 200


def test_busca_sem_acento_e_frase(portal):
    c = portal["client"]("ge")
    assert portal["reps"]["atd_ges"] in ids_in(c.get("/busca", params={"q": "diagnostico"}).text)
    assert portal["reps"]["com_ges"] in ids_in(c.get("/busca", params={"q": '"receita gama"'}).text)
    assert not ids_in(c.get("/busca", params={"q": '"gama receita"'}).text)


def test_limite_de_taxa_na_busca(portal):
    c = portal["client"]("op")
    codes = [c.get("/busca", params={"q": "x"}).status_code for _ in range(32)]
    assert codes.count(429) >= 1


def test_xss_no_assunto_e_escapado(portal, conn):
    from tests.conftest import add_report
    rid = add_report(conn, "x", '<script>alert(1)</script>"><img src=x onerror=alert(2)>', "texto", "atendimento")
    html = portal["client"]("op").get(f"/r/{rid}").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_path_traversal_no_download(portal, conn):
    conn.execute("UPDATE reports SET original_path='../../../etc/passwd' WHERE id=?", (portal["reps"]["atd_int"],))
    assert portal["client"]("op").get(f"/r/{portal['reps']['atd_int']}/download").status_code == 404


def test_usuario_admin_so_cadastra_dominio(portal):
    ad = portal["client"]("ad")
    r = ad.post("/admin/usuarios", data={"email": "x@gmail.com", "role": "admin", "csrf_token": csrf_of(ad)})
    assert r.status_code == 403


def test_admin_gerencia_fontes(portal, conn):
    ad = portal["client"]("ad")
    tok = csrf_of(ad)
    assert "callcenter@europcar.com.br" in ad.get("/admin/fontes").text
    r = ad.post("/admin/fontes", data={"email": "Frota@Europcar.com.br", "require_prefix": "1", "active": "1",
                                       "csrf_token": tok})
    assert r.status_code == 303 and r.headers["location"] == "/admin/fontes"
    assert conn.execute("SELECT email FROM mail_sources WHERE email='frota@europcar.com.br'").fetchone()
    bad = ad.post("/admin/fontes", data={"email": "nao-e-email", "csrf_token": tok})
    assert "erro=" in bad.headers["location"]
    dup = ad.post("/admin/fontes", data={"email": "frota@europcar.com.br", "csrf_token": tok})
    assert "erro=" in dup.headers["location"]
    sid = conn.execute("SELECT id FROM mail_sources WHERE email='frota@europcar.com.br'").fetchone()["id"]
    ad.post("/admin/fontes", data={"source_id": sid, "require_prefix": "1", "csrf_token": tok})  # desativa
    assert conn.execute("SELECT active FROM mail_sources WHERE id=?", (sid,)).fetchone()["active"] == 0
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action='fonte_alterada'").fetchone()[0] == 2
    # sem CSRF e sem perfil admin: bloqueado
    assert ad.post("/admin/fontes", data={"email": "x@europcar.com.br"}).status_code == 403
    assert portal["client"]("ge").get("/admin/fontes").status_code == 404


def test_franqueado_nao_ve_outro_franqueado_nem_na_busca(portal):
    f1 = portal["client"]("f1")
    r = f1.get("/busca", params={"q": "omega"})
    assert ids_in(r.text) == {portal["reps"]["fr1"]}
    for path in ("", "/conteudo", "/download"):
        assert f1.get(f"/r/{portal['reps']['fr2']}{path}").status_code == 404
    assert f1.get("/enviar").status_code == 404  # franqueados só consultam
    assert "Administração" not in f1.get("/").text


def test_admin_cria_grupo_franqueado_e_define_visibilidade(portal, conn):
    ad = portal["client"]("ad")
    tok = csrf_of(ad)
    dir_id = conn.execute("SELECT id FROM groups WHERE slug='diretoria'").fetchone()[0]
    r = ad.post("/admin/grupos", data={"name": "Franqueado Cuiabá", "kind": "franqueado", "active": "1",
                                       "csrf_token": tok})
    assert r.status_code == 303
    gid = conn.execute("SELECT id FROM groups WHERE slug='franqueado-cuiaba'").fetchone()[0]
    # diretoria passa a ver o novo franqueado (mantendo o que já via)
    vistos = [str(x[0]) for x in conn.execute("SELECT content_group_id FROM group_visibility WHERE viewer_group_id=?",
                                              (dir_id,)).fetchall() if x[0] != dir_id]
    ad.post("/admin/grupos", data={"group_id": dir_id, "name": "Diretoria", "kind": "interno", "active": "1",
                                   "can_upload": "1", "ve": vistos + [str(gid)], "csrf_token": tok})
    assert conn.execute("SELECT 1 FROM group_visibility WHERE viewer_group_id=? AND content_group_id=?",
                        (dir_id, gid)).fetchone()
    # usuário entra no grupo pelo cadastro
    ad.post("/admin/usuarios", data={"email": "cuiaba@europcar.com.br", "group_id": gid, "active": "1",
                                     "csrf_token": tok})
    assert conn.execute("SELECT group_id FROM users WHERE email='cuiaba@europcar.com.br'").fetchone()[0] == gid
    # 'Todos' não é editável nem atribuível
    todos = conn.execute("SELECT id FROM groups WHERE slug='todos'").fetchone()[0]
    assert ad.post("/admin/grupos", data={"group_id": todos, "name": "X", "kind": "interno",
                                          "csrf_token": tok}).status_code == 403
    assert ad.post("/admin/usuarios", data={"email": "y@europcar.com.br", "group_id": todos,
                                            "csrf_token": tok}).status_code == 403


def _cidades(conn):
    from tests.conftest import add_group, add_report, add_user
    pai = conn.execute("SELECT id FROM groups WHERE slug='franqueados'").fetchone()[0]
    ids = {}
    for slug in ("jpa", "vcp", "ssa"):
        g = add_group(conn, f"franq-{slug}", f"Franqueado {slug}")
        conn.execute("UPDATE groups SET parent_id=? WHERE id=?", (pai, g))
        add_user(conn, f"{slug}@europcar.com.br", group=f"franq-{slug}")
    ids["rede"] = add_report(conn, "c1", "Comunicado à rede", "kappa rede", "comercial", groups=("franqueados",))
    ids["jpa"] = add_report(conn, "c2", "Resultado JPA", "kappa jpa", "comercial", groups=("franq-jpa",))
    ids["vcp"] = add_report(conn, "c3", "Resultado VCP", "kappa vcp", "comercial", groups=("franq-vcp",))
    add_user(conn, "fin@europcar.com.br", group="financeiro")
    ids["fin"] = add_report(conn, "c4", "Fechamento financeiro", "kappa caixa", "financeiro", groups=("financeiro",))
    return ids


def test_hierarquia_de_franqueados_e_financeiro(portal, conn):
    c = _cidades(conn)
    ver = lambda who: ids_in(portal["client"](who).get("/busca", params={"q": "kappa"}).text)
    assert ver("jpa") == {c["rede"], c["jpa"]}                 # vê o grupo pai e a própria cidade
    assert ver("ssa") == {c["rede"]}                           # não vê outras cidades
    assert ver("dir") == {c["rede"], c["jpa"], c["vcp"], c["fin"]}  # diretoria vê Franqueados (e subgrupos) e Financeiro
    assert ver("ge") == set()                                  # gestores não veem financeiro nem franqueados
    assert ver("fin") == {c["fin"]}
    assert portal["client"]("jpa").get(f"/r/{c['vcp']}").status_code == 404


def test_grupo_pai_sem_ciclo(portal, conn):
    ad = portal["client"]("ad")
    tok = csrf_of(ad)
    pai = conn.execute("SELECT id FROM groups WHERE slug='franqueados'").fetchone()[0]
    r = ad.post("/admin/grupos", data={"name": "Franqueado Campinas", "kind": "franqueado", "parent_id": pai,
                                       "active": "1", "csrf_token": tok})
    assert r.status_code == 303
    filho = conn.execute("SELECT id, parent_id FROM groups WHERE slug='franqueado-campinas'").fetchone()
    assert filho["parent_id"] == pai
    # tornar o pai filho do próprio filho é recusado
    r = ad.post("/admin/grupos", data={"group_id": pai, "name": "Franqueados", "kind": "franqueado",
                                       "parent_id": filho["id"], "active": "1", "csrf_token": tok})
    assert "erro=" in r.headers["location"]
    assert conn.execute("SELECT parent_id FROM groups WHERE id=?", (pai,)).fetchone()[0] is None


def test_a2_admin_despublica_e_corrige_publico_pelo_detalhe(portal, conn):
    ad, op = portal["client"]("ad"), portal["client"]("op")
    rid = portal["reps"]["atd_int"]
    assert "Administração deste relatório" in ad.get(f"/r/{rid}").text
    r = ad.post(f"/admin/r/{rid}", data={"acao": "despublicar", "voltar": "detalhe", "csrf_token": csrf_of(ad)})
    assert r.headers["location"] == f"/r/{rid}"
    assert op.get(f"/r/{rid}").status_code == 404
    gest = conn.execute("SELECT id FROM groups WHERE slug='gestores'").fetchone()[0]
    ad.post(f"/admin/r/{rid}", data={"acao": "publicar", "grupos": gest, "voltar": "detalhe", "csrf_token": csrf_of(ad)})
    assert op.get(f"/r/{rid}").status_code == 404 and portal["client"]("ge").get(f"/r/{rid}").status_code == 200
    assert "Administração deste relatório" not in portal["client"]("ge").get(f"/r/{rid}").text


def test_remetente_nao_autenticado_exige_confirmacao(portal, conn):
    ad = portal["client"]("ad")
    rid = portal["reps"]["quar"]
    conn.execute("UPDATE reports SET sender_verified=0 WHERE id=?", (rid,))
    geral = conn.execute("SELECT id FROM groups WHERE slug='geral'").fetchone()[0]
    base = {"acao": "publicar", "grupos": geral, "csrf_token": csrf_of(ad)}
    assert ad.post(f"/admin/r/{rid}", data=base).status_code == 403
    assert ad.post(f"/admin/r/{rid}", data={**base, "confirmar_remetente": "1"}).status_code == 303


def test_m1_menu_enviar_segue_permissao_do_grupo(portal, conn):
    assert "Enviar arquivo" not in portal["client"]("op").get("/").text
    conn.execute("UPDATE groups SET can_upload=1 WHERE slug='geral'")
    assert "Enviar arquivo" in portal["client"]("op").get("/").text


def test_m2_ultimo_admin_nao_perde_acesso_e_edicao_preenchida(portal, conn):
    ad = portal["client"]("ad")
    r = ad.post("/admin/usuarios", data={"email": "ad@europcar.com.br", "active": "1", "csrf_token": csrf_of(ad)})
    assert "ultimo-admin" in r.headers["location"]
    assert conn.execute("SELECT role FROM users WHERE email='ad@europcar.com.br'").fetchone()[0] == "admin"
    pagina = ad.get("/admin/usuarios", params={"editar": "ge@europcar.com.br"}).text
    assert 'value="ge@europcar.com.br"' in pagina and "Alterar ge@europcar.com.br" in pagina


def test_m3_nao_admin_nao_publica_para_todos_nem_grupo_pai(portal, conn):
    ge = portal["client"]("ge")
    pagina = ge.get("/enviar").text
    assert "> Todos</label>" not in pagina and "Franqueados</label>" not in pagina
    assert "Gestores</label>" in pagina


def test_m4_modo_teste_nao_sobe_em_endereco_publico(monkeypatch):
    from cio.config import Settings
    monkeypatch.setenv("CIO_ENV", "dev")
    monkeypatch.setenv("CIO_BASE_URL", "https://relatorios.europcar.com.br")
    with pytest.raises(RuntimeError):
        Settings().validate()


def test_m8_login_nao_mostra_texto_livre(portal):
    c = portal["client"](None)
    assert "Ligue para 0800" not in c.get("/login", params={"erro": "Ligue para 0800"}).text
    assert "ainda não foi liberado" in c.get("/login", params={"erro": "sem-acesso"}).text


def test_m7_indicador_de_sincronizacao(portal, conn):
    assert "Aguardando a primeira sincronização" in portal["client"]("op").get("/").text
    conn.execute("INSERT INTO ingest_runs(started_at, finished_at, ok) VALUES ('2026-11-14T10:00:00Z',"
                 "'2026-11-14T10:00:05Z', 1)")
    assert "Atualizado em 14/11/2026" in portal["client"]("op").get("/").text
