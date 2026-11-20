"""Autenticidade do remetente (A1): confere o resultado DMARC gravado pelo servidor do Zoho.

O Zoho registra no topo do cabeçalho `ARC-Authentication-Results: i=1; mx.zohomail.com; ... dmarc=pass
header.from=<remetente>` (verificado em relatórios reais em 14/11/2026). Só vale o PRIMEIRO cabeçalho de
autenticação emitido por mx.zohomail.com (o mais recente, inserido pelo próprio Zoho): cabeçalhos mais abaixo
podem ter sido forjados por quem enviou a mensagem.
"""
from __future__ import annotations

import re

TRUSTED_AUTHSERV = "mx.zohomail.com"
_HEADER = re.compile(r"^(arc-authentication-results|authentication-results)\s*:(.*)$", re.I)


def _unfold(raw: str) -> list[str]:
    lines: list[str] = []
    for line in (raw or "").replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += " " + line.strip()
        elif line.strip() == "":
            break  # fim do cabeçalho
        else:
            lines.append(line.rstrip())
    return lines


def check_dmarc(raw_header: str, sender: str) -> tuple[bool, str]:
    for line in _unfold(raw_header):
        m = _HEADER.match(line)
        if not m or TRUSTED_AUTHSERV not in m.group(2).lower():
            continue
        value = m.group(2).lower()
        dm = re.search(r"dmarc=(\w+)", value)
        hf = re.search(r"header\.from=<?([^\s;>]+)>?", value)
        if not dm:
            return False, "resultado DMARC ausente"
        if dm.group(1) != "pass":
            return False, f"DMARC={dm.group(1)}"
        sender_domain = sender.lower().rsplit("@", 1)[-1]
        from_domain = (hf.group(1) if hf else "").rsplit("@", 1)[-1]
        if from_domain and from_domain != sender_domain:
            return False, "domínio autenticado diferente do remetente"
        return True, "DMARC=pass"
    return False, "sem resultado de autenticação do Zoho"
