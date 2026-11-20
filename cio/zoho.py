"""Cliente mínimo das APIs Zoho Mail (somente leitura) e WorkDrive (upload/pastas).

- Tokens nunca são registrados em log nem expostos ao frontend.
- Retentativas com backoff exponencial em 429/5xx/erros de rede; renovação em 401.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Protocol

import httpx

from .config import Settings
from .groups import parse_recipients

log = logging.getLogger("cio.zoho")


class ZohoError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class MailMessage:
    account_id: str
    message_id: str
    folder_id: str
    sender: str
    subject: str
    received_ms: int
    has_attachment: bool
    recipients: tuple[str, ...] = ()   # Para + CC, em minúsculas

    @property
    def received_iso(self) -> str:
        return datetime.fromtimestamp(self.received_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class MailAttachment:
    attachment_id: str
    name: str
    size: int


class ZohoAPI(Protocol):
    """Interface usada pelo ingestor (permite trocar por um fake em testes)."""

    def search_messages(self, account_id: str, sender: str, since_ms: int) -> Iterator[MailMessage]: ...
    def excluded_folder_ids(self, account_id: str) -> set[str]: ...
    def get_content(self, msg: MailMessage) -> str: ...
    def get_auth_header(self, msg: MailMessage) -> str: ...
    def get_auth_header(self, msg: MailMessage) -> str:
        """Cabeçalho bruto (leve) da mensagem, para conferir a autenticidade do remetente."""
        r = self._request("GET", f"{self.s.zoho_mail_api}/accounts/{msg.account_id}/folders/{msg.folder_id}"
                                 f"/messages/{msg.message_id}/header", params={"raw": "true"})
        return ((r.json() or {}).get("data") or {}).get("headerContent") or ""

    def get_original(self, msg: MailMessage) -> bytes: ...
    def list_attachments(self, msg: MailMessage) -> list[MailAttachment]: ...
    def get_attachment(self, msg: MailMessage, att: MailAttachment) -> bytes: ...
    def ensure_folder(self, parent_id: str, name: str) -> str: ...
    def upload(self, parent_id: str, filename: str, data: bytes, mime: str) -> str: ...


class TokenManager:
    def __init__(self, settings: Settings, http: httpx.Client):
        self.s = settings
        self.http = http
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._expires - 120:
                if not (self.s.ingest_client_id and self.s.ingest_client_secret and self.s.ingest_refresh_token):
                    raise ZohoError("Credenciais de ingestão não configuradas.")
                r = self.http.post(
                    f"{self.s.zoho_accounts_url}/oauth/v2/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.s.ingest_client_id,
                        "client_secret": self.s.ingest_client_secret,
                        "refresh_token": self.s.ingest_refresh_token,
                    },
                    timeout=30,
                )
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.status_code != 200 or "access_token" not in body:
                    # Não incluir corpo/segredos no erro.
                    raise ZohoError(f"Falha ao renovar token (HTTP {r.status_code}, {body.get('error', 'erro')}).",
                                    r.status_code, retryable=r.status_code >= 500)
                self._token = body["access_token"]
                self._expires = time.time() + int(body.get("expires_in", 3600))
            return self._token


class ZohoClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None, max_attempts: int = 6,
                 base_delay: float = 1.0, sleep=time.sleep):
        self.s = settings
        self.http = http or httpx.Client(timeout=40, follow_redirects=False)
        self.tokens = TokenManager(settings, self.http)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep

    # ---------- núcleo com retentativas ----------
    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        refreshed = False
        for attempt in range(1, self.max_attempts + 1):
            headers = kw.pop("headers", {}) or {}
            headers["Authorization"] = f"Zoho-oauthtoken {self.tokens.token()}"
            try:
                r = self.http.request(method, url, headers=headers, **kw)
            except httpx.TransportError as e:
                err = ZohoError(f"Erro de rede: {type(e).__name__}", retryable=True)
            else:
                if r.status_code == 401 and not refreshed:
                    self.tokens.token(force=True)
                    refreshed = True
                    kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                    continue
                if r.status_code < 400:
                    return r
                retryable = r.status_code == 429 or r.status_code >= 500
                err = ZohoError(f"HTTP {r.status_code} em {method} {httpx.URL(url).path}", r.status_code, retryable)
            kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
            if not err.retryable or attempt == self.max_attempts:
                raise err
            delay = min(self.base_delay * (2 ** (attempt - 1)), 300)
            log.warning("Tentativa %s falhou (%s); nova tentativa em %.0fs", attempt, err, delay)
            self.sleep(delay)
        raise ZohoError("Tentativas esgotadas")

    # ---------- Zoho Mail (somente leitura) ----------
    EXCLUDED_FOLDER_TYPES = {"Sent", "Drafts", "Spam", "Trash", "Outbox", "Templates"}

    def excluded_folder_ids(self, account_id: str) -> set[str]:
        """Pastas cujas mensagens nunca são ingeridas. Verificado em 12/11/2026: com a conta IMAP
        callcenter@ acoplada, cada relatório aparece também em 'Sent' com outro messageId."""
        r = self._request("GET", f"{self.s.zoho_mail_api}/accounts/{account_id}/folders")
        return {str(f.get("folderId")) for f in (r.json() or {}).get("data") or []
                if f.get("folderType") in self.EXCLUDED_FOLDER_TYPES}

    def search_messages(self, account_id: str, sender: str, since_ms: int) -> Iterator[MailMessage]:
        since = datetime.fromtimestamp(max(since_ms, 0) / 1000, tz=timezone.utc).strftime("%d-%b-%Y")
        start, limit = 1, 25
        while True:
            r = self._request(
                "GET", f"{self.s.zoho_mail_api}/accounts/{account_id}/messages/search",
                params={"searchKey": f"sender:{sender}::fromDate:{since}", "start": start, "limit": limit,
                        "includeto": "false"},
            )
            items = (r.json() or {}).get("data") or []
            for m in items:
                received = int(m.get("receivedTime") or 0)
                if received < since_ms:
                    continue
                yield MailMessage(
                    account_id=account_id,
                    message_id=str(m.get("messageId")),
                    folder_id=str(m.get("folderId", "")),
                    sender=(m.get("fromAddress") or "").lower(),
                    subject=m.get("subject") or "",
                    received_ms=received,
                    has_attachment=str(m.get("hasAttachment", "0")) == "1",
                    recipients=tuple(parse_recipients(m.get("toAddress", ""), m.get("ccAddress", ""))),
                )
            if len(items) < limit or start > 2000:
                break
            start += limit

    def get_content(self, msg: MailMessage) -> str:
        r = self._request("GET", f"{self.s.zoho_mail_api}/accounts/{msg.account_id}/folders/{msg.folder_id}"
                                 f"/messages/{msg.message_id}/content", params={"includeBlockContent": "true"})
        return ((r.json() or {}).get("data") or {}).get("content") or ""

    def get_original(self, msg: MailMessage) -> bytes:
        r = self._request("GET", f"{self.s.zoho_mail_api}/accounts/{msg.account_id}/messages/{msg.message_id}"
                                 f"/originalmessage")
        return r.content

    def list_attachments(self, msg: MailMessage) -> list[MailAttachment]:
        r = self._request("GET", f"{self.s.zoho_mail_api}/accounts/{msg.account_id}/folders/{msg.folder_id}"
                                 f"/messages/{msg.message_id}/attachmentinfo")
        data = (r.json() or {}).get("data") or {}
        return [MailAttachment(str(a.get("attachmentId")), a.get("attachmentName") or "anexo",
                               int(a.get("attachmentSize") or 0)) for a in data.get("attachments") or []]

    def get_attachment(self, msg: MailMessage, att: MailAttachment) -> bytes:
        r = self._request("GET", f"{self.s.zoho_mail_api}/accounts/{msg.account_id}/folders/{msg.folder_id}"
                                 f"/messages/{msg.message_id}/attachments/{att.attachment_id}")
        return r.content

    # ---------- WorkDrive ----------
    def ensure_folder(self, parent_id: str, name: str) -> str:
        r = self._request(
            "POST", f"{self.s.zoho_workdrive_api}/files",
            json={"data": {"attributes": {"name": name, "parent_id": parent_id}, "type": "files"}},
            headers={"Accept": "application/vnd.api+json"},
        )
        return r.json()["data"]["id"]

    def upload(self, parent_id: str, filename: str, data: bytes, mime: str) -> str:
        r = self._request(
            "POST", f"{self.s.zoho_workdrive_api}/upload",
            params={"parent_id": parent_id, "filename": filename, "override-name-exist": "false"},
            files={"content": (filename, data, mime)},
        )
        body = r.json()
        return body["data"][0]["attributes"]["resource_id"]
