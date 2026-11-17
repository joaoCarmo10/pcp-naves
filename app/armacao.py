"""Controle de armação: lista do dia seguinte divulgada ao armador e marcação de peças armadas."""
import hashlib
import hmac
import os
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Armacao, Divulgacao, ItemProg, Peca, RevisaoProg
from .status import chave_natural


def token_armador() -> str:
    """Link fixo do armador (só leitura), derivado da chave do app: muda se a SECRET_KEY mudar."""
    chave = (os.getenv("SECRET_KEY") or os.getenv("APP_PASSWORD") or "pcp").encode()
    return hmac.new(chave, b"armador", hashlib.sha256).hexdigest()[:20]


def proximo_dia_util(d: date) -> date:
    d += timedelta(days=1)
    return d + timedelta(days=1) if d.weekday() == 6 else d


def itens_do_dia(db: Session, rev: RevisaoProg, d: date) -> list[ItemProg]:
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.data == d)))
    return sorted(itens, key=lambda i: (i.nave, i.forma, i.obra, chave_natural(i.peca)))


def situacao(db: Session, itens: list) -> dict:
    """Para cada item (ou dict da foto divulgada): armado?, origem e quando. Fonte: app, Plannix (Hora da Armação)
    ou peça já fabricada."""
    cods = {(i["codigo"] if isinstance(i, dict) else i.codigo) for i in itens} - {None, ""}
    ids = {(i["id"] if isinstance(i, dict) else i.id) for i in itens}
    app_cod = {a.codigo: a for a in db.scalars(select(Armacao).where(Armacao.codigo.in_(cods)))} if cods else {}
    app_item = {a.item_id: a for a in db.scalars(select(Armacao).where(Armacao.item_id.in_(ids)))} if ids else {}
    pecas = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_(cods)))} if cods else {}
    out = {}
    for i in itens:
        iid, cod = (i["id"], i["codigo"]) if isinstance(i, dict) else (i.id, i.codigo)
        a = app_cod.get(cod) if cod else app_item.get(iid)
        p = pecas.get(cod)
        if a:
            out[iid] = {"armado": True, "origem": "App", "quando": a.armado_em}
        elif p and p.hora_armacao:
            out[iid] = {"armado": True, "origem": "Plannix", "quando": p.hora_armacao}
        elif p and p.fabricada:
            out[iid] = {"armado": True, "origem": "Fabricada", "quando": p.data_fabricacao}
        else:
            out[iid] = {"armado": False, "origem": "", "quando": None}
    return out


def marcar(db: Session, codigos: list[str], item_ids: list[int], armado: bool) -> int:
    n = 0
    for c in codigos:
        a = db.scalar(select(Armacao).where(Armacao.codigo == c))
        if armado and not a:
            db.add(Armacao(codigo=c)); n += 1
        elif not armado and a:
            db.delete(a); n += 1
    for iid in item_ids:
        a = db.scalar(select(Armacao).where(Armacao.item_id == iid))
        if armado and not a:
            db.add(Armacao(item_id=iid)); n += 1
        elif not armado and a:
            db.delete(a); n += 1
    db.commit()
    return n


def foto(i: ItemProg) -> dict:
    return {"id": i.id, "codigo": i.codigo, "obra": i.obra, "peca": i.peca, "forma": i.forma, "nave": i.nave, "secao": i.secao,
            "comp": i.comprimento, "qtd": i.qtd, "produto": i.produto}


def divulgar(db: Session, rev: RevisaoProg, d: date) -> Divulgacao:
    dv = Divulgacao(dia=d, revisao_id=rev.id, criada_em=datetime.now(), itens=[foto(i) for i in itens_do_dia(db, rev, d)])
    db.add(dv)
    db.commit()
    return dv


def ultima_divulgacao(db: Session, d: date) -> Divulgacao | None:
    return db.scalar(select(Divulgacao).where(Divulgacao.dia == d).order_by(Divulgacao.criada_em.desc()).limit(1))


def diferencas(dv: Divulgacao | None, itens: list[ItemProg]) -> tuple[set, list[dict]]:
    """Itens incluídos depois da divulgação (ids) e itens que estavam na lista e saíram."""
    if not dv:
        return set(), []
    chave = lambda x: x["codigo"] or f'{x["peca"]}|{x["forma"]}'
    antes = {chave(x): x for x in dv.itens}
    agora = {chave(foto(i)): i for i in itens}
    incluidos = {i.id for k, i in agora.items() if k not in antes}
    retirados = [x for k, x in antes.items() if k not in agora]
    return incluidos, retirados
