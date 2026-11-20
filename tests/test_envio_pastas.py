"""Envio manual (PDF/JPG/PNG), áreas configuráveis e pastas de pesquisa."""
import io
import re

import pytest

from tests.test_portal_security import csrf_of, ids_in

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def make_pdf(text: str) -> bytes:
    """PDF mínimo válido com uma linha de texto (sem dependências)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref))
    return out.getvalue()


def area_id(conn, slug):
    return conn.execute("SELECT id FROM areas WHERE slug=?", (slug,)).fetchone()[0]


def enviar(client, conn, data, nome="arquivo.pdf", area="atendimento", grupos=("geral",), titulo="Fechamento frota",
           descricao="frota mensal"):
    gids = [conn.execute("SELECT id FROM groups WHERE slug=?", (g,)).fetchone()[0] for g in grupos]
    return client.post("/enviar", data={"csrf_token": csrf_of(client), "titulo": titulo, "data": "2026-11-10",
                                        "area_id": area_id(conn, area), "priority": "normal",
                                        "grupos": gids, "descricao": descricao},
                       files={"arquivo": (nome, data, "application/octet-stream")})


def test_gestor_envia_pdf_e_texto_entra_na_busca(portal, conn):
    ge = portal["client"]("ge")
    r = enviar(ge, conn, make_pdf("Quilometragem media da frota Guarulhos"))
    assert r.status_code == 303 and r.headers["location"].startswith("/r/")
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    row = conn.execute("SELECT source, file_mime, status, text_plain FROM reports WHERE id=?", (rid,)).fetchone()
    assert (row["source"], row["file_mime"], row["status"]) == ("manual", "application/pdf", "publicado")
    assert "Quilometragem" in row["text_plain"]
    assert rid in ids_in(ge.get("/busca", params={"q": "quilometragem"}).text)
    arq = ge.get(f"/r/{rid}/arquivo")
    assert arq.status_code == 200 and arq.headers["content-type"] == "application/pdf"
    assert "sandbox" not in arq.headers["content-security-policy"]
    dl = ge.get(f"/r/{rid}/download")
    assert dl.headers["content-disposition"].startswith("attachment") and ".pdf" in dl.headers["content-disposition"]
    # operador da área vê; o PDF também é protegido por permissão
    assert portal["client"]("op").get(f"/r/{rid}/arquivo").status_code == 200
    assert portal["client"]("sem").get(f"/r/{rid}/arquivo").status_code == 404


@pytest.mark.parametrize("data,mime", [(PNG, "image/png"), (JPG, "image/jpeg")])
def test_imagem_encontrada_pela_descricao(portal, conn, data, mime):
    ad = portal["client"]("ad")
    r = enviar(ad, conn, data, nome="foto.png", descricao="painel de vistoria do patio")
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    assert ad.get(f"/r/{rid}/arquivo").headers["content-type"] == mime
    assert rid in ids_in(ad.get("/busca", params={"q": "vistoria patio"}).text)
    assert f'src="/r/{rid}/arquivo"' in ad.get(f"/r/{rid}").text


@pytest.mark.parametrize("data", [b"MZ\x90\x00exe", b"<html><script>alert(1)</script>", b"GIF89a....", b""])
def test_rejeita_formato_pelo_conteudo(portal, conn, data):
    r = enviar(portal["client"]("ad"), conn, data, nome="disfarcado.png")
    assert r.status_code == 400 and ("PDF, JPG ou PNG" in r.text or "Selecione" in r.text)
    assert conn.execute("SELECT count(*) FROM reports WHERE source='manual'").fetchone()[0] == 0


def test_limite_de_tamanho_e_duplicado(portal, conn, settings):
    ad = portal["client"]("ad")
    settings.max_attachment_bytes = 100
    assert enviar(ad, conn, PNG + b"\x00" * 200).status_code == 400
    settings.max_attachment_bytes = 25 * 1024 * 1024
    assert enviar(ad, conn, PNG).headers["location"].startswith("/r/")
    assert enviar(ad, conn, PNG, titulo="outro").status_code == 400  # mesmo arquivo


def test_permissoes_de_envio(portal, conn):
    assert portal["client"]("op").get("/enviar").status_code == 404
    assert portal["client"]("op").post("/enviar", data={}).status_code == 404
    ge = portal["client"]("ge")
    assert enviar(ge, conn, PNG, grupos=("diretoria",)).status_code == 400   # grupo acima do dele
    assert enviar(ge, conn, PNG, grupos=("franqueado-1",)).status_code == 400  # grupo que não vê
    assert enviar(ge, conn, PNG, grupos=()).status_code == 400                 # sem público
    assert portal["client"]("f1").get("/enviar").status_code == 404                         # franqueado só consulta
    r = ge.post("/enviar", data={"titulo": "x"}, files={"arquivo": ("a.png", PNG, "image/png")})
    assert r.status_code == 403  # sem CSRF


def test_area_nova_e_desativada(portal, conn):
    ad, ge = portal["client"]("ad"), portal["client"]("ge")
    tok = csrf_of(ad)
    assert ad.post("/admin/areas", data={"name": "Frota Nacional", "active": "1", "csrf_token": tok}).status_code == 303
    assert conn.execute("SELECT slug FROM areas WHERE name='Frota Nacional'").fetchone()["slug"] == "frota-nacional"
    assert "erro=" in ad.post("/admin/areas", data={"name": "Frota Nacional", "csrf_token": tok}).headers["location"]
    # desativar Comercial esconde os relatórios dela do gestor, mas não do admin
    com = portal["reps"]["com_ges"]
    assert com in ids_in(ge.get("/historico").text)
    ad.post("/admin/areas", data={"area_id": area_id(conn, "comercial"), "name": "Comercial", "csrf_token": tok})
    assert com not in ids_in(ge.get("/historico").text)
    assert ge.get(f"/r/{com}").status_code == 404
    assert com in ids_in(ad.get("/historico").text)
    assert portal["client"]("ge").get("/admin/areas").status_code == 404


def test_pastas_de_pesquisa_respeitam_permissao(portal, conn):
    ad = portal["client"]("ad")
    r = ad.post("/admin/pastas", data={"name": "Segredos", "q": "segredo", "active": "1", "position": 1,
                                       "csrf_token": csrf_of(ad)})
    assert r.status_code == 303
    fid = conn.execute("SELECT id FROM search_folders WHERE name='Segredos'").fetchone()["id"]
    ge, op = portal["client"]("ge"), portal["client"]("op")
    assert "Segredos" in ge.get("/pastas").text
    assert ids_in(ge.get(f"/pastas/{fid}").text) == {portal["reps"]["com_ges"]}
    assert ids_in(op.get(f"/pastas/{fid}").text) == set()
    ad.post("/admin/pastas", data={"folder_id": fid, "name": "Segredos", "q": "segredo", "csrf_token": csrf_of(ad)})
    assert ge.get(f"/pastas/{fid}").status_code == 404 and "Segredos" not in ge.get("/pastas").text
    assert op.post("/admin/pastas", data={"name": "x"}).status_code == 404


def test_pasta_qualquer_palavra(portal, conn):
    ad = portal["client"]("ad")
    tok = csrf_of(ad)
    ad.post("/admin/pastas", data={"name": "Todas", "q": "alfa beta", "active": "1", "csrf_token": tok})
    ad.post("/admin/pastas", data={"name": "Qualquer", "q": "alfa beta", "match_any": "1", "active": "1",
                                   "csrf_token": tok})
    ids = {n: conn.execute("SELECT id FROM search_folders WHERE name=?", (n,)).fetchone()["id"] for n in ("Todas", "Qualquer")}
    ge = portal["client"]("ge")
    assert ids_in(ge.get(f"/pastas/{ids['Todas']}").text) == set()   # nenhum relatório tem as duas
    assert ids_in(ge.get(f"/pastas/{ids['Qualquer']}").text) == {portal["reps"]["atd_int"], portal["reps"]["atd_ges"]}


def test_corpo_grande_recusado_antes_de_ler(portal, conn, settings):
    """Corpo acima do limite é recusado (413) sem ser lido inteiro: com e sem login, com e sem Content-Length."""
    settings.max_attachment_bytes = 1024
    grande = PNG + b"\x00" * (3 * 1024 * 1024)
    r = enviar(portal["client"]("ad"), conn, grande)
    assert r.status_code == 413
    assert conn.execute("SELECT count(*) FROM reports WHERE source='manual'").fetchone()[0] == 0
    anon = portal["client"](None)  # sem login
    assert anon.post("/logout", content=b"a" * (2 * 1024 * 1024),
                     headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413

    def pedacos():  # sem Content-Length (transferência em partes)
        for _ in range(3):
            yield b"a" * (1024 * 1024)
    assert anon.post("/logout", content=pedacos(),
                     headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_mensagem_de_erro_nao_vem_do_endereco(portal):
    ad = portal["client"]("ad")
    assert "Ligue para" not in ad.get("/enviar", params={"erro": "Ligue para 0800 e informe sua senha"}).text
