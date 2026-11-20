from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from cio.config import Settings
from cio.db import connect, migrate
from cio.zoho import MailAttachment, MailMessage, ZohoError


PG_URL = os.environ.get("CIO_TEST_DATABASE_URL", "")  # ex.: postgresql://cio_app@/cio_portal?host=/tmp&port=55432


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("CIO_ENV", "test")
    monkeypatch.setenv("CIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CIO_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ZOHO_MAIL_ACCOUNT_IDS", "acc1")
    monkeypatch.setenv("ZOHO_LOGIN_CLIENT_ID", "login-client")
    schema = None
    if PG_URL:
        # Cada teste usa um schema próprio no PostgreSQL (isolamento total), removido ao final.
        import uuid
        from urllib.parse import quote
        schema = "t_" + uuid.uuid4().hex[:12]
        admin = connect(PG_URL)
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.close()
        sep = "&" if "?" in PG_URL else "?"
        monkeypatch.setenv("DATABASE_URL", f"{PG_URL}{sep}options={quote(f'-csearch_path={schema},public')}")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    from cio.config import get_settings
    yield get_settings()
    if schema:
        admin = connect(PG_URL)
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


requires_sqlite = pytest.mark.skipif(bool(PG_URL), reason="backup próprio só existe no modo SQLite")


@pytest.fixture
def conn(settings):
    c = connect(settings)
    migrate(c, settings.authorized_senders)
    yield c
    c.close()


@dataclass
class FakeZoho:
    """Simula Zoho Mail/WorkDrive. Nunca acessa a rede."""
    messages: list[MailMessage] = field(default_factory=list)
    contents: dict[str, str] = field(default_factory=dict)
    fail_content: set[str] = field(default_factory=set)
    fail_search: bool = False
    attachments: dict[str, list[tuple[MailAttachment, bytes]]] = field(default_factory=dict)
    uploads: list[tuple[str, str]] = field(default_factory=list)
    folders: list[tuple[str, str]] = field(default_factory=list)
    content_calls: int = 0

    def add(self, mid: str, subject: str, html: str, sender: str = "callcenter@europcar.com.br",
            received_ms: int = 1_794_000_000_000, att: bool = False, folder: str = "f1",
            recipients: tuple = ()) -> MailMessage:
        m = MailMessage("acc1", mid, folder, sender, subject, received_ms, att, tuple(recipients))
        self.messages.append(m)
        self.contents[mid] = html
        return m

    def search_messages(self, account_id, sender, since_ms):
        if self.fail_search:
            raise ZohoError("HTTP 503", 503, True)
        return iter([m for m in self.messages if m.sender == sender and m.received_ms >= since_ms])

    def excluded_folder_ids(self, account_id):
        return {"sent"}

    dmarc_fail: set = field(default_factory=set)
    fail_attachments: set = field(default_factory=set)

    def get_auth_header(self, msg):
        res = "fail" if msg.message_id in self.dmarc_fail else "pass"
        return (f"ARC-Authentication-Results: i=1; mx.zohomail.com;\r\n\tdkim={res};\r\n"
                f"\tdmarc={res} header.from=<{msg.sender}>\r\nFrom: {msg.sender}\r\n")

    def get_content(self, msg):
        self.content_calls += 1
        if msg.message_id in self.fail_content:
            raise ZohoError("HTTP 500 em GET /content", 500, True)
        return self.contents[msg.message_id]

    def get_original(self, msg):
        return b"From: x\r\n\r\n" + self.contents[msg.message_id].encode()

    def list_attachments(self, msg):
        if msg.message_id in self.fail_attachments:
            raise ZohoError("HTTP 500 em GET /attachmentinfo", 500, True)
        return [a for a, _ in self.attachments.get(msg.message_id, [])]

    def get_attachment(self, msg, att):
        return dict((a.attachment_id, d) for a, d in self.attachments[msg.message_id])[att.attachment_id]

    def ensure_folder(self, parent_id, name):
        fid = f"wd-{len(self.folders) + 1}"
        self.folders.append((parent_id, name))
        return fid

    def upload(self, parent_id, filename, data, mime):
        self.uploads.append((parent_id, filename))
        return f"res-{len(self.uploads)}"


@pytest.fixture
def fake():
    return FakeZoho()


def area_id(conn, slug):
    return conn.execute("SELECT id FROM areas WHERE slug=?", (slug,)).fetchone()["id"]


def group_id(conn, slug):
    row = conn.execute("SELECT id FROM groups WHERE slug=?", (slug,)).fetchone()
    return row["id"] if row else None


def add_group(conn, slug, name, kind="franqueado", sees=()):
    gid = conn.insert("INSERT INTO groups(slug, name, kind) VALUES (?,?,?)", (slug, name, kind))
    conn.execute("INSERT INTO group_visibility VALUES (?,?)", (gid, gid))
    for other in sees:
        conn.execute("INSERT INTO group_visibility VALUES (?,?)", (gid, group_id(conn, other)))
    return gid


def add_user(conn, email, role="operacional", group=None):
    return conn.insert("INSERT INTO users(email, role, group_id) VALUES (?,?,?)",
                       (email, role, group_id(conn, group) if group else None))


CONF_TO_GROUPS = {"interno": ("geral",), "interno-gestores": ("gestores",), "restrito": ()}


def add_report(conn, mid, subject, text, area, conf="interno", status="publicado", priority="normal",
               received="2026-11-10T10:00:00Z", original_path="", groups=None):
    rid = conn.insert(
        "INSERT INTO reports(zoho_message_id, account_id, sender, subject, received_at, area_id, priority, "
        "confidentiality, status, content_sha256, body_html_sanitized, text_plain, original_path) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "acc1", "callcenter@europcar.com.br", subject, received, area_id(conn, area) if area else None,
         priority, conf, status, mid, f"<p>{text}</p>", text, original_path))
    for slug in (groups if groups is not None else CONF_TO_GROUPS.get(conf, ())):
        conn.execute("INSERT INTO report_groups VALUES (?,?)", (rid, group_id(conn, slug)))
    return rid


@pytest.fixture
def portal(settings, conn):
    """Cenário de isolamento: Geral (op), Gestores (ge), admin (ad), sem grupo (sem),
    dois franqueados (f1, f2) e diretoria (dir)."""
    from cio.app import create_app

    ids = {
        "op": add_user(conn, "op@europcar.com.br", group="geral"),
        "ge": add_user(conn, "ge@europcar.com.br", group="gestores"),
        "ad": add_user(conn, "ad@europcar.com.br", "admin"),
        "sem": add_user(conn, "sem@europcar.com.br"),
    }
    add_group(conn, "franqueado-1", "Franqueado 1")
    add_group(conn, "franqueado-2", "Franqueado 2")
    conn.execute("INSERT INTO group_visibility SELECT d.id, f.id FROM groups d, groups f "
                 "WHERE d.slug='diretoria' AND f.slug='franqueado-1'")
    ids["f1"] = add_user(conn, "f1@europcar.com.br", group="franqueado-1")
    ids["f2"] = add_user(conn, "f2@europcar.com.br", group="franqueado-2")
    ids["dir"] = add_user(conn, "dir@europcar.com.br", group="diretoria")
    orig = settings.originals_dir / "r.html"
    orig.write_text("<p>original</p>")
    rel = str(orig.relative_to(settings.data_dir))
    reps = {
        "atd_int": add_report(conn, "m1", "Resumo Desk", "tickets abertos alfa", "atendimento", original_path=rel),
        "atd_ges": add_report(conn, "m2", "Diagnóstico", "auditoria sigilosa beta", "atendimento", "interno-gestores"),
        "com_ges": add_report(conn, "m3", "Relatório de reservas", "receita gama segredo", "comercial",
                              "interno-gestores", original_path=rel),
        "fin_int": add_report(conn, "m4", "Financeiro", "netting delta", "financeiro", groups=("diretoria",)),
        "quar": add_report(conn, "m5", "Quarentena", "conteudo epsilon segredo", "atendimento", "interno",
                           "quarentena"),
        "restr": add_report(conn, "m6", "Restrito", "dado restrito zeta segredo", "atendimento", "restrito"),
    }
    app = create_app(settings)

    def client_for(who: str | None) -> TestClient:
        c = TestClient(app, base_url="https://testserver", follow_redirects=False)
        if who:
            r = c.post("/auth/dev-login", data={"email": f"{who}@europcar.com.br" if "@" not in who else who})
            assert r.status_code == 303 and r.headers["location"] == "/", r.text
        return c

    reps["fr1"] = add_report(conn, "m7", "Resultado Franqueado 1", "vendas omega franquia", "comercial",
                             groups=("franqueado-1",))
    reps["fr2"] = add_report(conn, "m8", "Resultado Franqueado 2", "vendas omega franquia", "comercial",
                             groups=("franqueado-2",))
    reps["todos"] = add_report(conn, "m9", "Comunicado geral", "comunicado sigma rede", "comunicados",
                               groups=("todos",))
    return {"app": app, "client": client_for, "ids": ids, "reps": reps}
