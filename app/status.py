"""Status de cada peça de projeto (nome no desenho, ex.: VR13) a partir das peças físicas do Plannix."""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Peca

CORES = {  # RGB 0-1 para o PDF e hex para a tela
    "FAB": ((0.30, 0.69, 0.31), "#4caf50"),
    "PARC": ((1.00, 0.75, 0.00), "#ffbf00"),
    "PROG": ((0.27, 0.51, 0.85), "#4582d9"),
    "ATRASO": ((0.85, 0.33, 0.08), "#d84315"),
    "FALTA": ((0.90, 0.22, 0.21), "#e53935"),
}
LEGENDA = [("FAB", "Fabricada"), ("PARC", "Fabricada em parte"), ("PROG", "Programada"), ("ATRASO", "Atrasada (sem nova data)"),
           ("FALTA", "Sem programação")]


@dataclass
class StatusPeca:
    peca: str
    produto: str = ""
    secao: str = ""
    total: int = 0
    fabricadas: int = 0
    programadas: int = 0
    bloqueadas: int = 0
    atrasadas: int = 0       # programada para um dia que passou, não feita e sem data nova
    volume: float = 0.0
    volume_fab: float = 0.0
    datas: list[date] = field(default_factory=list)

    @property
    def falta(self) -> int:
        return self.total - self.fabricadas - self.programadas - self.atrasadas

    @property
    def status(self) -> str:
        if self.fabricadas == self.total:
            return "FAB"
        if self.fabricadas:
            return "PARC"
        if self.programadas:
            return "PROG"
        if self.atrasadas:
            return "ATRASO"
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
    """Status por nome a partir das unidades do Plannix, já sem as que estão fora da área de atuação da obra."""
    return status_de_unidades(unidades_da_obra(db, obra_id))


def status_de_unidades(un: dict[str, list[dict]]) -> dict[str, StatusPeca]:
    out: dict[str, StatusPeca] = {}
    for nome, us in un.items():
        if not us:
            continue
        s = out[nome] = StatusPeca(peca=nome, produto=us[0].get("produto", ""), secao=us[0].get("secao", ""))
        for u in us:
            s.total += 1
            s.volume += u["vol"]
            if u["st"] == "FAB":
                s.fabricadas += 1
                s.volume_fab += u["vol"]
            elif u["st"] == "BLOQ":
                s.bloqueadas += 1
            elif u["st"] == "PROG":
                s.programadas += 1
                if u["data"]:
                    s.datas.append(u["data"])
            elif u["st"] == "ATRASO":
                s.atrasadas += 1
        s.datas = sorted(set(s.datas))
    return out


def fora_da_area(plantas: list, unidades: dict[str, list[dict]]) -> dict[str, int]:
    """Quantas unidades de cada nome ficam fora da área de atuação (ex.: Bay, eixos 26+ são de outra empresa).
    Só vale para obras com área definida em alguma planta. Pilar repetido em várias folhas conta pela folha de maior
    quantidade (é a mesma peça); peças diferentes em folhas diferentes somam."""
    fs = [p for p in plantas if p.marcas]
    if not any(p.area for p in fs):
        return {}
    den: dict[str, list[int]] = defaultdict(list)
    fora: dict[str, list[int]] = defaultdict(list)
    for p in fs:
        d: dict[str, int] = defaultdict(int)
        f: dict[str, int] = defaultdict(int)
        for m in p.marcas:
            cx = (m["r"][0] + m["r"][2]) / 2
            if p.area and not (p.area["x0"] <= cx <= p.area["x1"]):
                f[m["t"]] += 1
            else:
                d[m["t"]] += 1
        for n in set(d) | set(f):
            den[n].append(d[n])
            fora[n].append(f[n])
    out = {}
    for n, fo in fora.items():
        nu = len(unidades.get(n, []))
        if not nu or not sum(fo):
            continue
        rep = sum(den[n]) + sum(fo) >= 1.5 * nu
        fd, ff = (max(den[n]), max(fo)) if rep else (sum(den[n]), sum(fo))
        k = min(ff, max(0, nu - fd))
        if k:
            out[n] = k
    return out


def resumo(status: dict[str, StatusPeca], nomes: set[str] | None = None) -> dict:
    itens = [s for k, s in status.items() if nomes is None or k in nomes]
    t = sum(s.total for s in itens)
    f = sum(s.fabricadas for s in itens)
    p = sum(s.programadas for s in itens)
    v = sum(s.volume for s in itens)
    vf = sum(s.volume_fab for s in itens)
    b = sum(getattr(s, "bloqueadas", 0) for s in itens)
    a = sum(getattr(s, "atrasadas", 0) for s in itens)
    return dict(pecas=t, fabricadas=f, programadas=p, falta=t - f - p - b - a, bloqueadas=b, atrasadas=a, volume=v, volume_fab=vf,
                pct=(f / t if t else 0), pct_vol=(vf / v if v else 0))


def por_produto(status: dict[str, StatusPeca]) -> list[dict]:
    g = defaultdict(lambda: dict(total=0, fab=0, prog=0, vol=0.0, volf=0.0))
    for s in status.values():
        r = g[s.produto]
        r["total"] += s.total; r["fab"] += s.fabricadas; r["prog"] += s.programadas
        r["vol"] += s.volume; r["volf"] += s.volume_fab
    return [dict(produto=k, **v, pct=(v["fab"] / v["total"] if v["total"] else 0)) for k, v in sorted(g.items())]


# ------------------------------------------------------------------ uma etiqueta = uma unidade (código do Plannix)

CORES["SEM"] = ((0.62, 0.62, 0.62), "#9e9e9e")
CORES["BLOQ"] = ((0.44, 0.19, 0.63), "#7030a0")
LEGENDA_UNID = [("FAB", "Fabricada"), ("PROG", "Programada"), ("ATRASO", "Atrasada (sem nova data)"), ("FALTA", "Sem programação"), ("BLOQ", "Desenho bloqueado (Autodoc)"),
                ("SEM", "Sem peça no Plannix")]


def unidades_da_obra(db: Session, obra_id: int, escopo: bool = True) -> dict[str, list[dict]]:
    """Unidades físicas por nome, na ordem em que ocupam as etiquetas: fabricadas (data), programadas (data), resto."""
    from .programacao import codigos_atrasados, codigos_programados
    prog = codigos_programados(db)
    atras = codigos_atrasados(db)
    pecas = list(db.scalars(select(Peca).where(Peca.obra_id == obra_id)))
    vol_nome = {p.peca: p.volume for p in pecas if p.volume}
    from . import premissas as pm
    prs = [pr for pr in pm.ativas(db) if pr.tipo == "pendente"]
    from . import autodoc
    ad = autodoc.indice(db)
    out: dict[str, list[dict]] = defaultdict(list)
    for p in pecas:
        pend = bool(prs) and pm.pendente(prs, p)
        x = autodoc.de(ad, p.obra_id, p.peca)
        if p.fabricada and not pend:
            st, d = "FAB", p.data_fabricacao
        elif x and x.bloqueado:
            st, d = "BLOQ", None        # desenho bloqueado no Autodoc: não pode estar programada
        elif p.codigo in prog:
            st, d = "PROG", prog[p.codigo]
        elif p.codigo in atras:
            st, d = "ATRASO", atras[p.codigo]
        else:
            st, d = "FALTA", None
        out[p.peca].append({"cod": p.codigo, "st": st, "data": d, "vol": volume_estimado(p, vol_nome),
                            "produto": p.produto, "secao": p.secao, "pend": pend, "item": p.item or "", "etapa": p.etapa or "",
                            "comp": p.comprimento})
    # peça idêntica feita antes do dia dela (adiantada) libera a data futura: a atrasada de mesmo nome herda essa data
    for us in out.values():
        livres = sorted(prog[u["cod"]] for u in us if u["st"] == "FAB" and u["cod"] in prog)
        for u in sorted((u for u in us if u["st"] == "ATRASO"), key=lambda u: u["data"]):
            if not livres:
                break
            u["st"], u["data"] = "PROG", livres.pop(0)
    ordem = {"FAB": 0, "PROG": 1, "ATRASO": 2, "FALTA": 3, "BLOQ": 4}
    for us in out.values():   # pendente (premissa) vem antes das outras não feitas: é peça nossa, não sai na área
        us.sort(key=lambda u: (ordem[u["st"]], 0 if u["pend"] else 1, u["data"] or date.max, u["cod"] or ""))
    if escopo:
        from .models import Planta
        fora = fora_da_area(list(db.scalars(select(Planta).where(Planta.obra_id == obra_id))), out)
        for n, k in fora.items():
            # sai do fim da fila (as que não estão nas etiquetas de dentro); pendente por premissa é nossa e fica
            us = out[n]
            tirar = set()
            for i in range(len(us) - 1, -1, -1):
                if len(tirar) >= k:
                    break
                if not us[i].get("pend"):
                    tirar.add(i)
            out[n] = [u for i, u in enumerate(us) if i not in tirar]
    return {n: us for n, us in out.items() if us}


def atribuir(marcas: list, unidades: dict[str, list[dict]]) -> list[dict]:
    """Cada etiqueta da folha recebe uma unidade diferente, em ordem de leitura (de cima para baixo, da esquerda para
    a direita). Etiqueta sem unidade sobrando = 'SEM'. A quantidade é exata; a posição de cada código é indicativa."""
    por_nome: dict[str, list] = defaultdict(list)
    for m in marcas:
        por_nome[m["t"]].append(m)
    out = []
    for nome, ms in por_nome.items():
        us = unidades.get(nome, [])
        for i, m in enumerate(sorted(ms, key=lambda m: (round(m["r"][1] / 15), m["r"][0]))):
            u = us[i] if i < len(us) else None
            out.append({**m, "cod": u["cod"] if u else None, "st": u["st"] if u else "SEM",
                        "data": u["data"] if u else None, "vol": u["vol"] if u else 0.0})
    return out


def tipo(u: dict) -> str:
    """Tipo da unidade no Plannix: item (linha do contrato) + produto + seção. Separa peças de mesmo nome que são de
    estruturas diferentes da obra (ex.: LE1 do Farol x LE1 do Barracão)."""
    base = f"{u.get('item', '')}|{u.get('produto', '')}|{u.get('secao', '')}"
    return f"{base}|{u['etapa']}" if u.get("etapa") else base


def _fora(u: dict, tf: set) -> bool:
    """Tipo excluído da folha: pela chave completa ou pela chave sem etapa (marcação antiga vale para todas as etapas)."""
    k = tipo(u)
    return k in tf or k.rsplit("|", 1)[0] in tf if u.get("etapa") else k in tf


def atribuir_obra(folhas: list[tuple[int, list]], unidades: dict[str, list[dict]],
                  fora: dict[int, set] | None = None) -> dict[int, list[dict]]:
    """Distribui as unidades pelas folhas da obra, por nome:
    - unidades diferentes em folhas diferentes (V1 no pav 1 e V1 no pav 2): a folha seguinte continua de onde a
      anterior parou, então nenhum código se repete;
    - a mesma peça desenhada em várias folhas (pilar que atravessa os pavimentos): a soma das etiquetas passa muito do
      Plannix (>= 1,5x); aí cada folha mostra os MESMOS códigos, e a peça continua contando uma vez só na obra.
    fora[folha]: tipos (item|produto|seção) que não são da estrutura desenhada na folha; não entram nela."""
    fora = fora or {}
    por_folha = {fid: defaultdict(list) for fid, _ in folhas}
    soma: dict[str, int] = defaultdict(int)
    for fid, marcas in folhas:
        for m in marcas:
            por_folha[fid][m["t"]].append(m)
            soma[m["t"]] += 1
    repetida = {n for n, q in soma.items() if q >= 1.5 * max(1, len(unidades.get(n, [])))}
    usados: dict[str, set] = defaultdict(set)
    out: dict[int, list[dict]] = {}
    for fid, marcas in folhas:
        tf = fora.get(fid) or set()
        resto = {}
        for n in por_folha[fid]:
            us = [u for u in unidades.get(n, []) if not _fora(u, tf)]
            resto[n] = us if n in repetida else [u for u in us if u["cod"] not in usados[n]]
        out[fid] = atribuir(marcas, resto)
        for m in out[fid]:
            if m["st"] != "SEM":
                usados[m["t"]].add(m["cod"])
    return out


def tipos_da_folha(atribuidas: list[dict], unidades: dict[str, list[dict]], fora: set | None = None) -> list[dict]:
    """Tipos do Plannix das peças desta folha cujo nome aparece em mais de um tipo (estruturas diferentes com o mesmo
    nome). Cada um com as peças, quantidade e comprimentos, para o usuário marcar o que é desta folha."""
    fora = fora or set()
    nomes = {m["t"] for m in atribuidas}
    g: dict[str, dict] = {}
    for n in nomes:
        us = unidades.get(n, [])
        if len({tipo(u) for u in us}) < 2:
            continue
        # só pede conferência quando a escolha muda o resultado: tipos de fato diferentes (não só o item do contrato
        # com a mesma seção e comprimento) e status diferentes entre eles (se tudo está fabricado, tanto faz)
        assin = defaultdict(set)
        for u in us:
            assin[tipo(u)].add((u.get("produto", ""), (u.get("secao") or "").upper(), u.get("etapa", ""), round(u.get("comp") or 0, 2)))
        if len({frozenset(v) for v in assin.values()}) < 2:
            continue
        if len({u["st"] for u in us}) < 2:
            continue
        for u in us:
            k = tipo(u)
            x = g.setdefault(k, {"chave": k, "item": u.get("item", ""), "produto": u.get("produto", ""), "etapa": u.get("etapa", ""),
                                 "secao": u.get("secao", ""), "pecas": set(), "qtd": 0, "comps": set(), "dentro": not _fora(u, fora)})
            x["pecas"].add(n)
            x["qtd"] += 1
            if u.get("comp"):
                x["comps"].add(round(u["comp"], 2))
    out = []
    for x in g.values():
        x["pecas"] = sorted(x["pecas"], key=chave_natural)
        x["comps"] = sorted(x["comps"])
        out.append(x)
    return sorted(out, key=lambda x: (x["produto"], chave_natural(x["item"]), x["secao"], x["etapa"]))


def desenhadas(atrib_obra: dict[int, list[dict]]) -> dict[str, int]:
    """Códigos distintos desenhados na obra, por nome (a mesma peça em várias folhas conta uma vez)."""
    c: dict[str, set] = defaultdict(set)
    for ms in atrib_obra.values():
        for m in ms:
            if m["st"] != "SEM":
                c[m["t"]].add(m["cod"])
    return {n: len(v) for n, v in c.items()}


def status_folha(atribuidas: list[dict], st_obra: dict[str, StatusPeca],
                 desenhadas_obra: dict[str, int] | None = None) -> dict[str, StatusPeca]:
    """Contagem da folha a partir das etiquetas (cada uma uma unidade): é o que aparece pintado."""
    out: dict[str, StatusPeca] = {}
    for m in atribuidas:
        base = st_obra.get(m["t"])
        if not base:
            continue
        s = out.get(m["t"])
        if not s:
            s = out[m["t"]] = StatusPeca(peca=m["t"], produto=base.produto, secao=base.secao)
            s.plannix, s.sem = base.total, 0
        if m["st"] == "SEM":
            s.sem += 1
            continue
        s.total += 1
        s.volume += m["vol"]
        if m["st"] == "FAB":
            s.fabricadas += 1
            s.volume_fab += m["vol"]
        elif m["st"] == "PROG":
            s.programadas += 1
            if m["data"]:
                s.datas.append(m["data"])
        elif m["st"] == "ATRASO":
            s.atrasadas += 1
    for s in out.values():
        s.datas = sorted(set(s.datas))
        s.nao_desenhadas = max(0, s.plannix - (desenhadas_obra or {}).get(s.peca, s.total))
    return out
