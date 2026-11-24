"""Programado x realizado: filtros, análise e relatório (tela de impressão e Excel).

Regras de leitura usadas em todo o relatório:
- Programadas = peças com data já vencida no período (pendentes de hoje em diante não entram no PPC).
- PPC = no prazo ÷ programadas. Realizado = (no prazo + com atraso + data a confirmar) ÷ programadas.
- "Atrasadas" = não realizadas + realizadas com atraso. O filtro de status só restringe as listas de peças;
  os indicadores continuam sobre todas as programadas do recorte (senão o PPC perderia o sentido).
"""
from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import programacao as pg
from .models import RevisaoProg

ATRASADAS = ("NÃO REALIZADA", "COM ATRASO")
FILTROS_STATUS = {
    "atrasadas": ("Só atrasadas (não realizadas + com atraso)", set(ATRASADAS)),
    "todas": ("Todas as programadas", None),
    "nao": ("Não realizadas", {"NÃO REALIZADA"}),
    "atraso": ("Realizadas com atraso", {"COM ATRASO"}),
    "confirmar": ("Data a confirmar", {"DATA A CONFIRMAR"}),
    "prazo": ("No prazo", {"NO PRAZO"}),
}
SEM_CAUSA = "Sem justificativa"
MIN_PECAS = 5   # grupo com menos peças que isso não entra em "pior obra / pior tipo"


def _d(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def segunda(d: date) -> date:
    return d - timedelta(days=d.weekday())


def revisao_para(db: Session, dia: date) -> RevisaoProg | None:
    """Revisão mais recente que cobre o dia."""
    return db.scalar(select(RevisaoProg).where(RevisaoProg.inicio <= dia, RevisaoProg.fim >= dia, RevisaoProg.rascunho.is_not(True))
                     .order_by(RevisaoProg.criada_em.desc(), RevisaoProg.id.desc()))


def periodo(db: Session, qp, rev_atual: RevisaoProg | None) -> dict:
    """Interpreta revisão, semana e datas da URL. Sem nada: a última semana fechada da revisão mais recente."""
    hoje = date.today()
    ontem = hoje - timedelta(days=1)
    rev = db.get(RevisaoProg, int(qp["rev"])) if (qp.get("rev") or "").isdigit() else None
    ini, fim = _d(qp.get("de")), _d(qp.get("ate"))
    sem = _d(qp.get("semana"))
    if sem:
        ini, fim = segunda(sem), segunda(sem) + timedelta(days=6)
    if not rev:
        rev = revisao_para(db, ini or ontem) or rev_atual
    if not rev:
        return {"rev": None}
    if not ini:
        ini = max(rev.inicio, segunda(min(ontem, rev.fim)))
    if not fim:
        fim = min(rev.fim, ontem, ini + timedelta(days=6))
    if fim < ini:
        fim = ini
    semanas = []
    d = segunda(rev.inicio)
    while d <= rev.fim:
        semanas.append(d)
        d += timedelta(days=7)
    return {"rev": rev, "ini": ini, "fim": fim, "semanas": semanas,
            "semana": segunda(ini) if ini == segunda(ini) and (fim - ini).days <= 6 else None}


def filtrar(ls, f: dict) -> list:
    """Recorte por obra, nave e produto (vale para indicadores e listas)."""
    return [l for l in ls
            if (not f.get("obra") or l.item.obra == f["obra"])
            and (not f.get("nave") or (l.item.nave or "-") == f["nave"])
            and (not f.get("produto") or (l.item.produto or "-") == f["produto"])]


def so_status(ls, chave: str) -> list:
    alvo = FILTROS_STATUS.get(chave or "atrasadas", (None, None))[1]
    return [l for l in ls if alvo is None or l.status in alvo]


def opcoes(ls) -> dict:
    return {"obra": sorted({l.item.obra for l in ls if l.item.obra}),
            "nave": sorted({l.item.nave or "-" for l in ls}),
            "produto": sorted({l.item.produto or "-" for l in ls})}


def _vol(l) -> float:
    i = l.item
    if not i.volume:
        return 0.0
    return i.volume * (l.qtd / i.qtd) if i.qtd else i.volume


def grupo(ls, chave) -> list[dict]:
    """Agregado por chave com peças, volume, PPC, realizado e atraso."""
    g: dict = defaultdict(lambda: {"c": Counter(), "v": Counter(), "desv": []})
    for l in ls:
        x = g[chave(l)]
        x["c"][l.status] += l.qtd
        x["v"][l.status] += _vol(l)
        if l.status == "COM ATRASO" and l.desvio:
            x["desv"] += [l.desvio] * l.qtd
    out = []
    for k, x in g.items():
        c, v = x["c"], x["v"]
        prog = sum(c.values()) - c["PENDENTE"] - c["SEM INFORMAÇÃO"]
        vprog = sum(v.values()) - v["PENDENTE"] - v["SEM INFORMAÇÃO"]
        feito = c["NO PRAZO"] + c["COM ATRASO"] + c["DATA A CONFIRMAR"]
        out.append({
            "chave": k, "prog": prog, "pend": c["PENDENTE"], **{s: c[s] for s in pg.STATUS},
            "atrasadas": c["NÃO REALIZADA"] + c["COM ATRASO"],
            "ppc": c["NO PRAZO"] / prog if prog else None, "feito": feito / prog if prog else None,
            "vol_prog": vprog, "vol_prazo": v["NO PRAZO"],
            "vol_feito": v["NO PRAZO"] + v["COM ATRASO"] + v["DATA A CONFIRMAR"],
            "ppc_vol": v["NO PRAZO"] / vprog if vprog else None,
            "atraso_medio": sum(x["desv"]) / len(x["desv"]) if x["desv"] else None,
            "atraso_max": max(x["desv"]) if x["desv"] else None})
    return out


def vazio() -> dict:
    """Agregado zerado (recorte sem peças)."""
    return {"chave": "t", "prog": 0, "pend": 0, **{s: 0 for s in pg.STATUS}, "atrasadas": 0, "ppc": None, "feito": None,
            "vol_prog": 0.0, "vol_prazo": 0.0, "vol_feito": 0.0, "ppc_vol": None, "atraso_medio": None, "atraso_max": None}


def _causa(l) -> str:
    return (l.ap.causa if l.ap and l.ap.causa else SEM_CAUSA)


def analisar(ls, ls_anterior=None) -> dict:
    """Tudo o que o relatório mostra. ls já recortado por obra/nave/produto."""
    medidas = [l for l in ls if l.status not in ("PENDENTE", "SEM INFORMAÇÃO")]
    g = grupo(ls, lambda l: "t")
    tot = g[0] if g else None
    ant = None
    if ls_anterior:
        a = grupo(ls_anterior, lambda l: "t")
        ant = a[0] if a and a[0]["prog"] else None
    por_obra = sorted(grupo(ls, lambda l: l.item.obra or "-"), key=lambda a: -a["prog"])
    por_produto = sorted(grupo(ls, lambda l: l.item.produto or "-"), key=lambda a: -a["prog"])
    por_nave = sorted(grupo(ls, lambda l: l.item.nave or "-"), key=lambda a: a["chave"])
    por_dia = sorted(grupo(ls, lambda l: l.item.data), key=lambda a: a["chave"])
    por_forma = sorted([a for a in grupo(ls, lambda l: l.item.forma or "-") if a["prog"] >= 3],
                       key=lambda a: (a["ppc"] if a["ppc"] is not None else 1, -a["prog"]))
    obra_prod = []
    for o in por_obra:
        filhos = sorted(grupo([l for l in ls if (l.item.obra or "-") == o["chave"]], lambda l: l.item.produto or "-"),
                        key=lambda a: -a["prog"])
        obra_prod.append({"obra": o, "produtos": filhos})

    atras = [l for l in medidas if l.status in ATRASADAS]
    n_atras = sum(l.qtd for l in atras)
    causas = Counter()
    for l in atras:
        causas[_causa(l)] += l.qtd
    pareto, acum = [], 0
    for c, n in sorted(causas.items(), key=lambda kv: (kv[0] == SEM_CAUSA, -kv[1])):
        acum += n
        pareto.append({"causa": c, "n": n, "pct": n / n_atras if n_atras else 0, "acum": acum / n_atras if n_atras else 0,
                       "obras": Counter(l.item.obra for l in atras if _causa(l) == c).most_common(3)})
    justificadas = sum(l.qtd for l in atras if l.ap and (l.ap.causa or l.ap.comentario or l.ap.ocorrido))

    # plano de ação: ações registradas, agrupadas por ação + responsável
    acoes: dict = defaultdict(lambda: {"n": 0, "obras": set(), "causas": set()})
    for l in atras:
        if l.ap and l.ap.acao:
            k = (l.ap.acao.strip(), (l.ap.responsavel or "").strip())
            acoes[k]["n"] += l.qtd
            acoes[k]["obras"].add(l.item.obra)
            acoes[k]["causas"].add(_causa(l))
    plano = sorted(({"acao": k[0], "resp": k[1], "n": v["n"], "obras": sorted(v["obras"]), "causas": sorted(v["causas"])}
                    for k, v in acoes.items()), key=lambda x: -x["n"])

    # detalhe das atrasadas: obra -> tipo de peça -> linhas
    det: dict = defaultdict(lambda: defaultdict(list))
    for l in sorted(atras, key=lambda l: (l.item.obra, l.item.produto, l.item.data, l.item.peca)):
        det[l.item.obra or "-"][l.item.produto or "-"].append(l)
    detalhe = [{"obra": o, "n": sum(l.qtd for ps in g.values() for l in ps),
                "tipos": [{"produto": p, "linhas": ls_, "n": sum(l.qtd for l in ls_)} for p, ls_ in g.items()]}
               for o, g in sorted(det.items(), key=lambda kv: -sum(l.qtd for ps in kv[1].values() for l in ps))]

    return {"tot": tot, "ant": ant, "por_obra": por_obra, "por_produto": por_produto, "por_nave": por_nave,
            "por_dia": por_dia, "por_forma": por_forma, "obra_prod": obra_prod, "pareto": pareto,
            "n_atras": n_atras, "justificadas": justificadas, "plano": plano, "detalhe": detalhe,
            "destaques": destaques(tot, ant, por_obra, por_produto, por_nave, por_dia, pareto, n_atras, justificadas)}


def _n(v, d=1) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dias(v) -> str:
    return f"{_n(v, 1) if isinstance(v, float) and v != int(v) else int(v)} dia{'s' if v != 1 else ''}"


def _p(v):
    return f"{v:.0%}" if v is not None else "-"


def destaques(tot, ant, por_obra, por_produto, por_nave, por_dia, pareto, n_atras, justificadas) -> list[str]:
    """Frases de leitura rápida, só com fatos do próprio recorte."""
    if not tot or not tot["prog"]:
        return ["Nenhuma peça programada com data vencida no período."]
    out = [f"{tot['prog']} peças programadas ({_n(tot['vol_prog'])} m³): {tot['NO PRAZO']} no prazo, "
           f"{tot['COM ATRASO']} com atraso, {tot['NÃO REALIZADA']} não realizadas. PPC {_p(tot['ppc'])} em peças "
           f"e {_p(tot['ppc_vol'])} em volume."]
    if ant and ant["ppc"] is not None and tot["ppc"] is not None:
        dif = (tot["ppc"] - ant["ppc"]) * 100
        out.append(f"Em relação à semana anterior (PPC {_p(ant['ppc'])}), o PPC "
                   + ("subiu" if dif > 0.5 else "caiu" if dif < -0.5 else "ficou estável")
                   + (f" {abs(dif):.0f} pontos." if abs(dif) > 0.5 else "."))
    cand = [a for a in por_obra if a["prog"] >= MIN_PECAS and a["ppc"] is not None]
    if len(cand) > 1:
        pior = min(cand, key=lambda a: a["ppc"])
        out.append(f"Obra com pior PPC: {pior['chave']} ({_p(pior['ppc'])}, {pior['atrasadas']} de {pior['prog']} peças atrasadas).")
    cand = [a for a in por_produto if a["prog"] >= MIN_PECAS and a["ppc"] is not None]
    if len(cand) > 1:
        pior = min(cand, key=lambda a: a["ppc"])
        out.append(f"Tipo de peça com pior PPC: {pior['chave']} ({_p(pior['ppc'])}, {pior['atrasadas']} de {pior['prog']} peças atrasadas).")
    cand = [a for a in por_nave if a["prog"] >= MIN_PECAS and a["ppc"] is not None]
    if len(cand) > 1:
        pior = min(cand, key=lambda a: a["ppc"])
        out.append(f"Nave com pior PPC: {pior['chave']} ({_p(pior['ppc'])}).")
    cand = [a for a in por_dia if a["prog"] and a["ppc"] is not None]
    if len(cand) > 1:
        pior = min(cand, key=lambda a: a["ppc"])
        out.append(f"Dia mais crítico: {pg.DIAS[pior['chave'].weekday()]} {pior['chave']:%d/%m} ({_p(pior['ppc'])} de PPC, "
                   f"{pior['atrasadas']} atrasadas).")
    reais = [c for c in pareto if c["causa"] != SEM_CAUSA]
    if reais:
        c = reais[0]
        out.append(f"Principal causa de não cumprimento: {c['causa']} ({c['n']} peças, {_p(c['pct'])} das atrasadas).")
    if tot["COM ATRASO"] and tot["atraso_medio"] is not None:
        out.append(f"As {tot['COM ATRASO']} peças feitas com atraso saíram em média {_dias(round(tot['atraso_medio'], 1))} depois "
                   f"do programado (máximo {_dias(tot['atraso_max'])}).")
    if n_atras:
        sem = n_atras - justificadas
        out.append(f"{justificadas} de {n_atras} peças atrasadas têm justificativa registrada ({_p(justificadas / n_atras)})."
                   + (f" Faltam {sem}." if sem else ""))
    return out


# ---------------------------------------------------------------- Excel

def planilha(ctx: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    A = ctx["a"]
    wb = Workbook()
    azul = PatternFill("solid", fgColor="1F3A68")
    fino = Side(style="thin", color="D0D5DD")

    def aba(titulo, cab, linhas, pct_cols=(), larg=None):
        ws = wb.create_sheet(titulo[:31])
        ws.append(cab)
        for l in linhas:
            ws.append(l)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF", name="Arial", size=10)
            c.fill = azul
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.font = Font(name="Arial", size=10)
                c.border = Border(bottom=fino)
                if c.column - 1 in pct_cols and isinstance(c.value, (int, float)):
                    c.number_format = "0%"
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for k, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)), 1):
            w = max(len(str(x.value or "")) for x in col)
            ws.column_dimensions[get_column_letter(k)].width = (larg or {}).get(k, min(max(9, w + 2), 50))
        return ws

    # resumo
    ws = wb.active
    ws.title = "Resumo"
    t = A["tot"] or {}
    linhas = [("Relatório", "Programado x realizado"), ("Período", f"{ctx['ini']:%d/%m/%Y} a {ctx['fim']:%d/%m/%Y}"),
              ("Revisão da programação", ctx["r"].nome), ("Base Plannix", ctx.get("base") or "-"),
              ("Filtros", ctx["filtros_txt"] or "nenhum"), ("", ""),
              ("Programadas (peças)", t.get("prog", 0)), ("No prazo", t.get("NO PRAZO", 0)), ("Com atraso", t.get("COM ATRASO", 0)),
              ("Data a confirmar", t.get("DATA A CONFIRMAR", 0)), ("Não realizadas", t.get("NÃO REALIZADA", 0)),
              ("PPC (peças)", t.get("ppc")), ("Realizado (peças)", t.get("feito")),
              ("Volume programado (m³)", round(t.get("vol_prog", 0), 2)), ("Volume no prazo (m³)", round(t.get("vol_prazo", 0), 2)),
              ("PPC (volume)", t.get("ppc_vol")), ("Atraso médio das feitas com atraso (dias)", t.get("atraso_medio")),
              ("PPC da semana anterior", (A["ant"] or {}).get("ppc")), ("", ""), ("Destaques", "")]
    for k, v in linhas:
        ws.append([k, v])
    for d in A["destaques"]:
        ws.append(["", d])
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 110
    for row in ws.iter_rows():
        row[0].font = Font(bold=True, name="Arial", size=10)
        row[1].font = Font(name="Arial", size=10)
        if row[0].value and str(row[0].value).startswith(("PPC", "Realizado")) and isinstance(row[1].value, float):
            row[1].number_format = "0%"
    ws["A1"].font = Font(bold=True, name="Arial", size=14)

    cab = ["{}", "Programadas", "No prazo", "Com atraso", "Data a confirmar", "Não realizadas", "PPC", "Realizado",
           "Vol. programado (m³)", "Vol. no prazo (m³)", "PPC volume", "Atraso médio (dias)"]

    def lin(a, nome=None):
        return [nome if nome is not None else a["chave"], a["prog"], a["NO PRAZO"], a["COM ATRASO"], a["DATA A CONFIRMAR"],
                a["NÃO REALIZADA"], a["ppc"], a["feito"], round(a["vol_prog"], 2), round(a["vol_prazo"], 2), a["ppc_vol"],
                round(a["atraso_medio"], 1) if a["atraso_medio"] is not None else None]
    pcts = (6, 7, 10)
    aba("Por obra", [cab[0].format("Obra")] + cab[1:], [lin(a) for a in A["por_obra"]], pcts)
    aba("Por tipo de peça", [cab[0].format("Tipo de peça")] + cab[1:], [lin(a) for a in A["por_produto"]], pcts)
    aba("Obra x tipo", ["Obra", "Tipo de peça"] + cab[1:],
        [[g["obra"]["chave"]] + lin(p) for g in A["obra_prod"] for p in g["produtos"]], tuple(x + 1 for x in pcts))
    aba("Por nave", [cab[0].format("Nave")] + cab[1:], [lin(a) for a in A["por_nave"]], pcts)
    aba("Por dia", [cab[0].format("Dia")] + cab[1:],
        [lin(a, f"{pg.DIAS[a['chave'].weekday()]} {a['chave']:%d/%m}") for a in A["por_dia"]], pcts)
    aba("Por fôrma", [cab[0].format("Fôrma")] + cab[1:], [lin(a) for a in A["por_forma"]], pcts)
    aba("Causas (Pareto)", ["Causa", "Peças", "% das atrasadas", "% acumulado", "Obras mais afetadas"],
        [[c["causa"], c["n"], c["pct"], c["acum"], ", ".join(f"{o} ({n})" for o, n in c["obras"])] for c in A["pareto"]], (2, 3))
    aba("Plano de ação", ["Ação", "Responsável", "Peças", "Obras", "Causas"],
        [[p["acao"], p["resp"], p["n"], ", ".join(p["obras"]), ", ".join(p["causas"])] for p in A["plano"]])
    det = []
    for o in A["detalhe"]:
        for tp in o["tipos"]:
            for l in tp["linhas"]:
                i, a = l.item, l.ap
                det.append([o["obra"], tp["produto"], i.data, pg.DIAS[i.data.weekday()], i.nave, i.forma, i.peca, i.codigo,
                            i.secao, l.qtd, round(_vol(l), 3), l.status, l.data_fab, l.desvio, (a.causa if a else "") or SEM_CAUSA,
                            a.acao if a else "", a.responsavel if a else "", (a.comentario or a.ocorrido) if a else ""])
    ws = aba("Atrasadas (justificativas)", ["Obra", "Tipo de peça", "Data programada", "Dia", "Nave", "Fôrma", "Peça", "Código",
                                            "Seção", "Qtd", "Volume (m³)", "Status", "Data de fabricação", "Atraso (dias)",
                                            "Causa", "Ação", "Responsável", "Comentário"], det,
             larg={16: 40, 18: 50})
    for row in ws.iter_rows(min_row=2):
        for k in (2, 12):
            if row[k].value:
                row[k].number_format = "DD/MM/YYYY"
    todas = [[l.item.data, l.item.nave, l.item.forma, l.item.obra, l.item.produto, l.item.peca, l.item.codigo, l.item.secao,
              l.qtd, round(_vol(l), 3), l.status, l.data_fab, l.desvio, (l.ap.causa if l.ap else "") or "",
              (l.ap.comentario or l.ap.ocorrido) if l.ap else ""] for l in ctx["todas"]]
    ws = aba("Base (todas as linhas)", ["Data programada", "Nave", "Fôrma", "Obra", "Tipo de peça", "Peça", "Código", "Seção",
                                        "Qtd", "Volume (m³)", "Status", "Data de fabricação", "Atraso (dias)", "Causa", "Comentário"], todas)
    for row in ws.iter_rows(min_row=2):
        for k in (0, 11):
            if row[k].value:
                row[k].number_format = "DD/MM/YYYY"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- o que ficou para trás

def pendencias(ls, ordem: str = "tipo", hoje: date | None = None) -> dict:
    """Peças não realizadas do recorte, agrupadas por tipo de peça -> obra (ou obra -> tipo)."""
    hoje = hoje or date.today()
    nao = [l for l in ls if l.status == "NÃO REALIZADA"]
    k1, k2 = ((lambda l: l.item.produto or "-"), (lambda l: l.item.obra or "-"))
    if ordem == "obra":
        k1, k2 = k2, k1
    g: dict = defaultdict(lambda: defaultdict(list))
    for l in nao:
        g[k1(l)][k2(l)].append(l)

    def soma(ls_):
        return sum(l.qtd for l in ls_), sum(_vol(l) for l in ls_)

    grupos = []
    for a, sub in g.items():
        filhos = []
        for b, ls_ in sub.items():
            ls_.sort(key=lambda l: (l.item.data, l.item.forma, l.item.peca))
            n, v = soma(ls_)
            filhos.append({"nome": b, "linhas": ls_, "n": n, "vol": v, "mais_antiga": ls_[0].item.data})
        filhos.sort(key=lambda x: -x["n"])
        n = sum(f["n"] for f in filhos)
        grupos.append({"nome": a, "filhos": filhos, "n": n, "vol": sum(f["vol"] for f in filhos)})
    grupos.sort(key=lambda x: -x["n"])
    tipos = sorted({l.item.produto or "-" for l in nao})
    obras = sorted({l.item.obra or "-" for l in nao})
    mat = defaultdict(lambda: [0, 0.0])
    for l in nao:
        m = mat[(l.item.obra or "-", l.item.produto or "-")]
        m[0] += l.qtd
        m[1] += _vol(l)
    sem_causa = sum(l.qtd for l in nao if not (l.ap and l.ap.causa))
    n, v = soma(nao)
    return {"grupos": grupos, "n": n, "vol": v, "tipos": tipos, "obras": obras, "mat": dict(mat), "ordem": ordem,
            "sem_causa": sem_causa, "dias": lambda l: (hoje - l.item.data).days,
            "por_obra": {o: sum(l.qtd for l in nao if (l.item.obra or "-") == o) for o in obras},
            "por_tipo": {t: sum(l.qtd for l in nao if (l.item.produto or "-") == t) for t in tipos}}


# ---------------------------------------------------------------- tabela no layout da planilha de programação

def tabela_programacao(ctx: dict) -> bytes:
    """Programado x realizado no mesmo layout da planilha de programação (abas por nave, uma linha por peça,
    X no dia da concretagem), com status, atraso e justificativas. Para entregar à produção."""
    import io
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ini, fim = ctx["ini"], ctx["fim"]
    dias = [ini + timedelta(days=k) for k in range((fim - ini).days + 1)][:14]
    hoje = date.today()
    AZUL = "1F3864"
    fino = Side(style="thin", color="BFBFBF")
    borda = Border(left=fino, right=fino, top=fino, bottom=fino)
    f_cab = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    f_txt = Font(name="Calibri", size=10)
    f_neg = Font(name="Calibri", size=10, bold=True)
    centro = Alignment(horizontal="center", vertical="center", wrap_text=True)
    esq = Alignment(horizontal="left", vertical="center", wrap_text=True)
    COR = {"NO PRAZO": "E2F0D9", "COM ATRASO": "FFF2CC", "DATA A CONFIRMAR": "DDEBF7", "NÃO REALIZADA": "F8CBAD",
           "PENDENTE": "FFFFFF", "SEM INFORMAÇÃO": "F2F2F2"}
    ROT = {"NO PRAZO": "No prazo", "COM ATRASO": "Com atraso", "DATA A CONFIRMAR": "Data a confirmar",
           "NÃO REALIZADA": "Não realizada", "PENDENTE": "A realizar", "SEM INFORMAÇÃO": "Sem informação"}

    cab = (["DATA", "DIA", "FÔRMA", "OBRA", "PEÇA", "CÓDIGO", "PRODUTO", "SEÇÃO", "COMP. (m)", "QTD", "VOL. (m³)", "TIPO",
            "OBSERVAÇÃO", "REALIZADO", "STATUS", "DATA FABRIC."]
           + [f"{pg.DIAS[d.weekday()]}\n{d:%d/%m}" for d in dias]
           + ["ATRASO (dias)", "CAUSA", "O QUE HOUVE", "AÇÃO", "RESPONSÁVEL", "DESENHO (AUTODOC)"])
    larg = [10, 6, 22, 12, 17, 12, 11, 10, 9, 6, 9, 9, 28, 10, 14, 11] + [7] * len(dias) + [9, 18, 34, 28, 16, 22]
    c_status = 15
    c_dia0 = 17

    wb = Workbook()
    ws0 = wb.active
    ws0.title = "RESUMO"

    def aba(titulo, linhas):
        ws = wb.create_sheet(titulo)
        ws["A1"] = f"PROGRAMADO X REALIZADO | {titulo.replace('N1 + N3', 'NAVES 1 E 3').replace('N2', 'NAVE 2')} | {ini:%d/%m} a {fim:%d/%m/%Y}"
        ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=AZUL)
        ws["A2"] = (f"Programação: {ctx['r'].nome} · Base Plannix: {ctx.get('base') or '-'}"
                    + (f" · Recorte: {ctx['filtros_txt']}" if ctx.get("filtros_txt") else ""))
        ws["A2"].font = Font(name="Calibri", size=9, italic=True, color="595959")
        for k, t in enumerate(cab, 1):
            c = ws.cell(3, k, t)
            c.font, c.alignment, c.border = f_cab, centro, borda
            c.fill = PatternFill("solid", fgColor=AZUL)
            ws.column_dimensions[get_column_letter(k)].width = larg[k - 1]
        ws.row_dimensions[3].height = 30
        r = 4
        for l in linhas:
            i, a = l.item, l.ap
            realizado = "S" if l.status in ("NO PRAZO", "COM ATRASO", "DATA A CONFIRMAR") else ("N" if l.status == "NÃO REALIZADA" else "")
            atraso = l.desvio if l.status == "COM ATRASO" else ((hoje - i.data).days if l.status == "NÃO REALIZADA" else None)
            vals = ([i.data, pg.DIAS[i.data.weekday()], i.forma, i.obra, i.peca, i.codigo or "", i.produto, i.secao,
                     i.comprimento, l.qtd, round(_vol(l), 3) if i.volume else None, i.tipo, i.obs, realizado, ROT[l.status],
                     l.data_fab]
                    + ["X" if l.data_fab == d else ("•" if i.data == d and l.data_fab != d else None) for d in dias]
                    + [atraso, a.causa if a else "", (a.comentario or a.ocorrido) if a else "", a.acao if a else "",
                       a.responsavel if a else "",
                       (f"{l.ad['status']} R{l.ad['rev']}" if getattr(l, "ad", None) else "")])
            cor = PatternFill("solid", fgColor=COR[l.status])
            for k, v in enumerate(vals, 1):
                c = ws.cell(r, k, v)
                c.font, c.border, c.fill = f_txt, borda, cor
                c.alignment = esq if k in (3, 13, c_dia0 + len(dias) + 1, c_dia0 + len(dias) + 2, c_dia0 + len(dias) + 3) else centro
            ws.cell(r, 1).number_format = "dd/mm/yy"
            ws.cell(r, 16).number_format = "dd/mm/yy"
            ws.cell(r, 9).number_format = "0.00"
            ws.cell(r, 11).number_format = "0.000"
            ws.cell(r, 5).font = f_neg
            ws.cell(r, c_status).font = f_neg
            if getattr(l, "ad", None) and l.ad["bloqueado"]:
                ws.cell(r, len(vals)).font = Font(name="Calibri", size=10, bold=True, color="C00000")
            for k, d in enumerate(dias):
                c = ws.cell(r, c_dia0 + k)
                if c.value == "X":
                    c.font = Font(name="Calibri", size=11, bold=True, color="375623" if l.data_fab <= i.data else "9C5700")
                elif c.value == "•":
                    c.font = Font(name="Calibri", size=11, bold=True, color="C00000" if l.status == "NÃO REALIZADA" else "7F7F7F")
            ws.row_dimensions[r].height = 18
            r += 1
        ws.freeze_panes = "F4"
        ws.auto_filter.ref = f"A3:{get_column_letter(len(cab))}{max(r - 1, 3)}"
        ws.sheet_view.zoomScale = 85
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "3:3"
        # legenda
        r += 1
        ws.cell(r, 1, "Legenda:").font = f_neg
        for k, (s, nome) in enumerate([("NO PRAZO", "No prazo"), ("COM ATRASO", "Com atraso"), ("NÃO REALIZADA", "Não realizada"),
                                       ("DATA A CONFIRMAR", "Data a confirmar"), ("PENDENTE", "A realizar")]):
            c = ws.cell(r, 3 + k * 2, nome)
            c.fill, c.font, c.border, c.alignment = PatternFill("solid", fgColor=COR[s]), f_txt, borda, centro
        ws.cell(r + 1, 1, "X = dia da concretagem (Plannix ou informada pelo PCP)   • = dia programado em que a peça não foi concretada").font = Font(name="Calibri", size=9, italic=True)
        return ws

    ord_ = lambda l: (l.item.data, l.item.forma, l.item.obra, l.item.peca)
    ls = [l for l in ctx["todas"]]
    grupos = [("N1 + N3", sorted([l for l in ls if l.item.nave != "N2"], key=ord_)),
              ("N2", sorted([l for l in ls if l.item.nave == "N2"], key=ord_))]
    for nome, linhas in grupos:
        if linhas:
            aba(nome, linhas)

    # resumo por dia e por nave (mesmas regras do PPC)
    ws = ws0
    ws["A1"] = f"PROGRAMADO X REALIZADO | {ini:%d/%m} a {fim:%d/%m/%Y}"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=AZUL)
    ws["A2"] = "PPC = no prazo ÷ programadas (só dias já vencidos). Realizado = no prazo + com atraso + data a confirmar."
    ws["A2"].font = Font(name="Calibri", size=9, italic=True, color="595959")
    cab_r = ["", "Programadas", "No prazo", "Com atraso", "Data a confirmar", "Não realizadas", "A realizar", "PPC", "Realizado"]

    def bloco(r0, titulo, ags, rot):
        ws.cell(r0, 1, titulo).font = Font(name="Calibri", size=11, bold=True, color=AZUL)
        for k, t in enumerate(cab_r, 1):
            c = ws.cell(r0 + 1, k, t if k > 1 else rot)
            c.font, c.alignment, c.border, c.fill = f_cab, centro, borda, PatternFill("solid", fgColor=AZUL)
        r = r0 + 2
        for a in ags:
            nome = (f"{pg.DIAS[a['chave'].weekday()]} {a['chave']:%d/%m}" if isinstance(a["chave"], date)
                    else {"N1": "Nave 1", "N2": "Nave 2", "N3": "Nave 3"}.get(a["chave"], a["chave"]))
            vals = [nome, a["prog"], a["NO PRAZO"], a["COM ATRASO"], a["DATA A CONFIRMAR"], a["NÃO REALIZADA"], a["pend"], a["ppc"], a["feito"]]
            for k, v in enumerate(vals, 1):
                c = ws.cell(r, k, v)
                c.font, c.border, c.alignment = (f_neg if titulo.startswith("Total") else f_txt), borda, centro
                if k >= 8 and v is not None:
                    c.number_format = "0%"
            r += 1
        return r + 1

    r = 4
    r = bloco(r, "Por dia", sorted(grupo(ls, lambda l: l.item.data), key=lambda a: a["chave"]), "Dia")
    r = bloco(r, "Por nave", sorted(grupo(ls, lambda l: l.item.nave or "-"), key=lambda a: a["chave"]), "Nave")
    r = bloco(r, "Por obra", sorted(grupo(ls, lambda l: l.item.obra or "-"), key=lambda a: -a["prog"]), "Obra")
    tot = grupo(ls, lambda l: "Total")
    if tot:
        bloco(r, "Total do período", tot, "")
    ws.column_dimensions["A"].width = 18
    for col in "BCDEFGHI":
        ws.column_dimensions[col].width = 13
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
