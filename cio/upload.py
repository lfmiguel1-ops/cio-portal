"""Envio manual de arquivos (PDF, JPG, PNG) como relatórios.

O arquivo é tratado como não confiável: o tipo é verificado pelo conteúdo (assinatura binária),
não pela extensão; o tamanho é limitado; o nome é descartado (o portal gera o seu).
Sem OCR: imagens são encontradas pelo título e pela descrição.
"""
from __future__ import annotations

import hashlib
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .db import Database, now_iso
from .sanitize import safe_filename

log = logging.getLogger("cio.upload")

TYPES = {  # assinatura -> (extensão, mime)
    b"%PDF-": ("pdf", "application/pdf"),
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
}


class UploadError(ValueError):
    pass


def detect_type(data: bytes) -> tuple[str, str]:
    for magic, info in TYPES.items():
        if data.startswith(magic):
            return info
    raise UploadError("Formato não aceito. Envie um arquivo PDF, JPG ou PNG.")


def pdf_text(data: bytes, max_chars: int = 500_000) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return ""
        parts = []
        for page in reader.pages[:300]:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) > max_chars:
                break
        return "\n".join(parts).replace("\x00", "")[:max_chars]
    except Exception:  # PDF corrompido ou malicioso: guarda o arquivo, sem texto
        log.warning("Não foi possível extrair texto do PDF enviado.")
        return ""


@dataclass
class ManualReport:
    title: str
    description: str
    area_id: int
    priority: str
    group_ids: list[int]
    reference_date: str  # YYYY-MM-DD


def save_upload(db: Database, s: Settings, uploader_email: str, meta: ManualReport, data: bytes,
                workdrive_upload=None) -> int:
    if not data:
        raise UploadError("Selecione um arquivo.")
    if not meta.group_ids:
        raise UploadError("Escolha ao menos um grupo que poderá ver o arquivo.")
    if len(data) > s.max_attachment_bytes:
        raise UploadError(f"Arquivo maior que o limite de {s.max_attachment_bytes // (1024 * 1024)} MB.")
    ext, mime = detect_type(data)
    digest = hashlib.sha256(data).hexdigest()
    dup = db.execute("SELECT id FROM reports WHERE source='manual' AND content_sha256=?", (digest,)).fetchone()
    if dup:
        raise UploadError("Este arquivo já foi enviado anteriormente.")

    ref = datetime.strptime(meta.reference_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    stem = f"{ref:%Y%m%d}_{safe_filename(meta.title, 60)}_{uuid.uuid4().hex[:8]}"
    folder = s.originals_dir / "manual" / f"{ref:%Y}" / f"{ref:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.{ext}"
    from .backup import write_protected
    write_protected(path, data)

    extracted = pdf_text(data) if ext == "pdf" else ""
    text = meta.description.strip()
    if extracted:
        text += "\n\n" + extracted
    wd_id = ""
    if workdrive_upload:
        try:
            wd_id = workdrive_upload(f"{stem}.{ext}", data, mime, ref) or ""
        except Exception as e:  # cópia no WorkDrive é complementar: não impede o envio
            log.warning("Falha ao copiar envio manual para o WorkDrive: %s", type(e).__name__)
    rid = db.insert(
        "INSERT INTO reports(zoho_message_id, account_id, folder_id, sender, subject, received_at, area_id, priority, "
        "confidentiality, status, status_reason, content_sha256, version, body_html_sanitized, text_plain, "
        "original_path, workdrive_html_id, source, file_path, file_mime, description, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'',?,?,?,'manual',?,?,?,?,?)",
        (f"manual-{uuid.uuid4().hex}", "manual", "", uploader_email, meta.title.strip()[:300],
         ref.strftime("%Y-%m-%dT%H:%M:%SZ"), meta.area_id, meta.priority, "interno", "publicado",
         f"envio manual por {uploader_email}", digest, text, str(path.relative_to(s.data_dir)), wd_id,
         str(path.relative_to(s.data_dir)), mime, meta.description.strip()[:5000], now_iso(), now_iso()))
    from .groups import set_report_groups
    set_report_groups(db, rid, meta.group_ids)
    return rid
