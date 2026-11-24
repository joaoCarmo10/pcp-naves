"""Planilha do replanejamento no padrão da programação da fábrica (Rev03): RESUMO, N1 + N3, N2, GANTT FÔRMAS,
REMANEJAMENTO, PENDÊNCIAS, FÔRMAS e PREMISSAS. Mesmas colunas da Rev03 (A a AA) e, no fim, aço frouxo, protensão
e aço ativo. Reimportável pelo app (cabeçalho DATA, FÔRMA, OBRA, PEÇA, CÓDIGO...)."""
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.properties import PageSetupProperties
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import formas as fm
from .models import Armacao, Forma, ItemProg, Obra, Peca, RevisaoProg
from .status import chave_natural

FN_ = "Calibri"
F10 = Font(name=FN_, size=10)
F10B = Font(name=FN_, size=10, bold=True)
FH = Font(name=FN_, size=10, bold=True, color="FFFFFF")
FT = Font(name=FN_, size=14, bold=True, color="1F3864")
FS = Font(name=FN_, size=10, color="595959")
FSEC = Font(name=FN_, size=11, bold=True, color="1F3864")
HDR = PatternFill("solid", fgColor="1F3864")
GRP = PatternFill("solid", fgColor="D9E1F2")
ZEB = PatternFill("solid", fgColor="F2F2F2")
thin = Side(style="thin", color="BFBFBF")
BD = Border(left=thin, right=thin, top=thin, bottom=thin)
CEN = Alignment(horizontal="center", vertical="center", wrap_text=True)
MID = Alignment(horizontal="center", vertical="center")
TOP = Alignment(vertical="top", wrap_text=True)
DS = 'CHOOSE(WEEKDAY({c}),"DOM","SEG","TER","QUA","QUI","SEX","SÁB")'
HR, R0 = 3, 4   # cabeçalho e 1ª linha de dados, como na Rev03

# colunas da programação (Rev03 A..AA + aço frouxo, protensão, aço ativo)
PC = ["DATA", "DIA", "FÔRMA", "OBRA", "PEÇA", "CÓDIGO", "PRODUTO", "SEÇÃO", "COMP. (m)", "QTD", "VOL. (m³)", "PAVIMENTO",
      "ARMAÇÃO", "TIPO", "ADAPTAÇÃO", "OBSERVAÇÃO", "REALIZADO", "Realizado", "SEG", "TER", "QUA", "QUI", "SEX", "SAB",
      "O que houve", "Porque", "Plano de ação", "AÇO FROUXO (kg)", "PROTENSÃO", "AÇO ATIVO (kg)"]
PW = [10, 6, 22, 12, 17, 12, 11, 10, 9, 6, 9, 10, 12, 12, 16, 34, 10, 10, 5.4, 5.4, 5.4, 5.4, 5.4, 5.4, 18, 16, 18, 11, 22, 10]
C = {n: L(i) for i, n in enumerate(PC, 1)}     # letra de cada coluna
FILL_TIPO = {"ATRASADA": "FCE4D6", "REMANEJADA": "E4DFEC", "PENDENTE": "FCE4D6", "AVULSA": "E2EFDA", "PRIORIDADE": "FFF2CC"}


def _titulo(ws, t, sub):
    ws["A1"] = t
    ws["A1"].font = FT
    ws["A2"] = sub
    ws["A2"].font = FS
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 90


def _hdr(ws, r, cols, widths=None, c0=1):
    for i, c in enumerate(cols):
        x = ws.cell(r, c0 + i, c)
        x.font, x.fill, x.border, x.alignment = FH, HDR, BD, CEN
    ws.row_dimensions[r].height = 30
    if widths:
        for i, w in enumerate(widths):
            ws.column_dimensions[L(c0 + i)].width = w


def _cell(ws, r, c, v, fmt=None, al=None, font=None, fill=None):
    x = ws.cell(r, c, v)
    x.font, x.border = font or F10, BD
    if fmt:
        x.number_format = fmt
    if al:
        x.alignment = al
    if fill:
        x.fill = fill
    return x


def _imprimir(ws, per, orient="landscape", titulo_linhas=True):
    ws.page_setup.orientation = orient
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    if titulo_linhas:
        ws.print_title_rows = f"{HR}:{HR}"
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    ws.oddFooter.left.text = "PCP - Programação de produção " + per
    ws.oddFooter.right.text = "Página &P de &N"
    ws.oddFooter.left.size = ws.oddFooter.right.size = 8


def _tipo(i: ItemProg) -> str:
    o = i.obs or ""
    if i.tipo == "AVULSA":
        return "AVULSA"
    if "Remanejada" in o:
        return "REMANEJADA"
    if o.startswith("Pendente"):
        return "PENDENTE"
    if "Prioridade" in o:
        return "PRIORIDADE"
    if "Atrasada" in o:
        return "ATRASADA"
    if (i.tipo or "").upper() in ("SEQ. LAJES", "FIXA", "PCP", "REAPROVEITAR"):
        return i.tipo.upper()
    return "PCP"


def _adaptacao(i: ItemProg, f: Forma | None) -> str:
    """Como a Rev03: 'adaptar h 80→60' quando a seção da peça difere da montagem nominal da fôrma."""
    if not f or f.tipo != "linear" or not i.secao or not f.secao:
        return ""
    try:
        pb, ph = [float(x) for x in fm._norm(i.secao).split("X")[:2]]
        fb, fh = [float(x) for x in fm._norm(f.secao).split("X")[:2]]
    except (ValueError, AttributeError):
        return ""
    s = []
    if pb != fb:
        s.append(f"b {fb:g}→{pb:g}")
    if ph != fh:
        s.append(f"h {fh:g}→{ph:g}")
    return ("adaptar " + " e ".join(s)) if s else ""


def gerar(db: Session, rev: RevisaoProg, inicio: date | None = None) -> bytes:
    from .replanejar import protensao_dos_itens
    rel = rev.relatorio or {}
    ini = inicio or rev.inicio
    itens = [i for i in rev.itens if i.data >= ini]
    fim_per = date.fromisoformat(rel["fim"]) if rel.get("fim") else rev.fim
    ult = max([i.data for i in itens] + [ini])
    dias = [ini + timedelta(days=k) for k in range((ult - ini).days + 1)]
    dias = [d for d in dias if d.weekday() != 6]
    per = f"{ini:%d/%m/%Y} a {fim_per:%d/%m/%Y}"
    sub = f"Período: {per}   |   Replanejamento sobre {rel.get('base', rev.nome)}   |   Emissão: {datetime.now():%d/%m/%Y %H:%M}   |   PCP"

    cods = [i.codigo for i in itens if i.codigo]
    pecas = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_(cods)))} if cods else {}
    arm_app = {a.codigo: a.armado_em for a in db.scalars(select(Armacao).where(Armacao.codigo.in_(cods)))} if cods else {}
    prot = protensao_dos_itens(db, itens)
    from . import cadastro as cad
    idx = cad.indice(db, {p.obra_id for p in pecas.values()})
    formas = list(db.scalars(select(Forma).order_by(Forma.nave, Forma.codigo)))
    forma_de = {f.nome_completo: f for f in formas}

    def forma_item(i):
        return forma_de.get(i.forma) or next((f for f in formas if fm.item_e_da_forma(i.forma, f)), None)

    ordem_forma = {f.nome_completo: k for k, f in enumerate(sorted(formas, key=lambda f: (f.nave, chave_natural(f.nome))))}

    wb = Workbook()
    wb.remove(wb.active)

    # ===================== PROGRAMAÇÃO (N1 + N3 e N2)
    frouxo_sem_dado = 0
    for nome, naves, tit, frz in [("N1 + N3", ("N1", "N3"), "PROGRAMAÇÃO DE PRODUÇÃO | NAVES 1 E 3", "E4"),
                                  ("N2", ("N2",), "PROGRAMAÇÃO DE PRODUÇÃO | NAVE 2 (LAJES)", "D4")]:
        ws = wb.create_sheet(nome)
        _titulo(ws, tit, sub)
        _hdr(ws, HR, PC, PW)
        lst = sorted([i for i in itens if i.nave in naves],
                     key=lambda i: (i.data, ordem_forma.get(i.forma, 999), i.forma, i.obra, chave_natural(i.peca), i.codigo or ""))
        r, prev = R0, None
        for i in lst:
            p = pecas.get(i.codigo or "")
            z = ZEB if i.data.toordinal() % 2 == 0 else None
            tp = _tipo(i)
            arm = (p.hora_armacao if p and p.hora_armacao else None) or arm_app.get(i.codigo or "")
            c = cad.de_peca(idx, p) if p else None
            frouxo = (p.peso_aco_frouxo if p and p.peso_aco_frouxo else None) or (c.aco_passivo if c and c.aco_passivo else None)
            if frouxo is None and i.produto not in ("LAJE", "NEW JERSEY") and i.codigo:
                frouxo_sem_dado += 1
            pr = prot.get(i.codigo or "", {})
            v = [i.data, f'=IF(A{r}="","",{DS.format(c=f"A{r}")})', i.forma, i.obra, i.peca, i.codigo or None, i.produto,
                 i.secao or None, i.comprimento or None, i.qtd or 1, round(i.volume, 3) if i.volume else None,
                 (p.item if p and p.item else None), arm, tp, _adaptacao(i, forma_item(i)) or None, i.obs or None,
                 None, None, None, None, None, None, None, None, None, None, None,
                 round(frouxo * (i.qtd or 1), 1) if frouxo else None, pr.get("txt") or None, pr.get("ativo")]
            for j, val in enumerate(v, 1):
                _cell(ws, r, j, val, fill=z)
            ws.cell(r, 1).number_format = "dd/mm/yy"
            ws.cell(r, 6).number_format = "@"
            ws.cell(r, 9).number_format = "0.00"
            ws.cell(r, 11).number_format = "0.000"
            ws.cell(r, 13).number_format = "dd/mm/yy"
            ws.cell(r, 28).number_format = "#,##0.0"
            ws.cell(r, 30).number_format = "#,##0.0"
            for cc in (1, 2, 10, 13, 14, 17, 18, 19, 20, 21, 22, 23, 24):
                ws.cell(r, cc).alignment = MID
            ws.cell(r, 16).alignment = Alignment(vertical="center", wrap_text=False)
            if tp in FILL_TIPO:
                ws.cell(r, 14).fill = PatternFill("solid", fgColor=FILL_TIPO[tp])
            if pr.get("fonte") == "Plannix" and pr.get("txt"):
                ws.cell(r, 29).font = Font(name=FN_, size=10, italic=True, color="595959")
            if arm is None and i.produto not in ("LAJE", "NEW JERSEY", "ESCADA") and tp != "AVULSA":
                ws.cell(r, 13).value = "a armar"
                ws.cell(r, 13).font = Font(name=FN_, size=10, color="C00000")
            if prev and i.data != prev:
                for j in range(1, len(PC) + 1):
                    ws.cell(r, j).border = Border(left=thin, right=thin, bottom=thin, top=Side(style="medium", color="1F3864"))
            prev = i.data
            r += 1
        n = max(r - 1, R0)
        # total do dia no rodapé
        _cell(ws, r + 1, 9, "TOTAL", font=F10B, fill=GRP, al=MID)
        _cell(ws, r + 1, 10, f"=SUM(J{R0}:J{n})", font=F10B, fill=GRP, al=MID)
        _cell(ws, r + 1, 11, f"=SUM(K{R0}:K{n})", fmt="0.0", font=F10B, fill=GRP, al=MID)
        _cell(ws, r + 1, 28, f"=SUM(AB{R0}:AB{n})", fmt="#,##0", font=F10B, fill=GRP, al=MID)
        ws.freeze_panes = frz
        ws.auto_filter.ref = f"A{HR}:{L(len(PC))}{n}"
        ws.conditional_formatting.add(f"Q{R0}:R{n}", CellIsRule(operator="equal", formula=['"S"'], fill=PatternFill("solid", fgColor="C6EFCE")))
        ws.conditional_formatting.add(f"Q{R0}:R{n}", CellIsRule(operator="equal", formula=['"N"'], fill=PatternFill("solid", fgColor="FFC7CE")))
        dv = DataValidation(type="list", formula1='"S,N"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"Q{R0}:R{n + 300}")
        dx = DataValidation(type="list", formula1='"X"', allow_blank=True)
        ws.add_data_validation(dx)
        dx.add(f"S{R0}:X{n + 300}")
        leg = r + 3
        ws.cell(leg, 1, "TIPO: ATRASADA = vinha de antes do início · REMANEJADA = não coube no período e foi para depois · "
                        "PENDENTE = o Plannix aponta como feita, mas não foi · PRIORIDADE = premissa fura-fila · "
                        "AVULSA = peça fora do Plannix · PCP / FIXA / SEQ. LAJES = como na programação base.").font = FS
        ws.cell(leg + 1, 1, "ARMAÇÃO: data da armação (Plannix ou app); \"a armar\" = ainda sem armação, armar no dia anterior. "
                            "AÇO FROUXO: peso do Plannix por peça × QTD (sem o dado do Plannix, o da prancha). "
                            "PROTENSÃO: da prancha; em itálico, o modelo do Plannix. REALIZADO: S ou N no fim do dia; X no dia em que saiu.").font = FS
        _imprimir(ws, per)

    # ===================== GANTT FÔRMAS
    ws = wb.create_sheet("GANTT FÔRMAS")
    _titulo(ws, "OCUPAÇÃO DAS FÔRMAS POR DIA", sub)
    ws["A3"] = "Fôrmas lineares: comprimento programado ÷ comprimento útil. Fôrmas por unidade: peças ÷ capacidade diária. Pista: uma por dia (alternam)."
    ws["A3"].font = FS
    nd, C0 = len(dias), 4

    def sumif(col, crit):
        return "+".join(f"SUMIFS('{s}'!${col}:${col},{crit.format(s=s)})" for s in ("N1 + N3", "N2"))

    def cab_dias(r):
        for k, d in enumerate(dias):
            x = ws.cell(r, C0 + k, d)
            x.number_format = "[$-416]ddd dd/mm"
            x.font, x.fill, x.border, x.alignment = FH, HDR, BD, CEN
        ws.row_dimensions[r].height = 30

    fs_ord = sorted([f for f in formas if f.situacao != "desativada"], key=lambda f: (f.nave, chave_natural(f.nome)))

    def bloco(r0, titulo_, modo):
        ws.cell(r0, 1, titulo_).font = FSEC
        h = r0 + 1
        _hdr(ws, h, ["FÔRMA", "CAPACIDADE", "UN."])
        cab_dias(h)
        r, navant = h + 1, None
        for f in fs_ord:
            if f.nave != navant:
                ws.cell(r, 1, {"N1": "NAVE 1", "N2": "NAVE 2", "N3": "NAVE 3"}.get(f.nave, f.nave)).font = F10B
                for k in range(nd + 3):
                    ws.cell(r, 1 + k).fill, ws.cell(r, 1 + k).border = GRP, BD
                navant, r = f.nave, r + 1
            linear = f.tipo in ("linear", "pista")
            cap = (f.comprimento or 0) * (f.capacidade or 1) if linear else (f.capacidade or 1)
            _cell(ws, r, 1, f.nome_completo)
            _cell(ws, r, 2, cap or None, al=MID, fmt="0.##")
            _cell(ws, r, 3, "m" if linear else "pç/dia", al=MID)
            for k in range(nd):
                col = L(C0 + k)
                crit = "'{s}'!$C:$C,$A" + str(r) + ",'{s}'!$A:$A," + col + "$" + str(h)
                if modo == "%":
                    fml = f"=IFERROR(({sumif('I' if linear else 'J', crit)})/$B{r},0)"
                    fmt = '0%;-0%;""'
                else:
                    fml = "=" + sumif("J", crit)
                    fmt = '0;-0;""'
                _cell(ws, r, C0 + k, fml, fmt=fmt, al=MID)
            r += 1
        return h + 1, r - 1, h

    f1, l1, h1 = bloco(5, "OCUPAÇÃO DA FÔRMA (%)", "%")
    rng = f"{L(C0)}{f1}:{L(C0 + nd - 1)}{l1}"
    ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["1.0001"], fill=PatternFill("solid", fgColor="F8CBAD"),
                                                  font=Font(name=FN_, size=10, bold=True, color="9C0006")))
    ws.conditional_formatting.add(rng, ColorScaleRule(start_type="num", start_value=0, start_color="FFFFFF", end_type="num", end_value=1,
                                                      end_color="8EA9DB"))
    f2, l2, h2 = bloco(l1 + 3, "QUANTIDADE DE PEÇAS", "n")
    r = l2 + 3
    ws.cell(r, 1, "RESUMO DO DIA").font = FSEC
    h3 = r + 1
    _hdr(ws, h3, ["INDICADOR", "", ""])
    ws.merge_cells(start_row=h3, start_column=1, end_row=h3, end_column=3)
    cab_dias(h3)
    linhas = [("Volume Nave 1 (m³)", "N1*", "K"), ("Volume Nave 3 (m³)", "N3*", "K"), ("Lajes Nave 2 (m)", "N2*", "I"),
              ("Aço frouxo (kg)", "*", "AB"), ("Peças programadas", None, "J"), ("Peças realizadas", "S", "J"), ("PPC", "ppc", None)]
    for j, (lab, kind, col_) in enumerate(linhas):
        rr = h3 + 1 + j
        _cell(ws, rr, 1, lab, font=F10B if kind == "ppc" else F10)
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=3)
        for k in range(nd):
            col = L(C0 + k)
            dc = f"{col}${h3}"
            if kind in ("N1*", "N3*", "N2*", "*"):
                fml = "=" + sumif(col_, "'{s}'!$C:$C,\"" + kind + "\",'{s}'!$A:$A," + dc)
                fmt = '#,##0;-#,##0;""' if kind == "*" else '0.0;-0.0;""'
            elif kind is None:
                fml, fmt = "=" + sumif("J", "'{s}'!$A:$A," + dc), "0"
            elif kind == "S":
                fml, fmt = "=" + sumif("J", "'{s}'!$A:$A," + dc + ",'{s}'!$R:$R,\"S\""), '0;-0;""'
            else:
                fml, fmt = f'=IF({col}{rr - 1}=0,"",{col}{rr - 1}/{col}{rr - 2})', "0%"
            _cell(ws, rr, C0 + k, fml, fmt=fmt, al=MID, font=F10B if kind == "ppc" else F10)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 11
    ws.column_dimensions["C"].width = 7
    for k in range(nd):
        ws.column_dimensions[L(C0 + k)].width = 10
    ws.freeze_panes = ws.cell(f1, C0)
    _imprimir(ws, per, titulo_linhas=False)

    # ===================== RESUMO
    wr = wb.create_sheet("RESUMO", 0)
    _titulo(wr, "PROGRAMAÇÃO DE PRODUÇÃO | REPLANEJAMENTO", sub)
    wr["A3"] = ("Prioridade: " + " · ".join(f"{o['prio']} {o['obra']}" for o in rel.get("por_obra", []) if o.get("prio", 99) < 99)
                + ". Programadas, volume e aço calculados nas abas N1 + N3 e N2.")
    wr["A3"].font = FS
    AC = ["OBRA", "PRIORIDADE", "PEÇAS QUE FALTAM", "PROGRAMADAS NO PERÍODO", "ATRASADAS REPOSTAS", "REMANEJADAS APÓS O PERÍODO",
          "FICAM PARA DEPOIS", "SEM FÔRMA", "DESENHO BLOQUEADO", "% DA FILA PROGRAMADA", "VOLUME PROGRAMADO (m³)", "AÇO FROUXO PROGRAMADO (kg)"]
    _hdr(wr, 5, AC, [24, 11, 11, 13, 12, 14, 11, 10, 11, 12, 13, 14])
    rema = defaultdict(int)
    for i in itens:
        if "Remanejada" in (i.obs or ""):
            rema[i.obra] += i.qtd or 1
    r = 6
    obras_lin = list(rel.get("por_obra", []))
    for o in [x for x in {i.obra for i in itens} if x not in {y["obra"] for y in obras_lin}]:
        obras_lin.append({"obra": o, "prio": 99})
    ini_s, fim_s = f"DATE({ini.year},{ini.month},{ini.day})", f"DATE({fim_per.year},{fim_per.month},{fim_per.day})"
    for o in obras_lin:
        crit = f"'{{s}}'!$D:$D,$A{r},'{{s}}'!$A:$A,\">=\"&{ini_s},'{{s}}'!$A:$A,\"<=\"&{fim_s}"
        vals = [o["obra"], o.get("prio") if o.get("prio", 99) < 99 else "-", o.get("faltam") or None,
                "=" + sumif("J", crit), o.get("atrasadas_repostas") or None, rema.get(o["obra"]) or None,
                o.get("ficam_para_depois") or None, o.get("sem_forma") or None, o.get("bloqueadas") or None,
                f'=IF(N(C{r})=0,"",D{r}/C{r})',
                "=" + sumif("K", f"'{{s}}'!$D:$D,$A{r}"), "=" + sumif("AB", f"'{{s}}'!$D:$D,$A{r}")]
        for j, v in enumerate(vals, 1):
            _cell(wr, r, j, v, al=None if j == 1 else MID)
        for j in (3, 4, 5, 6, 7, 8, 9):
            wr.cell(r, j).number_format = '0;-0;"-"'
        wr.cell(r, 10).number_format = "0%"
        wr.cell(r, 11).number_format = "0.0"
        wr.cell(r, 12).number_format = "#,##0"
        r += 1
    _cell(wr, r, 1, "TOTAL", font=F10B, fill=GRP)
    _cell(wr, r, 2, None, fill=GRP)
    for j in range(3, 10):
        _cell(wr, r, j, f"=SUM({L(j)}6:{L(j)}{r - 1})", al=MID, font=F10B, fill=GRP, fmt='0;-0;"-"')
    _cell(wr, r, 10, f'=IF(C{r}=0,"",SUMIF(C6:C{r - 1},">0",D6:D{r - 1})/C{r})', fmt="0%", al=MID, font=F10B, fill=GRP)
    _cell(wr, r, 11, f"=SUM(K6:K{r - 1})", fmt="0.0", al=MID, font=F10B, fill=GRP)
    _cell(wr, r, 12, f"=SUM(L6:L{r - 1})", fmt="#,##0", al=MID, font=F10B, fill=GRP)
    wr.conditional_formatting.add(f"J6:J{r}", DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1, color="8EA9DB"))
    r += 1
    wr.cell(r, 1, "Peças que faltam: todas as não fabricadas das obras ativas que podem entrar (sem as excluídas por premissa ou fora da área).").font = FS
    r += 2
    wr.cell(r, 1, "NÃO ENTRAM NA PROGRAMAÇÃO (PREMISSAS E ESCOPO)").font = FSEC
    r += 1
    _hdr(wr, r, ["MOTIVO", "", "", "PEÇAS"])
    wr.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    for k, v in (rel.get("excluidas") or {}).items():
        r += 1
        _cell(wr, r, 1, k)
        wr.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        _cell(wr, r, 4, v, al=MID)
    r += 2
    wr.cell(r, 1, "NÚMEROS DO REPLANEJAMENTO").font = FSEC
    for t, v in [("Atrasadas reprogramadas", f"{rel.get('atrasadas_repostas', 0)} de {rel.get('atrasadas', 0)}"),
                 ("Peças já programadas que mudaram de dia ou fôrma", len(rel.get("mudaram", []))),
                 ("Remanejadas para depois do período", rel.get("remanejadas", 0)),
                 ("Saíram da programação por já estarem fabricadas", rel.get("ja_feitas", 0)),
                 ("Peças sem dado de aço frouxo (Plannix e prancha)", frouxo_sem_dado)]:
        r += 1
        _cell(wr, r, 1, t)
        wr.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        _cell(wr, r, 4, v, al=MID, font=F10B)
    wr.freeze_panes = "B6"
    _imprimir(wr, per, titulo_linhas=False)

    # ===================== REMANEJAMENTO
    wm = wb.create_sheet("REMANEJAMENTO")
    _titulo(wm, "PEÇAS QUE MUDARAM DE DIA OU DE FÔRMA", sub)
    wm["A3"] = "Em relação à programação base. Dias > 0: foi para depois; < 0: adiantada."
    wm["A3"].font = FS
    _hdr(wm, 5, ["OBRA", "PEÇA", "CÓDIGO", "ANTES", "DEPOIS", "DIAS"], [16, 17, 12, 30, 30, 7])
    for k, x in enumerate(rel.get("mudaram", []), 6):
        for j, v in enumerate([x["obra"], x["peca"], x["codigo"], x["antes"], x["depois"], x["dias"]], 1):
            _cell(wm, k, j, v, al=MID if j == 6 else None)
        wm.cell(k, 3).number_format = "@"
        wm.cell(k, 6).number_format = "+0;-0;0"
        if x["dias"] > 0:
            wm.cell(k, 6).font = Font(name=FN_, size=10, bold=True, color="C00000")
    wm.freeze_panes = "A6"
    wm.auto_filter.ref = f"A5:F{max(6, 5 + len(rel.get('mudaram', [])))}"
    _imprimir(wm, per, titulo_linhas=False)

    # ===================== PENDÊNCIAS
    wp = wb.create_sheet("PENDÊNCIAS")
    _titulo(wp, "PEÇAS FORA DA PROGRAMAÇÃO", sub)
    wp["A3"] = "Ao resolver o motivo, ajustar a premissa ou o cadastro da fôrma no app e gerar o replanejamento de novo."
    wp["A3"].font = FS
    _hdr(wp, 5, ["MOTIVO", "OBRA", "PEÇA", "CÓDIGO", "PRODUTO", "SEÇÃO", "COMP. (m)", "OBSERVAÇÃO"], [20, 16, 17, 12, 12, 11, 9, 50])
    pl = rel.get("pendencias", [])
    for k, x in enumerate(pl, 6):
        for j, v in enumerate([x["motivo"], x["obra"], x["peca"], x["codigo"], x["produto"], x["secao"], x["comp"], x["obs"]], 1):
            _cell(wp, k, j, v)
        wp.cell(k, 4).number_format = "@"
        wp.cell(k, 7).number_format = "0.00"
    wp.freeze_panes = "A6"
    wp.auto_filter.ref = f"A5:H{max(6, 5 + len(pl))}"
    _imprimir(wp, per, titulo_linhas=False)

    # ===================== FÔRMAS
    wf = wb.create_sheet("FÔRMAS")
    _titulo(wf, "CADASTRO DE FÔRMAS", sub)
    wf["A3"] = "Como está no app (Fôrmas e naves). Adaptações = seções aceitas além da montagem nominal."
    wf["A3"].font = FS
    _hdr(wf, 5, ["NAVE", "FÔRMA", "NOME NA PROGRAMAÇÃO", "SEÇÃO (cm)", "ADAPTAÇÕES", "COMP. ÚTIL (m)", "CAPACIDADE", "PRODUZ", "OBSERVAÇÃO"],
         [7, 10, 26, 11, 24, 11, 16, 14, 40])
    for k, f in enumerate(fs_ord, 6):
        cap = (f"{f.capacidade:g} pç/dia" if f.tipo == "unidade" else f"{f.capacidade:g} concretagem/dia") + (f", a cada {f.ritmo_dias} dias" if f.ritmo_dias > 1 else "")
        for j, v in enumerate([f.nave, f.codigo, f.nome_completo, f.secao or "-", f.secoes_aceitas or None, f.comprimento, cap,
                               f.produtos or None, f.obs or None], 1):
            _cell(wf, k, j, v, al=MID if j in (1, 2, 4, 6) else None)
    wf.freeze_panes = "A6"
    _imprimir(wf, per, titulo_linhas=False)

    # ===================== PREMISSAS
    from . import premissas as pm
    wq = wb.create_sheet("PREMISSAS")
    _titulo(wq, "PREMISSAS DA PROGRAMAÇÃO", sub)
    _hdr(wq, 5, ["Nº", "TEMA", "PREMISSA", "VALIDADE"], [5, 26, 100, 16])
    k = 6
    for t in pm.REGRAS_FIXAS:
        _cell(wq, k, 1, k - 5, al=MID)
        _cell(wq, k, 2, "Regra fixa", font=F10B, al=TOP)
        _cell(wq, k, 3, t, al=TOP)
        _cell(wq, k, 4, "sempre", al=MID)
        wq.row_dimensions[k].height = 30 if len(t) > 110 else 16
        k += 1
    for x in rel.get("premissas", []):
        alvo = " ".join(v for v in [x["obra"], x["pecas"], x["produto"], x["secoes"], x["forma"]] if v)
        texto = f"{alvo}. {x['obs']}" if x["obs"] else alvo
        datas = " a ".join(f"{v[8:10]}/{v[5:7]}" for v in [x["de"], x["ate"]] if v)
        _cell(wq, k, 1, k - 5, al=MID)
        _cell(wq, k, 2, x["tipo"], font=F10B, al=TOP)
        _cell(wq, k, 3, texto, al=TOP)
        _cell(wq, k, 4, datas or "quinzena", al=MID)
        wq.row_dimensions[k].height = 30 if len(texto) > 110 else 16
        k += 1
    _imprimir(wq, per, "portrait", titulo_linhas=False)

    wb._sheets = [wb[n] for n in ["RESUMO", "N1 + N3", "N2", "GANTT FÔRMAS", "REMANEJAMENTO", "PENDÊNCIAS", "FÔRMAS", "PREMISSAS"]]
    wb.active = 0
    for s in wb.worksheets:
        s.sheet_properties.tabColor = {"RESUMO": "1F3864", "N1 + N3": "2F5597", "N2": "2F5597", "GANTT FÔRMAS": "2F5597"}.get(s.title, "A6A6A6")
    wb.properties.creator = "PCP"
    wb.properties.title = f"Programação de produção {per}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
