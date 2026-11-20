from cio.classify import classify, validate_regex
from cio.sanitize import content_hash, html_to_text, safe_filename, sanitize_html

MALICIOUS = """
<div onclick="steal()" style="background:url(https://evil/x.png);color:red">Olá
<script>alert(1)</script><img src="https://tracker.example/p.gif" onerror="alert(2)">
<a href="javascript:alert(3)">link</a><iframe src="https://evil"></iframe>
<form action="https://evil"><input name=p></form><svg onload=alert(4)></svg>
<style>body{display:none}</style><a href="https://ok.example/x">ok</a></div>
"""


def test_sanitize_remove_vetores_xss():
    out = sanitize_html(MALICIOUS).lower()
    for bad in ("<script", "onerror", "onclick", "javascript:", "<iframe", "<form", "<input", "<svg", "onload",
                "tracker.example", "url(", "<style"):
        assert bad not in out, bad
    assert "olá" in out
    assert 'href="https://ok.example/x"' in out
    assert "color:red" in out.replace(" ", "")


def test_texto_extraido_preserva_tabela_e_ignora_script():
    html = "<h2>Título</h2><table><tr><td>17</td><td>Em aberto</td></tr></table><script>x()</script>"
    t = html_to_text(html)
    assert "Título" in t and "17 | Em aberto" in t and "x()" not in t


def test_hash_estavel_com_espacos():
    assert content_hash("<p>a  b</p>\n") == content_hash("<p>a b</p>")
    assert content_hash("<p>a</p>") != content_hash("<p>b</p>")


def test_nome_seguro_sem_path_traversal():
    assert "/" not in safe_filename("../../etc/passwd") and ".." not in safe_filename("../../etc/passwd")
    assert safe_filename("🔵 Relatório — 11/11") == "Relatorio-11_11"


def test_regras_iniciais_publicam_tipos_conhecidos(conn):
    c = classify(conn, "callcenter@europcar.com.br", "🔵 Relatório de reservas — 11/11/2026 | Europcar Brasil")
    assert c.status == "publicado" and c.rule_id == 1 and c.audience_mode == "destinatarios"
    assert conn.execute("SELECT slug FROM groups WHERE id=?", (c.group_id,)).fetchone()[0] == "gestores"


def test_sem_regra_vai_para_quarentena_restrito(conn):
    c = classify(conn, "callcenter@europcar.com.br", "🔵 Algo novo")
    assert (c.status, c.group_id, c.area_id) == ("quarentena", None, None)


def test_regra_publicar_e_remetente_errado(conn):
    assert classify(conn, "callcenter@europcar.com.br", "🔵 [Atendimento Europcar] 2 alertas").status == "publicado"
    # mesmo assunto de remetente não previsto na regra não é publicado
    assert classify(conn, "outro@europcar.com.br", "🔵 [Atendimento Europcar] 2 alertas").status == "quarentena"


def test_regex_invalida_nunca_casa(conn):
    conn.execute("INSERT INTO classification_rules(position, name, subject_regex, action, area_id) "
                 "VALUES (1, 'quebrada', '([', 'publicar', 1)")
    assert classify(conn, "callcenter@europcar.com.br", "🔵 qualquer").status == "quarentena"
    assert validate_regex("([") and validate_regex("a" * 301) and validate_regex("^ok$") is None


def test_imagem_embutida_permitida_externa_e_data_href_bloqueados():
    png = "data:image/png;base64,iVBORw0KGgo="
    out = sanitize_html(f'<img src="{png}" alt="grafico"><img src="https://evil/x.png">'
                        '<a href="data:text/html,<script>alert(1)</script>">x</a><a href="https://ok/">ok</a>')
    assert png in out and "evil" not in out and "data:text" not in out
    assert 'target="_blank"' in out and 'rel="noopener noreferrer nofollow"' in out


def test_imagem_cid_vira_data_uri():
    from email.message import EmailMessage
    from cio.sanitize import inline_cid_images
    m = EmailMessage()
    m.set_content("x")
    m.add_alternative('<p><img src="cid:graf1"></p>', subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG\r\n\x1a\nabc", maintype="image", subtype="png", cid="<graf1>")
    html = inline_cid_images('<p><img src="cid:graf1"></p>', m.as_bytes())
    assert "data:image/png;base64," in sanitize_html(html)
