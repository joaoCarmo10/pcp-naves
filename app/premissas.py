"""Premissas do replanejamento: regras de negócio editáveis na tela (o que não entra, o que fura a fila, fôrma parada,
peça avulsa fora do Plannix). As regras de fôrma que não mudam ficam no código (REGRAS_FIXAS)."""
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Peca, Premissa

TIPOS = {
    "excluir": "Não programar",
    "somente": "Da obra, só programar",
    "nao_antes": "Só a partir de",
    "primeiro": "Fura a fila",
    "parar_forma": "Parar fôrma",
    "avulsa": "Peça avulsa (fora do Plannix)",
}

REGRAS_FIXAS = [
    "Até a véspera do início a programação não muda (histórico do programado x realizado, com apontamentos).",
    "Fila: fura-fila > atrasadas > já programadas (na data original) > novas; dentro: prioridade da obra, armada (armação mais antiga primeiro), item/pavimento, peça.",
    "Lajes seguem a ordem da programação base (cronograma de lajes e ordem da folha) e não usam o critério de armação.",
    "Nave 2: as pistas P1 e P2 alternam, uma por dia (segue a pista da base quando ela já tinha laje no dia).",
    "Pista: um arranjo de cordoalha por dia.",
    "Uma seção por fôrma por dia, exceto N1 F1, F2, F6 e F7 (mistas). A F6 e a F7 usam a mesma montagem no dia.",
    "Montagem da fôrma no dia: a que a programação base previa para o dia; senão continua a do dia anterior enquanto houver peça dela; só depois muda (adaptação).",
    "Folga de 0,20 m entre peças na mesma fôrma linear (não vale para laje).",
    "A peça entra na fôrma pelo cadastro (seção e adaptações aceitas, comprimento útil, produto).",
    "Primeiro dia: só peça já armada ou que já estava programada para esse dia (armação D-1).",
    "Não entram: obras inativas, fora da área de atuação da obra, desenho bloqueado no Autodoc.",
]

# premissas da quinzena 16-30/11 (alinhamentos de 13, 14 e 16/11) e de 23/11
INICIAIS = [
    dict(tipo="excluir", obra="7Log", obs="Tirar os pilares da 7Log (23/11)"),
    dict(tipo="excluir", obra="QPC", obs="Tirar a QPC (23/11)"),
    dict(tipo="somente", obra="Gandú", produto="PILAR", secoes="40x40, 30x40", obs="Gandú: só pilares 40x40 e 30x40 na quinzena (14/11)"),
    dict(tipo="excluir", obra="Aratu", pecas="PP4, PP9, PP31", obs="Suspensas no alinhamento de 13/11"),
    dict(tipo="excluir", obra="Aratu", produto="LAJE", obs="Laje fora da sequência da Nave 2 na quinzena"),
    dict(tipo="excluir", obra="Bay", pecas="PP6", obs="Não programar (PCP 16/11)"),
    dict(tipo="excluir", obra="Bay", produto="LAJE", obs="Laje fora da sequência da Nave 2 na quinzena"),
    dict(tipo="nao_antes", obra="Bay", pecas="PP19", de=date(2026, 11, 30), obs="Só a partir de 30/11 (14/11)"),
    dict(tipo="excluir", obra="Cau Biglia", produto="LAJE", obs="Laje h16 a reaproveitar, sem produção"),
    dict(tipo="primeiro", obra="Bay", pecas="VS1-2", obs="Prioridade (14/11)"),
    dict(tipo="primeiro", obra="Elite", pecas="S1-VP849, S2-VP829", obs="Protendidas já armadas (14/11)"),
    dict(tipo="parar_forma", forma="N3ESC2", de=date(2026, 11, 25), ate=date(2026, 11, 27),
         obs="Escadas paradas para as do Barracão Hot Park (23/11)"),
    dict(tipo="avulsa", obra="Hot Park Barracão", pecas="LE1", produto="ESCADA", secoes="E=15", forma="N3ESC1",
         de=date(2026, 11, 25), volume=1.094, obs="Barracão Hot Park, 1 por dia, outras escadas paradas. Prancha R00 em aprovação"),
    dict(tipo="avulsa", obra="Hot Park Barracão", pecas="LE2", produto="ESCADA", secoes="E=15", forma="N3ESC1",
         de=date(2026, 11, 26), volume=0.626, obs="Barracão Hot Park, 1 por dia, outras escadas paradas. Prancha R00 em aprovação"),
    dict(tipo="avulsa", obra="Hot Park Barracão", pecas="LE3", produto="ESCADA", secoes="E=15", forma="N3ESC1",
         de=date(2026, 11, 27), volume=0.780, obs="Barracão Hot Park, 1 por dia, outras escadas paradas. Prancha R00 em aprovação"),
]


def semear(db: Session) -> int:
    if db.scalar(select(Premissa.id).limit(1)):
        return 0
    for p in INICIAIS:
        db.add(Premissa(**p))
    db.commit()
    return len(INICIAIS)


def _n(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[\s×]", "", s.upper().replace("×", "X"))


def _lista(s: str) -> set[str]:
    return {_n(x) for x in (s or "").split(",") if _n(x)}


def casa_obra(pr: Premissa, obra: str) -> bool:
    return not pr.obra or _n(pr.obra) in _n(obra)


def casa(pr: Premissa, p: Peca) -> bool:
    """A peça casa com obra, nomes, produto e seções da premissa (campo vazio = qualquer)."""
    if not casa_obra(pr, p.obra.nome):
        return False
    if pr.pecas and _n(p.peca) not in _lista(pr.pecas):
        return False
    if pr.produto and _n(p.produto) != _n(pr.produto):
        return False
    if pr.secoes and _n(p.secao) not in _lista(pr.secoes):
        return False
    return True


def ativas(db: Session) -> list[Premissa]:
    return list(db.scalars(select(Premissa).where(Premissa.ativa.is_(True)).order_by(Premissa.tipo, Premissa.id)))


def motivo_fora(prs: list[Premissa], p: Peca) -> str | None:
    """Se a peça não pode entrar pelas premissas, o motivo (obs da premissa)."""
    for pr in prs:
        if pr.tipo == "excluir" and casa(pr, p):
            return pr.obs or TIPOS["excluir"]
    for pr in prs:
        if pr.tipo == "somente" and casa_obra(pr, p.obra.nome) and not casa(pr, p):
            return pr.obs or f"{pr.obra}: só {pr.produto} {pr.secoes}".strip()
    return None


def nao_antes(prs: list[Premissa], p: Peca) -> date | None:
    ds = [pr.de for pr in prs if pr.tipo == "nao_antes" and pr.de and casa(pr, p)]
    return max(ds) if ds else None


def fura_fila(prs: list[Premissa], p: Peca) -> bool:
    return any(pr.tipo == "primeiro" and casa(pr, p) for pr in prs)


def forma_parada(prs: list[Premissa], codigo: str, d: date) -> bool:
    return any(pr.tipo == "parar_forma" and _n(pr.forma) == _n(codigo) and (not pr.de or pr.de <= d) and (not pr.ate or d <= pr.ate)
               for pr in prs)
