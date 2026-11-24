"""Ocupação das fôrmas dia a dia a partir de uma revisão da programação (vigente ou rascunho).

Linear: comprimento das peças + 0,20 m de folga entre peças (laje sem folga) ÷ comprimento útil.
Unidade: peças ÷ capacidade do dia. Pista: as duas alternam; no dia sem laje programada a pista aparece como
"alterna" (não conta como ociosa). Fôrma parada por premissa, fora da vigência ou na folga do ritmo também não conta."""
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import formas as fm
from . import premissas as pm
from .models import Forma, ItemProg, RevisaoProg
from .planejar import capacidade
from .status import chave_natural

FOLGA = 0.20
NAVES = {"N1": "Nave 1", "N2": "Nave 2", "N3": "Nave 3"}


@dataclass
class Cel:
    cap: float = 0.0
    uso: float = 0.0
    n: int = 0
    vol: float = 0.0
    motivo: str = ""                    # sem capacidade no dia: parada, alterna, ritmo, fora de vigência
    pecas: list = field(default_factory=list)
    secoes: set = field(default_factory=set)

    @property
    def pct(self) -> float | None:
        return self.uso / self.cap if self.cap else None


def _usos(f: Forma, its: list[ItemProg]) -> float:
    if f.tipo == "unidade":
        return float(sum(i.qtd or 1 for i in its))
    total, n = 0.0, 0
    for i in its:
        for _ in range(i.qtd or 1):
            laje = fm._norm(i.produto) == "LAJE"
            total += (i.comprimento or 0) + (FOLGA if n and not laje and f.tipo == "linear" else 0)
            n += 1
    return total


def calcular(db: Session, rev: RevisaoProg, ini: date, fim: date) -> dict:
    formas = [f for f in db.scalars(select(Forma)) if f.situacao != "desativada"]
    formas.sort(key=lambda f: (f.nave, 0 if f.tipo != "pista" else 1, chave_natural(f.nome)))
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.data >= ini, ItemProg.data <= fim)))
    dias = [ini + timedelta(days=k) for k in range((fim - ini).days + 1)]
    dias = [d for d in dias if d.weekday() != 6 and (d.weekday() != 5 or any(i.data == d for i in itens))]
    prs = pm.ativas(db)
    por: dict[tuple, list] = defaultdict(list)
    sem_forma = []
    for i in itens:
        f = next((f for f in formas if i.forma and fm.item_e_da_forma(i.forma, f)), None)
        (por[(f.id, i.data)] if f else sem_forma).append(i)
    pistas = [f for f in formas if f.tipo == "pista"]
    cel: dict[tuple, Cel] = {}
    for f in formas:
        for d in dias:
            its = por.get((f.id, d), [])
            c = Cel()
            cap = capacidade(f, d)
            if pm.forma_parada(prs, f.codigo, d):
                c.motivo = "parada"
            elif not f.vigente(d):
                c.motivo = "fora de vigência"
            elif not cap:
                c.motivo = "folga do ritmo" if f.ritmo_dias > 1 else "sem capacidade"
            elif f.tipo == "pista" and len(pistas) > 1 and not its and any(por.get((g.id, d)) for g in pistas if g.id != f.id):
                c.motivo = "alterna"
            else:
                c.cap = cap
            c.uso = _usos(f, its)
            c.n = sum(i.qtd or 1 for i in its)
            c.vol = sum(i.volume or 0 for i in its)
            c.pecas = sorted({f"{i.obra} {i.peca}" + (f" ×{sum(j.qtd or 1 for j in its if j.obra == i.obra and j.peca == i.peca)}") for i in its})
            c.secoes = {i.secao for i in its if i.secao}
            if its and not c.cap:
                c.cap = cap or (f.comprimento or 1 if f.tipo != "unidade" else f.capacidade or 1)   # programado em dia sem capacidade: mostra mesmo assim
            cel[(f.id, d)] = c
    # resumos
    linhas_forma = []
    for f in formas:
        cs = [cel[(f.id, d)] for d in dias if cel[(f.id, d)].cap]
        cap = sum(c.cap for c in cs)
        uso = sum(c.uso for c in cs)
        dias_uso = sum(1 for c in cs if c.n)
        linhas_forma.append(dict(f=f, media=uso / cap if cap else None, dias_disp=len(cs), dias_uso=dias_uso,
                                 ociosos=len(cs) - dias_uso, acima=sum(1 for c in cs if c.pct and c.pct > 1.0001),
                                 n=sum(c.n for c in cs), vol=sum(c.vol for c in cs)))
    nave_dia = {}
    for nv in NAVES:
        fs = [f for f in formas if f.nave == nv]
        for d in dias:
            cs = [cel[(f.id, d)] for f in fs]
            disp = [c for c in cs if c.cap]
            # média simples das fôrmas disponíveis no dia (cada fôrma pesa igual)
            pcts = [min(c.pct, 1.5) for c in disp if c.pct is not None]
            nave_dia[(nv, d)] = dict(pct=sum(pcts) / len(pcts) if pcts else None, n=sum(c.n for c in cs),
                                     vol=sum(c.vol for c in cs), ociosas=sum(1 for c in disp if not c.n), disp=len(disp))
    dia_tot = {d: dict(n=sum(nave_dia[(nv, d)]["n"] for nv in NAVES), vol=sum(nave_dia[(nv, d)]["vol"] for nv in NAVES)) for d in dias}
    medias = [x["media"] for x in linhas_forma if x["media"] is not None]
    return dict(formas=formas, dias=dias, cel=cel, linhas_forma=linhas_forma, nave_dia=nave_dia, dia_tot=dia_tot,
                sem_forma=sem_forma, media_geral=sum(medias) / len(medias) if medias else None,
                ociosas=[x for x in linhas_forma if x["media"] is not None and x["media"] < 0.3],
                acima=[(f, d, cel[(f.id, d)]) for f in formas for d in dias if (cel[(f.id, d)].pct or 0) > 1.0001])


def cor(p: float | None) -> str:
    if p is None:
        return "#ffffff"
    if p > 1.0001:
        return "#f8cbad"
    if p == 0:
        return "#f2f2f2"
    # azul claro -> azul forte
    t = max(0.0, min(1.0, p))
    r = int(255 - (255 - 142) * t)
    g = int(255 - (255 - 169) * t)
    b = int(255 - (255 - 219) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def planilha(ctx: dict, rev: RevisaoProg) -> bytes:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    from openpyxl.worksheet.properties import PageSetupProperties
    from .programacao import DIAS
    FN = "Calibri"
    F10, F10B = Font(name=FN, size=10), Font(name=FN, size=10, bold=True)
    FH = Font(name=FN, size=10, bold=True, color="FFFFFF")
    FT = Font(name=FN, size=14, bold=True, color="1F3864")
    FS = Font(name=FN, size=10, color="595959")
    HDR, GRP = PatternFill("solid", fgColor="1F3864"), PatternFill("solid", fgColor="D9E1F2")
    thin = Side(style="thin", color="BFBFBF")
    BD = Border(left=thin, right=thin, top=thin, bottom=thin)
    CEN = Alignment(horizontal="center", vertical="center", wrap_text=True)
    dias, formas, cel = ctx["dias"], ctx["formas"], ctx["cel"]
    per = f"{dias[0]:%d/%m/%Y} a {dias[-1]:%d/%m/%Y}" if dias else ""

    def cab(ws, r, cols, c0=1):
        for k, t in enumerate(cols):
            x = ws.cell(r, c0 + k, t)
            x.font, x.fill, x.border, x.alignment = FH, HDR, BD, CEN
        ws.row_dimensions[r].height = 30

    def setup(ws, orient="landscape"):
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation = orient
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.oddFooter.left.text = "PCP - Ocupação das fôrmas " + per
        ws.oddFooter.right.text = "Página &P de &N"

    wb = Workbook()
    ws = wb.active
    ws.title = "OCUPAÇÃO"
    ws["A1"] = "OCUPAÇÃO DAS FÔRMAS POR DIA"
    ws["A1"].font = FT
    ws["A2"] = f"Período: {per}   |   {rev.nome}   |   PCP"
    ws["A2"].font = FS
    ws["A3"] = "% = comprimento programado (com 0,20 m de folga) ÷ comprimento útil; fôrma por unidade = peças ÷ capacidade. " \
               "'alt' = pista do outro dia; 'par' = parada; vazio = sem capacidade."
    ws["A3"].font = FS
    cab(ws, 5, ["FÔRMA", "CAPAC.", "UN."] + [f"{DIAS[d.weekday()]}\n{d:%d/%m}" for d in dias] + ["MÉDIA", "DIAS OCIOSOS"])
    r, nav = 6, None
    for f, lf in zip(formas, ctx["linhas_forma"]):
        if f.nave != nav:
            nav = f.nave
            ws.cell(r, 1, NAVES.get(nav, nav).upper()).font = F10B
            for k in range(len(dias) + 5):
                ws.cell(r, 1 + k).fill, ws.cell(r, 1 + k).border = GRP, BD
            r += 1
        x = ws.cell(r, 1, f.nome_completo)
        x.font, x.border = F10, BD
        cap = f.capacidade if f.tipo == "unidade" else (f.comprimento or 0) * (f.capacidade or 1)
        for k, v in ((2, cap), (3, "pç/dia" if f.tipo == "unidade" else "m")):
            x = ws.cell(r, k, v)
            x.font, x.border, x.alignment = F10, BD, CEN
        for k, d in enumerate(dias):
            c = cel[(f.id, d)]
            v = c.pct if c.cap else {"alterna": "alt", "parada": "par"}.get(c.motivo)
            x = ws.cell(r, 4 + k, v)
            x.font, x.border, x.alignment = F10, BD, CEN
            if isinstance(v, float):
                x.number_format = '0%'
            elif v:
                x.font = Font(name=FN, size=9, italic=True, color="808080")
        x = ws.cell(r, 4 + len(dias), lf["media"])
        x.font, x.border, x.alignment, x.number_format = F10B, BD, CEN, "0%"
        x = ws.cell(r, 5 + len(dias), lf["ociosos"] or None)
        x.font, x.border, x.alignment = F10, BD, CEN
        r += 1
    rng = f"D7:{L(4 + len(dias))}{r - 1}"
    ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["1.0001"], fill=PatternFill("solid", fgColor="F8CBAD"),
                                                  font=Font(name=FN, size=10, bold=True, color="9C0006")))
    ws.conditional_formatting.add(rng, ColorScaleRule(start_type="num", start_value=0, start_color="FFFFFF", end_type="num",
                                                      end_value=1, end_color="8EA9DB"))
    r += 1
    ws.cell(r, 1, "POR NAVE (média das fôrmas disponíveis no dia · peças · m³)").font = Font(name=FN, size=11, bold=True, color="1F3864")
    r += 1
    cab(ws, r, ["NAVE", "", ""] + [f"{DIAS[d.weekday()]}\n{d:%d/%m}" for d in dias])
    for nv, nome in NAVES.items():
        for lab, chave, fmt in (("% médio", "pct", "0%"), ("peças", "n", "0"), ("m³", "vol", "0.0")):
            r += 1
            x = ws.cell(r, 1, f"{nome} · {lab}")
            x.font, x.border = F10B if lab == "% médio" else F10, BD
            for k, d in enumerate(dias):
                x = ws.cell(r, 4 + k, ctx["nave_dia"][(nv, d)][chave] or None)
                x.font, x.border, x.alignment, x.number_format = F10, BD, CEN, fmt
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 7
    for k in range(len(dias) + 2):
        ws.column_dimensions[L(4 + k)].width = 9
    ws.freeze_panes = "D6"
    setup(ws)

    wd = wb.create_sheet("DETALHE")
    wd["A1"] = "PEÇAS POR FÔRMA E DIA"
    wd["A1"].font = FT
    wd["A2"] = f"Período: {per}   |   {rev.nome}"
    wd["A2"].font = FS
    cab(wd, 4, ["DIA", "DATA", "NAVE", "FÔRMA", "QTD", "USO", "CAPACIDADE", "UN.", "%", "VOL. (m³)", "SEÇÕES", "PEÇAS PROGRAMADAS"])
    r = 5
    for d in dias:
        for f in formas:
            c = cel[(f.id, d)]
            if not c.n and not c.cap:
                continue
            vals = [DIAS[d.weekday()], d, f.nave, f.nome_completo, c.n or None, round(c.uso, 2) if c.uso else None,
                    c.cap or None, "pç" if f.tipo == "unidade" else "m", c.pct, round(c.vol, 2) if c.vol else None,
                    ", ".join(sorted(c.secoes)) or None, "; ".join(c.pecas) or ("OCIOSA" if c.cap else c.motivo)]
            for k, v in enumerate(vals, 1):
                x = wd.cell(r, k, v)
                x.font, x.border = F10, BD
            wd.cell(r, 2).number_format = "dd/mm/yy"
            wd.cell(r, 9).number_format = "0%"
            if c.pct is not None and c.pct > 1.0001:
                wd.cell(r, 9).fill = PatternFill("solid", fgColor="F8CBAD")
            elif c.cap and not c.n:
                wd.cell(r, 12).font = Font(name=FN, size=10, bold=True, color="C00000")
            r += 1
    for k, w in enumerate([6, 10, 6, 24, 6, 8, 10, 5, 7, 9, 14, 80], 1):
        wd.column_dimensions[L(k)].width = w
    wd.freeze_panes = "A5"
    wd.auto_filter.ref = f"A4:L{max(5, r - 1)}"
    setup(wd)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
