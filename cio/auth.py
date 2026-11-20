"""Sessões, CSRF, limite de taxa e login Zoho OIDC (Authorization Code + PKCE + state + nonce)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlencode

import httpx
import jwt

from .access import User
from .config import Settings
from .db import Database, now_iso

COOKIE = "__Host-sid"
FLOW_COOKIE = "__Host-oidc"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------- sessões ----------------
def create_session(conn: Database, user_id: int, s: Settings) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(id_hash, user_id, csrf_token, created_at, last_seen_at, expires_at) VALUES (?,?,?,?,?,?)",
        (_h(token), user_id, secrets.token_urlsafe(24), now, now, now + s.session_max_days * 86400))
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user_id))
    return token


def load_session(conn: Database, token: str | None, s: Settings) -> tuple[User, str] | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT s.*, u.email, u.name, u.role, u.active, u.group_id FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.id_hash=?", (_h(token),)).fetchone()
    now = time.time()
    if not row:
        return None
    if not row["active"] or now > row["expires_at"] or now - row["last_seen_at"] > s.session_idle_hours * 3600:
        conn.execute("DELETE FROM sessions WHERE id_hash=?", (_h(token),))
        return None
    if now - row["last_seen_at"] > 60:
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE id_hash=?", (now, _h(token)))
    from .groups import visible_group_ids
    return (User(row["user_id"], row["email"], row["name"], row["role"], row["group_id"],
                 tuple(visible_group_ids(conn, row["group_id"]))), row["csrf_token"])


def destroy_session(conn: Database, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE id_hash=?", (_h(token),))


def check_csrf(expected: str, provided: str | None) -> bool:
    return bool(provided) and hmac.compare_digest(expected, provided)


# ---------------- limite de taxa (em memória, por processo) ----------------
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


# ---------------- OIDC Zoho ----------------
def _sign(s: Settings, payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    mac = hmac.new(s.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def _unsign(s: Settings, value: str | None, max_age: int = 600) -> dict | None:
    if not value or "." not in value:
        return None
    raw, mac = value.rsplit(".", 1)
    if not hmac.compare_digest(mac, hmac.new(s.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()):
        return None
    data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    if time.time() - data.get("t", 0) > max_age:
        return None
    return data


def begin_login(s: Settings) -> tuple[str, str]:
    """Retorna (url_de_autorização, valor_do_cookie_de_fluxo)."""
    state, nonce, verifier = (secrets.token_urlsafe(24) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code", "client_id": s.login_client_id, "scope": "openid email profile",
        "redirect_uri": f"{s.base_url}/auth/callback", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
        "access_type": "online",
    }
    return (f"{s.zoho_accounts_url}/oauth/v2/auth?{urlencode(params)}",
            _sign(s, {"state": state, "nonce": nonce, "v": verifier, "t": time.time()}))


class LoginError(Exception):
    pass


_jwks_cache: dict = {"at": 0.0, "client": None}


def _jwks_client(s: Settings) -> jwt.PyJWKClient:
    if not _jwks_cache["client"] or time.time() - _jwks_cache["at"] > 3600:
        _jwks_cache["client"] = jwt.PyJWKClient(f"{s.zoho_accounts_url}/oauth/v2/keys")
        _jwks_cache["at"] = time.time()
    return _jwks_cache["client"]


def finish_login(s: Settings, code: str, state: str, flow_cookie: str | None,
                 http: httpx.Client | None = None, key_resolver=None) -> str:
    """Troca o código, valida o id_token e devolve o e-mail verificado."""
    flow = _unsign(s, flow_cookie)
    if not flow or not hmac.compare_digest(flow["state"], state or ""):
        raise LoginError("Sessão de login expirada ou inválida. Tente novamente.")
    http = http or httpx.Client(timeout=20)
    r = http.post(f"{s.zoho_accounts_url}/oauth/v2/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": s.login_client_id,
        "client_secret": s.login_client_secret, "redirect_uri": f"{s.base_url}/auth/callback",
        "code_verifier": flow["v"],
    })
    body = r.json() if r.status_code == 200 else {}
    id_token = body.get("id_token")
    if not id_token:
        raise LoginError("Não foi possível concluir o login com a Zoho.")
    key = key_resolver(id_token) if key_resolver else _jwks_client(s).get_signing_key_from_jwt(id_token).key
    try:
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=s.login_client_id,
                            issuer=s.zoho_accounts_url, options={"require": ["exp", "iat", "iss", "aud"]})
    except jwt.PyJWTError:
        raise LoginError("Identidade não pôde ser validada.")
    if not hmac.compare_digest(claims.get("nonce", ""), flow["nonce"]):
        raise LoginError("Identidade não pôde ser validada.")
    email = (claims.get("email") or "").lower()
    if claims.get("email_verified") in (False, "false") or not email.endswith("@" + s.allowed_domain):
        raise LoginError("Conta não autorizada.")
    return email
