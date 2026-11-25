"""Dashboard de produção: cards do período, matriz obra|etapa > tipo > peça x mês/dia e avanço por tipo de peça."""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import programacao as pg
from . import pxr_rel as pxr
from .models import Obra, Peca
from .status import chave_natural, status_da_obra

METRICAS = {"pecas": ("Peças", 0), "vol": ("Volume (m³)", 1), "aco": ("Aço frouxo (kg)", 0)}
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _vol(p: Peca) -> float:
    return p.volume or p.vol_projeto or 0.0


def montar(db: Session, qp) -> dict:
    hoje = date.today()
    de = _d(qp.get("de")) or hoje.replace(day=1)
    ate = _d(qp.get("ate")) or hoje
    if ate < de:
        de, ate = ate, de
    f = {"obra": qp.get("obra") or "", "tipo": qp.get("tipo") or "", "m": qp.get("m") or "pecas",
         "col": qp.get("col") or ("dia" if (ate - de).days <= 31 else "mes")}
    if f["m"] not in METRICAS:
        f["m"] = "pecas"
    obras = {o.id: o for o in db.scalars(select(Obra))}
    no_escopo = {oid for oid, o in obras.items() if (o.nome == f["obra"] if f["obra"] else o.ativa)}

    # ---------------- produção do período (Plannix: data de fabricação)
    feitas = [p for p in db.scalars(select(Peca).where(Peca.fabricada.is_(True), Peca.data_fabricacao >= de,
                                                        Peca.data_fabricacao <= ate))
              if p.obra_id in no_escopo and (not f["tipo"] or p.produto == f["tipo"])]
    val = {"pecas": lambda p: 1, "vol": _vol, "aco": lambda p: p.peso_aco_frouxo or 0.0}[f["m"]]
    col = (lambda d: d.isoformat()) if f["col"] == "dia" else (lambda d: f"{d:%Y-%m}")
    cols, d = [], de
    while d <= ate:
        k = col(d)
        if k not in cols:
            cols.append(k)
        d += timedelta(days=1)
    rot = {k: (f"{date.fromisoformat(k):%d/%m}" if f["col"] == "dia" else f"{MESES[int(k[5:]) - 1]}/{k[2:4]}") for k in cols}
    arv: dict = {}
    for p in feitas:
        o = obras[p.obra_id].nome
        c = col(p.data_fabricacao)
        v = val(p)
        for chave in ((o,), (o, p.produto or "-"), (o, p.produto or "-", p.peca)):
            arv.setdefault(chave, defaultdict(float))[c] += v
    linhas = []
    for (o,) in sorted(k for k in arv if len(k) == 1):
        linhas.append({"n": 1, "id": o, "pai": "", "rot": o, "v": arv[(o,)]})
        for (_, t) in sorted(k[:2] for k in arv if len(k) == 2 and k[0] == o):
            linhas.append({"n": 2, "id": f"{o}|{t}", "pai": o, "rot": t, "v": arv[(o, t)]})
            for k in sorted((k for k in arv if len(k) == 3 and k[0] == o and k[1] == t), key=lambda k: chave_natural(k[2])):
                linhas.append({"n": 3, "id": "|".join(k), "pai": f"{o}|{t}", "rot": k[2], "v": arv[k]})
    tot_col = defaultdict(float)
    for (o,), v in ((k, v) for k, v in arv.items() if len(k) == 1):
        for c, x in v.items():
            tot_col[c] += x

    # ---------------- cards
    dias_prod = len({p.data_fabricacao for p in feitas})
    vol = sum(_vol(p) for p in feitas)
    aco = sum(p.peso_aco_frouxo or 0 for p in feitas)
    ativo = sum((p.taxa_aco_prot or 0) * _vol(p) for p in feitas)
    sem_aco = sum(1 for p in feitas if not p.peso_aco_frouxo)
    ppc = None
    ontem = hoje - timedelta(days=1)
    fim_ppc = min(ate, ontem)
    rev = pxr.revisao_para(db, fim_ppc) if fim_ppc >= de else None
    if rev:
        nomes = {obras[i].nome for i in no_escopo}
        ls = [l for l in pg.linhas(db, rev, max(de, rev.inicio), min(fim_ppc, rev.fim))
              if l.item.obra in nomes and (not f["tipo"] or (l.item.produto or "-") == f["tipo"])]
        g = pxr.grupo(ls, lambda l: "t")
        ppc = g[0] if g else None
    cards = {"pecas": len(feitas), "vol": vol, "aco": aco, "ativo": ativo, "sem_aco": sem_aco, "dias": dias_prod,
             "vol_dia": vol / dias_prod if dias_prod else 0, "ppc": ppc, "rev": rev}

    # ---------------- avanço por tipo de peça (posição de hoje, obras do escopo)
    tipos: dict = {}
    for oid in no_escopo:
        for s in status_da_obra(db, oid).values():
            if f["tipo"] and s.produto != f["tipo"]:
                continue
            t = tipos.setdefault(s.produto or "-", {"tipo": s.produto or "-", "total": 0, "fab": 0, "prog": 0, "atras": 0,
                                                    "vol": 0.0, "volf": 0.0, "obras": {}})
            ob = t["obras"].setdefault(obras[oid].nome, {"total": 0, "fab": 0, "prog": 0, "atras": 0, "pend": []})
            for k, kk in (("total", "total"), ("fab", "fabricadas"), ("prog", "programadas"), ("atras", "atrasadas")):
                t[k] += getattr(s, kk)
                ob[k] += getattr(s, kk)
            t["vol"] += s.volume
            t["volf"] += s.volume_fab
            if s.total > s.fabricadas:
                ob["pend"].append((s.peca, s.total - s.fabricadas, s.datas[0] if s.datas else None))
    av = []
    for t in sorted(tipos.values(), key=lambda t: -(t["total"] - t["fab"])):
        t["pct"] = t["fab"] / t["total"] if t["total"] else 0
        t["pct_vol"] = t["volf"] / t["vol"] if t["vol"] else 0
        t["obras"] = sorted(({"nome": k, **v, "pct": v["fab"] / v["total"] if v["total"] else 0,
                              "pend": sorted(v["pend"], key=lambda x: chave_natural(x[0]))} for k, v in t["obras"].items()),
                            key=lambda x: x["nome"])
        av.append(t)
    opcoes = {"obra": sorted(o.nome for o in obras.values() if o.ativa),
              "tipo": sorted({p for (p,) in db.execute(select(Peca.produto).distinct()) if p})}
    return {"f": f, "de": de, "ate": ate, "cols": cols, "rot": rot, "linhas": linhas, "tot_col": tot_col,
            "cards": cards, "av": av, "opcoes": opcoes, "METRICAS": METRICAS, "dec": METRICAS[f["m"]][1]}
