"""Portal web (FastAPI + Jinja2, renderização no servidor, pt-BR)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import access, auth
from .access import SearchParams, User
from .classify import validate_regex
from .config import Settings, get_settings
from .db import Database, audit, connect, migrate, now_iso

log = logging.getLogger("cio.app")
HERE = Path(__file__).parent
TZ = ZoneInfo("America/Sao_Paulo")

PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self' https://accounts.zoho.com; "
            "frame-ancestors 'none'")
FILE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; object-src 'self'; frame-ancestors 'self'"
CONTENT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; "
               "sandbox allow-popups allow-popups-to-escape-sandbox")
# M8: mensagens fixas na tela de login (nada de texto livre vindo da URL)
LOGIN_ERRORS = {
    "sessao": "Sessão de login expirada ou inválida. Tente novamente.",
    "zoho": "Não foi possível concluir o login com a Zoho. Tente novamente.",
    "identidade": "Sua identidade não pôde ser confirmada. Tente novamente.",
    "conta": "Esta conta não está autorizada a usar o portal.",
    "sem-acesso": "Seu acesso ainda não foi liberado. Fale com o administrador.",
}
_LOGIN_ERROR_CODES = {"Sessão de login expirada ou inválida. Tente novamente.": "sessao",
                      "Não foi possível concluir o login com a Zoho.": "zoho",
                      "Identidade não pôde ser validada.": "identidade", "Conta não autorizada.": "conta"}


def create_app(settings: Settings | None = None, zoho_factory=None, oidc_http=None, oidc_key_resolver=None) -> FastAPI:
    s = settings or get_settings()
    s.validate()
    boot = connect(s)
    migrate(boot, s.authorized_senders)
    boot.close()

    app = FastAPI(title="Central de Inteligência Operacional", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = s
    app.state.limiter = auth.RateLimiter()
    # Só em desenvolvimento/demonstração sem HTTPS: cookies sem "Secure" (o prefixo __Host- exige HTTPS).
    insecure = s.is_dev and os.environ.get("CIO_DEV_INSECURE_COOKIES") == "1"
    SECURE = not insecure
    SID = auth.COOKIE if SECURE else "cio_sid"
    FLOW = auth.FLOW_COOKIE if SECURE else "cio_oidc"
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    def fmt_dt(value: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(TZ) \
                .strftime("%d/%m/%Y %H:%M")
        except (TypeError, ValueError):
            return value or ""

    def snippet(value: str) -> Markup:
        safe = str(escape(value or ""))
        return Markup(safe.replace("\u0002", "<mark>").replace("\u0003", "</mark>"))

    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["snippet"] = snippet

    # ---------------- infraestrutura ----------------
    def get_db():
        conn = connect(s)
        try:
            yield conn
        finally:
            conn.close()

    class BodyLimit:
        """Recusa (413) corpos de requisição acima do limite ANTES de lê-los por inteiro.

        Sem isso, o envio só seria medido depois de gravado no /tmp (memória do container): um corpo de vários
        GB esgotaria a memória. Vale para qualquer rota, com ou sem login, e também sem Content-Length."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
                return await self.inner(scope, receive, send)
            limit = (s.max_attachment_bytes + 1024 * 1024) if scope["path"] == "/enviar" else 1024 * 1024
            declared = dict(scope.get("headers") or []).get(b"content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                return await PlainTextResponse("Arquivo grande demais.", status_code=413)(scope, receive, send)
            seen = 0

            async def limited():
                nonlocal seen
                message = await receive()
                if message["type"] == "http.request":
                    seen += len(message.get("body", b""))
                    if seen > limit:
                        raise HTTPException(status_code=413)
                return message

            # O 413 lançado aqui é respondido pelo tratador de erros do portal (página "Arquivo grande demais.").
            await self.inner(scope, limited, send)

    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        is_content = request.url.path.endswith("/conteudo")
        is_file = request.url.path.endswith("/arquivo")
        if is_file:
            response.headers["Content-Security-Policy"] = FILE_CSP
        else:
            response.headers["Content-Security-Policy"] = CONTENT_CSP if is_content else PAGE_CSP
        is_content = is_content or is_file
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if is_content else "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if not s.is_dev:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    class Ctx:
        def __init__(self, user: User, csrf: str, conn: Database):
            self.user, self.csrf, self.conn = user, csrf, conn

    def client_ip(request: Request) -> str:
        # Atrás do proxy (Coolify/Traefik ou Caddy) o último endereço de X-Forwarded-For é o que o proxy viu;
        # os anteriores podem ser forjados pelo cliente.
        xff = request.headers.get("x-forwarded-for", "")
        return xff.split(",")[-1].strip() if xff else (request.client.host if request.client else "")

    def current(request: Request, conn: Database = Depends(get_db)) -> Ctx:
        found = auth.load_session(conn, request.cookies.get(SID), s)
        if not found:
            raise HTTPException(status_code=401)
        return Ctx(found[0], found[1], conn)

    def admin(ctx: Ctx = Depends(current)) -> Ctx:
        if not ctx.user.is_admin:
            raise HTTPException(status_code=404)
        return ctx

    def active_groups(conn: Database):
        """Grupos ativos em ordem de árvore; 'caminho' = nome completo (ex.: Franqueados › Campinas)."""
        from .groups import ordered_groups
        return ordered_groups(conn)

    def form_group_ids(form, allowed: set[int]) -> list[int]:
        ids = []
        for v in form.getlist("grupos"):
            try:
                g = int(v)
            except (TypeError, ValueError):
                continue
            if g in allowed and g not in ids:
                ids.append(g)
        return ids

    def require_csrf(ctx: Ctx, token: str | None) -> None:
        if not auth.check_csrf(ctx.csrf, token):
            raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    def render(request: Request, name: str, ctx: Ctx | None, status: int = 200, **kw):
        sync = None
        if ctx:
            from .ingest import last_sync
            sync = last_sync(ctx.conn)
        kw.update(request=request, user=ctx.user if ctx else None, csrf=ctx.csrf if ctx else "",
                  areas=access.visible_areas(ctx.conn, ctx.user) if ctx else [],
                  pode_enviar=can_upload(ctx) if ctx else False, sync=sync)
        return templates.TemplateResponse(request, name, kw, status_code=status)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        msg = {404: "Página não encontrada.", 403: "Ação não permitida.", 429: "Muitas solicitações. Aguarde.",
               413: "Arquivo grande demais."}
        return templates.TemplateResponse(request, "erro.html", {"request": request, "user": None, "areas": [],
                                          "csrf": "", "mensagem": msg.get(exc.status_code, "Erro.")},
                                          status_code=exc.status_code)

    def set_session_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(SID, token, httponly=True, secure=SECURE, samesite="lax", path="/",
                        max_age=s.session_max_days * 86400)

    # ---------------- autenticação ----------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, erro: str = ""):
        return templates.TemplateResponse(request, "login.html", {"request": request, "user": None, "areas": [],
                                          "csrf": "", "erro": LOGIN_ERRORS.get(erro, ""), "dev": s.is_dev})

    @app.get("/auth/start")
    def auth_start(request: Request):
        if not app.state.limiter.allow("login:" + client_ip(request), 30, 60):
            raise HTTPException(status_code=429)
        url, flow = auth.begin_login(s)
        resp = RedirectResponse(url, status_code=303)
        resp.set_cookie(FLOW, flow, httponly=True, secure=SECURE, samesite="lax", path="/", max_age=600)
        return resp

    @app.get("/auth/callback")
    def auth_callback(request: Request, code: str = "", state: str = "", conn: Database = Depends(get_db)):
        ip = client_ip(request)
        if not app.state.limiter.allow("login:" + ip, 30, 60):
            raise HTTPException(status_code=429)
        try:
            email = auth.finish_login(s, code, state, request.cookies.get(FLOW), http=oidc_http,
                                      key_resolver=oidc_key_resolver)
        except auth.LoginError as e:
            audit(conn, "login_falha", ip=ip, details=str(e))
            return RedirectResponse(f"/login?erro={_LOGIN_ERROR_CODES.get(str(e), 'zoho')}", status_code=303)
        return _complete_login(conn, email, ip)

    def _complete_login(conn: Database, email: str, ip: str) -> Response:
        user = conn.execute("SELECT id FROM users WHERE email=? AND active=1", (email,)).fetchone()
        if not user:
            audit(conn, "login_negado", ip=ip, details="e-mail fora da lista de autorizados")
            return RedirectResponse("/login?erro=sem-acesso", status_code=303)
        token = auth.create_session(conn, user["id"], s)
        audit(conn, "login", user_id=user["id"], ip=ip)
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp, token)
        resp.delete_cookie(FLOW, path="/", secure=SECURE, httponly=True)
        return resp

    if s.is_dev:
        @app.post("/auth/dev-login")
        def dev_login(request: Request, email: str = Form(...), conn: Database = Depends(get_db)):
            """SOMENTE em CIO_ENV=dev/test. Inexistente em produção."""
            return _complete_login(conn, email.lower().strip(), client_ip(request))

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form(""), ctx: Ctx = Depends(current)):
        require_csrf(ctx, csrf_token)
        auth.destroy_session(ctx.conn, request.cookies.get(SID))
        audit(ctx.conn, "logout", user_id=ctx.user.id)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SID, path="/", secure=SECURE, httponly=True)
        return resp

    @app.get("/saude", response_class=PlainTextResponse)
    def health(conn: Database = Depends(get_db)):
        conn.execute("SELECT 1").fetchone()
        return "ok"

    # ---------------- páginas ----------------
    def today_range() -> tuple[str, str]:
        now = datetime.now(TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        f = "%Y-%m-%dT%H:%M:%SZ"
        return start.strftime(f), end.strftime(f)

    @app.get("/", response_class=HTMLResponse)
    def inicio(request: Request, ctx: Ctx = Depends(current)):
        clause, params = access.visibility_clause(ctx.user)
        start, end = today_range()
        base = "SELECT r.id, r.subject, r.sender, r.received_at, r.priority, r.status, r.source, r.file_mime, a.name AS area_name FROM reports r LEFT JOIN areas a ON a.id=r.area_id WHERE " + clause
        ultimos = ctx.conn.execute(base + " ORDER BY r.received_at DESC LIMIT 10", params).fetchall()
        hoje = ctx.conn.execute(f"SELECT count(*) FROM reports r WHERE {clause} AND r.received_at>=? AND "
                                f"r.received_at<?", [*params, start, end]).fetchone()[0]
        alertas = ctx.conn.execute(base + " AND r.priority='alta' AND r.received_at>=? ORDER BY r.received_at DESC "
                                   "LIMIT 5", [*params, start]).fetchall()
        return render(request, "inicio.html", ctx, ultimos=ultimos, hoje=hoje, alertas=alertas)

    @app.get("/hoje", response_class=HTMLResponse)
    def hoje(request: Request, ctx: Ctx = Depends(current)):
        clause, params = access.visibility_clause(ctx.user)
        start, end = today_range()
        rows = ctx.conn.execute(
            f"SELECT r.id, r.subject, r.sender, r.received_at, r.priority, r.status, r.source, r.file_mime, a.name AS area_name FROM reports r LEFT JOIN areas a ON a.id=r.area_id WHERE {clause} "
            f"AND r.received_at>=? AND r.received_at<? ORDER BY r.priority='alta' DESC, r.received_at DESC",
            [*params, start, end]).fetchall()
        return render(request, "lista.html", ctx, titulo="Relatórios de hoje", rows=rows)

    @app.get("/areas", response_class=HTMLResponse)
    def areas(request: Request, ctx: Ctx = Depends(current)):
        return render(request, "areas.html", ctx)

    @app.get("/areas/{slug}", response_class=HTMLResponse)
    def area(request: Request, slug: str, pagina: int = 1, ctx: Ctx = Depends(current)):
        if slug not in {a["slug"] for a in access.visible_areas(ctx.conn, ctx.user)}:
            raise HTTPException(status_code=404)
        p = SearchParams(area=slug, pagina=pagina)
        rows, total = access.search(ctx.conn, ctx.user, p)
        nome = ctx.conn.execute("SELECT name FROM areas WHERE slug=?", (slug,)).fetchone()["name"]
        return render(request, "busca.html", ctx, p=p, rows=rows, total=total, titulo=nome, so_lista=True)

    def _search_page(request: Request, ctx: Ctx, titulo: str, p: SearchParams):
        if not app.state.limiter.allow(f"busca:{ctx.user.id}", 30, 60):
            raise HTTPException(status_code=429)
        rows, total = access.search(ctx.conn, ctx.user, p)
        if p.q:
            audit(ctx.conn, "busca", user_id=ctx.user.id, details=f"{len(p.q)} caracteres, {total} resultados")
        return render(request, "busca.html", ctx, p=p, rows=rows, total=total, titulo=titulo, so_lista=False)

    @app.get("/busca", response_class=HTMLResponse)
    def busca(request: Request, q: str = "", area: str = "", prioridade: str = "", de: str = "", ate: str = "",
              status: str = "", ordem: str = "relevancia", pagina: int = 1, ctx: Ctx = Depends(current)):
        p = SearchParams(q=q[:200], area=area, prioridade=prioridade, de=de, ate=ate, status=status,
                         ordem=ordem if q else "data", pagina=pagina)
        return _search_page(request, ctx, "Busca", p)

    @app.get("/historico", response_class=HTMLResponse)
    def historico(request: Request, area: str = "", prioridade: str = "", de: str = "", ate: str = "",
                  ordem: str = "data", pagina: int = 1, ctx: Ctx = Depends(current)):
        p = SearchParams(area=area, prioridade=prioridade, de=de, ate=ate, ordem=ordem, pagina=pagina)
        return _search_page(request, ctx, "Histórico", p)

    @app.get("/r/{report_id}", response_class=HTMLResponse)
    def detalhe(request: Request, report_id: int, ctx: Ctx = Depends(current)):
        r = access.get_report(ctx.conn, ctx.user, report_id)
        if not r:
            raise HTTPException(status_code=404)
        anexos = ctx.conn.execute("SELECT id, filename, size, supported FROM attachments WHERE report_id=?",
                                  (report_id,)).fetchall()
        versoes = ctx.conn.execute("SELECT version, created_at FROM report_versions WHERE report_id=? ORDER BY "
                                   "version DESC", (report_id,)).fetchall()
        audit(ctx.conn, "leitura", user_id=ctx.user.id, target=str(report_id))
        from .groups import report_group_names
        extra = {}
        if ctx.user.is_admin:
            extra = {"all_groups": active_groups(ctx.conn),
                     "all_areas": ctx.conn.execute("SELECT * FROM areas WHERE active=1 ORDER BY name").fetchall(),
                     "grupos_atuais": {g[0] for g in ctx.conn.execute(
                         "SELECT group_id FROM report_groups WHERE report_id=?", (report_id,)).fetchall()}}
        return render(request, "detalhe.html", ctx, r=r, anexos=anexos, versoes=versoes,
                      grupos=report_group_names(ctx.conn, report_id), **extra)

    @app.get("/r/{report_id}/conteudo", response_class=HTMLResponse)
    def conteudo(report_id: int, ctx: Ctx = Depends(current)):
        r = access.get_report(ctx.conn, ctx.user, report_id)
        if not r:
            raise HTTPException(status_code=404)
        page = ("<!doctype html><html><head><meta charset='utf-8'><style>body{font-family:Arial,sans-serif;"
                "margin:12px;color:#1a1a1a;overflow-wrap:anywhere}table{max-width:100%}</style></head><body>"
                + r["body_html_sanitized"] + "</body></html>")
        return HTMLResponse(page)

    def _safe_path(rel: str) -> Path:
        base = s.data_dir.resolve()
        p = (base / rel).resolve()
        if base not in p.parents or not p.is_file():
            raise HTTPException(status_code=404)
        return p

    @app.get("/r/{report_id}/download")
    def download(report_id: int, ctx: Ctx = Depends(current)):
        r = access.get_report(ctx.conn, ctx.user, report_id)
        if not r or not r["download_allowed"] or not r["original_path"]:
            raise HTTPException(status_code=404)
        if not app.state.limiter.allow(f"dl:{ctx.user.id}", 30, 60):
            raise HTTPException(status_code=429)
        path = _safe_path(r["original_path"])
        audit(ctx.conn, "download", user_id=ctx.user.id, target=str(report_id))
        # Sempre como anexo e octet-stream: o navegador não renderiza HTML não sanitizado.
        ext = path.suffix if r["source"] == "manual" else ".html"
        return FileResponse(path, media_type="application/octet-stream", filename=f"relatorio-{report_id}{ext}",
                            content_disposition_type="attachment")

    @app.get("/r/{report_id}/anexo/{att_id}")
    def anexo(report_id: int, att_id: int, ctx: Ctx = Depends(current)):
        r = access.get_report(ctx.conn, ctx.user, report_id)
        if not r or not r["download_allowed"]:
            raise HTTPException(status_code=404)
        a = ctx.conn.execute("SELECT * FROM attachments WHERE id=? AND report_id=?", (att_id, report_id)).fetchone()
        if not a or not a["local_path"]:
            raise HTTPException(status_code=404)
        audit(ctx.conn, "download_anexo", user_id=ctx.user.id, target=f"{report_id}/{att_id}")
        return FileResponse(_safe_path(a["local_path"]), media_type="application/octet-stream",
                            filename=a["filename"], content_disposition_type="attachment")

    # ---------------- administração ----------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request, aviso: str = "", ctx: Ctx = Depends(admin)):
        c = ctx.conn
        stats = {k: c.execute(f"SELECT count(*) FROM reports WHERE status='{k}'").fetchone()[0]
                 for k in ("publicado", "quarentena", "despublicado", "erro")}
        runs = c.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 10").fetchall()
        erros = c.execute("SELECT * FROM ingest_errors WHERE resolved=0 ORDER BY id DESC LIMIT 50").fetchall()
        from .ingest import MAX_ATTEMPTS
        known = {r[0] for r in c.execute("SELECT email FROM users WHERE active=1").fetchall()}
        monitored = set(s.monitored_emails)
        fila = []
        for r in c.execute("SELECT r.id, r.subject, r.sender, r.received_at, r.status_reason, r.area_id, "
                           "r.recipients, r.sender_verified, a.name AS area_name FROM reports r LEFT JOIN areas a "
                           "ON a.id=r.area_id WHERE r.status='quarentena' ORDER BY r.received_at DESC LIMIT 100"
                           ).fetchall():
            rec = [x for x in (r["recipients"] or "").split(",") if x and x not in monitored]
            fila.append({**dict(zip(r.keys(), r)),
                         "reconhecidos": [x for x in rec if x in known],
                         "ignorados": [x for x in rec if x not in known],
                         "grupos_sugeridos": {g[0] for g in c.execute(
                             "SELECT group_id FROM report_groups WHERE report_id=?", (r["id"],)).fetchall()}})
        avisos = {"iniciada": "Sincronização iniciada. Atualize a página em alguns instantes.",
                  "sincronizando": "Já existe uma sincronização em andamento.",
                  "tentar": "A mensagem voltará a ser processada na próxima sincronização.",
                  "backup": "Backup iniciado. O resultado aparece em Auditoria (backup / restore_test)."}
        from . import __version__
        from .backup import last_backup_info
        ultimo_teste = c.execute("SELECT at, details FROM audit_log WHERE action='restore_test' ORDER BY id DESC "
                                 "LIMIT 1").fetchone()
        return render(request, "admin/index.html", ctx, db_pg=c.is_pg, stats=stats, runs=runs, erros=erros, fila=fila,
                      ultimo_backup=last_backup_info(s), ultimo_teste=ultimo_teste, versao=__version__,
                      aviso=avisos.get(aviso, ""), max_tentativas=MAX_ATTEMPTS,
                      all_areas=c.execute("SELECT * FROM areas WHERE active=1 ORDER BY name").fetchall(),
                      all_groups=active_groups(c))

    @app.post("/admin/r/{report_id}")
    async def admin_report(report_id: int, request: Request, ctx: Ctx = Depends(admin)):
        form = await request.form()
        require_csrf(ctx, form.get("csrf_token"))
        acao = str(form.get("acao", ""))
        r = ctx.conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404)
        if acao == "publicar" and not r["sender_verified"] and not form.get("confirmar_remetente"):
            raise HTTPException(status_code=403, detail="Remetente não autenticado: confirme antes de publicar.")
        if acao == "publicar":
            gids = form_group_ids(form, {g["id"] for g in active_groups(ctx.conn)})
            if not gids:
                raise HTTPException(status_code=403, detail="Escolha ao menos um grupo antes de publicar.")
            try:
                area_id = int(form.get("area_id") or 0) or r["area_id"]
            except ValueError:
                area_id = r["area_id"]
            from .groups import set_report_groups
            with ctx.conn.transaction():
                ctx.conn.execute("UPDATE reports SET status='publicado', area_id=?, status_reason='publicado manualmente', "
                                 "updated_at=? WHERE id=?", (area_id, now_iso(), report_id))
                set_report_groups(ctx.conn, report_id, gids)
        elif acao in {"despublicar", "quarentena"}:
            ctx.conn.execute("UPDATE reports SET status=?, status_reason=?, updated_at=? WHERE id=?",
                             ("despublicado" if acao == "despublicar" else "quarentena", f"{acao} manual", now_iso(),
                              report_id))
        else:
            raise HTTPException(status_code=403)
        audit(ctx.conn, f"admin_{acao}", user_id=ctx.user.id, target=str(report_id))
        destino = f"/r/{report_id}" if form.get("voltar") == "detalhe" else "/admin"
        return RedirectResponse(destino, status_code=303)

    @app.get("/admin/regras", response_class=HTMLResponse)
    def regras(request: Request, erro: str = "", ctx: Ctx = Depends(admin)):
        rows = ctx.conn.execute("SELECT r.*, a.name AS area_name FROM classification_rules r LEFT JOIN areas a ON "
                                "a.id=r.area_id ORDER BY position, id").fetchall()
        return render(request, "admin/regras.html", ctx, rules=rows, erro=erro, all_groups=active_groups(ctx.conn),
                      all_areas=ctx.conn.execute("SELECT * FROM areas ORDER BY name").fetchall())

    @app.post("/admin/regras")
    def salvar_regra(rule_id: int = Form(0), name: str = Form(...), position: int = Form(100),
                     sender_pattern: str = Form(""), subject_regex: str = Form(""), area_id: int = Form(0),
                     priority: str = Form("normal"), group_id: int = Form(0),
                     audience_mode: str = Form("destinatarios"), action: str = Form("quarentena"),
                     active: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        require_csrf(ctx, csrf_token)
        for pat in (sender_pattern, subject_regex):
            err = validate_regex(pat)
            if err:
                return RedirectResponse(f"/admin/regras?erro={err}", status_code=303)
        if priority not in {"baixa", "normal", "alta"} or audience_mode not in {"destinatarios", "fixo"} \
                or action not in {"publicar", "quarentena"}:
            raise HTTPException(status_code=403)
        if group_id and group_id not in {g["id"] for g in active_groups(ctx.conn)}:
            raise HTTPException(status_code=403)
        vals = (position, name[:100], sender_pattern, subject_regex, area_id or None, priority, group_id or None,
                audience_mode, action, 1 if active else 0)
        if rule_id:
            ctx.conn.execute("UPDATE classification_rules SET position=?, name=?, sender_pattern=?, subject_regex=?, "
                             "area_id=?, priority=?, group_id=?, audience_mode=?, action=?, active=? WHERE id=?",
                             (*vals, rule_id))
        else:
            rule_id = ctx.conn.insert("INSERT INTO classification_rules(position, name, sender_pattern, "
                                       "subject_regex, area_id, priority, group_id, audience_mode, action, active) "
                                       "VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
        audit(ctx.conn, "regra_alterada", user_id=ctx.user.id, target=str(rule_id), details=name[:100])
        return RedirectResponse("/admin/regras", status_code=303)

    @app.post("/admin/reclassificar")
    def reclassificar(csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        """Reaplica regras aos itens em quarentena (não altera publicados/despublicados)."""
        require_csrf(ctx, csrf_token)
        from .classify import classify
        from .groups import resolve_audience, set_report_groups
        n = 0
        for r in ctx.conn.execute("SELECT id, sender, subject, folder_id, recipients, source FROM reports "
                                  "WHERE status='quarentena' AND source='email' AND sender_verified=1").fetchall():
            c = classify(ctx.conn, r["sender"], r["subject"], r["folder_id"])
            aud = resolve_audience(ctx.conn, c.group_id, c.audience_mode,
                                   [x for x in (r["recipients"] or "").split(",") if x],
                                   set(s.monitored_emails) | {r["sender"].lower()})
            status = c.status if aud.group_ids and not aud.broader else "quarentena"
            with ctx.conn.transaction():
                ctx.conn.execute("UPDATE reports SET area_id=?, priority=?, status=?, status_reason=?, rule_id=? "
                                 "WHERE id=?", (c.area_id, c.priority, status, f"{c.reason}; público: {aud.reason}",
                                                c.rule_id, r["id"]))
                set_report_groups(ctx.conn, r["id"], aud.group_ids)
            n += status == "publicado"
        audit(ctx.conn, "reclassificar", user_id=ctx.user.id, details=f"{n} publicados")
        return RedirectResponse("/admin", status_code=303)

    EMAIL_RE = __import__("re").compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")

    @app.get("/admin/fontes", response_class=HTMLResponse)
    def fontes(request: Request, erro: str = "", ctx: Ctx = Depends(admin)):
        rows = ctx.conn.execute("SELECT * FROM mail_sources ORDER BY active DESC, email").fetchall()
        return render(request, "admin/fontes.html", ctx, fontes=rows, erro=erro[:200], prefixo=s.subject_prefix,
                      contas=s.mail_account_ids)

    @app.post("/admin/fontes")
    def salvar_fonte(source_id: int = Form(0), email: str = Form(""), note: str = Form(""),
                     require_prefix: str = Form(""), active: str = Form(""), csrf_token: str = Form(""),
                     ctx: Ctx = Depends(admin)):
        require_csrf(ctx, csrf_token)
        req, act = (1 if require_prefix else 0), (1 if active else 0)
        if source_id:
            row = ctx.conn.execute("SELECT email FROM mail_sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404)
            ctx.conn.execute("UPDATE mail_sources SET require_prefix=?, active=?, note=? WHERE id=?",
                             (req, act, note[:200], source_id))
            target = row["email"]
        else:
            email = email.strip().lower()
            if len(email) > 254 or not EMAIL_RE.match(email):
                return RedirectResponse("/admin/fontes?erro=E-mail inválido. Use o formato nome@dominio.com.br.",
                                        status_code=303)
            if ctx.conn.execute("SELECT 1 FROM mail_sources WHERE email=?", (email,)).fetchone():
                return RedirectResponse("/admin/fontes?erro=Este e-mail já está na lista. Altere-o na tabela.",
                                        status_code=303)
            ctx.conn.execute("INSERT INTO mail_sources(email, require_prefix, active, note) VALUES (?,?,?,?)",
                             (email, req, act, note[:200]))
            target = email
        audit(ctx.conn, "fonte_alterada", user_id=ctx.user.id, target=target,
              details=f"ativa={act} exige_prefixo={req}")
        return RedirectResponse("/admin/fontes", status_code=303)

    @app.get("/admin/usuarios", response_class=HTMLResponse)
    def usuarios(request: Request, editar: str = "", erro: str = "", ctx: Ctx = Depends(admin)):
        from .groups import ordered_groups
        caminhos = {g["id"]: g["caminho"] for g in ordered_groups(ctx.conn, active_only=False)}
        users = [{**dict(zip(u.keys(), u)), "group_name": caminhos.get(u["group_id"])}
                 for u in ctx.conn.execute("SELECT * FROM users ORDER BY email").fetchall()]
        atual = next((u for u in users if u["email"] == editar.lower()), None)
        erros = {"ultimo-admin": "Não é possível: este é o último administrador ativo. Cadastre outro antes."}
        return render(request, "admin/usuarios.html", ctx, users=users, atual=atual, erro=erros.get(erro, ""),
                      all_groups=[g for g in active_groups(ctx.conn) if g["kind"] != "todos"])

    @app.post("/admin/usuarios")
    async def salvar_usuario(request: Request, ctx: Ctx = Depends(admin)):
        form = await request.form()
        require_csrf(ctx, form.get("csrf_token"))
        email = str(form.get("email", "")).strip().lower()
        if not email.endswith("@" + s.allowed_domain) or len(email) > 254:
            raise HTTPException(status_code=403, detail="E-mail fora do domínio.")
        role = "admin" if form.get("admin") else "operacional"
        valid = {g["id"] for g in active_groups(ctx.conn) if g["kind"] != "todos"}
        try:
            gid = int(form.get("group_id") or 0) or None
        except ValueError:
            gid = None
        if gid is not None and gid not in valid:
            raise HTTPException(status_code=403)
        active = 1 if form.get("active") else 0
        atual = ctx.conn.execute("SELECT role, active FROM users WHERE email=?", (email,)).fetchone()
        if atual and atual["role"] == "admin" and atual["active"] and (role != "admin" or not active):
            outros = ctx.conn.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1 AND email<>?",
                                      (email,)).fetchone()[0]
            if not outros:
                return RedirectResponse("/admin/usuarios?erro=ultimo-admin", status_code=303)
        ctx.conn.execute("INSERT INTO users(email, name, role, active, group_id) VALUES (?,?,?,?,?) ON CONFLICT(email) "
                         "DO UPDATE SET name=excluded.name, role=excluded.role, active=excluded.active, "
                         "group_id=excluded.group_id", (email, str(form.get("name", ""))[:100], role, active, gid))
        uid = ctx.conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        if not active:
            ctx.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(ctx.conn, "usuario_alterado", user_id=ctx.user.id, target=email,
              details=f"admin={role == 'admin'} grupo={gid} ativo={active}")
        return RedirectResponse("/admin/usuarios", status_code=303)

    # ---------------- grupos de acesso ----------------
    @app.get("/admin/grupos", response_class=HTMLResponse)
    def admin_grupos(request: Request, erro: str = "", ctx: Ctx = Depends(admin)):
        from .groups import ordered_groups
        counts = {r["id"]: (r["pessoas"], r["relatorios"]) for r in ctx.conn.execute(
            "SELECT g.id, (SELECT count(*) FROM users u WHERE u.group_id=g.id AND u.active=1) AS pessoas, "
            "(SELECT count(*) FROM report_groups rg WHERE rg.group_id=g.id) AS relatorios FROM groups g").fetchall()}
        lista = [{**g, "pessoas": counts[g["id"]][0], "relatorios": counts[g["id"]][1]}
                 for g in ordered_groups(ctx.conn, active_only=False)]
        sees = {}
        for row in ctx.conn.execute("SELECT viewer_group_id, content_group_id FROM group_visibility").fetchall():
            sees.setdefault(row[0], set()).add(row[1])
        return render(request, "admin/grupos.html", ctx, lista=lista, sees=sees, erro=erro[:200])

    @app.post("/admin/grupos")
    async def salvar_grupo(request: Request, ctx: Ctx = Depends(admin)):
        form = await request.form()
        require_csrf(ctx, form.get("csrf_token"))
        name = str(form.get("name", "")).strip()[:60]
        kind = str(form.get("kind", "interno"))
        if not name or kind not in {"interno", "franqueado"}:
            return RedirectResponse("/admin/grupos?erro=Informe nome e tipo do grupo.", status_code=303)
        try:
            gid = int(form.get("group_id") or 0)
            position = int(form.get("position") or 100)
        except ValueError:
            raise HTTPException(status_code=403)
        active, up = (1 if form.get("active") else 0), (1 if form.get("can_upload") else 0)
        all_ids = {g["id"] for g in ctx.conn.execute("SELECT id FROM groups WHERE kind<>'todos'").fetchall()}
        try:
            parent_id = int(form.get("parent_id") or 0) or None
        except ValueError:
            parent_id = None
        from .groups import would_cycle
        if parent_id is not None and (parent_id not in all_ids or (gid and would_cycle(ctx.conn, gid, parent_id))):
            return RedirectResponse("/admin/grupos?erro=Grupo pai inválido: um grupo não pode ficar dentro de "
                                    "si mesmo nem de um subgrupo seu.", status_code=303)
        with ctx.conn.transaction():
            if gid:
                row = ctx.conn.execute("SELECT kind FROM groups WHERE id=?", (gid,)).fetchone()
                if not row or row["kind"] == "todos":
                    raise HTTPException(status_code=403)
                ctx.conn.execute("UPDATE groups SET name=?, kind=?, can_upload=?, position=?, active=?, parent_id=? "
                                 "WHERE id=?", (name, kind, up, position, active, parent_id, gid))
            else:
                slug = slugify(name)
                if not slug or ctx.conn.execute("SELECT 1 FROM groups WHERE slug=?", (slug,)).fetchone():
                    return RedirectResponse("/admin/grupos?erro=Já existe um grupo com esse nome.", status_code=303)
                gid = ctx.conn.insert("INSERT INTO groups(slug, name, kind, can_upload, position, active, parent_id) "
                                      "VALUES (?,?,?,?,?,?,?)", (slug, name, kind, up, position, active, parent_id))
                all_ids.add(gid)
            ctx.conn.execute("DELETE FROM group_visibility WHERE viewer_group_id=?", (gid,))
            ctx.conn.execute("INSERT INTO group_visibility VALUES (?,?)", (gid, gid))
            for v in form.getlist("ve"):
                try:
                    other = int(v)
                except ValueError:
                    continue
                if other in all_ids and other != gid:
                    ctx.conn.execute("INSERT INTO group_visibility VALUES (?,?) ON CONFLICT DO NOTHING", (gid, other))
        audit(ctx.conn, "grupo_alterado", user_id=ctx.user.id, target=str(gid),
              details=f"{name} tipo={kind} pai={parent_id} ativo={active} envia={up}")
        return RedirectResponse("/admin/grupos", status_code=303)

    @app.get("/admin/auditoria", response_class=HTMLResponse)
    def auditoria(request: Request, ctx: Ctx = Depends(admin)):
        rows = ctx.conn.execute("SELECT l.*, u.email FROM audit_log l LEFT JOIN users u ON u.id=l.user_id "
                                "ORDER BY l.id DESC LIMIT 300").fetchall()
        return render(request, "admin/auditoria.html", ctx, rows=rows)

    @app.post("/admin/ingerir")
    def ingerir_agora(csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        require_csrf(ctx, csrf_token)
        if not zoho_factory:
            raise HTTPException(status_code=403, detail="Ingestão não configurada.")
        import threading
        from .ingest import run_once

        def job():
            conn = connect(s)
            try:
                res = run_once(conn, zoho_factory(), s, mode="manual")
                if res.errors:
                    from .alerts import send_admin_alert
                    send_admin_alert(
                        conn, s, "ingestao", "Falha na captura de e-mails",
                        f"A sincronização manual registrou {res.errors} erro(s). "
                        "Verifique Administração › erros não resolvidos.")
            except RuntimeError:
                pass  # outra sincronização já está rodando
            except Exception:
                log.exception("Sincronização manual falhou")
                try:
                    from .alerts import send_admin_alert
                    send_admin_alert(conn, s, "ingestao", "Sincronização manual falhou",
                                     "A sincronização manual não foi concluída. Verifique o log do container.")
                except Exception:
                    log.exception("Não foi possível enviar o alerta de ingestão")
            finally:
                conn.close()

        if (s.data_dir / "ingest.lock").exists() and _lock_busy():
            return RedirectResponse("/admin?aviso=sincronizando", status_code=303)
        threading.Thread(target=job, daemon=True).start()
        audit(ctx.conn, "ingestao_manual", user_id=ctx.user.id)
        return RedirectResponse("/admin?aviso=iniciada", status_code=303)

    def _lock_busy() -> bool:
        import fcntl
        with open(s.data_dir / "ingest.lock", "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False

    @app.post("/admin/erros/{error_id}/tentar")
    def tentar_de_novo(error_id: int, csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        """A4: devolve uma mensagem isolada para a fila de captura."""
        require_csrf(ctx, csrf_token)
        from .ingest import retry_message
        if not retry_message(ctx.conn, error_id):
            raise HTTPException(status_code=404)
        audit(ctx.conn, "erro_tentar_de_novo", user_id=ctx.user.id, target=str(error_id))
        return RedirectResponse("/admin?aviso=tentar", status_code=303)

    @app.post("/admin/backup")
    def backup_agora(csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        """Backup completo (banco + arquivos) e teste de restauração, em segundo plano."""
        require_csrf(ctx, csrf_token)
        import threading

        from .backup import run_backup, verify_set

        def job():
            conn = connect(s)
            try:
                made = run_backup(conn, s)
                verify_set(s, made["conjunto"], reference=conn)
            except Exception:
                log.exception("Backup manual falhou")
                try:
                    from .alerts import send_admin_alert
                    send_admin_alert(conn, s, "backup", "Backup manual falhou",
                                     "O backup iniciado na Administração não foi concluído. "
                                     "Verifique o log do container.")
                    audit(conn, "backup_falhou", details="ver log do container")
                except Exception:
                    log.exception("Não foi possível registrar a falha de backup")
            finally:
                conn.close()

        threading.Thread(target=job, daemon=True).start()
        audit(ctx.conn, "backup_manual", user_id=ctx.user.id)
        return RedirectResponse("/admin?aviso=backup", status_code=303)


    # ---------------- envio manual de arquivos ----------------
    def upload_areas(ctx: Ctx):
        return ctx.conn.execute("SELECT id, name FROM areas WHERE active=1 ORDER BY name").fetchall()

    def upload_groups(ctx: Ctx):
        """Admin publica para qualquer grupo; os demais, só para grupos que o próprio grupo enxerga."""
        groups_ = active_groups(ctx.conn)
        if ctx.user.is_admin:
            return groups_
        from .groups import visible_group_ids
        allowed = set(visible_group_ids(ctx.conn, ctx.user.group_id))
        pais = {g["parent_id"] for g in groups_ if g.get("parent_id")}
        return [g for g in groups_ if g["id"] in allowed and g["kind"] != "todos" and g["id"] not in pais]

    def can_upload(ctx: Ctx) -> bool:
        if ctx.user.is_admin:
            return True
        g = ctx.conn.execute("SELECT can_upload FROM groups WHERE id=? AND active=1",
                             (ctx.user.group_id or -1,)).fetchone()
        return bool(g and g["can_upload"])

    @app.get("/enviar", response_class=HTMLResponse)
    def enviar_form(request: Request, ctx: Ctx = Depends(current)):
        if not can_upload(ctx):
            raise HTTPException(status_code=404)
        return render(request, "enviar.html", ctx, erro="", destinos=upload_areas(ctx),
                      grupos=upload_groups(ctx),
                      hoje=datetime.now(TZ).strftime("%Y-%m-%d"), limite_mb=s.max_attachment_bytes // (1024 * 1024))

    @app.post("/enviar")
    async def enviar(request: Request, ctx: Ctx = Depends(current)):
        if not can_upload(ctx):
            raise HTTPException(status_code=404)
        if not app.state.limiter.allow(f"envio:{ctx.user.id}", 20, 3600):
            raise HTTPException(status_code=429)
        form = await request.form(max_files=1, max_fields=20)
        require_csrf(ctx, form.get("csrf_token"))
        from .upload import ManualReport, UploadError, save_upload

        def back(msg: str):
            # A mensagem é exibida direto (não pelo endereço): um link de fora não consegue pôr texto na tela.
            return render(request, "enviar.html", ctx, status=400, erro=msg, destinos=upload_areas(ctx),
                          grupos=upload_groups(ctx), hoje=datetime.now(TZ).strftime("%Y-%m-%d"),
                          limite_mb=s.max_attachment_bytes // (1024 * 1024))

        title = str(form.get("titulo", "")).strip()
        area_ids = {a["id"] for a in upload_areas(ctx)}
        try:
            area_id = int(form.get("area_id") or 0)
        except ValueError:
            area_id = 0
        gids = form_group_ids(form, {g["id"] for g in upload_groups(ctx)})
        prio = str(form.get("priority", "normal"))
        ref = str(form.get("data", ""))
        if not title or len(title) > 300:
            return back("Informe um título (até 300 caracteres).")
        if area_id not in area_ids:
            return back("Escolha a área (assunto) do arquivo.")
        if not gids:
            return back("Escolha ao menos um grupo que poderá ver o arquivo.")
        if prio not in {"baixa", "normal", "alta"}:
            return back("Prioridade inválida.")
        try:
            datetime.strptime(ref, "%Y-%m-%d")
        except ValueError:
            return back("Data inválida.")
        file = form.get("arquivo")
        if file is None or not hasattr(file, "read"):
            return back("Selecione um arquivo.")
        data = await file.read(s.max_attachment_bytes + 1)
        wd = None
        if zoho_factory and s.workdrive_root_folder_id:
            def wd(name, content, mime, refdt):
                from .ingest import Ingestor
                ing = Ingestor(ctx.conn, zoho_factory(), s)
                folder = ing._wd_folder(["_envios_manuais", f"{refdt:%Y}", f"{refdt:%m}"])
                return ing.api.upload(folder, name, content, mime)
        try:
            rid = save_upload(ctx.conn, s, ctx.user.email, ManualReport(
                title, str(form.get("descricao", ""))[:5000], area_id, prio, gids, ref), data, wd)
        except UploadError as e:
            return back(str(e))
        audit(ctx.conn, "envio_manual", user_id=ctx.user.id, target=str(rid), details=f"{len(data)} bytes")
        return RedirectResponse(f"/r/{rid}", status_code=303)

    @app.get("/r/{report_id}/arquivo")
    def arquivo(report_id: int, ctx: Ctx = Depends(current)):
        r = access.get_report(ctx.conn, ctx.user, report_id)
        if not r or r["source"] != "manual" or not r["file_path"]:
            raise HTTPException(status_code=404)
        audit(ctx.conn, "leitura_arquivo", user_id=ctx.user.id, target=str(report_id))
        return FileResponse(_safe_path(r["file_path"]), media_type=r["file_mime"], content_disposition_type="inline",
                            filename=f"relatorio-{report_id}{_safe_path(r['file_path']).suffix}")

    # ---------------- pastas de pesquisa (buscas salvas) ----------------
    def folder_params(f, pagina: int = 1) -> SearchParams:
        return SearchParams(q=f["q"], area=f["area_slug"], prioridade=f["priority"],
                            ordem="relevancia" if f["q"] else "data", pagina=pagina, qualquer=bool(f["match_any"]))

    @app.get("/pastas", response_class=HTMLResponse)
    def pastas(request: Request, ctx: Ctx = Depends(current)):
        rows = ctx.conn.execute("SELECT * FROM search_folders WHERE active=1 ORDER BY position, name").fetchall()
        items = [{"id": f["id"], "name": f["name"], "q": f["q"],
                  "total": access.search(ctx.conn, ctx.user, folder_params(f))[1]} for f in rows]
        return render(request, "pastas.html", ctx, pastas=items)

    @app.get("/pastas/{folder_id}", response_class=HTMLResponse)
    def pasta(request: Request, folder_id: int, pagina: int = 1, ctx: Ctx = Depends(current)):
        f = ctx.conn.execute("SELECT * FROM search_folders WHERE id=? AND active=1", (folder_id,)).fetchone()
        if not f:
            raise HTTPException(status_code=404)
        p = folder_params(f, pagina)
        rows, total = access.search(ctx.conn, ctx.user, p)
        return render(request, "busca.html", ctx, p=p, rows=rows, total=total, titulo=f["name"], so_lista=True)

    # ---------------- administração: áreas e pastas ----------------
    def slugify(name: str) -> str:
        import unicodedata
        base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        return "-".join("".join(ch if ch.isalnum() else " " for ch in base).split())[:40]

    @app.get("/admin/areas", response_class=HTMLResponse)
    def admin_areas(request: Request, erro: str = "", ctx: Ctx = Depends(admin)):
        rows = ctx.conn.execute("SELECT a.*, (SELECT count(*) FROM reports r WHERE r.area_id=a.id) AS total "
                                "FROM areas a ORDER BY a.active DESC, a.name").fetchall()
        return render(request, "admin/areas.html", ctx, lista=rows, erro=erro[:200])

    @app.post("/admin/areas")
    def salvar_area(area_id: int = Form(0), name: str = Form(""), active: str = Form(""),
                    csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        require_csrf(ctx, csrf_token)
        name = name.strip()[:60]
        if not name:
            return RedirectResponse("/admin/areas?erro=Informe o nome da área.", status_code=303)
        act = 1 if active else 0
        if area_id:
            ctx.conn.execute("UPDATE areas SET name=?, active=? WHERE id=?", (name, act, area_id))
            target = str(area_id)
        else:
            slug = slugify(name)
            if not slug or ctx.conn.execute("SELECT 1 FROM areas WHERE slug=?", (slug,)).fetchone():
                return RedirectResponse("/admin/areas?erro=Já existe uma área com esse nome.", status_code=303)
            ctx.conn.execute("INSERT INTO areas(slug, name, active) VALUES (?,?,?)", (slug, name, act))
            target = slug
        audit(ctx.conn, "area_alterada", user_id=ctx.user.id, target=target, details=f"{name} ativa={act}")
        return RedirectResponse("/admin/areas", status_code=303)

    @app.get("/admin/pastas", response_class=HTMLResponse)
    def admin_pastas(request: Request, ctx: Ctx = Depends(admin)):
        rows = ctx.conn.execute("SELECT * FROM search_folders ORDER BY active DESC, position, name").fetchall()
        return render(request, "admin/pastas.html", ctx, lista=rows,
                      all_areas=ctx.conn.execute("SELECT slug, name FROM areas WHERE active=1 ORDER BY name").fetchall())

    @app.post("/admin/pastas")
    def salvar_pasta(folder_id: int = Form(0), name: str = Form(""), q: str = Form(""), area_slug: str = Form(""),
                     priority: str = Form(""), position: int = Form(100), active: str = Form(""),
                     match_any: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin)):
        require_csrf(ctx, csrf_token)
        name = name.strip()[:80]
        if not name or priority not in {"", "baixa", "normal", "alta"}:
            raise HTTPException(status_code=403)
        vals = (name, access.clean_query(q), area_slug[:40], priority, position, 1 if active else 0,
                1 if match_any else 0)
        if folder_id:
            ctx.conn.execute("UPDATE search_folders SET name=?, q=?, area_slug=?, priority=?, position=?, active=?, "
                             "match_any=? WHERE id=?", (*vals, folder_id))
        else:
            folder_id = ctx.conn.insert("INSERT INTO search_folders(name, q, area_slug, priority, position, active, "
                                        "match_any) VALUES (?,?,?,?,?,?,?)", vals)
        audit(ctx.conn, "pasta_alterada", user_id=ctx.user.id, target=str(folder_id), details=name)
        return RedirectResponse("/admin/pastas", status_code=303)

    return app
