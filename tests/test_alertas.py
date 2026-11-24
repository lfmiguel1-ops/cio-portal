"""Alertas por e-mail ao admin: throttle por chave, não-configurado não quebra,
falha de envio não marca throttle (tenta de novo no próximo ciclo) e nunca interrompe a chamada.
O corpo é texto técnico — o módulo não aceita conteúdo de relatórios."""
import json

from cio import alerts


class FakeSMTP:
    """Substituto de smtplib.SMTP/SMTP_SSL: nunca acessa a rede."""
    sends: list = []
    fail = False

    def __init__(self, host, port, timeout=30, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise OSError("conexão recusada")
        FakeSMTP.sends.append((msg["To"], msg["Subject"]))


def _configurar(settings):
    settings.admin_alert_email = "admin@europcar.com.br"
    settings.smtp_user = "portal@europcar.com.br"
    settings.smtp_password = "senha-de-aplicativo"


def test_envia_uma_vez_e_aplica_throttle_por_chave(conn, settings, monkeypatch):
    _configurar(settings)
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sends, FakeSMTP.fail = [], False
    assert alerts.send_admin_alert(conn, settings, "ingestao", "Falha na captura", "corpo técnico")
    assert not alerts.send_admin_alert(conn, settings, "ingestao", "Falha de novo", "corpo técnico 2")
    assert alerts.send_admin_alert(conn, settings, "backup", "Backup falhou", "corpo técnico 3")
    assert [(to, sub) for to, sub in FakeSMTP.sends] == [
        ("admin@europcar.com.br", "[CIO Portal] Falha na captura"),
        ("admin@europcar.com.br", "[CIO Portal] Backup falhou"),
    ]
    # estado persistente: o throttle sobrevive a reinícios do processo
    assert "ingestao" in json.loads((settings.data_dir / "alertas.json").read_text())
    # rastreável na auditoria (sem conteúdo no log)
    acoes = {r[0] for r in conn.execute("SELECT action FROM audit_log").fetchall()}
    assert "alerta_enviado" in acoes


def test_sem_configuracao_retorna_false_e_nao_quebra(conn, settings, monkeypatch):
    # sem destinatário e sem SMTP: ingestion/backup seguem normalmente
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sends, FakeSMTP.fail = [], False
    assert alerts.send_admin_alert(conn, settings, "ingestao", "S", "x") is False
    assert FakeSMTP.sends == []
    settings.admin_alert_email = "admin@europcar.com.br"  # destinatário, mas sem SMTP
    assert alerts.send_admin_alert(conn, settings, "ingestao", "S", "x") is False
    assert FakeSMTP.sends == []


def test_falha_de_envio_nao_marca_throttle(conn, settings, monkeypatch):
    _configurar(settings)
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sends, FakeSMTP.fail = [], True
    assert alerts.send_admin_alert(conn, settings, "backup", "S", "x") is False
    assert alerts.send_admin_alert(conn, settings, "backup", "S", "x") is False  # tentou de novo
    FakeSMTP.fail = False
    assert alerts.send_admin_alert(conn, settings, "backup", "S", "x") is True


def test_cooldown_curto_permite_reenvio(conn, settings, monkeypatch):
    _configurar(settings)
    settings.alert_cooldown_min = 0  # sem espera
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sends, FakeSMTP.fail = [], False
    assert alerts.send_admin_alert(conn, settings, "ingestao", "S1", "x")
    assert alerts.send_admin_alert(conn, settings, "ingestao", "S2", "x")
    assert [sub for _, sub in FakeSMTP.sends] == ["[CIO Portal] S1", "[CIO Portal] S2"]


def test_smtp_segredo_nao_aparece_no_repr_de_settings(conn, settings):
    settings.smtp_password = "SEGREDAO"
    assert "SEGREDAO" not in repr(settings)
