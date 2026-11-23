"""Replanejamento automático: a partir de uma data, redistribui nas fôrmas todas as peças que faltam fabricar.

Regras (na ordem em que pesam):
1. Até a véspera do início, a programação fica como estava (histórico do programado x realizado, com apontamentos).
2. Fila de peças, nesta ordem: atrasadas (programadas antes do início e não fabricadas), programadas do início em
   diante (pela data original), depois as que nunca foram programadas. Dentro de cada grupo: prioridade da obra,
   armadas primeiro, data original, obra, produto e nome.
3. Cada dia (segunda a sábado, se a programação base usa sábado) e cada fôrma vigente recebem as primeiras peças da
   fila que encaixam: produto/seção aceitos, comprimento útil, capacidade do dia (metros ou peças), ritmo da fôrma.
   A fôrma original da peça tem preferência.
4. Pista de protensão: um arranjo de cordoalha por pista por dia.
5. Primeiro dia: só entra peça já armada ou que já estava programada para esse dia (a armação é D-1).
6. Não entram: peças de obras inativas, fora da área de atuação (ex.: Bay depois do eixo 25) e com desenho bloqueado
   no Autodoc. Linhas da programação sem peça no Plannix (ex.: New Jersey avulso) ficam onde estavam.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import formas as fm
from .models import Apontamento, Armacao, Forma, ItemProg, Obra, Peca, RevisaoProg
from .planejar import capacidade
from .status import chave_natural, unidades_da_obra

CLASSE = {0: "Atrasada", 1: "Programada", 2: "Nova"}


@dataclass(eq=False)
class Dem:
    p: Peca
    classe: int
    prio: int
    armada: bool
    data0: date | None = None      # data na programação base
    forma0: str = ""               # fôrma na programação base
    tipo0: str = ""
    arranjo: str | None = None
    feito: bool = False
    chave: tuple = field(default=())


def _fora_de_escopo(db: Session, obras: list[Obra]) -> set[str]:
    out = set()
    for o in obras:
        if not o.plantas:
            continue
        com = {u["cod"] for us in unidades_da_obra(db, o.id).values() for u in us}
        sem = {u["cod"] for us in unidades_da_obra(db, o.id, escopo=False).values() for u in us}
        out |= sem - com
    return out


def _arranjo(p: Peca, idx) -> str | None:
    from . import cadastro as cad
    if p.modelo_protensao:
        return p.modelo_protensao.strip().upper()
    c = cad.de_peca(idx, p)
    return c.protensao if c and c.protensao else None


def _uso(f: Forma, comp: float | None) -> float:
    return 1 if f.tipo == "unidade" else (comp or 0)


def replanejar(db: Session, base: RevisaoProg, inicio: date, fim: date, sabado: bool | None = None) -> RevisaoProg:
    from . import autodoc
    from . import cadastro as cad
    obras = list(db.scalars(select(Obra)))
    ativas = {o.id for o in obras if o.ativa}
    prio = {o.id: o.prioridade or 99 for o in obras}
    nome_obra = {o.nome: o for o in obras}
    fora = _fora_de_escopo(db, [o for o in obras if o.ativa])
    armadas_app = {c for (c,) in db.execute(select(Armacao.codigo).where(Armacao.codigo.is_not(None)))}
    ad = autodoc.indice(db)
    idx = cad.indice(db)

    nf: dict[str, Peca] = {}
    excluidas = Counter()
    todas = list(db.scalars(select(Peca)))
    existe_cod = {p.codigo for p in todas}
    existe_nome = {(p.obra.nome, p.peca) for p in todas}
    for p in todas:
        if p.fabricada:
            continue
        if p.obra_id not in ativas:
            continue
        if p.codigo in fora:
            excluidas["Fora da área de atuação (outra empresa)"] += 1
            continue
        nf[p.codigo] = p

    itens = sorted(base.itens, key=lambda i: (i.data, i.id))
    if sabado is None:
        sabado = any(i.data.weekday() == 5 for i in itens)
    nova = RevisaoProg(nome=f"Replanejamento {inicio:%d/%m} a {fim:%d/%m} (base {base.nome})"[:200],
                       inicio=base.inicio or inicio, fim=max(fim, base.fim or fim), rascunho=True)
    db.add(nova)

    # 1. histórico até a véspera, com os apontamentos
    for i in itens:
        if i.data < inicio:
            it = ItemProg(data=i.data, nave=i.nave, forma=i.forma, obra=i.obra, peca=i.peca, codigo=i.codigo, produto=i.produto,
                          secao=i.secao, comprimento=i.comprimento, qtd=i.qtd, volume=i.volume, tipo=i.tipo, obs=i.obs)
            nova.itens.append(it)
            if i.apontamento:
                a = i.apontamento
                it.apontamento = Apontamento(realizado=a.realizado, ocorrido=a.ocorrido, data_execucao=a.data_execucao,
                                             comentario=a.comentario, causa=a.causa, acao=a.acao, responsavel=a.responsavel)

    # 2. fila
    fila: dict[str, Dem] = {}
    fixos: list[ItemProg] = []
    ja_feitas = 0
    livres_por_nome: dict[tuple, list[str]] = defaultdict(list)
    for c, p in sorted(nf.items()):
        livres_por_nome[(p.obra.nome, p.peca)].append(c)

    def pegar(cod: str, i: ItemProg):
        p = nf[cod]
        livres_por_nome[(p.obra.nome, p.peca)].remove(cod)
        fila[cod] = Dem(p=p, classe=0 if i.data < inicio else 1, prio=prio[p.obra_id],
                        armada=bool(p.hora_armacao or cod in armadas_app), data0=i.data, forma0=i.forma, tipo0=i.tipo)

    for i in itens:
        if i.codigo:
            if i.codigo in nf and i.codigo not in fila:
                pegar(i.codigo, i)
            elif i.data >= inicio and i.codigo not in nf:
                if i.codigo in existe_cod:
                    ja_feitas += 1     # fabricada (ou fora de escopo / obra inativa): sai
                else:
                    fixos.append(i)    # código que não está no Plannix: fica onde está
            continue
        # lote sem código: casa com peças iguais (obra + nome) ainda não fabricadas
        base_nome = (i.peca or "").split(" (")[0].strip()
        livres = livres_por_nome.get((i.obra, base_nome), [])
        n = min(i.qtd or 1, len(livres))
        for cod in list(livres[:n]):
            pegar(cod, i)
        if n < (i.qtd or 1) and i.data >= inicio:
            if (i.obra, base_nome) in existe_nome:
                ja_feitas += (i.qtd or 1) - n
            elif not n:
                fixos.append(i)      # sem peça no Plannix (ex.: New Jersey avulso): fica onde está
    for c, p in nf.items():
        if c not in fila:
            fila[c] = Dem(p=p, classe=2, prio=prio[p.obra_id], armada=bool(p.hora_armacao or c in armadas_app))

    bloqueadas: list[Dem] = []
    for c, d in list(fila.items()):
        x = autodoc.de(ad, d.p.obra_id, d.p.peca)
        if x and x.bloqueado:
            bloqueadas.append(d)
            del fila[c]
        else:
            d.arranjo = _arranjo(d.p, idx)
            d.chave = (d.classe, d.prio, 0 if d.armada else 1, d.data0 or date.max, d.p.obra.nome, d.p.produto,
                       chave_natural(d.p.peca), d.p.codigo)
    ordem = sorted(fila.values(), key=lambda d: d.chave)

    # 3. encaixe dia a dia
    formas = list(db.scalars(select(Forma).order_by(Forma.nave, Forma.codigo)))
    aceita: dict[int, list[Dem]] = {}
    for f in formas:
        ok = [d for d in ordem if fm.aceita(f, d.p.produto, d.p.secao, d.p.comprimento)
              and (f.tipo == "unidade" or (d.p.comprimento or 0) > 0)]
        # fôrma original da peça tem preferência dentro do mesmo grupo (atrasada/programada/nova)
        ok.sort(key=lambda d: (d.classe, 0 if d.forma0 and fm.item_e_da_forma(d.forma0, f) else 1, d.chave))
        aceita[f.id] = ok
    dias = [inicio + timedelta(days=k) for k in range((fim - inicio).days + 1)]
    dias = [d for d in dias if d.weekday() != 6 and (sabado or d.weekday() != 5)]
    ocup_forma: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    pref = {(f.id, id(d)) for f in formas for d in aceita[f.id] if d.forma0 and fm.item_e_da_forma(d.forma0, f)}
    for dia in dias:
        vig = [f for f in formas if f.vigente(dia) and capacidade(f, dia)]
        fixos_dia = [i for i in fixos if i.data == dia]
        est = {}
        for f in vig:
            meus_fixos = [i for i in fixos_dia if fm.item_e_da_forma(i.forma, f)]
            est[f.id] = {"cap": capacidade(f, dia), "usado": sum(_uso(f, i.comprimento) * (i.qtd or 1) for i in meus_fixos),
                         "arr": None}
        # 1ª passada: cada fôrma pega as peças que já eram dela; 2ª: completa com o resto da fila
        for so_minhas in (True, False):
            for f in vig:
                e = est[f.id]
                for d in aceita[f.id]:
                    if d.feito or (so_minhas and (f.id, id(d)) not in pref):
                        continue
                    if dia == inicio and not (d.armada or d.data0 == inicio):
                        continue
                    u = _uso(f, d.p.comprimento)
                    if e["usado"] + u > e["cap"] + 1e-6:
                        if f.tipo == "unidade":
                            break
                        continue
                    if f.tipo == "pista" and d.arranjo is not None:
                        if e["arr"] is not None and d.arranjo != e["arr"]:
                            continue
                        e["arr"] = d.arranjo
                    d.feito = True
                    e["usado"] += u
                    p = d.p
                    obs = []
                    if d.classe == 0:
                        obs.append(f"Atrasada (prog. {d.data0:%d/%m})")
                    elif d.classe == 1 and (d.data0 != dia or (f.id, id(d)) not in pref):
                        obs.append(f"Antes {d.data0:%d/%m} {d.forma0}".strip())
                    elif d.classe == 2:
                        obs.append("Nova na programação")
                    if not d.armada:
                        obs.append("armar D-1")
                    nova.itens.append(ItemProg(data=dia, nave=f.nave, forma=f.nome_completo, obra=p.obra.nome, peca=p.peca,
                                               codigo=p.codigo, produto=p.produto, secao=p.secao, comprimento=p.comprimento,
                                               qtd=1, volume=p.volume, tipo=d.tipo0 or "PROGRAMADA", obs="; ".join(obs)[:300]))
        for f in vig:
            ocup_forma[f.nome_completo][0] += est[f.id]["usado"]
            ocup_forma[f.nome_completo][1] += est[f.id]["cap"]
        for i in fixos_dia:
            nova.itens.append(ItemProg(data=i.data, nave=i.nave, forma=i.forma, obra=i.obra, peca=i.peca, codigo=i.codigo,
                                       produto=i.produto, secao=i.secao, comprimento=i.comprimento, qtd=i.qtd,
                                       volume=i.volume, tipo=i.tipo, obs=i.obs))

    # 4. relatório
    aceitas_ids = {id(d) for ds in aceita.values() for d in ds}
    sem_forma = [d for d in ordem if id(d) not in aceitas_ids]
    sem_forma_set = {id(d) for d in sem_forma}
    bloq_ids = {id(d) for d in bloqueadas}
    sobra = [d for d in ordem if not d.feito and id(d) not in sem_forma_set]
    por_obra = defaultdict(lambda: Counter())
    for d in ordem + bloqueadas:
        o = d.p.obra.nome
        por_obra[o]["faltam"] += 1
        if d.feito:
            por_obra[o]["programadas"] += 1
            por_obra[o]["atrasadas_repostas"] += d.classe == 0
        elif id(d) in sem_forma_set:
            por_obra[o]["sem_forma"] += 1
        elif id(d) in bloq_ids:
            por_obra[o]["bloqueadas"] += 1
        else:
            por_obra[o]["ficam_para_depois"] += 1
            por_obra[o]["atrasadas_fora"] += d.classe == 0
    def grupos(ds):
        g = Counter((d.p.obra.nome, d.p.produto, d.p.secao or "-") for d in ds)
        return [dict(obra=o, produto=pr, secao=s, n=n) for (o, pr, s), n in sorted(g.items(), key=lambda x: (-x[1], x[0]))]
    nova.relatorio = dict(
        inicio=inicio.isoformat(), fim=fim.isoformat(), base=base.nome, base_id=base.id, sabado=sabado,
        fila=len(ordem) + len(bloqueadas), programadas=sum(d.feito for d in ordem),
        atrasadas=sum(d.classe == 0 for d in ordem), atrasadas_repostas=sum(d.feito and d.classe == 0 for d in ordem),
        ja_feitas=ja_feitas, fixos=len(fixos), excluidas=dict(excluidas),
        bloqueadas=[dict(obra=d.p.obra.nome, peca=d.p.peca, codigo=d.p.codigo) for d in bloqueadas][:200],
        sem_forma=grupos(sem_forma), sobra=grupos(sobra),
        sobra_atrasadas=[dict(obra=d.p.obra.nome, peca=d.p.peca, codigo=d.p.codigo, data0=d.data0.isoformat(), forma0=d.forma0)
                         for d in sobra if d.classe == 0][:300],
        por_obra=[dict(obra=o, prio=prio[nome_obra[o].id] if o in nome_obra else 99, **dict(c)) for o, c in
                  sorted(por_obra.items(), key=lambda x: (prio[nome_obra[x[0]].id] if x[0] in nome_obra else 99, x[0]))],
        ocupacao=[dict(forma=k, uso=round(u, 1), cap=round(c, 1), pct=round(u / c, 3) if c else 0)
                  for k, (u, c) in sorted(ocup_forma.items())],
    )
    db.commit()
    return nova


def publicar(db: Session, rev: RevisaoProg) -> None:
    """O rascunho passa a ser a programação vigente (vale para status das plantas, PxR e armação)."""
    from datetime import datetime
    rev.rascunho = False
    rev.criada_em = datetime.now()
    db.commit()


def planilha(rev: RevisaoProg, inicio: date | None = None) -> bytes:
    """Programação no layout da planilha da fábrica: abas 'N1 + N3' e 'N2', uma linha por peça (código),
    X no dia da concretagem. Reimportável pelo app (cabeçalho DATA, FÔRMA, OBRA, PEÇA, CÓDIGO...)."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from .programacao import DIAS

    ini = inicio or rev.inicio
    itens = [i for i in rev.itens if i.data >= ini]
    fim = max([i.data for i in itens] + [ini])
    dias = [ini + timedelta(days=k) for k in range((fim - ini).days + 1)]
    dias = [d for d in dias if d.weekday() != 6]
    AZUL = "1F3864"
    fino = Side(style="thin", color="BFBFBF")
    borda = Border(left=fino, right=fino, top=fino, bottom=fino)
    f_cab = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    f_txt = Font(name="Calibri", size=10)
    centro = Alignment(horizontal="center", vertical="center", wrap_text=True)
    esq = Alignment(horizontal="left", vertical="center", wrap_text=True)
    COR = {"Atrasada": "F8CBAD", "Antes": "FFF2CC", "Nova": "DDEBF7"}
    cab = (["DATA", "DIA", "FÔRMA", "OBRA", "PEÇA", "CÓDIGO", "PRODUTO", "SEÇÃO", "COMP. (M)", "QTD", "VOL. (M³)", "TIPO", "OBSERVAÇÃO"]
           + [f"{DIAS[d.weekday()]}\n{d:%d/%m}" for d in dias])
    larg = [10, 6, 24, 14, 16, 12, 11, 10, 9, 6, 9, 12, 34] + [7] * len(dias)

    wb = Workbook()
    ws0 = wb.active
    ws0.title = "RESUMO"
    r = rev.relatorio or {}
    ws0["A1"] = f"REPLANEJAMENTO DA PRODUÇÃO | {ini:%d/%m} a {fim:%d/%m/%Y}"
    ws0["A1"].font = Font(name="Calibri", size=14, bold=True, color=AZUL)
    ws0["A2"] = f"Base: {r.get('base', '-')} · {rev.nome}"
    ws0["A2"].font = Font(name="Calibri", size=9, italic=True, color="595959")
    linha = 4
    for t, v in [("Peças na fila", r.get("fila")), ("Programadas no período", r.get("programadas")),
                 ("Atrasadas reprogramadas", f"{r.get('atrasadas_repostas', 0)} de {r.get('atrasadas', 0)}"),
                 ("Já fabricadas (saíram da programação)", r.get("ja_feitas"))]:
        ws0.cell(linha, 1, t).font = f_txt
        ws0.cell(linha, 2, v).font = Font(name="Calibri", size=10, bold=True)
        linha += 1
    linha += 1
    cab_o = ["OBRA", "PRIOR.", "FALTAM", "PROGRAMADAS", "FICAM P/ DEPOIS", "SEM FÔRMA", "DESENHO BLOQ."]
    for k, t in enumerate(cab_o, 1):
        c = ws0.cell(linha, k, t)
        c.font, c.alignment, c.border, c.fill = f_cab, centro, borda, PatternFill("solid", fgColor=AZUL)
    for o in r.get("por_obra", []):
        linha += 1
        for k, v in enumerate([o["obra"], o.get("prio"), o.get("faltam", 0), o.get("programadas", 0),
                               o.get("ficam_para_depois", 0), o.get("sem_forma", 0), o.get("bloqueadas", 0)], 1):
            c = ws0.cell(linha, k, v)
            c.font, c.border, c.alignment = f_txt, borda, (esq if k == 1 else centro)
    for k, w in enumerate([26, 8, 10, 13, 15, 11, 13], 1):
        ws0.column_dimensions[get_column_letter(k)].width = w

    def aba(titulo, naves):
        ws = wb.create_sheet(titulo)
        ws["A1"] = f"PROGRAMAÇÃO DA PRODUÇÃO | {titulo.replace('N1 + N3', 'NAVES 1 E 3').replace('N2', 'NAVE 2')} | {ini:%d/%m} a {fim:%d/%m/%Y}"
        ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=AZUL)
        ws["A2"] = rev.nome
        ws["A2"].font = Font(name="Calibri", size=9, italic=True, color="595959")
        for k, t in enumerate(cab, 1):
            c = ws.cell(3, k, t)
            c.font, c.alignment, c.border = f_cab, centro, borda
            c.fill = PatternFill("solid", fgColor=AZUL)
            ws.column_dimensions[get_column_letter(k)].width = larg[k - 1]
        ws.row_dimensions[3].height = 30
        lin = 4
        sel = sorted((i for i in itens if i.nave in naves), key=lambda i: (i.data, i.forma, i.obra, chave_natural(i.peca), i.codigo or ""))
        for i in sel:
            d, obs = i.data, i.obs
            vals = ([d, DIAS[d.weekday()], i.forma, i.obra, i.peca, i.codigo or "", i.produto, i.secao, i.comprimento, i.qtd or 1,
                     round(i.volume, 3) if i.volume else None, i.tipo, obs] + ["X" if x == d else None for x in dias])
            cor = next((COR[c] for c in COR if (obs or "").startswith(c)), None)
            for kk, v in enumerate(vals, 1):
                c = ws.cell(lin, kk, v)
                c.font, c.border = f_txt, borda
                c.alignment = esq if kk in (3, 6, 13) else centro
                if cor and kk <= 13:
                    c.fill = PatternFill("solid", fgColor=cor)
            ws.cell(lin, 1).number_format = "dd/mm/yy"
            ws.cell(lin, 9).number_format = "0.00"
            ws.cell(lin, 11).number_format = "0.000"
            ws.cell(lin, 5).font = Font(name="Calibri", size=10, bold=True)
            for kk in range(len(dias)):
                c = ws.cell(lin, 14 + kk)
                if c.value == "X":
                    c.font = Font(name="Calibri", size=11, bold=True, color="375623")
            lin += 1
        ws.freeze_panes = "D4"
        ws.auto_filter.ref = f"A3:{get_column_letter(len(cab))}{max(lin - 1, 3)}"
        leg = lin + 1
        for t, cor in [("Atrasada (vinha de antes do início)", "F8CBAD"), ("Mudou de dia ou de fôrma", "FFF2CC"),
                       ("Nova na programação", "DDEBF7")]:
            ws.cell(leg, 3, t).fill = PatternFill("solid", fgColor=cor)
            ws.cell(leg, 3).font = f_txt
            leg += 1

    aba("N1 + N3", {"N1", "N3"})
    aba("N2", {"N2"})

    ws = wb.create_sheet("NÃO COUBE")
    ws["A1"] = "O QUE NÃO ENTROU NO PERÍODO"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=AZUL)
    lin = 3
    for titulo, lista, cols in [("Sem fôrma compatível (cadastrar fôrma/adaptação ou rever seção)", r.get("sem_forma", []), ("obra", "produto", "secao", "n")),
                                ("Ficam para depois do período (capacidade)", r.get("sobra", []), ("obra", "produto", "secao", "n")),
                                ("Atrasadas que ainda não couberam", r.get("sobra_atrasadas", []), ("obra", "peca", "codigo", "data0", "forma0")),
                                ("Desenho bloqueado no Autodoc", r.get("bloqueadas", []), ("obra", "peca", "codigo"))]:
        ws.cell(lin, 1, titulo).font = Font(name="Calibri", size=11, bold=True, color=AZUL)
        lin += 1
        for k, t in enumerate(cols, 1):
            c = ws.cell(lin, k, {"secao": "SEÇÃO", "n": "QTD", "data0": "PROG. ANTES", "forma0": "FÔRMA ANTES"}.get(t, t.upper()))
            c.font, c.fill, c.border = f_cab, PatternFill("solid", fgColor=AZUL), borda
        for x in lista:
            lin += 1
            for k, t in enumerate(cols, 1):
                c = ws.cell(lin, k, x.get(t))
                c.font, c.border = f_txt, borda
        if not lista:
            lin += 1
            ws.cell(lin, 1, "Nenhuma.").font = f_txt
        lin += 2
    for k, w in enumerate([24, 16, 14, 12, 26], 1):
        ws.column_dimensions[get_column_letter(k)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
