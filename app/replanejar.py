"""Replanejamento automático: a partir de uma data, redistribui nas fôrmas todas as peças que faltam fabricar.
As regras fixas estão em premissas.REGRAS_FIXAS; as de negócio (o que não entra, fura-fila, fôrma parada, peça avulsa)
são as premissas editáveis na tela."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import formas as fm
from . import premissas as pm
from .models import Apontamento, Armacao, Forma, ItemProg, Obra, Peca, RevisaoProg
from .planejar import capacidade
from .status import chave_natural, unidades_da_obra

FOLGA = 0.20                                    # m entre peças na fôrma linear
MISTAS = {"N1F1", "N1F2", "N1F6", "N1F7"}       # aceitam seções diferentes no mesmo dia
MESMA_MONTAGEM = [("N1F6", "N1F7")]             # fôrma dupla: a mesma seção nas duas no dia


@dataclass(eq=False)
class Dem:
    p: Peca
    classe: int                    # 0 atrasada, 1 programada, 2 nova
    prio: int
    armada: bool
    arm: datetime | None = None
    data0: date | None = None      # data na programação base
    forma0: str = ""               # fôrma na programação base
    tipo0: str = ""
    ordem0: int = 0                # posição na programação base (sequência de lajes)
    arranjo: str | None = None
    fura: bool = False
    desde: date | None = None      # premissa "só a partir de"
    feito: bool = False
    chave: tuple = field(default=())

    @property
    def sec(self) -> str:
        return fm._norm(self.p.secao)


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


def _laje(d: Dem) -> bool:
    return fm._norm(d.p.produto) == "LAJE"


def replanejar(db: Session, base: RevisaoProg, inicio: date, fim: date, sabado: bool | None = None) -> RevisaoProg:
    from . import autodoc
    from . import cadastro as cad
    pm.semear(db)
    prs = pm.ativas(db)
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
    excl_lista = []
    todas = list(db.scalars(select(Peca)))
    existe_cod = {p.codigo for p in todas}
    existe_nome = {(p.obra.nome, p.peca) for p in todas}
    for p in todas:
        if p.fabricada or p.obra_id not in ativas:
            continue
        if p.codigo in fora:
            excluidas["Fora da área de atuação (outra empresa)"] += 1
            continue
        m = pm.motivo_fora(prs, p)
        if m:
            excluidas[m] += 1
            excl_lista.append(p.codigo)
            continue
        nf[p.codigo] = p
    excl_set = set(excl_lista)

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
    tiradas_premissa = 0
    livres_por_nome: dict[tuple, list[str]] = defaultdict(list)
    for c, p in sorted(nf.items()):
        livres_por_nome[(p.obra.nome, p.peca)].append(c)

    def nova_dem(p: Peca, classe: int, i: ItemProg | None = None, k: int = 0) -> Dem:
        return Dem(p=p, classe=classe, prio=prio[p.obra_id], armada=bool(p.hora_armacao or p.codigo in armadas_app),
                   arm=p.hora_armacao, data0=i.data if i else None, forma0=i.forma if i else "", tipo0=i.tipo if i else "",
                   ordem0=k, fura=pm.fura_fila(prs, p), desde=pm.nao_antes(prs, p))

    def pegar(cod: str, i: ItemProg, k: int):
        p = nf[cod]
        livres_por_nome[(p.obra.nome, p.peca)].remove(cod)
        fila[cod] = nova_dem(p, 0 if i.data < inicio else 1, i, k)

    for k, i in enumerate(itens):
        if i.codigo:
            if i.codigo in nf and i.codigo not in fila:
                pegar(i.codigo, i, k)
            elif i.data >= inicio and i.codigo not in nf:
                if i.codigo in excl_set:
                    tiradas_premissa += 1
                elif i.codigo in existe_cod:
                    ja_feitas += 1     # fabricada (ou fora de escopo / obra inativa): sai
                else:
                    fixos.append(i)    # código que não está no Plannix: fica onde está
            continue
        # lote sem código: casa com peças iguais (obra + nome) ainda não fabricadas
        base_nome = (i.peca or "").split(" (")[0].strip()
        livres = livres_por_nome.get((i.obra, base_nome), [])
        n = min(i.qtd or 1, len(livres))
        for cod in list(livres[:n]):
            pegar(cod, i, k)
        if n < (i.qtd or 1) and i.data >= inicio:
            if (i.obra, base_nome) in existe_nome:
                ja_feitas += (i.qtd or 1) - n
            elif not n:
                fixos.append(i)      # sem peça no Plannix (ex.: New Jersey avulso): fica onde está
    for c, p in nf.items():
        if c not in fila:
            fila[c] = nova_dem(p, 2)

    # peças avulsas das premissas (fora do Plannix): data e fôrma fixas
    formas = list(db.scalars(select(Forma).order_by(Forma.nave, Forma.codigo)))
    por_cod = {}
    for f in formas:
        por_cod.setdefault(f.codigo, f)
    avulsas = 0
    for pr in prs:
        if pr.tipo != "avulsa" or not pr.de or not (inicio <= pr.de <= fim):
            continue
        f = por_cod.get(pr.forma)
        fixos.append(ItemProg(data=pr.de, nave=f.nave if f else "", forma=f.nome_completo if f else pr.forma, obra=pr.obra,
                              peca=pr.pecas, codigo=None, produto=pr.produto, secao=pr.secoes, comprimento=None, qtd=1,
                              volume=pr.volume, tipo="AVULSA", obs=pr.obs[:300]))
        avulsas += 1

    bloqueadas: list[Dem] = []
    for c, d in list(fila.items()):
        x = autodoc.de(ad, d.p.obra_id, d.p.peca)
        if x and x.bloqueado:
            bloqueadas.append(d)
            del fila[c]
            continue
        d.arranjo = _arranjo(d.p, idx)
        laje = _laje(d)   # laje: sequência da base (cronograma / ordem da folha), sem critério de armação
        d.chave = (0 if d.fura else 1, d.classe, d.prio, 0 if (laje or d.armada) else 1,
                   datetime.min if laje else (d.arm or datetime.max), d.data0 or date.max, d.ordem0 if laje else 0,
                   d.p.obra.nome, [] if laje else chave_natural(d.p.item or ""), d.p.produto, chave_natural(d.p.peca), d.p.codigo)
    ordem = sorted(fila.values(), key=lambda d: d.chave)

    # 3. encaixe dia a dia
    aceita: dict[int, list[Dem]] = {}
    for f in formas:
        aceita[f.id] = [d for d in ordem if fm.aceita(f, d.p.produto, d.p.secao, d.p.comprimento)
                        and (f.tipo == "unidade" or (d.p.comprimento or 0) > 0)]
    pref = {(f.id, id(d)) for f in formas for d in aceita[f.id] if d.forma0 and fm.item_e_da_forma(d.forma0, f)}
    dias = [inicio + timedelta(days=k) for k in range((fim - inicio).days + 1)]
    dias = [d for d in dias if d.weekday() != 6 and (sabado or d.weekday() != 5)]

    # pistas alternadas: segue a pista da base no dia; sem base, alterna com a do dia anterior
    pistas = sorted({f.codigo for f in formas if f.tipo == "pista"})
    pista_base: dict[date, set[str]] = defaultdict(set)
    for i in itens:
        for f in formas:
            if f.tipo == "pista" and fm.item_e_da_forma(i.forma, f):
                pista_base[i.data].add(f.codigo)
    ultima = next((sorted(pista_base[d])[0] for d in sorted(pista_base, reverse=True) if d < inicio and pista_base[d]), None)
    pista_do_dia = {}
    for dia in dias:
        if pista_base.get(dia) and len(pista_base[dia]) == 1:
            pista_do_dia[dia] = next(iter(pista_base[dia]))
        elif pistas:
            pista_do_dia[dia] = pistas[(pistas.index(ultima) + 1) % len(pistas)] if ultima in pistas else pistas[0]
        ultima = pista_do_dia.get(dia, ultima)

    def cap_dia(f: Forma, dia: date) -> float:
        if pm.forma_parada(prs, f.codigo, dia):
            return 0
        if f.tipo == "pista" and len(pistas) > 1 and pista_do_dia.get(dia) != f.codigo:
            return 0
        return capacidade(f, dia)

    ocup_forma: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    # montagem de cada fôrma na programação base: a do dia (alinhamento) e a da véspera do início (continuidade)
    sec_base: dict[tuple, str] = {}
    sec_ant: dict[str, str] = {}
    for i in itens:
        f = next((f for f in formas if fm.item_e_da_forma(i.forma, f)), None)
        if not f or not i.secao:
            continue
        if i.data >= inicio:
            sec_base.setdefault((f.codigo, i.data), fm._norm(i.secao))
        else:
            sec_ant[f.codigo] = fm._norm(i.secao)
    def alocar_dia(dia: date, so_compromisso: bool = False) -> None:
        vig = [f for f in formas if f.vigente(dia) and cap_dia(f, dia)]
        fixos_dia = [i for i in fixos if i.data == dia]
        est = {}
        for f in vig:
            meus = [i for i in fixos_dia if fm.item_e_da_forma(i.forma, f)]
            est[f.id] = {"cap": cap_dia(f, dia), "usado": 0.0, "n": 0, "arr": None, "sec": None}
            for i in meus:
                est[f.id]["usado"] += (i.qtd or 1) if f.tipo == "unidade" else (i.comprimento or 0) + (FOLGA if est[f.id]["n"] else 0)
                est[f.id]["n"] += i.qtd or 1

        def pode(d: Dem) -> bool:
            if d.feito or (d.desde and dia < d.desde) or (so_compromisso and d.classe == 2 and not d.fura):
                return False
            return not (dia == inicio and not (d.armada or d.data0 == inicio or _laje(d)))

        # seção do dia em cada fôrma de seção única: continua a de ontem se ainda tiver peça; senão a da 1ª da fila
        par = {a: b for x in MESMA_MONTAGEM for a, b in (x, x[::-1])}
        for f in vig:
            if f.codigo in MISTAS and f.codigo not in par or f.tipo == "pista" or not fm.secoes(f):
                continue
            if f.codigo in par and est.get(next((g.id for g in vig if g.codigo == par[f.codigo]), -1), {}).get("sec"):
                est[f.id]["sec"] = est[next(g.id for g in vig if g.codigo == par[f.codigo])]["sec"]
                continue
            cands = [d for d in aceita[f.id] if pode(d)]
            if not cands:
                continue
            ant = sec_ant.get(f.codigo)
            prevista = sec_base.get((f.codigo, dia))
            minhas = [d for d in cands if (f.id, id(d)) in pref]
            if prevista and any(d.sec == prevista for d in cands):
                est[f.id]["sec"] = prevista            # montagem que a programação base já previa para o dia
            elif ant and any(d.sec == ant for d in cands) and not (minhas and minhas[0].sec != ant and minhas[0].classe == 0):
                est[f.id]["sec"] = ant
            else:
                est[f.id]["sec"] = (minhas or cands)[0].sec

        # 1ª passada: cada fôrma pega as peças que já eram dela; 2ª: completa com o resto da fila
        for so_minhas in (True, False):
            for f in vig:
                e = est[f.id]
                for d in aceita[f.id]:
                    if (so_minhas and (f.id, id(d)) not in pref) or not pode(d):
                        continue
                    if e["sec"] and d.sec != e["sec"] and f.codigo not in MISTAS:
                        continue
                    if f.codigo in par and e["sec"] and d.sec != e["sec"]:
                        continue
                    if f.tipo == "unidade":
                        u = 1
                    else:
                        u = (d.p.comprimento or 0) + (FOLGA if e["n"] and f.tipo == "linear" and not _laje(d) else 0)
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
                    e["n"] += 1
                    if not e["sec"] and f.codigo in par:
                        e["sec"] = d.sec
                    p = d.p
                    obs = []
                    if d.fura:
                        obs.append("Prioridade (premissa)")
                    if so_compromisso:
                        obs.append(f"Remanejada para depois de {fim:%d/%m}")
                    if d.classe == 0:
                        obs.append(f"Atrasada (prog. {d.data0:%d/%m})")
                    elif d.classe == 1 and (d.data0 != dia or (f.id, id(d)) not in pref):
                        obs.append(f"Antes {d.data0:%d/%m} {d.forma0}".strip())
                    elif d.classe == 2:
                        obs.append("Nova na programação")
                    if not d.armada and not _laje(d):
                        obs.append("armar D-1")
                    nova.itens.append(ItemProg(data=dia, nave=f.nave, forma=f.nome_completo, obra=p.obra.nome, peca=p.peca,
                                               codigo=p.codigo, produto=p.produto, secao=p.secao, comprimento=p.comprimento,
                                               qtd=1, volume=p.volume, tipo=d.tipo0 or "PROGRAMADA", obs="; ".join(obs)[:300]))
        for f in vig:
            ocup_forma[f.nome_completo][0] += est[f.id]["usado"]
            ocup_forma[f.nome_completo][1] += est[f.id]["cap"]
            if est[f.id]["sec"] and est[f.id]["n"]:
                sec_ant[f.codigo] = est[f.id]["sec"]
        for i in fixos_dia:
            nova.itens.append(ItemProg(data=i.data, nave=i.nave, forma=i.forma, obra=i.obra, peca=i.peca, codigo=i.codigo,
                                       produto=i.produto, secao=i.secao, comprimento=i.comprimento, qtd=i.qtd,
                                       volume=i.volume, tipo=i.tipo, obs=i.obs))

    for dia in dias:
        alocar_dia(dia)
    # o que já estava compromissado (atrasadas e programadas da base) e não coube vai para os dias seguintes
    extra = []
    dia = fim
    while len(extra) < 12 and any(not d.feito and d.classe in (0, 1) for d in ordem):
        dia += timedelta(days=1)
        if dia.weekday() == 6 or (not sabado and dia.weekday() == 5):
            continue
        if dia not in pista_do_dia and pistas:
            ant = pista_do_dia[max(pista_do_dia)] if pista_do_dia else pistas[0]
            pista_do_dia[dia] = pistas[(pistas.index(ant) + 1) % len(pistas)]
        alocar_dia(dia, so_compromisso=True)
        extra.append(dia)
    if extra:
        nova.fim = max(nova.fim, extra[-1])

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

    def motivo_sem_forma(d: Dem) -> str:
        mesmas = [f for f in formas if fm.aceita(f, d.p.produto, d.p.secao, None)]
        if mesmas:
            return f"Comprimento {d.p.comprimento or 0:g} m maior que a fôrma"
        return "Nenhuma fôrma com essa seção/produto"
    sem_f = Counter((d.p.obra.nome, d.p.produto, d.p.secao or "-", motivo_sem_forma(d)) for d in sem_forma)
    nova.relatorio = dict(
        inicio=inicio.isoformat(), fim=fim.isoformat(), base=base.nome, base_id=base.id, sabado=sabado,
        fila=len(ordem) + len(bloqueadas), programadas=sum(d.feito for d in ordem),
        atrasadas=sum(d.classe == 0 for d in ordem), atrasadas_repostas=sum(d.feito and d.classe == 0 for d in ordem),
        ja_feitas=ja_feitas, tiradas_premissa=tiradas_premissa, fixos=len(fixos) - avulsas, avulsas=avulsas,
        excluidas=dict(excluidas.most_common()),
        premissas=[dict(tipo=pm.TIPOS.get(p.tipo, p.tipo), obra=p.obra, pecas=p.pecas, produto=p.produto, secoes=p.secoes,
                        forma=p.forma, de=p.de.isoformat() if p.de else "", ate=p.ate.isoformat() if p.ate else "", obs=p.obs)
                   for p in prs],
        pistas={d.isoformat(): p for d, p in pista_do_dia.items()},
        dias_extra=[d.isoformat() for d in extra],
        remanejadas=sum(1 for i in nova.itens if "Remanejada" in (i.obs or "")),
        mudaram=sorted([dict(obra=i.obra, peca=i.peca, codigo=i.codigo, antes=f"{d0.data0:%d/%m} {d0.forma0}", depois=f"{i.data:%d/%m} {i.forma}",
                             dias=(i.data - d0.data0).days)
                        for i in nova.itens if i.codigo and i.data >= inicio and (d0 := fila.get(i.codigo)) and d0.classe == 1
                        and (i.data != d0.data0 or not fm.item_e_da_forma(d0.forma0, next((f for f in formas if f.nome_completo == i.forma), formas[0])))],
                       key=lambda x: (-x["dias"], x["obra"], x["peca"]))[:400],
        bloqueadas=[dict(obra=d.p.obra.nome, peca=d.p.peca, codigo=d.p.codigo) for d in bloqueadas][:200],
        sem_forma=[dict(obra=o, produto=pr, secao=s, motivo=m, n=n) for (o, pr, s, m), n in
                   sorted(sem_f.items(), key=lambda x: (-x[1], x[0]))],
        sobra=grupos(sobra),
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
    COR = {"Remanejada": "E4DFEC", "Prioridade": "F8CBAD", "Atrasada": "F8CBAD", "Antes": "FFF2CC", "Nova": "DDEBF7",
           "Barracão": "E2F0D9"}
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
                 ("Remanejadas para depois do período", r.get("remanejadas", 0)),
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
    linha += 2
    ws0.cell(linha, 1, "PREMISSAS APLICADAS").font = Font(name="Calibri", size=11, bold=True, color=AZUL)
    for x in r.get("premissas", []):
        linha += 1
        alvo = " ".join(v for v in [x["obra"], x["pecas"], x["produto"], x["secoes"], x["forma"]] if v)
        datas = " a ".join(f"{v[8:10]}/{v[5:7]}" for v in [x["de"], x["ate"]] if v)
        ws0.cell(linha, 1, f"{x['tipo']}: {alvo}" + (f" ({datas})" if datas else "") + (f". {x['obs']}" if x["obs"] else "")).font = f_txt

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
            cor = "E2F0D9" if i.tipo == "AVULSA" else next((COR[c] for c in COR if (obs or "").startswith(c)), None)
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
                       ("Nova na programação", "DDEBF7"), ("Remanejada para depois do período", "E4DFEC"),
                       ("Peça avulsa (premissa, fora do Plannix)", "E2F0D9")]:
            ws.cell(leg, 3, t).fill = PatternFill("solid", fgColor=cor)
            ws.cell(leg, 3).font = f_txt
            leg += 1

    aba("N1 + N3", {"N1", "N3"})
    aba("N2", {"N2"})

    ws = wb.create_sheet("NÃO COUBE")
    ws["A1"] = "O QUE NÃO ENTROU NO PERÍODO"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=AZUL)
    lin = 3
    for titulo, lista, cols in [("Sem fôrma compatível (cadastrar fôrma/adaptação ou rever seção)", r.get("sem_forma", []), ("obra", "produto", "secao", "motivo", "n")),
                                ("Ficam para depois do período (capacidade)", r.get("sobra", []), ("obra", "produto", "secao", "n")),
                                ("Atrasadas que ainda não couberam", r.get("sobra_atrasadas", []), ("obra", "peca", "codigo", "data0", "forma0")),
                                ("Desenho bloqueado no Autodoc", r.get("bloqueadas", []), ("obra", "peca", "codigo"))]:
        ws.cell(lin, 1, titulo).font = Font(name="Calibri", size=11, bold=True, color=AZUL)
        lin += 1
        for k, t in enumerate(cols, 1):
            c = ws.cell(lin, k, {"secao": "SEÇÃO", "n": "QTD", "data0": "PROG. ANTES", "forma0": "FÔRMA ANTES", "motivo": "MOTIVO"}.get(t, t.upper()))
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
    for k, w in enumerate([24, 16, 14, 34, 26], 1):
        ws.column_dimensions[get_column_letter(k)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
