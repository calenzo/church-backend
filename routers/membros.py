"""Aba Membros: aniversariantes, destinatários dos lembretes, horário e histórico.

Funcionalidade isolada — não altera contatos, IA, departamentos nem WhatsApp.
O disparo automático vive em services/birthday_scheduler.py; aqui ficam apenas
os endpoints de gerenciamento usados pela aba.

IMPORTANTE: rotas fixas (config, hoje, proximos, destinatarios, lembretes)
devem ser registradas ANTES das parametrizadas (/{member_id}) para evitar
que o FastAPI tente casar strings como "config" no parâmetro int.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import (
    BirthdayConfig,
    BirthdayLog,
    BirthdayRecipient,
    Church,
    Member,
    User,
)
from schemas import (
    BirthdayConfigIn,
    BirthdayConfigOut,
    MemberCreate,
    MemberOut,
    MemberUpdate,
    RecipientCreate,
    RecipientOut,
    RecipientUpdate,
    ReminderLogOut,
    UpcomingBirthday,
)
from routers.board import _resolve_church
from services.evolution import EvolutionError, send_text
from services.phone import canonical as canonical_phone

router = APIRouter(prefix="/api/membros", tags=["membros"])

_TZ = ZoneInfo("America/Sao_Paulo")
CHURCH_LABEL = "Recâmaras do Rei"


# ------------------------------- helpers -------------------------------


def _fmt_birthday(day: int, month: int) -> str:
    return f"{day:02d}/{month:02d}"


def _fmt_phone(digits: str) -> str:
    d = digits or ""
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return digits


def _member_out(m: Member, today: date | None = None) -> MemberOut:
    today = today or datetime.now(_TZ).date()
    return MemberOut(
        id=m.id,
        name=m.name,
        birth_day=m.birth_day,
        birth_month=m.birth_month,
        birthday=_fmt_birthday(m.birth_day, m.birth_month),
        is_today=(m.birth_day == today.day and m.birth_month == today.month),
    )


def _get_member(db: Session, church_id: int, member_id: int) -> Member:
    member = db.get(Member, member_id)
    if not member or member.church_id != church_id:
        raise HTTPException(status_code=404, detail="Membro não encontrado")
    return member


def _reject_duplicate(db: Session, church_id: int, name: str, day: int, month: int, exclude_id: int | None = None):
    query = db.query(Member).filter(
        Member.church_id == church_id,
        Member.name.ilike(name.strip()),
        Member.birth_day == day,
        Member.birth_month == month,
    )
    if exclude_id:
        query = query.filter(Member.id != exclude_id)
    if query.first():
        raise HTTPException(status_code=400, detail="Este membro já está cadastrado.")


def _recipient_or_404(db: Session, church_id: int, recipient_id: int) -> BirthdayRecipient:
    row = db.get(BirthdayRecipient, recipient_id)
    if not row or row.church_id != church_id:
        raise HTTPException(status_code=404, detail="Destinatário não encontrado")
    return row


def _get_or_create_config(db: Session, church_id: int) -> BirthdayConfig:
    config = db.query(BirthdayConfig).filter(BirthdayConfig.church_id == church_id).first()
    if not config:
        config = BirthdayConfig(church_id=church_id, send_time="08:00")
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


# =====================================================================
#  ROTAS FIXAS — registradas ANTES das parametrizadas para evitar
#  que PUT /config ou GET /destinatarios sejam casados com /{member_id}.
# =====================================================================


# ------------------------------- Aniversários -------------------------------


@router.get("/hoje", response_model=list[UpcomingBirthday])
def todays_birthdays(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Aniversariantes de hoje (ferramenta administrativa; o disparo é automático)."""
    church = _resolve_church(db, user, church_id)
    today = datetime.now(_TZ).date()
    rows = (
        db.query(Member)
        .filter(Member.church_id == church.id, Member.birth_day == today.day, Member.birth_month == today.month)
        .order_by(Member.name)
        .all()
    )
    return [
        UpcomingBirthday(name=m.name, birthday=_fmt_birthday(m.birth_day, m.birth_month), days_until=0, is_today=True)
        for m in rows
    ]


@router.get("/proximos", response_model=list[UpcomingBirthday])
def upcoming_birthdays(
    limit: int = Query(default=8, ge=1, le=30),
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Próximos aniversariantes a partir de hoje (lista visual da aba)."""
    church = _resolve_church(db, user, church_id)
    members = db.query(Member).filter(Member.church_id == church.id).all()
    today = datetime.now(_TZ).date()

    def sort_key(m: Member):
        delta = (date(today.year, m.birth_month, m.birth_day) - today).days
        if delta < 0:
            try:
                delta = (date(today.year + 1, m.birth_month, m.birth_day) - today).days
            except ValueError:  # 29/02 em ano não bissexto
                delta = (date(today.year + 1, 3, 1) - today).days
        return delta

    rows = sorted(members, key=sort_key)[:limit]
    return [
        UpcomingBirthday(
            name=m.name,
            birthday=_fmt_birthday(m.birth_day, m.birth_month),
            days_until=sort_key(m),
            is_today=(sort_key(m) == 0),
        )
        for m in rows
    ]


# ------------------------------- Destinatários -------------------------------


@router.get("/destinatarios", response_model=list[RecipientOut])
def list_recipients(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return (
        db.query(BirthdayRecipient)
        .filter(BirthdayRecipient.church_id == church.id)
        .order_by(BirthdayRecipient.name)
        .all()
    )


@router.post("/destinatarios", response_model=RecipientOut, status_code=201)
def create_recipient(
    data: RecipientCreate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    phone = canonical_phone(data.phone) or "".join(ch for ch in data.phone if ch.isdigit())
    if not phone or len(phone) < 10:
        raise HTTPException(status_code=400, detail="Telefone inválido (use DDD + número)")
    exists = (
        db.query(BirthdayRecipient)
        .filter(BirthdayRecipient.church_id == church.id, BirthdayRecipient.phone == phone[:20])
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="Este telefone já está cadastrado.")
    row = BirthdayRecipient(church_id=church.id, name=(data.name or "").strip(), phone=phone[:20], active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/destinatarios/{recipient_id}", response_model=RecipientOut)
def update_recipient(
    recipient_id: int,
    data: RecipientUpdate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    row = _recipient_or_404(db, church.id, recipient_id)
    if data.name is not None:
        row.name = data.name.strip()
    if data.phone is not None:
        phone = canonical_phone(data.phone) or "".join(ch for ch in data.phone if ch.isdigit())
        if not phone or len(phone) < 10:
            raise HTTPException(status_code=400, detail="Telefone inválido (use DDD + número)")
        clash = (
            db.query(BirthdayRecipient)
            .filter(
                BirthdayRecipient.church_id == church.id,
                BirthdayRecipient.phone == phone[:20],
                BirthdayRecipient.id != row.id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=400, detail="Este telefone já está cadastrado.")
        row.phone = phone[:20]
    if data.active is not None:
        row.active = data.active
    db.commit()
    db.refresh(row)
    return row


@router.delete("/destinatarios/{recipient_id}", status_code=204)
def delete_recipient(
    recipient_id: int,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    row = _recipient_or_404(db, church.id, recipient_id)
    db.delete(row)
    db.commit()


# ------------------------------- Configuração -------------------------------


@router.get("/config", response_model=BirthdayConfigOut)
def get_config(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return _get_or_create_config(db, church.id)


@router.put("/config", response_model=BirthdayConfigOut)
def update_config(
    data: BirthdayConfigIn,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    config = _get_or_create_config(db, church.id)
    config.send_time = data.send_time
    config.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(config)
    return config


# ------------------------------- Histórico / testes -------------------------------


@router.get("/lembretes", response_model=list[ReminderLogOut])
def list_logs(
    limit: int = Query(default=50, ge=1, le=200),
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Histórico de envios (reais e testes), mais recentes primeiro."""
    church = _resolve_church(db, user, church_id)
    return (
        db.query(BirthdayLog)
        .filter(BirthdayLog.church_id == church.id)
        .order_by(BirthdayLog.ref_date.desc(), BirthdayLog.id.desc())
        .limit(limit)
        .all()
    )


@router.post("/lembretes/teste", response_model=dict)
async def send_test(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Botão TESTAR LEMBRETE: confere os telefones sem registrar aniversário real."""
    church = _resolve_church(db, user, church_id)
    recipients = (
        db.query(BirthdayRecipient)
        .filter(BirthdayRecipient.church_id == church.id, BirthdayRecipient.active == True)  # noqa: E712
        .all()
    )
    if not recipients:
        raise HTTPException(status_code=400, detail="Cadastre ao menos um destinatário primeiro")
    from services.birthday_scheduler import resolve_instance, send_target_digits

    instance = await resolve_instance(church.id)
    if not instance:
        raise HTTPException(status_code=400, detail="Nenhum número do WhatsApp ativo para esta igreja")
    message = f"Teste de lembrete de aniversário — {CHURCH_LABEL}."
    results = []
    for r in recipients:
        destino = send_target_digits(r.phone)
        try:
            resp = await send_text(destino, message, instance=instance)
            # Verifica status na resposta da Evolution API
            resp_status = (resp or {}).get("status") if isinstance(resp, dict) else None
            ok = resp_status in (None, "SENT", "DELIVERY_ACK", "DISPLAYED", "READ", True)
            results.append({
                "name": r.name,
                "phone": r.phone,
                "destino": destino,
                "ok": ok,
                "evolution_status": resp_status,
                "raw": resp if not ok else None,
            })
        except EvolutionError as exc:
            results.append({"name": r.name, "phone": r.phone, "destino": destino, "ok": False, "error": str(exc)})
    failed = [r for r in results if not r["ok"]]
    return {"sent": len(results) - len(failed), "failed": len(failed), "results": results}


# =====================================================================
#  ROTAS PARAMETRIZADAS — registradas POR ÚLTIMO para não capturar
#  "config", "hoje", "proximos", "destinatarios", "lembretes" etc.
# =====================================================================


@router.get("", response_model=list[MemberOut])
def list_members(
    search: str | None = Query(default=None, max_length=120),
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista alfabética; busca opcional por parte do nome."""
    church = _resolve_church(db, user, church_id)
    query = db.query(Member).filter(Member.church_id == church.id)
    if search and search.strip():
        query = query.filter(Member.name.ilike(f"%{search.strip()}%"))
    rows = query.order_by(Member.name).all()
    today = datetime.now(_TZ).date()
    return [_member_out(m, today) for m in rows]


@router.post("", response_model=MemberOut, status_code=201)
def create_member(
    data: MemberCreate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Informe o nome completo")
    _reject_duplicate(db, church.id, name, data.birth_day, data.birth_month)
    member = Member(church_id=church.id, name=name, birth_day=data.birth_day, birth_month=data.birth_month)
    db.add(member)
    db.commit()
    db.refresh(member)
    return _member_out(member)


@router.put("/{member_id}", response_model=MemberOut)
def update_member(
    member_id: int,
    data: MemberUpdate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    member = _get_member(db, church.id, member_id)
    name = (data.name if data.name is not None else member.name).strip()
    day = data.birth_day if data.birth_day is not None else member.birth_day
    month = data.birth_month if data.birth_month is not None else member.birth_month
    if not name:
        raise HTTPException(status_code=400, detail="Nome não pode ficar vazio")
    try:
        MemberCreate(name=name, birth_day=day, birth_month=month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _reject_duplicate(db, church.id, name, day, month, exclude_id=member.id)
    member.name, member.birth_day, member.birth_month = name, day, month
    db.commit()
    db.refresh(member)
    return _member_out(member)


@router.delete("/{member_id}", status_code=204)
def delete_member(
    member_id: int,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    member = _get_member(db, church.id, member_id)
    db.delete(member)
    db.commit()
