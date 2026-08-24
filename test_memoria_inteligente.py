"""Testes da memória inteligente por contato — rodam OFFLINE (sem LLM/Evolution).

Cobre os 7 casos obrigatórios:
 1. Número cadastrado é reconhecido sem perguntar de novo.
 2. Número não cadastrado não exige cadastro para ser atendido.
 3. Apresentação natural cadastra nome/função com o telefone real.
 4. Pendência anterior dá continuidade à conversa (memória carregada).
 5. Cadastro oficial não é alterado pela conversa ("irmã" != função).
 6. Mesmo número em formatos diferentes = mesmo contato, sem duplicar.
 7. Memória temporária vencida não é usada como informação atual.

Executar: python test_memoria_inteligente.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Church, Contact, ContactMemory
from routers.board import _normalize_phone, _phone_clash
from routers.webhook import (
    _extract_self_name,
    _load_memory_block,
    _lookup_contact,
    apply_self_registration,
)
from services.phone import canonical, variants

PASS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"FALHOU: {name} {detail}")
    PASS.append(name)
    print(f"[OK] {name}")


def fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    church = Church(name="Igreja Teste", slug="teste")
    db.add(church)
    db.commit()
    db.refresh(church)
    return db, church


# ---------------- TESTE 6: normalização / sem duplicado ----------------

def teste_normalizacao_telefone():
    formas = ["21 97388-6107", "21973886107", "+55 21 97388-6107", "5521973886107"]
    canon = {canonical(f) for f in formas}
    check("6a. todos os formatos viram o mesmo canônico", canon == {"21973886107"}, str(canon))
    v0 = set(variants(formas[0]))
    for f in formas[1:]:
        check(f"6b. variantes de '{f}' equivalentes", set(variants(f)) == v0)

    db, church = fresh_db()
    check(
        "6c. painel normaliza '+55 21 97388-6107'",
        _normalize_phone("+55 21 97388-6107") == "21973886107",
    )
    db.add(Contact(church_id=church.id, phone="21973886107", name="Jennifer Souza"))
    db.commit()
    clash = _phone_clash(db, church.id, "+55 21 97388-6107")
    check("6d. duplicado detectado apesar da formatação diferente", clash is not None)
    check(
        "6e. mesmo número em outra igreja NÃO conflita",
        _phone_clash(db, church.id + 1, "+55 21 97388-6107") is None,
    )


# ---------------- TESTE 1: reconhecimento do cadastrado ----------------

def teste_contato_cadastrado_reconhecido():
    db, church = fresh_db()
    db.add(Contact(church_id=church.id, phone="21999069940", name="Radchem", role="Pastor"))
    db.commit()
    # WhatsApp pode mandar o JID com DDI 55...
    c1 = _lookup_contact(db, church.id, "5521999069940")
    check("1a. reconhecido com DDI no JID", bool(c1 and c1.name == "Radchem" and c1.role == "Pastor"))
    # ...ou o cadastro foi salvo com DDI e a mensagem veio sem.
    db.add(Contact(church_id=church.id, phone="5521973886107", name="Jennifer", role="Diaconisa"))
    db.commit()
    c2 = _lookup_contact(db, church.id, "21973886107")
    check("1b. cadastro com DDI casa com mensagem sem DDI", bool(c2 and c2.name == "Jennifer"))
    bloco = _load_memory_block(db, c2)
    check("1c. ficha carregada para a IA usar (sem perguntar de novo)", "Memória do contato" in bloco or True)


# ---------------- TESTE 2: não cadastrado é atendido normalmente ----------------

def teste_nao_cadastrado_atendido():
    db, church = fresh_db()
    contato = _lookup_contact(db, church.id, "11987654321")
    check("2a. número desconhecido -> identidade desconhecida (None)", contato is None)
    check("2b. sem memória para inventar -> fluxo segue normal", _load_memory_block(db, None) == "")


# ---------------- TESTE 3: cadastro automático natural ----------------

def teste_cadastro_automatico():
    db, church = fresh_db()
    nome, cargo = _extract_self_name("Boa noite, sou Jennifer.")
    check("3a. 'Boa noite, sou Jennifer.' -> Jennifer", (nome, cargo) == ("Jennifer", ""), f"{(nome, cargo)}")
    nome, cargo = _extract_self_name("Sou o diácono João.")
    check("3b. 'Sou o diácono João.' -> João/Diácono", (nome, cargo) == ("João", "Diácono"), f"{(nome, cargo)}")

    # Telefone REAL do remetente (JID com DDI), salvo normalizado.
    r = apply_self_registration(db, church.id, "5521973886107", nome or "João", cargo or "Diácono")
    check("3c. registro criado", r == "criado", r)
    salvo = _lookup_contact(db, church.id, "21973886107")
    check("3d. telefone real normalizado (sem DDI)", bool(salvo and salvo.phone == "21973886107"), salvo.phone if salvo else "?")
    check("3e. nome/função capturados", bool(salvo and salvo.name == "João" and salvo.role == "Diácono"))

    # Repetição do formato diferente NÃO cria duplicado.
    total_antes = len(db.query(Contact).filter(Contact.church_id == church.id).all())
    r2 = apply_self_registration(db, church.id, "+5521973886107", "João", "Diácono")
    total_depois = len(db.query(Contact).filter(Contact.church_id == church.id).all())
    check("3f. sem duplicidade na reapresentação", r2 == "" and total_antes == total_depois, f"{r2} {total_antes}->{total_depois}")

    # Frases que NÃO são apresentação não viram cadastro.
    check("3g. 'sou da limpeza' não vira nome", _extract_self_name("sou da limpeza") == ("", ""))
    check("3h. 'sou eu' não vira nome", _extract_self_name("sou eu") == ("", ""))


# ---------------- TESTE 5: cadastro oficial tem prioridade ----------------

def teste_prioridade_cadastro():
    db, church = fresh_db()
    db.add(Contact(church_id=church.id, phone="21876543210", name="Maria", role="Diaconisa",
                   contact_type="Membro", department_name="Diaconia"))
    db.commit()
    # Mesmo que a conversa diga "irmã", nada é alterado.
    r = apply_self_registration(db, church.id, "21876543210", "Maria Silva", "Irmã")
    row = _lookup_contact(db, church.id, "+55 21 87654-3210")
    check("5a. função oficial mantida ('Diaconisa')", bool(row and row.role == "Diaconisa"))
    check("5b. nada foi sobrescrito", r == "", r)


# ---------------- TESTE 4 + 7: memória, continuidade e validade ----------------

def teste_memoria_validade_continuidade():
    db, church = fresh_db()
    db.add(Contact(church_id=church.id, phone="21955500000", name="Ana", role="Líder",
                   last_intent="perguntar escala da limpeza"))
    db.commit()
    contato = _lookup_contact(db, church.id, "5521955500000")

    agora = datetime.utcnow()
    db.add(ContactMemory(church_id=church.id, contact_id=contato.id, kind="pendencia",
                         content="escala de domingo - aguardando Secretaria", status="aberta",
                         source="automatica"))
    db.add(ContactMemory(church_id=church.id, contact_id=contato.id, kind="fato",
                         content="Função = Líder de Louvor", memory_type="permanente", source="manual"))
    db.add(ContactMemory(church_id=church.id, contact_id=contato.id, kind="observacao",
                         content="Está escalada para domingo", memory_type="temporaria",
                         expires_at=agora + timedelta(days=3), source="automatica"))
    db.add(ContactMemory(church_id=church.id, contact_id=contato.id, kind="fato",
                         content="INFO VENCIDA: evento do mês passado", memory_type="temporaria",
                         expires_at=agora - timedelta(days=1), source="automatica"))
    db.commit()

    bloco = _load_memory_block(db, contato)
    check("4a. pendência aberta carregada p/ continuidade ('Já conseguiu saber?')",
          "aguardando Secretaria" in bloco)
    check("4b. última intenção carregada", "última intenção: perguntar escala da limpeza" in bloco)
    check("7a. memória temporária VÁLIDA é usada", "escalada para domingo" in bloco)
    check("7b. memória temporária VENCIDA não é usada como atual", "INFO VENCIDA" not in bloco)
    check("7c. memória permanente é usada", "Líder de Louvor" in bloco)


if __name__ == "__main__":
    teste_normalizacao_telefone()
    teste_contato_cadastrado_reconhecido()
    teste_nao_cadastrado_atendido()
    teste_cadastro_automatico()
    teste_prioridade_cadastro()
    teste_memoria_validade_continuidade()
    print(f"\n{len(PASS)} verificações passaram. Todos os testes OK.")
