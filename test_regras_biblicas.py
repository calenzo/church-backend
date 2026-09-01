"""Testes obrigatórios do módulo bíblico e teológico.

Duas camadas:
 1. OFFLINE (sempre): valida que o DEFAULT_SYSTEM_PROMPT contém todas as
    regras exigidas do módulo bíblico (anti-alucinação, níveis de certeza,
    correção de erro do usuário, tradição ≠ texto, etc.).
 2. LIVE (opcional): envia os 25 casos obrigatórios para a LLM configurada
    (services.llm) e confere se a resposta contém o marcador esperado.

Executar:
    python test_regras_biblicas.py          # só offline
    python test_regras_biblicas.py --live   # offline + LLM real (requer LLM no ar)
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from services import llm  # noqa: E402

PASS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"FALHOU: {name} {detail}")
    PASS.append(name)
    print(f"[OK] {name}")


def _norm(t):
    return t.lower().strip()


def _has(prompt, *fragments):
    p = _norm(prompt)
    return all(f.lower() in p for f in fragments)


# ---------------- 1) OFFLINE: compliance do prompt padrão ----------------

def teste_prompt_contem_nucleo():
    p = llm.DEFAULT_SYSTEM_PROMPT
    check("a. módulo bíblico presente", "MÓDULO BÍBLICO E TEOLÓGICO" in p.upper())
    check("b. regra absoluta anti-invenção", "NUNCA INVENTAR UMA RESPOSTA BÍBLICA" in p.upper())
    check("c. prioridade FIDELIDADE>PRECISÃO", _has(p, "fidelidade bíblica > precisão"))
    check("d. 4 níveis de certeza", "QUATRO NÍVEIS DE CERTEZA" in p.upper())
    check("e. nível 'A Bíblia não informa'", _has(p, "A Bíblia não informa"))
    check("f. confiança interna HIGH/MEDIUM/LOW", _has(p, "HIGH", "MEDIUM", "LOW"))
    check("g. pergunta ambígua pede contexto", _has(p, "Pergunta ambígua", "peça contexto") or _has(p, "pedir contexto"))
    check("h. anti-concordância/correção de erro", "ANTI-CONCORDÂNCIA" in p.upper())
    check("i. tradição não é texto bíblico", _has(p, "tradição não é texto bíblico"))
    check("j. proibido alucinar: listada", "PROIBIDO ALUCINAR" in p.upper())
    check("k. hierarquia da resposta", _has(p, "hierarquia da resposta"))
    check("l. escolas teológicas com posições", _has(p, "escolas teológicas", "calvinismo", "arminianismo"))
    check("m. segunda camada de validação", _has(p, "segunda camada de validação"))
    check("n. línguas originais sem falácia etimológica", _has(p, "falácia etimológica"))
    check("o. crítica textual sem alarmismo", _has(p, "crítica textual", "não chame automaticamente toda variante de erro"))
    check("p. princípio final (não invente)", _has(p, "se houver dúvida, NÃO INVENTE"))


def teste_prompt_nao_perde_conteudos_antigos():
    p = llm.DEFAULT_SYSTEM_PROMPT
    check("q. pregação em formato de leitura contínua", _has(p, "leitura contínua"))
    check("r. texto vs aplicação preservado", _has(p, "texto vs aplicação") or _has(p, "não atribua ao texto declarações que ele não contém"))
    check("s. não forçar promessas", _has(p, "não forçar promessas") or _has(p, "em fórmula automática"))
    check("t. camadas de assunto (seção 12) integrada", "BÍBLIA INTELIGENTE POR ASSUNTO" in p.upper())


# ---------------- 2) LIVE: 25 casos obrigatórios via LLM ----------------

CASOS = [
    ("Onde está escrito que o sol parou?", ["Josué 10"]),
    ("Quem pediu para o sol parar?", ["Josué"]),
    ("Qual foi o primeiro assassinato registrado na Bíblia?", ["Abel", "Gênesis 4"]),
    ("Quem matou Abel?", ["Caim"]),
    ("Quem matou Golias?", ["Davi", "1 Samuel 17"]),
    ("Sansão matou Golias?", ["não", "Davi"]),
    ("Qual era o nome do servo de Davi?", ["episódio", "detalhe", "qual episódio", "precisão"], "ambígua → pedir contexto"),
    ("Qual Ana era viúva e profetisa?", ["Lucas 2", "Fanuel"]),
    ("Ana mãe de Samuel era a mesma Ana de Lucas?", ["não"]),
    ("Moisés fez o sol parar?", ["não", "Josué"]),
    ('Onde está escrito "Jesus chorou"?', ["João 11"]),
    ("Quem foi o primeiro suicídio da Bíblia?", ["1 Samuel 31", "Abimeleque", "Juízes 9"], "nuance obrigatória"),
    ("Qual era o nome da esposa de Caim?", ["não informa", "não informad", "não revela"]),
    ("Quantos magos visitaram Jesus?", ["não informa", "não informad", "não revela", "três", "ouro, incenso e mirra"]),
    ("Qual era o fruto proibido?", ["não informa", "não informad", "maçã", "espécie"]),
    ("Qual era o nome da mulher samaritana?", ["não informa", "não informad", "não revela"]),
    ("Qual era o nome do ladrão arrependido na cruz?", ["não informa", "não informad", "não revela"]),
    ("Paulo caiu de um cavalo?", ["não informa", "montado em um cavalo", "cavalo"]),
    ("Maria Madalena era prostituta?", ["não afirma", "não informa", "não diz"]),
    ("Eram três reis magos?", ["não diz", "não informa", "três", "reis"]),
    ("Quantos animais de cada espécie Noé levou?", ["limpo", "limpos", "Gênesis 7"]),
    ("Quem escreveu Hebreus?", ["não é identificado", "autor não", "não se sabe"]),
    ("Quem escreveu o livro de Jó?", ["não identifica", "autor não", "não informa"]),
    ("Quem escreveu os cinco primeiros livros?", ["Moisés", "tradicional", "atribuição"]),
    ("Existe a palavra Trindade na Bíblia?", ["não aparece", "não existe", "Pai", "Espírito"]),
]


def _load_config():
    db = sqlite3.connect(str(Path(__file__).parent / "church.db"))
    row = db.execute(
        "SELECT base_url, model, api_key, temperature, system_prompt "
        "FROM llm_config WHERE church_id=1 ORDER BY id LIMIT 1"
    ).fetchone()
    db.close()
    if row is None:
        raise AssertionError("sem llm_config no church.db — rode a aplicação uma vez")
    base_url, model, api_key, temperature, system_prompt = row
    return SimpleNamespace(
        base_url=base_url,
        model=model,
        api_key=api_key or "",
        temperature=temperature or 0.3,
        system_prompt=system_prompt or "",
    )


def teste_25_casos_live():
    config = _load_config()
    depts = [{"name": "geral", "description": "assuntos gerais"}]
    for i, (pergunta, esperado, *opcional) in enumerate(CASOS, 1):
        nota = opcional[0] if opcional else ""
        minimo = set(f.lower() for f in esperado)
        try:
            resposta = llm.classify_and_reply(
                pergunta, depts, config, memory_text="", directory_text=""
            )["reply"].lower()
        except Exception as exc:
            raise AssertionError(f"FALHOU caso {i} ({pergunta}): erro ao chamar a LLM — {exc}")
        ausentes = [e for e in esperado if e.lower() not in resposta]
        check(
            f"caso {i}. {pergunta} {nota}",
            not ausentes,
            f"esperado: {minimo} | obtido: {resposta}",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="executa os 25 casos contra a LLM real")
    args = parser.parse_args()

    teste_prompt_contem_nucleo()
    teste_prompt_nao_perde_conteudos_antigos()
    if args.live:
        teste_25_casos_live()
    else:
        print("[i] --live não informado: pulando os 25 casos contra a LLM (requer LLM no ar).")

    print(f"\n{len(PASS)} verificações passaram. Todos os testes OK.")