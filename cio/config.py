"""Configuração lida exclusivamente de variáveis de ambiente (nunca do código)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "sim"}


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("CIO_ENV", "production"))
    # PostgreSQL em produção (ex.: postgresql://cio_app:SENHA@postgres:5432/cio_portal). Vazio = SQLite em data_dir.
    database_url: str = field(repr=False, default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CIO_DATA_DIR", "./data")))
    base_url: str = field(default_factory=lambda: os.environ.get("CIO_BASE_URL", "https://localhost"))
    secret_key: str = field(repr=False, default_factory=lambda: os.environ.get("CIO_SECRET_KEY", ""))
    allowed_domain: str = field(default_factory=lambda: os.environ.get("CIO_ALLOWED_DOMAIN", "europcar.com.br").lower())

    # Zoho — data center .com (verificado: contas em zoho.com)
    zoho_accounts_url: str = field(default_factory=lambda: os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com"))
    zoho_mail_api: str = field(default_factory=lambda: os.environ.get("ZOHO_MAIL_API", "https://mail.zoho.com/api"))
    zoho_workdrive_api: str = field(default_factory=lambda: os.environ.get("ZOHO_WORKDRIVE_API", "https://www.zohoapis.com/workdrive/api/v1"))

    # Cliente OAuth de INGESTÃO (Mail leitura + WorkDrive)
    ingest_client_id: str = field(default_factory=lambda: os.environ.get("ZOHO_INGEST_CLIENT_ID", ""))
    ingest_client_secret: str = field(repr=False, default_factory=lambda: os.environ.get("ZOHO_INGEST_CLIENT_SECRET", ""))
    ingest_refresh_token: str = field(repr=False, default_factory=lambda: os.environ.get("ZOHO_INGEST_REFRESH_TOKEN", ""))
    mail_account_ids: list[str] = field(default_factory=lambda: _list("ZOHO_MAIL_ACCOUNT_IDS"))
    workdrive_root_folder_id: str = field(default_factory=lambda: os.environ.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID", ""))

    # Cliente OAuth de LOGIN (somente openid email profile)
    login_client_id: str = field(default_factory=lambda: os.environ.get("ZOHO_LOGIN_CLIENT_ID", ""))
    login_client_secret: str = field(repr=False, default_factory=lambda: os.environ.get("ZOHO_LOGIN_CLIENT_SECRET", ""))

    # Ingestão
    ingest_enabled: bool = field(default_factory=lambda: _bool("INGEST_ENABLED", False))
    ingest_interval_min: int = field(default_factory=lambda: int(os.environ.get("INGEST_INTERVAL_MIN", "10")))
    authorized_senders: list[str] = field(default_factory=lambda: _list(
        "INGEST_SENDERS", "callcenter@europcar.com.br,luis.miguel@europcar.com.br"))
    # Caixas lidas pelo portal: estão sempre em cópia, por isso não definem grupo de público.
    monitored_emails: list[str] = field(default_factory=lambda: _list("MONITORED_EMAILS", "luis.miguel@europcar.com.br"))
    # A1: exige DMARC=pass registrado pelo Zoho; sem isso o relatório vai para a fila de revisão.
    require_dmarc: bool = field(default_factory=lambda: _bool("REQUIRE_DMARC", True))
    subject_prefix: str = field(default_factory=lambda: os.environ.get("INGEST_SUBJECT_PREFIX", "🔵"))
    max_attachment_bytes: int = field(default_factory=lambda: int(os.environ.get("MAX_ATTACHMENT_MB", "25")) * 1024 * 1024)
    admin_alert_email: str = field(default_factory=lambda: os.environ.get("ADMIN_ALERT_EMAIL", ""))

    # Sessão
    session_idle_hours: int = field(default_factory=lambda: int(os.environ.get("SESSION_IDLE_HOURS", "8")))
    session_max_days: int = field(default_factory=lambda: int(os.environ.get("SESSION_MAX_DAYS", "7")))

    # Retenção / backup
    retention_months: int = field(default_factory=lambda: int(os.environ.get("RETENTION_MONTHS", "24")))
    backup_keep_daily: int = field(default_factory=lambda: int(os.environ.get("BACKUP_KEEP_DAILY", "30")))
    backup_keep_monthly: int = field(default_factory=lambda: int(os.environ.get("BACKUP_KEEP_MONTHLY", "12")))
    # Volume SEPARADO de /data (outro disco/volume, copiado para fora do servidor pela TI).
    backup_dir: str = field(default_factory=lambda: os.environ.get("BACKUP_DIR", ""))
    # Banco vazio só para o teste de restauração (opcional). Sem ele, o teste só lê o dump.
    restore_test_database_url: str = field(repr=False, default_factory=lambda: os.environ.get(
        "RESTORE_TEST_DATABASE_URL", ""))
    pg_bin_dir: str = field(default_factory=lambda: os.environ.get("PG_BIN_DIR", ""))
    backup_to_workdrive: bool = field(default_factory=lambda: _bool("BACKUP_TO_WORKDRIVE", False))

    # Alertas ao administrador (SMTP do Zoho Mail; use senha de APLICATIVO, gerada no webmail — não a do login)
    alert_cooldown_min: int = field(default_factory=lambda: int(os.environ.get("ALERT_COOLDOWN_MIN", "60")))
    smtp_host: str = field(default_factory=lambda: os.environ.get("CIO_SMTP_HOST", "smtp.zoho.com"))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("CIO_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("CIO_SMTP_USER", ""))
    smtp_password: str = field(repr=False, default_factory=lambda: os.environ.get("CIO_SMTP_PASSWORD", ""))
    alert_from: str = field(default_factory=lambda: os.environ.get("CIO_ALERT_FROM", ""))

    @property
    def is_dev(self) -> bool:
        return self.env in {"dev", "test"}

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cio.db"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def backups_dir(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else self.data_dir / "backups"

    def validate(self) -> None:
        if not self.is_dev and len(self.secret_key) < 32:
            raise RuntimeError("CIO_SECRET_KEY ausente ou curta (mínimo 32 caracteres) em produção.")
        if not self.is_dev and not self.base_url.startswith("https://"):
            raise RuntimeError("CIO_BASE_URL deve usar https:// em produção.")
        # M4: o login simulado do modo de teste nunca pode ficar exposto num endereço público.
        if self.is_dev:
            from urllib.parse import urlparse
            host = (urlparse(self.base_url).hostname or "").lower()
            if host not in {"localhost", "127.0.0.1", "testserver", ""}:
                raise RuntimeError("CIO_ENV=dev/test só é permitido com CIO_BASE_URL local (localhost).")


def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.originals_dir.mkdir(parents=True, exist_ok=True)
    s.backups_dir.mkdir(parents=True, exist_ok=True)
    return s
