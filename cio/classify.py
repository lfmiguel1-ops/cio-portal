"""Classificação determinística por regras configuráveis (sem IA).

Política: na ausência de regra que case, o relatório vai para QUARENTENA com
confidencialidade 'restrito'. Nunca se infere liberação pela ausência de marcador.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .db import Database


@dataclass
class Classification:
    area_id: int | None
    priority: str
    confidentiality: str
    status: str          # 'publicado' | 'quarentena'
    rule_id: int | None
    reason: str
    group_id: int | None = None          # público padrão da regra
    audience_mode: str = "destinatarios"  # ou 'fixo'


def _match(pattern: str, value: str) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, value or "", flags=re.I) is not None
    except re.error:
        return False  # regra inválida nunca casa


def classify(conn: Database, sender: str, subject: str, folder_id: str = "") -> Classification:
    rules = conn.execute(
        "SELECT * FROM classification_rules WHERE active=1 ORDER BY position, id"
    ).fetchall()
    for r in rules:
        if r["folder_id"] and r["folder_id"] != folder_id:
            continue
        if _match(r["sender_pattern"], sender) and _match(r["subject_regex"], subject):
            status = "publicado" if r["action"] == "publicar" and r["area_id"] else "quarentena"
            reason = f"regra #{r['id']} {r['name']}"
            if status == "quarentena":
                reason += " (ação: quarentena)" if r["action"] == "quarentena" else " (sem área definida)"
            return Classification(r["area_id"], r["priority"], r["confidentiality"], status, r["id"], reason,
                                  r["group_id"], r["audience_mode"] or "destinatarios")
    return Classification(None, "normal", "restrito", "quarentena", None, "nenhuma regra correspondente")


def validate_regex(pattern: str) -> str | None:
    """Retorna mensagem de erro ou None. Limita tamanho para evitar regex patológica."""
    if len(pattern) > 300:
        return "Expressão muito longa (máx. 300 caracteres)."
    try:
        re.compile(pattern)
    except re.error as e:
        return f"Expressão inválida: {e}"
    return None
