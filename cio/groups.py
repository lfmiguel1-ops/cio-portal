"""Grupos de acesso: definem QUEM VÊ cada relatório (a área é só o assunto).

- Cada pessoa pertence a um grupo (ou nenhum). Administradores veem tudo.
- Cada relatório tem um ou mais grupos de público.
- Um grupo vê os próprios relatórios e os dos grupos listados em group_visibility
  (ex.: Diretoria vê Gestores, Financeiro e Geral). O grupo especial "Todos" é visível a qualquer pessoa ativa.
- Público de e-mails: grupos reais dos destinatários (sem agrupamento automático); revisão se for mais amplo
  que o previsto pela regra.
- Hierarquia (parent_id): quem está num subgrupo (ex.: Franqueado Campinas) vê também o que é enviado ao
  grupo pai (Franqueados), mas nunca o dos subgrupos irmãos; quem vê um grupo pai vê todos os subgrupos.
- Público de e-mails: grupos de quem está em Para/CC (exceto a caixa monitorada e o remetente).
  Se os destinatários cobrem todos os grupos, o público vira "Todos". Regras podem fixar o público.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .db import Database

EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# (slug, nome, tipo, pode_enviar_arquivos, posição)
DEFAULT_GROUPS = [
    ("todos", "Todos", "todos", 0, 0),
    ("diretoria", "Diretoria", "interno", 1, 10),
    ("gestores", "Gestores", "interno", 1, 20),
    ("financeiro", "Financeiro", "interno", 1, 25),
    ("geral", "Geral", "interno", 0, 30),
    ("franqueados", "Franqueados", "franqueado", 0, 40),
]
# grupo que vê -> grupos cujo conteúdo também vê
DEFAULT_VISIBILITY = {"diretoria": ["gestores", "financeiro", "geral", "franqueados"], "gestores": ["geral"],
                      "financeiro": ["geral"]}


def parse_recipients(*fields: str) -> list[str]:
    seen: list[str] = []
    for f in fields:
        for e in EMAIL_IN_TEXT.findall((f or "").replace("&lt;", "<").replace("&gt;", ">")):
            e = e.lower()
            if e not in seen:
                seen.append(e)
    return seen


def seed(db: Database) -> None:
    for slug, name, kind, up, pos in DEFAULT_GROUPS:
        db.execute("INSERT INTO groups(slug, name, kind, can_upload, position) VALUES (?,?,?,?,?) "
                   "ON CONFLICT (slug) DO NOTHING", (slug, name, kind, up, pos))
    ids = {r["slug"]: r["id"] for r in db.execute("SELECT id, slug FROM groups").fetchall()}
    for g in ids.values():  # todo grupo vê o próprio conteúdo
        db.execute("INSERT INTO group_visibility VALUES (?,?) ON CONFLICT DO NOTHING", (g, g))
    if not db.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
        # v3 (Financeiro e Franqueados): aplica as visibilidades padrão uma única vez; depois vale o que o
        # administrador configurar na tela.
        for viewer, contents in DEFAULT_VISIBILITY.items():
            for c in contents:
                db.execute("INSERT INTO group_visibility VALUES (?,?) ON CONFLICT DO NOTHING", (ids[viewer], ids[c]))


def group_ids(db: Database) -> dict[str, int]:
    return {r["slug"]: r["id"] for r in db.execute("SELECT id, slug FROM groups").fetchall()}


def _tree(db: Database) -> tuple[dict[int, dict], dict[int, list[int]]]:
    groups = {r["id"]: dict(zip(r.keys(), r)) for r in db.execute("SELECT * FROM groups").fetchall()}
    children: dict[int, list[int]] = {}
    for g in groups.values():
        if g.get("parent_id"):
            children.setdefault(g["parent_id"], []).append(g["id"])
    return groups, children


def ancestors(groups: dict[int, dict], gid: int) -> list[int]:
    out, cur, seen = [], groups.get(gid, {}).get("parent_id"), set()
    while cur and cur not in seen and cur in groups:
        out.append(cur)
        seen.add(cur)
        cur = groups[cur].get("parent_id")
    return out


def descendants(children: dict[int, list[int]], gid: int) -> list[int]:
    out, stack = [], list(children.get(gid, []))
    while stack:
        c = stack.pop()
        if c not in out:
            out.append(c)
            stack.extend(children.get(c, []))
    return out


def visible_group_ids(db: Database, group_id: int | None) -> list[int]:
    """Grupos cujo conteúdo um membro de `group_id` pode ver (inclui 'Todos').

    = o próprio grupo e seus subgrupos + os grupos pais (sem os irmãos)
      + cada grupo marcado em "Também vê" com seus subgrupos. Grupos inativos não contam.
    """
    groups, children = _tree(db)
    active = {gid for gid, g in groups.items() if g["active"]}
    result = {gid for gid, g in groups.items() if g["kind"] == "todos" and g["active"]}
    if group_id and group_id in active:
        result |= {group_id, *descendants(children, group_id), *ancestors(groups, group_id)}
        for r in db.execute("SELECT content_group_id FROM group_visibility WHERE viewer_group_id=?",
                            (group_id,)).fetchall():
            result |= {r[0], *descendants(children, r[0])}
    return sorted(g for g in result & active)


def full_name(groups: dict[int, dict], gid: int) -> str:
    names = [groups[a]["name"] for a in reversed(ancestors(groups, gid))] + [groups[gid]["name"]]
    return " › ".join(names)


def ordered_groups(db: Database, active_only: bool = True) -> list[dict]:
    """Grupos em ordem de árvore, com 'nivel' e 'caminho' para exibição."""
    groups, children = _tree(db)
    roots = sorted((g for g in groups.values() if not g.get("parent_id") or g["parent_id"] not in groups),
                   key=lambda g: (g["position"], g["name"]))
    out: list[dict] = []

    def walk(g, level):
        if active_only and not g["active"]:
            return
        out.append({**g, "nivel": level, "caminho": full_name(groups, g["id"])})
        for cid in sorted(children.get(g["id"], []), key=lambda c: (groups[c]["position"], groups[c]["name"])):
            walk(groups[cid], level + 1)

    for r in roots:
        walk(r, 0)
    return out


def would_cycle(db: Database, gid: int, new_parent: int | None) -> bool:
    if not new_parent:
        return False
    if new_parent == gid:
        return True
    groups, children = _tree(db)
    return new_parent in descendants(children, gid)


def recipient_groups(db: Database, recipients: list[str], exclude: set[str]) -> set[int]:
    emails = [e for e in recipients if e not in exclude]
    if not emails:
        return set()
    ph = ",".join("?" * len(emails))
    rows = db.execute(f"SELECT DISTINCT u.group_id FROM users u JOIN groups g ON g.id=u.group_id "
                      f"WHERE u.active=1 AND g.active=1 AND g.kind<>'todos' AND u.email IN ({ph})", emails).fetchall()
    return {r["group_id"] for r in rows}


@dataclass
class Audience:
    group_ids: list[int]
    reason: str
    broader: bool = False                 # público calculado mais amplo que o previsto pela regra
    unknown: list[str] = field(default_factory=list)  # destinatários não cadastrados (ignorados)


def viewers_of(db: Database, group_ids_: list[int]) -> set[int]:
    """Grupos cujos membros conseguem ver conteúdo publicado para `group_ids_`."""
    groups, _ = _tree(db)
    target = set(group_ids_)
    return {g for g in groups if groups[g]["kind"] != "todos" and target & set(visible_group_ids(db, g))}


def resolve_audience(db: Database, rule_group_id: int | None, audience_mode: str, recipients: list[str],
                     exclude: set[str]) -> Audience:
    """Público de um e-mail.

    - Regra com público fixo: usa o grupo da regra.
    - Senão: grupos das pessoas cadastradas em Para/CC, SEM agrupamento automático em grupo pai ou "Todos"
      (evita que grupos criados depois herdem o histórico). "Franqueados"/"Todos" só por regra fixa ou admin.
    - Se a regra tem público padrão e o calculado alcança alguém que o padrão não alcança -> broader=True
      (o relatório vai para a fila de revisão).
    """
    emails = [e for e in recipients if e not in exclude]
    known = {r["email"] for r in db.execute(
        "SELECT email FROM users WHERE active=1 AND email IN (%s)" % ",".join("?" * len(emails)), emails
    ).fetchall()} if emails else set()
    unknown = [e for e in emails if e not in known]
    if rule_group_id and audience_mode == "fixo":
        return Audience([rule_group_id], "público fixo da regra", unknown=unknown)
    found = recipient_groups(db, recipients, exclude)
    if found:
        broader = bool(rule_group_id) and not viewers_of(db, sorted(found)) <= viewers_of(db, [rule_group_id])
        reason = "grupos dos destinatários" + (" — mais amplo que o público previsto pela regra" if broader else "")
        return Audience(sorted(found), reason, broader, unknown)
    if rule_group_id:
        return Audience([rule_group_id], "público padrão da regra (nenhum destinatário cadastrado)", unknown=unknown)
    return Audience([], "sem público definido", unknown=unknown)


def set_report_groups(db: Database, report_id: int, ids: list[int]) -> None:
    db.execute("DELETE FROM report_groups WHERE report_id=?", (report_id,))
    for g in ids:
        db.execute("INSERT INTO report_groups(report_id, group_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                   (report_id, g))


def report_group_names(db: Database, report_id: int) -> list[str]:
    return [r["name"] for r in db.execute(
        "SELECT g.name FROM report_groups rg JOIN groups g ON g.id=rg.group_id WHERE rg.report_id=? "
        "ORDER BY g.position, g.name", (report_id,)).fetchall()]
