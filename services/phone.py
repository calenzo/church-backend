"""Normalização de telefones compartilhada por painel e webhook.

Garante que '21 97388-6107', '21973886107' e '+55 21 97388-6107'
sejam reconhecidos como o MESMO contato (sem duplicar cadastro).
"""

import re


def only_digits(raw: str) -> str:
    """Mantém apenas os dígitos do número informado."""
    return re.sub(r"\D", "", raw or "")


def canonical(raw: str) -> str:
    """Formato canônico de armazenamento: apenas dígitos, SEM o DDI 55
    quando é um número brasileiro completo (DDD + número = 10 ou 11 dígitos)."""
    d = only_digits(raw)
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    return d


def variants(raw: str) -> list[str]:
    """Formas equivalentes do mesmo número (com e sem DDI 55), sem repetição.
    Usada em buscas e verificação de duplicidade: qualquer formato digitado
    casa com o cadastro existente."""
    d = only_digits(raw)
    if not d:
        return []
    base = canonical(d)
    candidates = [base]
    if base.startswith("55"):
        candidates.append(base[2:])
    elif len(base) in (10, 11):
        candidates.append("55" + base)
    seen: set[str] = set()
    result: list[str] = []
    for v in candidates:
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result
