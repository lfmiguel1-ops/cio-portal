"""Alertas por e-mail ao administrador (custo zero: SMTP do Zoho Mail, biblioteca padrão).

- Configuração: ADMIN_ALERT_EMAIL + CIO_SMTP_USER + CIO_SMTP_PASSWORD (senha de aplicativo do Zoho Mail).
- Throttle por chave ("ingestao", "backup", "restauracao"): no máximo 1 e-mail a cada ALERT_COOLDOWN_MIN,
  com estado persistente em data_dir/alertas.json.
- O corpo NUNCA inclui conteúdo de relatórios — só contagens, etapa e erros técnicos (mesma política dos logs).
- Falha de envio é apenas registrada em log: alerta não pode derrubar ingestão nem backup.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from .config import Settings
from .db import Database, audit

log = logging.getLogger("cio.alerts")


def _state_path(s: Settings) -> Path:
    return s.data_dir / "alertas.json"


def _last_sent(s: Settings, key: str) -> float:
    try:
        return float(json.loads(_state_path(s).read_text()).get(key, 0.0))
    except (OSError, ValueError):
        return 0.0


def _mark_sent(s: Settings, key: str, when: float) -> None:
    try:
        path = _state_path(s)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data[key] = when
        path.write_text(json.dumps(data))
    except OSError:
        log.warning("Não foi possível persistir o estado de alertas (o throttle vale só nesta execução).")


def configured(s: Settings) -> bool:
    """Alerta totalmente configurado (destinatário + credenciais de envio)."""
    return bool(s.admin_alert_email and s.smtp_user and s.smtp_password)


def send_admin_alert(conn: Database, s: Settings, key: str, subject: str, body: str) -> bool:
    """Envia (com throttle por chave) um alerta para ADMIN_ALERT_EMAIL. True se enviado."""
    if not s.admin_alert_email:
        return False
    now = time.time()
    if now - _last_sent(s, key) < s.alert_cooldown_min * 60:
        log.info("Alerta '%s' suprimido pelo limite de frequência (%s min).", key, s.alert_cooldown_min)
        return False
    if not (s.smtp_user and s.smtp_password):
        log.warning("ADMIN_ALERT_EMAIL definido, mas envio não configurado (CIO_SMTP_USER/CIO_SMTP_PASSWORD).")
        return False
    msg = EmailMessage()
    msg["From"] = s.alert_from or s.smtp_user
    msg["To"] = s.admin_alert_email
    msg["Subject"] = f"[CIO Portal] {subject}"
    msg.set_content(f"Portal: {s.base_url}\n\n{body}\n\n"
                    f"(Alertas de uma mesma categoria são enviados no máximo 1 vez a cada "
                    f"{s.alert_cooldown_min} minutos. Este e-mail não inclui conteúdo de relatórios.)")
    try:
        if s.smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30,
                                                    context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        with server:
            server.ehlo()
            if s.smtp_port != 465:
                server.starttls(context=ssl.create_default_context())
            server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        log.error("Falha ao enviar alerta '%s': %s", key, e)
        return False
    _mark_sent(s, key, now)
    audit(conn, "alerta_enviado", details=key)
    return True
