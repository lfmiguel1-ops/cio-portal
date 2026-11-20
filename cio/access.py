"""Autorização (RBAC + área + confidencialidade) e busca FTS5.

Toda consulta de relatórios passa por `visibility_clause`, aplicada no SQL ANTES de
qualquer snippet/contagem — assim nenhum trecho de documento proibido é gerado.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .db import Database, Row

SORTS = {"data": "r.received_at DESC", "data_asc": "r.received_at ASC", "relevancia": "rank"}


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    group_id: int | None = None
    visible_groups: tuple[int, ...] = ()   # calculado no login/sessão (inclui hierarquia e "Todos")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def visibility_clause(user: User, alias: str = "r") -> tuple[str, list]:
    """Admin vê tudo. Demais: publicado, área ativa (ou sem área) e ao menos um grupo do relatório visível
    para o grupo da pessoa (o próprio, os configurados em group_visibility, ou 'Todos')."""
    if user.is_admin:
        return "1=1", []
    ids = list(user.visible_groups) or [-1]
    ph = ",".join("?" * len(ids))
    return (
        f"{alias}.status='publicado' AND ({alias}.area_id IS NULL OR {alias}.area_id IN "
        f"(SELECT id FROM areas WHERE active=1)) AND EXISTS (SELECT 1 FROM report_groups rg "
        f"WHERE rg.report_id={alias}.id AND rg.group_id IN ({ph}))",
        ids,
    )


def get_report(conn: Database, user: User, report_id: int) -> Row | None:
    """Retorna None tanto para inexistente quanto para não autorizado (mesma resposta 404)."""
    clause, params = visibility_clause(user)
    return conn.execute(
        f"SELECT r.*, a.name AS area_name, a.slug AS area_slug FROM reports r LEFT JOIN areas a ON a.id=r.area_id "
        f"WHERE r.id=? AND {clause}", [report_id, *params]).fetchone()


_TOKEN = re.compile(r'"([^"]+)"|(\S+)')


def any_words(q: str) -> str:
    """Converte "a b \"c d\"" em "a or b or \"c d\"" (sintaxe websearch do PostgreSQL)."""
    parts = [f'"{p}"' if p else w for p, w in _TOKEN.findall(q or "")]
    return " or ".join(x for x in parts if x.strip('"').strip() and x.lower() != "or")


def build_fts_query(q: str, any_: bool = False) -> str:
    """Converte a entrada do usuário numa expressão FTS5 segura.

    - "frase exata" -> frase; palavra -> prefixo; operadores/sintaxe FTS do usuário são neutralizados.
    """
    terms = []
    for phrase, word in _TOKEN.findall(q or ""):
        if phrase:
            clean = re.sub(r'["]', " ", phrase).strip()
            if clean:
                terms.append(f'"{clean}"')
        else:
            clean = re.sub(r"[^\w\-./@]", "", word, flags=re.UNICODE).strip("-./")
            if clean:
                terms.append(f'"{clean}"*')
    return (" OR " if any_ else " AND ").join(terms[:12])


HEADLINE_OPTS = "StartSel=\u0002, StopSel=\u0003, MaxWords=24, MinWords=10, MaxFragments=2, FragmentDelimiter=\" … \""


def clean_query(q: str) -> str:
    """Remove caracteres de controle (NUL quebra o PostgreSQL) e limita o tamanho."""
    return "".join(ch for ch in (q or "") if ch >= " " or ch == "\t")[:200].strip()


@dataclass
class SearchParams:
    q: str = ""
    area: str = ""
    prioridade: str = ""
    de: str = ""       # YYYY-MM-DD
    ate: str = ""      # YYYY-MM-DD
    status: str = ""   # apenas admin
    ordem: str = "data"
    qualquer: bool = False  # True: basta uma das palavras (pastas de pesquisa)
    pagina: int = 1
    por_pagina: int = 20


def search(conn: Database, user: User, p: SearchParams) -> tuple[list[Row], int]:
    clause, params = visibility_clause(user)
    where = [clause]
    q = clean_query(p.q)
    if not q:
        fts = ""
    elif conn.is_pg:
        fts = any_words(q) if p.qualquer else q
    else:
        fts = build_fts_query(q, p.qualquer)
    joins = "LEFT JOIN areas a ON a.id=r.area_id"
    select_extra, select_params = "'' AS trecho, 0 AS rank", []
    if fts and conn.is_pg:
        # PostgreSQL: dicionário português + unaccent; websearch_to_tsquery aceita "frase", OR e -palavra
        # e nunca gera erro de sintaxe com entrada do usuário.
        where.append("r.search_vector @@ websearch_to_tsquery('pt_unaccent', ?)")
        params.append(fts)
        select_extra = ("ts_headline('pt_unaccent', r.text_plain, websearch_to_tsquery('pt_unaccent', ?), ?) AS trecho, "
                        "-ts_rank(r.search_vector, websearch_to_tsquery('pt_unaccent', ?)) AS rank")
        select_params = [fts, HEADLINE_OPTS, fts]
    elif fts:
        joins += " JOIN reports_fts f ON f.rowid=r.id"
        where.append("reports_fts MATCH ?")
        params.append(fts)
        select_extra = ("snippet(reports_fts, 1, '\u0002', '\u0003', ' … ', 18) AS trecho, "
                        "bm25(reports_fts, 5.0, 1.0) AS rank")
    if p.area:
        where.append("a.slug=?")
        params.append(p.area)
    if p.prioridade in {"baixa", "normal", "alta"}:
        where.append("r.priority=?")
        params.append(p.prioridade)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.de or ""):
        where.append("r.received_at >= ?")
        params.append(p.de + "T00:00:00Z")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.ate or ""):
        where.append("r.received_at <= ?")
        params.append(p.ate + "T23:59:59Z")
    if user.is_admin and p.status in {"publicado", "quarentena", "despublicado", "erro"}:
        where.append("r.status=?")
        params.append(p.status)
    order = SORTS.get(p.ordem, SORTS["data"])
    if order == "rank" and not fts:
        order = SORTS["data"]
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM reports r {joins} WHERE {sql_where}", params).fetchone()[0]
    per = max(1, min(p.por_pagina, 50))
    page = max(1, p.pagina)
    rows = conn.execute(
        f"SELECT r.id, r.subject, r.sender, r.received_at, r.priority, r.confidentiality, r.status, r.source, r.file_mime, "
        f"a.name AS area_name, a.slug AS area_slug, {select_extra} "
        f"FROM reports r {joins} WHERE {sql_where} ORDER BY {order}, r.id DESC LIMIT ? OFFSET ?",
        [*select_params, *params, per, (page - 1) * per]).fetchall()
    return rows, total


def visible_areas(conn: Database, user: User) -> list[Row]:
    clause, params = visibility_clause(user)
    rows = conn.execute(
        f"SELECT a.slug, a.name, (SELECT count(*) FROM reports r WHERE r.area_id=a.id AND {clause}) AS total "
        f"FROM areas a WHERE a.active=1 ORDER BY a.name", params).fetchall()
    # Para quem não é admin, só aparecem as áreas em que há algo que a pessoa pode ver.
    return rows if user.is_admin else [r for r in rows if r["total"]]
