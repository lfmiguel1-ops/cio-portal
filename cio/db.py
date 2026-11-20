"""Acesso a banco: PostgreSQL (produção, via DATABASE_URL) ou SQLite (desenvolvimento/testes).

O código da aplicação escreve SQL portável com placeholders `?`. Diferenças de dialeto
(busca textual, esquema) ficam isoladas aqui, em access.py e nos arquivos schema_*.sql.
"""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).parent


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Row(tuple):
    """Linha acessível por índice, por nome (row["col"]) e iterável como tupla — igual a sqlite3.Row."""

    _keys: tuple[str, ...] = ()

    def __new__(cls, values, keys):
        obj = super().__new__(cls, values)
        obj._keys = keys
        return obj

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._keys.index(k))
            except ValueError:
                raise KeyError(k) from None
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._keys)


def _pg_row_factory(cursor):
    keys = tuple(c.name for c in cursor.description or ())
    return lambda values: Row(values, keys)


def _to_pg(sql: str) -> str:
    # `?` -> `%s`; `%` literal precisa ser escapado para o psycopg.
    return sql.replace("%", "%%").replace("?", "%s")


class Database:
    def __init__(self, raw, dialect: str):
        self.raw = raw
        self.dialect = dialect

    @property
    def is_pg(self) -> bool:
        return self.dialect == "postgres"

    def execute(self, sql: str, params: tuple | list = ()):
        if self.is_pg:
            return self.raw.execute(_to_pg(sql), tuple(params))
        return self.raw.execute(sql, tuple(params))

    def insert(self, sql: str, params: tuple | list = ()) -> int:
        """INSERT que devolve o id gerado (RETURNING funciona em PostgreSQL e SQLite ≥ 3.35)."""
        return self.execute(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()[0]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.is_pg:
            with self.raw.transaction():
                yield
        else:
            self.raw.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.raw.execute("ROLLBACK")
                raise
            self.raw.execute("COMMIT")

    def executescript(self, script: str) -> None:
        if self.is_pg:
            self.raw.execute(script)  # sem parâmetros: múltiplos comandos permitidos
        else:
            self.raw.executescript(script)

    def close(self) -> None:
        self.raw.close()


def connect(target: Any) -> Database:
    """`target`: Settings, URL postgresql://… ou caminho de arquivo SQLite."""
    url = getattr(target, "database_url", None)
    if url is None and isinstance(target, str) and (
            target.startswith(("postgres://", "postgresql://")) or "dbname=" in target):
        url = target  # URL ou conninfo "chave=valor" do PostgreSQL
    if url:
        import psycopg
        conn = psycopg.connect(url, autocommit=True, row_factory=_pg_row_factory,
                               connect_timeout=10, application_name="cio-portal")
        return Database(conn, "postgres")
    path = target.db_path if hasattr(target, "db_path") else target
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return Database(conn, "sqlite")


AREAS = [("atendimento", "Atendimento"), ("comercial", "Comercial"), ("operacao", "Operação"),
         ("financeiro", "Financeiro"), ("tecnologia", "Tecnologia"), ("comunicados", "Comunicados")]

# Regras iniciais (D6 aprovada em 12/11/2026): tipos conhecidos publicados automaticamente;
# qualquer outro assunto vai para QUARENTENA com confidencialidade 'restrito'.
RULES = [
    (1, 10, "Relatório de reservas", r"^callcenter@europcar\.com\.br$", "Relatório de reservas", "comercial",
     "normal", "interno-gestores"),
    (2, 20, "Zoho Desk – Resumo diário", "", r"Zoho Desk\s*[–-]\s*Resumo diário", "atendimento", "normal", "interno"),
    (3, 30, "Atendimento – alertas", r"^callcenter@europcar\.com\.br$", r"\[Atendimento Europcar\]", "atendimento",
     "alta", "interno"),
    (4, 40, "Diagnóstico de tickets", r"^callcenter@europcar\.com\.br$", r"\[Diagnóstico Europcar\]", "atendimento",
     "normal", "interno-gestores"),
    (5, 50, "Agente COMPLETO INTEGRAL", r"^callcenter@europcar\.com\.br$", "COMPLETO INTEGRAL", "atendimento",
     "alta", "interno-gestores"),
]


# Público padrão das regras iniciais (usado quando nenhum destinatário cadastrado é encontrado).
RULE_GROUPS = {1: "gestores", 2: "geral", 3: "geral", 4: "gestores", 5: "gestores"}


def _columns(db: Database, table: str) -> set[str]:
    if db.is_pg:
        rows = db.execute("SELECT column_name FROM information_schema.columns WHERE table_name=? "
                          "AND table_schema=current_schema()", (table,)).fetchall()
        return {r[0] for r in rows}
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


# Colunas acrescentadas depois da versão inicial (migração idempotente, sem perda de dados).
ADDED_COLUMNS = [
    ("areas", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("reports", "source", "TEXT NOT NULL DEFAULT 'email'"),      # 'email' | 'manual'
    ("reports", "file_path", "TEXT NOT NULL DEFAULT ''"),          # arquivo enviado manualmente
    ("reports", "file_mime", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "description", "TEXT NOT NULL DEFAULT ''"),
    ("search_folders", "match_any", "INTEGER NOT NULL DEFAULT 0"),   # 1 = qualquer uma das palavras
    ("users", "group_id", "INTEGER"),                                  # grupo de acesso da pessoa
    ("classification_rules", "group_id", "INTEGER"),                   # público padrão da regra
    ("classification_rules", "audience_mode", "TEXT NOT NULL DEFAULT 'destinatarios'"),  # ou 'fixo'
    ("reports", "recipients", "TEXT NOT NULL DEFAULT ''"),            # Para/CC do e-mail original
    ("groups", "parent_id", "INTEGER"),                                # grupo pai (ex.: Franqueados)
    ("reports", "attachments_pending", "INTEGER NOT NULL DEFAULT 0"),   # A3: anexos a (re)tentar
    ("reports", "sender_verified", "INTEGER NOT NULL DEFAULT 1"),       # A1: 0 = DMARC não confirmado
    ("ingest_errors", "folder_id", "TEXT NOT NULL DEFAULT ''"),
    ("ingest_errors", "received_ms", "BIGINT NOT NULL DEFAULT 0"),
]


def migrate(db: Database, initial_senders: list[str] | None = None) -> None:
    db.executescript((HERE / f"schema_{db.dialect}.sql").read_text(encoding="utf-8"))
    for table, col, ddl in ADDED_COLUMNS:
        if col not in _columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    for slug, name in AREAS:
        db.execute("INSERT INTO areas(slug, name) VALUES (?,?) ON CONFLICT (slug) DO NOTHING", (slug, name))
    for rid, pos, name, sender, subject, area, prio, conf in RULES:
        db.execute(
            "INSERT INTO classification_rules(id, position, name, sender_pattern, subject_regex, area_id, priority, "
            "confidentiality, action) SELECT ?,?,?,?,?, id, ?,?, 'publicar' FROM areas WHERE slug=? "
            "ON CONFLICT (id) DO NOTHING", (rid, pos, name, sender, subject, prio, conf, area))
    # Fontes iniciais (INGEST_SENDERS) só na primeira vez; depois são geridas pela tela de Administração.
    if not db.execute("SELECT 1 FROM mail_sources LIMIT 1").fetchone():
        for email in initial_senders or []:
            db.execute("INSERT INTO mail_sources(email, note) VALUES (?, 'fonte inicial') ON CONFLICT (email) DO NOTHING",
                       (email.lower(),))
    if db.is_pg:  # mantém a sequência à frente dos ids fixos das regras iniciais
        db.execute("SELECT setval(pg_get_serial_sequence('classification_rules','id'), "
                   "GREATEST((SELECT max(id) FROM classification_rules), 1))")
    db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?) ON CONFLICT (version) DO NOTHING",
               (now_iso(),))
    from . import groups
    groups.seed(db)
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
        # Migração 2: perfis/confidencialidade -> grupos de acesso (uma única vez).
        g = groups.group_ids(db)
        db.execute("UPDATE users SET group_id=? WHERE role='gestor' AND group_id IS NULL", (g["gestores"],))
        db.execute("UPDATE users SET group_id=? WHERE role='operacional' AND group_id IS NULL", (g["geral"],))
        for rid, slug in RULE_GROUPS.items():
            db.execute("UPDATE classification_rules SET group_id=? WHERE id=? AND group_id IS NULL", (g[slug], rid))
        for conf, slug in (("interno", "geral"), ("interno-gestores", "gestores")):
            db.execute("INSERT INTO report_groups(report_id, group_id) SELECT r.id, ? FROM reports r WHERE "
                       "r.confidentiality=? AND NOT EXISTS (SELECT 1 FROM report_groups x WHERE x.report_id=r.id)",
                       (g[slug], conf))
        db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?) ON CONFLICT (version) DO NOTHING",
                   (now_iso(),))
    db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?) ON CONFLICT (version) DO NOTHING",
               (now_iso(),))


def audit(db: Database, action: str, user_id: int | None = None, target: str = "",
          ip: str = "", details: str = "") -> None:
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""
    db.execute(
        "INSERT INTO audit_log(at, user_id, action, target, ip_hash, details) VALUES (?,?,?,?,?,?)",
        (now_iso(), user_id, action, target, ip_hash, details[:500]),
    )
