"""Carga inicial da aba Membros: aniversariantes e destinatários padrão.

Executa UMA ÚNICA VEZ por igreja, guardada pela flag BirthdaySeedFlag.
Reinícios do backend NUNCA duplicam os registros; depois da carga, tudo é
gerenciado pela própria aba (sem mexer em código).
"""

import logging
from datetime import datetime

from models import BirthdayConfig, BirthdayRecipient, BirthdaySeedFlag, Church, Member
from services.phone import canonical as canonical_phone
from database import SessionLocal

logger = logging.getLogger(__name__)

# (nome completo, dia, mês) — apenas DIA + MÊS; ano de nascimento não é necessário.
INITIAL_MEMBERS = [
    ("Ana Clara Queiroz Azevedo", 6, 9),
    ("Anderson Batista Silva", 14, 7),
    ("Andrea Fortes do Nascimento", 7, 2),
    ("Andressa Ferreira Calenzo", 28, 2),
    ("Antonia Conceição de Almeida", 4, 2),
    ("Antonia de Maria do Nascimento", 27, 6),
    ("Ária Bernardo Dias Pereira", 17, 6),
    ("Bryan Gabriel Dias Souza dos Santos", 17, 10),
    ("Carla de Mello Araújo", 22, 10),
    ("Carla Dias dos Santos", 9, 11),
    ("Carlos Henrique da Silva Calenzo", 5, 10),
    ("Claudia Maria da Silva Cordeiro", 17, 4),
    ("Dalva Silva do Nascimento", 25, 5),
    ("Daniel Dias Albuquerque de Brito", 11, 12),
    ("Daniele dos Santos Limeira", 17, 3),
    ("Davi dos Santos Alves", 21, 12),
    ("David Celes dos Santos", 9, 9),
    ("David Dos Santos Amaral", 12, 8),
    ("Edmar Clemente da Silva", 22, 11),
    ("Elenita de Azevedo Pereira", 2, 5),
    ("Elisete Pereira Lima Barbosa", 20, 6),
    ("Emanuelle Batista Ignácio", 28, 3),
    ("Enzo Gabriel de Jesus Bispo", 20, 3),
    ("Felipe Ferreira Tarcitano", 18, 11),
    ("Flavia Coutinho Souza", 18, 9),
    ("Francisco José dos Santos Filho", 6, 3),
    ("Gabriel do Nascimento Pompilho", 23, 6),
    ("Geizilene do Bom Parto dos Santos Silva", 2, 6),
    ("Genifer Vitória Sousa Santos", 14, 12),
    ("Gilberlania Mairla da Silva", 31, 5),
    ("Glauce Evangelina Celes Reis", 15, 7),
    ("Heitor dos Santos Ferreira", 23, 3),
    ("Heloiza dos Santos Ferreira", 18, 6),
    ("Hilda Herys Alves", 21, 11),
    ("Isaías Oliveira Carvalho", 17, 10),
    ("Isadora Bemardo Dias Pereira", 18, 10),
    ("Jeniffer de Souza Toledo da Silva", 17, 11),
    ("Jose dos Santos", 31, 5),
    ("José Francisco Gomes da Silva", 16, 4),
    ("José Roberto de Araújo Severo", 20, 11),
    ("Joseane Santos Menezes Severo", 20, 6),
    ("Juan Felipe Fonte Ferreira", 13, 4),
    ("Laura Celes dos Santos", 29, 4),
    ("Leandra dos Santos Silva", 28, 8),
    ("Levi dos Santos Severo", 8, 12),
    ("Lohan da Silva Lopes França", 17, 5),
    ("Luana Amorim Abreu", 31, 3),
    ("Luana Dias Souza dos Santos", 30, 5),
    ("Lucas de Almeida Araújo", 3, 5),
    ("Lucas Paula Francisco Alves", 5, 8),
    ("Luiz Henrique de Almeida Araújo", 2, 7),
    ("Maria Cecilia Miranda Tomás de Brito", 10, 5),
    ("Maria Dara Paiva de Sousa", 21, 5),
    ("Maria Eduarda Queiroz Azevedo", 19, 11),
    ("Maria Eduarda Romão de Souza", 5, 8),
    ("Maria Elisa da Cruz dos Santos", 4, 6),
    ("Maria Madalena de Araújo Lacerda", 7, 8),
    ("Maria Mirlene de Paiva", 6, 11),
    ("Maria Nádia Romão de Souza Araújo", 7, 2),
    ("Nelson Gabriel Rodriguês da Silva", 8, 7),
    ("Niedson Rodriguês da Silva", 26, 9),
    ("Paula Batista Cardoso Ignácio", 6, 12),
    ("Paulo Henrique Batista Ignácio", 19, 7),
    ("Paulo Roberto Ignácio", 2, 10),
    ("Pérola Gomes de Souza", 13, 10),
    ("Rafael Ramon Rodrigues Antunes do Nascimento", 23, 4),
    ("Renan Pereira Bemardo de Oliveira", 5, 4),
    ("Ronaldo Ancelmo Alves", 20, 4),
    ("Sophia Celes dos Santos", 6, 8),
    ("Sophia de Mello da Silva", 7, 8),
    ("Suelene de Azevedo Coutinho", 13, 2),
    ("Thauane Raymundo de Souza", 25, 8),
    ("Thaynara de almeida araújo", 2, 4),
    ("Tiago de Almeida de Araújo", 28, 2),
    ("Vanderléia Rodriguês de Oliveira", 18, 4),
    ("Veronica Batista de Lima", 23, 5),
    ("Wallace do Espírito Santo da Silva", 11, 7),
    ("Wanderley Sá Barbosa", 28, 11),
    ("Yasmin Dias Pereira", 9, 9),
]

# Destinatários iniciais dos lembretes (editáveis/removíveis pela aba).
INITIAL_RECIPIENTS = [
    ("Pastor Radchem", "21999069940"),
    ("Missionária Carla Dias — Secretaria", "21969117333"),
]


def _normalize(phone: str) -> str:
    return canonical_phone(phone) or "".join(ch for ch in phone if ch.isdigit())


def seed_birthday_data() -> None:
    """Insere a lista inicial apenas na PRIMEIRA execução de cada igreja."""
    with SessionLocal() as db:
        churches = db.query(Church).all()
        for church in churches:
            already = db.get(BirthdaySeedFlag, church.id)
            if already:
                continue
            existing_names = {
                (m.name or "").strip().lower() for m in db.query(Member).filter(Member.church_id == church.id).all()
            }
            added = 0
            for name, day, month in INITIAL_MEMBERS:
                if name.strip().lower() in existing_names:
                    continue
                db.add(Member(church_id=church.id, name=name.strip(), birth_day=day, birth_month=month))
                added += 1
            existing_phones = {
                r.phone
                for r in db.query(BirthdayRecipient)
                .filter(BirthdayRecipient.church_id == church.id)
                .all()
            }
            recipients_added = 0
            for name, phone in INITIAL_RECIPIENTS:
                normalized = _normalize(phone)
                if not normalized or normalized in existing_phones:
                    continue
                db.add(
                    BirthdayRecipient(
                        church_id=church.id, name=name, phone=normalized[:20], active=True
                    )
                )
                recipients_added += 1
            if not db.query(BirthdayConfig).filter(BirthdayConfig.church_id == church.id).first():
                db.add(BirthdayConfig(church_id=church.id, send_time="08:00"))
            db.add(BirthdaySeedFlag(church_id=church.id, seeded_at=datetime.utcnow()))
            db.commit()
            logger.info(
                "Carga inicial de membros da igreja %s: %d membros, %d destinatários",
                church.name,
                added,
                recipients_added,
            )
