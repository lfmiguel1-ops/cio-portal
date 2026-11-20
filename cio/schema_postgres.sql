-- Esquema PostgreSQL (produção). Idempotente: pode ser executado a cada inicialização.
-- Requer a extensão unaccent, criada uma vez pela TI (deploy/postgres_setup.sql).

CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS areas (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('operacional','gestor','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_areas (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, area_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    zoho_message_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    folder_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    received_at TEXT NOT NULL,
    area_id INTEGER REFERENCES areas(id),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('baixa','normal','alta')),
    confidentiality TEXT NOT NULL DEFAULT 'restrito' CHECK (confidentiality IN ('interno','interno-gestores','restrito')),
    status TEXT NOT NULL DEFAULT 'quarentena' CHECK (status IN ('publicado','quarentena','despublicado','erro')),
    status_reason TEXT NOT NULL DEFAULT '',
    rule_id INTEGER,
    content_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    body_html_sanitized TEXT NOT NULL DEFAULT '',
    text_plain TEXT NOT NULL DEFAULT '',
    original_path TEXT NOT NULL DEFAULT '',
    workdrive_html_id TEXT NOT NULL DEFAULT '',
    workdrive_eml_id TEXT NOT NULL DEFAULT '',
    download_allowed INTEGER NOT NULL DEFAULT 1,
    search_vector tsvector,
    created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    updated_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    UNIQUE (account_id, zoho_message_id)
);
CREATE INDEX IF NOT EXISTS ix_reports_received ON reports(received_at DESC);
CREATE INDEX IF NOT EXISTS ix_reports_status_area ON reports(status, area_id);
CREATE INDEX IF NOT EXISTS ix_reports_search ON reports USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS ix_reports_twin ON reports(account_id, content_sha256);

-- Busca em português que ignora acentos: "caucao" encontra "caução", "reserva" encontra "reservas".
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'pt_unaccent'
                   AND cfgnamespace = current_schema()::regnamespace) THEN
        CREATE TEXT SEARCH CONFIGURATION pt_unaccent (COPY = pg_catalog.portuguese);
        ALTER TEXT SEARCH CONFIGURATION pt_unaccent
            ALTER MAPPING FOR hword, hword_part, word WITH unaccent, portuguese_stem;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION reports_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('pt_unaccent', coalesce(NEW.subject, '')), 'A') ||
        setweight(to_tsvector('pt_unaccent', coalesce(NEW.text_plain, '')), 'B');
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reports_search ON reports;
CREATE TRIGGER trg_reports_search BEFORE INSERT OR UPDATE OF subject, text_plain ON reports
    FOR EACH ROW EXECUTE FUNCTION reports_search_vector();

CREATE TABLE IF NOT EXISTS report_versions (
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    original_path TEXT NOT NULL DEFAULT '',
    workdrive_html_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    PRIMARY KEY (report_id, version)
);

CREATE TABLE IF NOT EXISTS attachments (
    id SERIAL PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    size BIGINT NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    workdrive_id TEXT NOT NULL DEFAULT '',
    text_extracted TEXT NOT NULL DEFAULT '',
    supported INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS classification_rules (
    id SERIAL PRIMARY KEY,
    position INTEGER NOT NULL DEFAULT 100,
    name TEXT NOT NULL,
    sender_pattern TEXT NOT NULL DEFAULT '',
    subject_regex TEXT NOT NULL DEFAULT '',
    folder_id TEXT NOT NULL DEFAULT '',
    area_id INTEGER REFERENCES areas(id),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('baixa','normal','alta')),
    confidentiality TEXT NOT NULL DEFAULT 'restrito' CHECK (confidentiality IN ('interno','interno-gestores','restrito')),
    action TEXT NOT NULL DEFAULT 'quarentena' CHECK (action IN ('publicar','quarentena')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ingest_state (
    account_id TEXT PRIMARY KEY,
    checkpoint_ms BIGINT NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id SERIAL PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL DEFAULT 'normal',
    fetched INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    versions INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingest_errors (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES ingest_runs(id),
    account_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);

CREATE TABLE IF NOT EXISTS workdrive_folders (path TEXT PRIMARY KEY, folder_id TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    last_seen_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    user_id INTEGER,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    ip_hash TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at DESC);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Remetentes autorizados (fontes), administrados pela tela Administração › Fontes de e-mail.
CREATE TABLE IF NOT EXISTS mail_sources (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
    require_prefix INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);

-- Pastas de pesquisa: buscas salvas criadas pelo Administrador (aparecem no menu "Pastas").
CREATE TABLE IF NOT EXISTS search_folders (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    q TEXT NOT NULL DEFAULT '',
    area_slug TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);

-- Grupos de acesso (quem vê). Ver cio/groups.py.
CREATE TABLE IF NOT EXISTS groups (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'interno' CHECK (kind IN ('todos','interno','franqueado')),
    can_upload INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS group_visibility (
    viewer_group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    content_group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (viewer_group_id, content_group_id)
);
CREATE TABLE IF NOT EXISTS report_groups (
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (report_id, group_id)
);
CREATE INDEX IF NOT EXISTS ix_report_groups_group ON report_groups(group_id);

-- Cópias de uma mesma mensagem unidas a um relatório (ex.: mesmo comunicado enviado separadamente).
CREATE TABLE IF NOT EXISTS message_aliases (
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, message_id)
);
