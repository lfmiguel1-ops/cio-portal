"""Ingestão: Zoho Mail -> catálogo SQLite (+ cópia local e WorkDrive).

Garantias:
- Idempotência por (account_id, zoho_message_id) e por cópias unidas (message_aliases); mensagem já
  conhecida nem é baixada de novo (e-mails não mudam depois de enviados).
- Checkpoint persistente por conta, com sobreposição de 1 h para não perder mensagens.
- Falhas registradas em ingest_errors; checkpoint só avança até antes da 1ª falha (sem perda silenciosa).
  Após MAX_ATTEMPTS a mensagem é isolada (visível no admin, com "Tentar de novo") e a fila segue.
- Anexos com falha deixam o relatório "com anexos pendentes" e são tentados de novo a cada ciclo.
- Remetente precisa de DMARC=pass registrado pelo Zoho; senão o relatório vai para revisão.
- O conteúdo das mensagens é tratado como dado: nunca interpretado como instrução.
"""
from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import classify as clf
from . import groups
from .config import Settings
from .backup import write_protected
from .db import Database, audit, now_iso
from .sanitize import content_hash, html_to_text, inline_cid_images, safe_filename, sanitize_html
from .zoho import MailMessage, ZohoAPI, ZohoError

log = logging.getLogger("cio.ingest")
OVERLAP_MS = 60 * 60 * 1000
SUPPORTED_ATTACHMENTS = {".pdf", ".docx", ".txt", ".csv", ".html", ".htm"}


@dataclass
class RunResult:
    run_id: int | None = None
    fetched: int = 0
    new: int = 0
    versions: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0
    planned: list[dict] = field(default_factory=list)  # usado em dry-run


@contextmanager
def single_instance(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Outra ingestão já está em andamento.")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _now_iso() -> str:
    return now_iso()


def _iso_ms(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def active_sources(conn: Database) -> dict[str, bool]:
    """{remetente: exige_prefixo} das fontes ativas."""
    return {r["email"].lower(): bool(r["require_prefix"])
            for r in conn.execute("SELECT email, require_prefix FROM mail_sources WHERE active=1").fetchall()}


def is_eligible(msg: MailMessage, settings: Settings, sources: dict[str, bool]) -> bool:
    sender = msg.sender.lower()
    if sender not in sources:
        return False
    return (not sources[sender]) or msg.subject.lstrip().startswith(settings.subject_prefix)


def extract_attachment_text(name: str, data: bytes) -> tuple[str, bool]:
    ext = Path(name).suffix.lower()
    try:
        if ext in {".txt", ".csv"}:
            return data.decode("utf-8", "replace"), True
        if ext in {".html", ".htm"}:
            return html_to_text(data.decode("utf-8", "replace")), True
        if ext == ".pdf":
            try:
                from pypdf import PdfReader  # opcional
            except ImportError:
                return "", False
            import io
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages), True
        if ext == ".docx":
            try:
                import docx  # python-docx, opcional
            except ImportError:
                return "", False
            import io
            return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs), True
    except Exception:  # arquivo corrompido/malicioso: catalogar sem texto
        return "", False
    return "", False


MAX_ATTEMPTS = 5          # A4: depois disso a mensagem é isolada e a captura segue
TWIN_WINDOW_MS = 15 * 60 * 1000


class Ingestor:
    def __init__(self, conn: Database, api: ZohoAPI, settings: Settings):
        self.conn = conn
        self.api = api
        self.s = settings

    # ---------------- WorkDrive ----------------
    def _wd_folder(self, parts: list[str]) -> str:
        parent = self.s.workdrive_root_folder_id
        path = ""
        for p in parts:
            path = f"{path}/{p}"
            row = self.conn.execute("SELECT folder_id FROM workdrive_folders WHERE path=?", (path,)).fetchone()
            if row:
                parent = row["folder_id"]
                continue
            parent = self.api.ensure_folder(parent, p)
            self.conn.execute("INSERT INTO workdrive_folders(path, folder_id) VALUES (?,?) ON CONFLICT (path) "
                              "DO UPDATE SET folder_id=excluded.folder_id", (path, parent))
        return parent

    def _store_original(self, msg: MailMessage, area_slug: str, html_raw: str, eml: bytes) -> tuple[str, str, str]:
        dt = datetime.fromtimestamp(msg.received_ms / 1000, tz=timezone.utc)
        stem = f"{dt:%Y%m%d-%H%M}_{safe_filename(msg.subject, 60)}_{safe_filename(msg.message_id)}"
        local_dir = self.s.originals_dir / f"{dt:%Y}" / f"{dt:%m}"
        local_dir.mkdir(parents=True, exist_ok=True)
        html_path = local_dir / f"{stem}.html"
        write_protected(html_path, html_raw.encode("utf-8"))
        if eml:
            write_protected(local_dir / f"{stem}.eml", eml)
        wd_html = wd_eml = ""
        if self.s.workdrive_root_folder_id:
            folder = self._wd_folder([area_slug or "_quarentena", f"{dt:%Y}", f"{dt:%m}"])
            wd_html = self.api.upload(folder, f"{stem}.html", html_raw.encode("utf-8"), "text/html")
            if eml:
                wd_eml = self.api.upload(folder, f"{stem}.eml", eml, "message/rfc822")
        return str(html_path.relative_to(self.s.data_dir)), wd_html, wd_eml

    # ---------------- auxiliares ----------------
    def _known_report(self, msg: MailMessage) -> int | None:
        row = self.conn.execute("SELECT id FROM reports WHERE account_id=? AND zoho_message_id=?",
                                (msg.account_id, msg.message_id)).fetchone()
        if row:
            return row["id"]
        row = self.conn.execute("SELECT report_id FROM message_aliases WHERE account_id=? AND message_id=?",
                                (msg.account_id, msg.message_id)).fetchone()
        return row["report_id"] if row else None

    def _authenticated(self, msg: MailMessage) -> tuple[bool, str]:
        if not self.s.require_dmarc:
            return True, "verificação desativada"
        from .mailauth import check_dmarc
        return check_dmarc(self.api.get_auth_header(msg), msg.sender)

    def _audience(self, c, recipients: list[str], sender: str):
        exclude = set(self.s.monitored_emails) | {sender.lower()}
        return groups.resolve_audience(self.conn, c.group_id, c.audience_mode, recipients, exclude)

    @staticmethod
    def _decide(c, aud, auth_ok: bool, auth_detail: str) -> tuple[str, str]:
        status, reason = c.status, f"{c.reason}; público: {aud.reason}"
        if status == "publicado" and not aud.group_ids:
            status = "quarentena"  # nunca publicar sem público definido
        if status == "publicado" and aud.broader:
            status = "quarentena"  # A6: público mais amplo que o previsto -> revisão humana
        if status == "publicado" and not auth_ok:
            status, reason = "quarentena", f"{reason}; remetente não autenticado ({auth_detail})"
        if aud.unknown:
            reason += f"; {len(aud.unknown)} destinatário(s) não cadastrado(s)"
        return status, reason

    # ---------------- processamento de 1 mensagem ----------------
    def process(self, msg: MailMessage, run: RunResult, dry_run: bool = False) -> bool:
        """Retorna True se baixou/processou algo novo (conta no limite do lote)."""
        if self._known_report(msg):  # A4: já conhecida -> nem baixa o conteúdo
            run.duplicates += 1
            return False
        html_raw = (self.api.get_content(msg) or "").replace("\x00", "")  # PostgreSQL não aceita NUL
        digest = content_hash(html_raw)
        recipients = list(dict.fromkeys(r.strip().lower() for r in msg.recipients if r.strip()))
        c = clf.classify(self.conn, msg.sender, msg.subject, msg.folder_id)

        # A5: mesma mensagem com outro messageId (cópia/envio separado em poucos minutos) -> une ao existente
        twin = None
        for t in self.conn.execute("SELECT id, received_at, recipients, status, sender FROM reports WHERE "
                                   "account_id=? AND content_sha256=? AND subject=?",
                                   (msg.account_id, digest, msg.subject)).fetchall():
            if abs(_iso_ms(t["received_at"]) - msg.received_ms) <= TWIN_WINDOW_MS:
                twin = t
                break
        if twin is not None:
            merged = list(dict.fromkeys([*(twin["recipients"] or "").split(","), *recipients]))
            merged = [m for m in merged if m]
            aud = self._audience(c, merged, msg.sender)
            if dry_run:
                run.planned.append({"message_id": msg.message_id, "subject": msg.subject, "acao": "unir à cópia",
                                    "relatorio": twin["id"]})
                return True
            with self.conn.transaction():
                old = set(r[0] for r in self.conn.execute("SELECT group_id FROM report_groups WHERE report_id=?",
                                                           (twin["id"],)).fetchall())
                new_ids = sorted(old | set(aud.group_ids))
                groups.set_report_groups(self.conn, twin["id"], new_ids)
                status = twin["status"]
                if status == "publicado" and aud.broader:
                    status = "quarentena"
                self.conn.execute("UPDATE reports SET recipients=?, status=?, updated_at=? WHERE id=?",
                                  (",".join(merged), status, _now_iso(), twin["id"]))
                self.conn.execute("INSERT INTO message_aliases(account_id, message_id, report_id) VALUES (?,?,?) "
                                  "ON CONFLICT DO NOTHING", (msg.account_id, msg.message_id, twin["id"]))
            run.duplicates += 1
            return True

        aud = self._audience(c, recipients, msg.sender)
        auth_ok, auth_detail = self._authenticated(msg)
        status, reason = self._decide(c, aud, auth_ok, auth_detail)
        if dry_run:
            names = [r["name"] for r in self.conn.execute(
                "SELECT name FROM groups WHERE id IN (%s)" % ",".join("?" * len(aud.group_ids)),
                aud.group_ids).fetchall()] if aud.group_ids else []
            run.planned.append({"message_id": msg.message_id, "subject": msg.subject, "received": msg.received_iso,
                                "status": status, "reason": reason, "grupos": names,
                                "destinatarios_nao_cadastrados": aud.unknown, "acao": "novo"})
            return True
        area_slug = ""
        if c.area_id:
            area_slug = self.conn.execute("SELECT slug FROM areas WHERE id=?", (c.area_id,)).fetchone()["slug"]
        eml = b""
        try:
            eml = self.api.get_original(msg)
        except ZohoError as e:
            log.warning("Original .eml indisponível para %s: %s", msg.message_id, e)
        original_path, wd_html, wd_eml = self._store_original(msg, area_slug, html_raw, eml)
        sanitized = sanitize_html(inline_cid_images(html_raw, eml))
        text = html_to_text(html_raw)

        with self.conn.transaction():
            report_id = self.conn.insert(
                """INSERT INTO reports(zoho_message_id, account_id, folder_id, sender, subject, received_at, area_id,
                   priority, confidentiality, status, status_reason, rule_id, content_sha256, version,
                   body_html_sanitized, text_plain, original_path, workdrive_html_id, workdrive_eml_id, recipients,
                   attachments_pending, sender_verified)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)""",
                (msg.message_id, msg.account_id, msg.folder_id, msg.sender, msg.subject, msg.received_iso,
                 c.area_id, c.priority, c.confidentiality, status, reason, c.rule_id, digest,
                 sanitized, text, original_path, wd_html, wd_eml, ",".join(recipients), 1 if msg.has_attachment else 0,
                 1 if auth_ok else 0),
            )
            self.conn.execute(
                "INSERT INTO report_versions(report_id, version, content_sha256, original_path, workdrive_html_id) "
                "VALUES (?,1,?,?,?) ON CONFLICT (report_id, version) DO NOTHING",
                (report_id, digest, original_path, wd_html))
            groups.set_report_groups(self.conn, report_id, aud.group_ids)
        run.new += 1
        if msg.has_attachment:
            self._attachments_safely(msg, report_id, run)
        return True

    # ---------------- anexos (A3) ----------------
    def _attachments_safely(self, msg: MailMessage, report_id: int, run: RunResult) -> None:
        try:
            self._process_attachments(msg, report_id)
            self.conn.execute("UPDATE reports SET attachments_pending=0 WHERE id=?", (report_id,))
        except Exception as e:  # relatório fica com anexos pendentes e é tentado de novo nos próximos ciclos
            run.errors += 1
            self._error(run, msg.account_id, msg.message_id, "anexos", f"{type(e).__name__}: {e}", msg)

    def retry_pending_attachments(self, run: RunResult) -> None:
        rows = self.conn.execute("SELECT id, account_id, zoho_message_id, folder_id, sender, subject, received_at "
                                 "FROM reports WHERE attachments_pending=1 AND source='email' LIMIT 50").fetchall()
        for r in rows:
            m = MailMessage(r["account_id"], r["zoho_message_id"], r["folder_id"], r["sender"], r["subject"],
                            _iso_ms(r["received_at"]), True)
            self._attachments_safely(m, r["id"], run)

    def _process_attachments(self, msg: MailMessage, report_id: int) -> None:
        import hashlib
        prepared = []
        used: set[str] = set()
        for att in self.api.list_attachments(msg):
            name = safe_filename(att.name)
            base, n = name, 2
            while name in used:  # anexos com o mesmo nome não se sobrescrevem
                stem, dot, ext = base.rpartition(".")
                name = f"{stem or base}-{n}{dot}{ext}" if dot else f"{base}-{n}"
                n += 1
            used.add(name)
            if att.size > self.s.max_attachment_bytes:
                prepared.append((name, att.size, "", "", "", 0))
                continue
            data = self.api.get_attachment(msg, att)
            text, ok = extract_attachment_text(name, data)
            text = text.replace("\x00", "")
            d = self.s.originals_dir / "anexos" / str(report_id)
            write_protected(d / name, data)
            prepared.append((name, len(data), hashlib.sha256(data).hexdigest(),
                             str((d / name).relative_to(self.s.data_dir)), text[:500_000], 1 if ok else 0))
        # tudo baixado: grava de uma vez (falha no meio não deixa anexos pela metade)
        with self.conn.transaction():
            self.conn.execute("DELETE FROM attachments WHERE report_id=?", (report_id,))
            extra = ""
            for name, size, sha, path, text, ok in prepared:
                self.conn.execute(
                    "INSERT INTO attachments(report_id, filename, size, sha256, local_path, text_extracted, supported) "
                    "VALUES (?,?,?,?,?,?,?)", (report_id, name, size, sha, path, text, ok))
                if text:
                    extra += "\n\n[Anexo: " + name + "]\n" + text
            if extra:
                self.conn.execute("UPDATE reports SET text_plain = text_plain || ? WHERE id=?", (extra, report_id))

    # ---------------- ciclo completo ----------------
    def _isolated(self, account_id: str, message_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM ingest_errors WHERE account_id=? AND message_id=? AND resolved=0 "
                                "AND stage='processamento' AND attempts>=?", (account_id, message_id, MAX_ATTEMPTS)
                                ).fetchone()
        return bool(row)

    def run(self, since_ms: int | None = None, dry_run: bool = False, batch_limit: int = 200,
            mode: str = "normal") -> RunResult:
        res = RunResult()
        if not dry_run:
            res.run_id = self.conn.insert("INSERT INTO ingest_runs(started_at, mode) VALUES (?,?)", (_now_iso(), mode))
            self.retry_pending_attachments(res)
        sources = active_sources(self.conn)
        for account_id in self.s.mail_account_ids:
            state = self.conn.execute("SELECT checkpoint_ms FROM ingest_state WHERE account_id=?",
                                      (account_id,)).fetchone()
            checkpoint = state["checkpoint_ms"] if state else 0
            start_ms = since_ms if since_ms is not None else max(checkpoint - OVERLAP_MS, 0)
            msgs: dict[str, MailMessage] = {}
            try:
                excluded = self.api.excluded_folder_ids(account_id)
                for sender in sources:
                    for m in self.api.search_messages(account_id, sender, start_ms):
                        msgs[m.message_id] = m
            except ZohoError as e:
                res.errors += 1
                self._error(res, account_id, "", "busca", str(e))
                continue
            ordered = sorted(msgs.values(), key=lambda m: m.received_ms)
            res.fetched += len(ordered)
            new_checkpoint = checkpoint
            blocked = False
            processed = 0
            for m in ordered:
                if m.folder_id in excluded or not is_eligible(m, self.s, sources):
                    res.skipped += 1
                    if not blocked:
                        new_checkpoint = max(new_checkpoint, m.received_ms)
                    continue
                if processed >= batch_limit:
                    break
                if self._isolated(account_id, m.message_id):  # A4: não trava a fila
                    res.skipped += 1
                    if not blocked:
                        new_checkpoint = max(new_checkpoint, m.received_ms)
                    continue
                try:
                    if self.process(m, res, dry_run=dry_run):
                        processed += 1
                except Exception as e:  # registra; checkpoint não passa da falha até ela ser isolada
                    res.errors += 1
                    blocked = True
                    self._error(res, account_id, m.message_id, "processamento", f"{type(e).__name__}: {e}", m)
                    continue
                if not blocked:
                    new_checkpoint = max(new_checkpoint, m.received_ms)
            if not dry_run:
                self.conn.execute(
                    "INSERT INTO ingest_state(account_id, checkpoint_ms, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET checkpoint_ms=excluded.checkpoint_ms, "
                    "updated_at=excluded.updated_at", (account_id, new_checkpoint, _now_iso()))
        if not dry_run:
            self.conn.execute(
                "UPDATE ingest_runs SET finished_at=?, fetched=?, new=?, versions=?, duplicates=?, skipped=?, "
                "errors=?, ok=? WHERE id=?",
                (_now_iso(), res.fetched, res.new, res.versions, res.duplicates, res.skipped, res.errors,
                 1 if res.errors == 0 else 0, res.run_id))
            audit(self.conn, "ingest_run", target=str(res.run_id),
                  details=f"novos={res.new} dup={res.duplicates} erros={res.errors}")
        return res

    def _error(self, res: RunResult, account_id: str, message_id: str, stage: str, error: str,
               msg: MailMessage | None = None) -> None:
        log.error("Erro de ingestão [%s] msg=%s: %s", stage, message_id, error)
        if not res.run_id:
            return
        prev = self.conn.execute(
            "SELECT id FROM ingest_errors WHERE message_id=? AND stage=? AND resolved=0 AND account_id=?",
            (message_id, stage, account_id)).fetchone()
        if prev and message_id:
            self.conn.execute("UPDATE ingest_errors SET attempts=attempts+1, error=?, run_id=? WHERE id=?",
                              (error[:1000], res.run_id, prev["id"]))
        else:
            self.conn.execute(
                "INSERT INTO ingest_errors(run_id, account_id, message_id, stage, error, folder_id, received_ms) "
                "VALUES (?,?,?,?,?,?,?)", (res.run_id, account_id, message_id, stage, error[:1000],
                                           msg.folder_id if msg else "", msg.received_ms if msg else 0))


def consecutive_failed_runs(conn: Database, n: int = 3) -> bool:
    rows = conn.execute("SELECT ok FROM ingest_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT ?",
                        (n,)).fetchall()
    return len(rows) == n and all(r["ok"] == 0 for r in rows)


def mark_resolved(conn: Database) -> None:
    """Resolve erros cuja mensagem já está no catálogo (por id ou como cópia unida).
    Erros de ANEXOS só se resolvem quando o relatório não tem mais anexos pendentes (A3)."""
    conn.execute("UPDATE ingest_errors SET resolved=1 WHERE resolved=0 AND stage='processamento' AND ("
                 "message_id IN (SELECT zoho_message_id FROM reports) OR "
                 "message_id IN (SELECT message_id FROM message_aliases))")
    conn.execute("UPDATE ingest_errors SET resolved=1 WHERE resolved=0 AND stage='anexos' AND message_id IN "
                 "(SELECT zoho_message_id FROM reports WHERE attachments_pending=0)")


def retry_message(conn: Database, error_id: int) -> bool:
    """Admin: 'Tentar de novo' uma mensagem isolada — zera tentativas e recua o checkpoint até ela."""
    row = conn.execute("SELECT account_id, received_ms FROM ingest_errors WHERE id=? AND resolved=0",
                       (error_id,)).fetchone()
    if not row:
        return False
    with conn.transaction():
        conn.execute("UPDATE ingest_errors SET attempts=0 WHERE id=?", (error_id,))
        if row["received_ms"]:
            conn.execute("UPDATE ingest_state SET checkpoint_ms=? WHERE account_id=? AND checkpoint_ms>?",
                         (row["received_ms"] - 1, row["account_id"], row["received_ms"] - 1))
    return True


def last_sync(conn: Database) -> dict:
    """M7: situação da sincronização para exibir a todos (e alertar o admin)."""
    last_ok = conn.execute("SELECT finished_at FROM ingest_runs WHERE ok=1 AND finished_at IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    return {"ultima_ok": last_ok["finished_at"] if last_ok else None,
            "falhando": consecutive_failed_runs(conn)}


def run_once(conn: Database, api: ZohoAPI, settings: Settings, **kw) -> RunResult:
    with single_instance(settings.data_dir / "ingest.lock"):
        res = Ingestor(conn, api, settings).run(**kw)
        if not kw.get("dry_run"):
            mark_resolved(conn)
        return res
