"""Status de cada peça de projeto (nome no desenho, ex.: VR13) a partir das peças físicas do Plannix."""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Peca, Programacao

CORES = {  # RGB 0-1 para o PDF e hex para a tela
    "FAB": ((0.30, 0.69, 0.31), "#4caf50"),
    "PARC": ((1.00, 0.75, 0.00), "#ffbf00"),
    "PROG": ((0.27, 0.51, 0.85), "#4582d9"),
    "FALTA": ((0.90, 0.22, 0.21), "#e53935"),
}
LEGENDA = [("FAB", "Fabricada"), ("PARC", "Fabricada em parte"), ("PROG", "Programada"), ("FALTA", "Sem programação")]


@dataclass
class StatusPeca:
    peca: str
    produto: str = ""
    secao: str = ""
    total: int = 0
    fabricadas: int = 0
    programadas: int = 0
    volume: float = 0.0
    volume_fab: float = 0.0
    datas: list[date] = field(default_factory=list)

    @property
    def falta(self) -> int:
        return self.total - self.fabricadas - self.programadas

    @property
    def status(self) -> str:
        if self.fabricadas == self.total:
            return "FAB"
        if self.fabricadas:
            return "PARC"
        if self.programadas:
            return "PROG"
        return "FALTA"


def chave_natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def volume_estimado(p: Peca, vol_nome: dict[str, float]) -> float:
    """Volume do Plannix; se faltar (exportação completa não traz), usa o de outra peça com o mesmo nome
    ou seção x comprimento."""
    if p.volume:
        return p.volume
    if vol_nome.get(p.peca):
        return vol_nome[p.peca]
    n = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", p.secao or "")]
    if len(n) == 2 and p.comprimento:
        return n[0] / 100 * n[1] / 100 * p.comprimento
    return 0.0


def status_da_obra(db: Session, obra_id: int) -> dict[str, StatusPeca]:
    prog = {p.codigo: p.data for p in db.scalars(select(Programacao))}
    pecas = list(db.scalars(select(Peca).where(Peca.obra_id == obra_id)))
    vol_nome = {p.peca: p.volume for p in pecas if p.volume}
    out: dict[str, StatusPeca] = {}
    for p in pecas:
        s = out.setdefault(p.peca, StatusPeca(peca=p.peca, produto=p.produto, secao=p.secao))
        v = volume_estimado(p, vol_nome)
        s.total += 1
        s.volume += v
        if p.fabricada:
            s.fabricadas += 1
            s.volume_fab += v
        elif p.codigo in prog:
            s.programadas += 1
            if prog[p.codigo]:
                s.datas.append(prog[p.codigo])
    for s in out.values():
        s.datas = sorted(set(s.datas))
    return out


def resumo(status: dict[str, StatusPeca], nomes: set[str] | None = None) -> dict:
    itens = [s for k, s in status.items() if nomes is None or k in nomes]
    t = sum(s.total for s in itens)
    f = sum(s.fabricadas for s in itens)
    p = sum(s.programadas for s in itens)
    v = sum(s.volume for s in itens)
    vf = sum(s.volume_fab for s in itens)
    return dict(pecas=t, fabricadas=f, programadas=p, falta=t - f - p, volume=v, volume_fab=vf,
                pct=(f / t if t else 0), pct_vol=(vf / v if v else 0))


def por_produto(status: dict[str, StatusPeca]) -> list[dict]:
    g = defaultdict(lambda: dict(total=0, fab=0, prog=0, vol=0.0, volf=0.0))
    for s in status.values():
        r = g[s.produto]
        r["total"] += s.total; r["fab"] += s.fabricadas; r["prog"] += s.programadas
        r["vol"] += s.volume; r["volf"] += s.volume_fab
    return [dict(produto=k, **v, pct=(v["fab"] / v["total"] if v["total"] else 0)) for k, v in sorted(g.items())]
