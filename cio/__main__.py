"""Linha de comando.

  python -m cio serve                         # portal + agendador (ingestão, backup diário, teste mensal de restauração)
  python -m cio ingest [--dry-run] [--since AAAA-MM-DD] [--batch 50]
  python -m cio backup                        # backup + teste de restauração
  python -m cio add-user EMAIL PERFIL [--areas atendimento,comercial] [--name NOME]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .config import get_settings
from .db import connect, migrate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cio")


def _api(s):
    from .zoho import ZohoClient
    return ZohoClient(s)


def cmd_ingest(args) -> int:
    from .ingest import run_once
    s = get_settings()
    conn = connect(s)
    migrate(conn, s.authorized_senders)
    since = None
    if args.since:
        since = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    res = run_once(conn, _api(s), s, since_ms=since, dry_run=args.dry_run, batch_limit=args.batch,
                   mode="historico" if args.since else "normal")
    out = {k: v for k, v in res.__dict__.items() if k != "planned"}
    if args.dry_run:
        out["simulacao"] = res.planned
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if res.errors == 0 else 2


def _workdrive_uploader(s):
    """Cópia dos backups para a pasta privada do WorkDrive (opcional, BACKUP_TO_WORKDRIVE=true)."""
    if not (s.backup_to_workdrive and s.workdrive_root_folder_id):
        return None
    from .ingest import Ingestor

    def up(path):
        conn = connect(s)
        try:
            ing = Ingestor(conn, _api(s), s)
            folder = ing._wd_folder(["_backups"])
            ing.api.upload(folder, path.name, path.read_bytes(), "application/octet-stream")
        finally:
            conn.close()
    return up


def cmd_backup(_args) -> int:
    """Backup completo (banco + arquivos) seguido do teste de restauração do conjunto gerado."""
    from .backup import run_backup, verify_set
    s = get_settings()
    conn = connect(s)
    migrate(conn, s.authorized_senders)
    made = run_backup(conn, s, uploader=_workdrive_uploader(s))
    result = verify_set(s, made["conjunto"], reference=conn)
    print(json.dumps({"backup": made, "teste_restauracao": result}, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def cmd_verify_backup(args) -> int:
    from .backup import verify_set
    s = get_settings()
    conn = connect(s)
    result = verify_set(s, args.conjunto or None, reference=conn)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def cmd_restore_files(args) -> int:
    """Recuperação de desastre: extrai os arquivos de um conjunto (conferindo SHA-256) em --destino."""
    from pathlib import Path

    from .backup import restore_files
    s = get_settings()
    n = restore_files(s, args.conjunto, Path(args.destino))
    print(f"{n} arquivo(s) restaurado(s) em {args.destino}. Copie para {s.data_dir} com o portal parado.")
    return 0


def cmd_doctor(_args) -> int:
    """Diagnóstico para homologação (não mostra segredos)."""
    from . import __version__
    from .backup import BackupError, tool_versions
    s = get_settings()
    checks = {"versao": __version__, "ambiente": s.env, "https": s.base_url.startswith("https://")}
    try:
        conn = connect(s)
        migrate(conn, s.authorized_senders)
        checks["banco"] = conn.dialect
        try:
            checks["ferramentas_backup"] = tool_versions(s, conn)
        except BackupError as e:  # ex.: pg_dump ausente — o banco está ok, o problema é a ferramenta de backup
            checks["ferramentas_backup"] = {"ok": False, "erro": str(e)}
        checks["administradores_ativos"] = conn.execute(
            "SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
        conn.close()
    except Exception as e:
        checks["banco"] = f"ERRO: {type(e).__name__}"
    for name, path in (("dados_gravavel", s.data_dir), ("backup_gravavel", s.backups_dir)):
        try:
            probe = path / ".probe"
            probe.write_text("ok")
            probe.unlink()
            checks[name] = True
        except OSError:
            checks[name] = False
    checks["backup_separado_dos_dados"] = s.data_dir.resolve() not in s.backups_dir.resolve().parents
    checks["zoho_ingestao_configurado"] = bool(s.ingest_client_id and s.ingest_client_secret
                                               and s.ingest_refresh_token)
    checks["zoho_login_configurado"] = bool(s.login_client_id and s.login_client_secret)
    checks["dmarc_exigido"] = s.require_dmarc
    checks["alerta_configurado"] = s.admin_alert_email and bool(s.smtp_user and s.smtp_password)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    ok = (checks.get("banco") in {"postgres", "sqlite"} and checks["dados_gravavel"] and checks["backup_gravavel"]
          and checks.get("ferramentas_backup", {}).get("ok", True) is not False)
    return 0 if ok else 2


def cmd_add_user(args) -> int:
    """add-user EMAIL admin            -> administrador (vê tudo)
       add-user EMAIL <grupo>          -> pessoa no grupo (ex.: geral, gestores, diretoria, franqueado-1)"""
    s = get_settings()
    conn = connect(s)
    migrate(conn, s.authorized_senders)
    email = args.email.lower()
    if not email.endswith("@" + s.allowed_domain):
        print("E-mail fora do domínio.", file=sys.stderr)
        return 1
    role, gid = "operacional", None
    if args.role == "admin":
        role = "admin"
    else:
        row = conn.execute("SELECT id FROM groups WHERE slug=? AND kind<>'todos'", (args.role,)).fetchone()
        if not row:
            grupos = ", ".join(r[0] for r in conn.execute("SELECT slug FROM groups WHERE kind<>'todos'").fetchall())
            print(f"Grupo inexistente. Use 'admin' ou um destes: {grupos}", file=sys.stderr)
            return 1
        gid = row["id"]
    if args.group:
        row = conn.execute("SELECT id FROM groups WHERE slug=? AND kind<>'todos'", (args.group,)).fetchone()
        gid = row["id"] if row else gid
    conn.execute("INSERT INTO users(email, name, role, group_id) VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
                 "role=excluded.role, name=excluded.name, group_id=excluded.group_id, active=1",
                 (email, args.name or "", role, gid))
    print(f"Usuário {email} salvo ({'administrador' if role == 'admin' else args.role}).")
    return 0


def cmd_zoho_token(args) -> int:
    """Troca o código de uso único do Self Client (API Console) por um refresh token.
    O token é gravado direto no arquivo .env indicado (permissão 600) e NÃO é impresso."""
    import os
    import re
    from pathlib import Path

    import httpx
    s = get_settings()
    if not (s.ingest_client_id and s.ingest_client_secret):
        print("Defina ZOHO_INGEST_CLIENT_ID e ZOHO_INGEST_CLIENT_SECRET no .env antes.", file=sys.stderr)
        return 1
    r = httpx.post(f"{s.zoho_accounts_url}/oauth/v2/token", data={
        "grant_type": "authorization_code", "code": args.code, "client_id": s.ingest_client_id,
        "client_secret": s.ingest_client_secret}, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    token = body.get("refresh_token")
    if not token:
        print(f"Falha: {body.get('error', r.status_code)}. Gere um novo código (validade de poucos minutos).",
              file=sys.stderr)
        return 2
    if args.mostrar:
        # Para Coolify: o operador copia o valor para a variável secreta ZOHO_INGEST_REFRESH_TOKEN.
        print("ATENÇÃO: valor secreto. Copie para a variável ZOHO_INGEST_REFRESH_TOKEN e limpe o terminal.")
        print(token)
        return 0
    env = Path(args.env_file)
    text = env.read_text() if env.exists() else ""
    line = f"ZOHO_INGEST_REFRESH_TOKEN={token}"
    text = re.sub(r"^ZOHO_INGEST_REFRESH_TOKEN=.*$", line, text, flags=re.M) if re.search(
        r"^ZOHO_INGEST_REFRESH_TOKEN=", text, flags=re.M) else text + "\n" + line + "\n"
    env.write_text(text)
    os.chmod(env, 0o600)
    print(f"Refresh token gravado em {env} (escopos: {body.get('scope', 'n/d')}). Reinicie o serviço.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler

    from .app import create_app
    from .backup import run_backup, verify_set
    from .ingest import consecutive_failed_runs, run_once

    s = get_settings()
    app = create_app(s, zoho_factory=lambda: _api(s))
    sched = BackgroundScheduler(timezone="America/Sao_Paulo")

    api = _api(s)  # reaproveita o cliente (e o token) entre os ciclos

    def job_ingest():
        conn = connect(s)
        try:
            res = run_once(conn, api, s)
            if res.errors or consecutive_failed_runs(conn):
                from .alerts import send_admin_alert
                send_admin_alert(
                    conn, s, "ingestao", "Falha na captura de e-mails",
                    f"A captura de relatórios registrou {res.errors} erro(s) no ciclo terminado em "
                    f"{datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} (UTC). "
                    "Verifique Administração › erros não resolvidos.")
        except Exception:
            log.exception("Ciclo de ingestão falhou")
            try:
                from .alerts import send_admin_alert
                send_admin_alert(conn, s, "ingestao", "Ciclo de ingestão não pôde ser executado",
                                 "O ciclo de ingestão falhou antes de concluir. Verifique o log do container.")
            except Exception:
                log.exception("Não foi possível enviar o alerta de ingestão")
        finally:
            conn.close()

    def job_backup():
        conn = connect(s)
        try:
            run_backup(conn, s, uploader=_workdrive_uploader(s))
        except Exception:
            log.exception("ALERTA: backup diário falhou")
            try:
                from .alerts import send_admin_alert
                send_admin_alert(conn, s, "backup", "Backup diário falhou",
                                 "O backup das 02:00 não foi concluído. Verifique o log do container e "
                                 "Administração › Auditoria.")
                from .db import audit
                audit(conn, "backup_falhou", details="ver log do container")
            except Exception:
                log.exception("Não foi possível registrar a falha de backup")
        finally:
            conn.close()

    def job_restore_test():
        conn = connect(s)
        try:
            res = verify_set(s, reference=conn)
            if not res["ok"]:
                log.error("ALERTA: teste de restauração falhou: %s", res.get("problemas"))
                from .alerts import send_admin_alert
                send_admin_alert(conn, s, "restauracao", "Teste de restauração falhou",
                                 f"O teste semanal de restauração acusou problemas: {res.get('problemas')}")
        except Exception:
            log.exception("ALERTA: teste de restauração não pôde ser executado")
            from .alerts import send_admin_alert
            send_admin_alert(conn, s, "restauracao", "Teste de restauração não pôde ser executado",
                             "O teste semanal de restauração não rodou. Verifique o log do container.")
        finally:
            conn.close()

    if s.ingest_enabled:
        sched.add_job(job_ingest, "interval", minutes=s.ingest_interval_min, max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))
    # Backup diário (banco + arquivos) e teste de restauração semanal — SQLite e PostgreSQL.
    sched.add_job(job_backup, "cron", hour=2, minute=0, max_instances=1, coalesce=True)
    sched.add_job(job_restore_test, "cron", day_of_week="sun", hour=3, minute=30, max_instances=1, coalesce=True)
    sched.start()
    try:
        uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                    access_log=False)
    finally:
        sched.shutdown(wait=False)
    return 0


def main(argv=None) -> int:
    from . import __version__
    p = argparse.ArgumentParser(prog="cio")
    p.add_argument("--version", action="version", version=f"cio-portal {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_serve)
    si = sub.add_parser("ingest")
    si.add_argument("--dry-run", action="store_true")
    si.add_argument("--since")
    si.add_argument("--batch", type=int, default=50)
    si.set_defaults(func=cmd_ingest)
    sub.add_parser("backup", help="backup completo + teste de restauração").set_defaults(func=cmd_backup)
    sv = sub.add_parser("verificar-backup", help="teste de restauração de um conjunto (padrão: o mais recente)")
    sv.add_argument("--conjunto", default="")
    sv.set_defaults(func=cmd_verify_backup)
    sr = sub.add_parser("restaurar-arquivos", help="recuperação: extrai os arquivos de um conjunto")
    sr.add_argument("--conjunto", required=True)
    sr.add_argument("--destino", required=True)
    sr.set_defaults(func=cmd_restore_files)
    sub.add_parser("diagnostico", help="checagens de homologação (sem segredos)").set_defaults(func=cmd_doctor)
    su = sub.add_parser("add-user")
    su.add_argument("email")
    su.add_argument("role")
    su.add_argument("--group", default="", help="grupo do administrador (opcional), ex.: diretoria")
    su.add_argument("--name", default="")
    su.set_defaults(func=cmd_add_user)
    sz = sub.add_parser("zoho-token")
    sz.add_argument("--code", required=True)
    sz.add_argument("--env-file", default="/run/cio/.env")
    sz.add_argument("--mostrar", action="store_true", help="exibe o token (uso no Coolify) em vez de gravar no .env")
    sz.set_defaults(func=cmd_zoho_token)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
