"""Quadro de planejamento: peças faltantes arrastadas para as fôrmas do dia, com validação de encaixe e capacidade."""
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import formas as fm
from .models import Armacao, Forma, ItemProg, Obra, Peca, RevisaoProg
from .status import chave_natural

# prioridade das obras (alinhamento de 14/11): Farol > Elite > Bay > demais
PRIORIDADE_PADRAO = {"8243": 1, "8246": 2, "8249": 3, "8206": 4, "8239": 5, "8198": 6, "8240": 7, "8245": 8, "8250": 9}


def completar_prioridades(db: Session) -> None:
    for o in db.scalars(select(Obra).where(Obra.prioridade.is_(None), Obra.sigla.in_(list(PRIORIDADE_PADRAO)))):
        o.prioridade = PRIORIDADE_PADRAO[o.sigla]
    db.commit()


def capacidade(f: Forma, d: date) -> float:
    """Capacidade da fôrma no dia: metros (linear e pista) ou peças (unidade). Zero fora da vigência, em manutenção
    ou no dia de folga do ritmo (ex.: escada 2 a cada dois dias)."""
    if not f.vigente(d) or f.situacao != "ativa":
        return 0
    if f.ritmo_dias > 1 and f.inicio and (d - f.inicio).days % f.ritmo_dias:
        return 0
    if f.tipo == "unidade":
        return f.capacidade
    return (f.comprimento or 0) * f.capacidade


def uso(f: Forma, itens: list[ItemProg]) -> float:
    if f.tipo == "unidade":
        return sum(i.qtd for i in itens)
    return sum((i.comprimento or 0) * i.qtd for i in itens)


def itens_por_forma(db: Session, rev: RevisaoProg, d: date, formas: list[Forma]) -> tuple[dict[int, list[ItemProg]], list[ItemProg]]:
    """Itens do dia separados por fôrma do cadastro; os que não casam com nenhuma fôrma vão para 'sem fôrma'."""
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.data == d)))
    por = defaultdict(list)
    sobra = []
    for i in itens:
        f = next((f for f in formas if i.forma and fm.item_e_da_forma(i.forma, f)), None)
        (por[f.id] if f else sobra).append(i)
    return por, sobra


def faltantes(db: Session, rev: RevisaoProg | None) -> list[dict]:
    """Peças não fabricadas das obras ativas que ainda não estão nesta revisão, agrupadas por peça igual e ordenadas
    pelos critérios do PCP: prioridade da obra, armadas primeiro, produto e nome da peça."""
    ja = set()
    if rev:
        ja = {c for (c,) in db.execute(select(ItemProg.codigo).where(ItemProg.revisao_id == rev.id, ItemProg.codigo.is_not(None)))}
    armadas_app = {c for (c,) in db.execute(select(Armacao.codigo).where(Armacao.codigo.is_not(None)))}
    from . import autodoc
    from . import cadastro as cad
    idx = cad.indice(db)
    ad = autodoc.indice(db)
    grupos: dict[tuple, dict] = {}
    q = select(Peca, Obra).join(Obra).where(Peca.fabricada.is_(False), Obra.ativa.is_(True))
    for p, o in db.execute(q):
        if p.codigo in ja:
            continue
        k = (o.id, p.peca, p.item, p.secao, p.comprimento, p.produto)
        g = grupos.get(k)
        if not g:
            c = cad.de_peca(idx, p)
            g = grupos[k] = {"obra": o.nome, "prio": o.prioridade or 99, "peca": p.peca, "secao": p.secao, "comp": p.comprimento,
                             "produto": p.produto, "codigos": [], "armadas": 0,
                             "prot": c.protensao if c else None, "aco": round(c.aco_passivo) if c and c.aco_passivo else None,
                             "e2": (c.etapa2_obs or "sim") if c and c.segunda_etapa else None,
                             "ad": autodoc.resumo(autodoc.de(ad, o.id, p.peca))}
        g["codigos"].append(p.codigo)
        if p.hora_armacao or p.codigo in armadas_app:
            g["armadas"] += 1
    out = list(grupos.values())
    for g in out:
        g["codigos"].sort()
    out.sort(key=lambda g: (g["prio"], 0 if g["armadas"] else 1, g["obra"], g["produto"], chave_natural(g["peca"])))
    return out


def validar(f: Forma, d: date, pecas: list[Peca], ocupado: float) -> list[str]:
    """Problemas de colocar estas peças na fôrma neste dia (lista vazia = pode)."""
    probs = []
    cap = capacidade(f, d)
    if not f.vigente(d):
        probs.append(f"{f.nome} não está vigente em {d:%d/%m}.")
    elif f.situacao != "ativa":
        probs.append(f"{f.nome} está {fm.SITUACOES.get(f.situacao, f.situacao).lower()}.")
    elif cap == 0:
        probs.append(f"{f.nome} não produz em {d:%d/%m} (ritmo de {f.ritmo_dias} em {f.ritmo_dias} dias).")
    ruins = sorted({p.peca for p in pecas if not fm.aceita(f, p.produto, p.secao, p.comprimento)})
    if ruins:
        motivo = []
        if f.produtos:
            motivo.append(f"produto {f.produtos}")
        if f.secao or f.secoes_aceitas:
            motivo.append("seção " + ", ".join(x for x in [f.secao, f.secoes_aceitas] if x))
        if f.tipo != "unidade" and f.comprimento:
            motivo.append(f"até {f.comprimento:g} m")
        probs.append(f"{', '.join(ruins[:5])}{'...' if len(ruins) > 5 else ''} não cabe(m) na {f.nome} ({'; '.join(motivo) or 'sem seção cadastrada'}).")
    novo = sum(1 for _ in pecas) if f.tipo == "unidade" else sum(p.comprimento or 0 for p in pecas)
    if cap and ocupado + novo > cap + 1e-6:
        un = "pç" if f.tipo == "unidade" else "m"
        probs.append(f"Passa da capacidade da {f.nome}: {ocupado + novo:.1f} de {cap:g} {un}.")
    return probs


def alocar(db: Session, rev: RevisaoProg, f: Forma, d: date, codigos: list[str], forcar: bool = False) -> dict:
    pecas = [p for p in db.scalars(select(Peca).where(Peca.codigo.in_(codigos)))]
    ja = {c for (c,) in db.execute(select(ItemProg.codigo).where(ItemProg.revisao_id == rev.id, ItemProg.codigo.in_(codigos)))}
    pecas = [p for p in pecas if p.codigo not in ja]
    if not pecas:
        return {"ok": False, "problemas": ["Essas peças já estão nesta revisão."], "pode_forcar": False}
    vig = [x for x in db.scalars(select(Forma)) if x.vigente(d)]
    por, _ = itens_por_forma(db, rev, d, vig)
    probs = aviso_parada(db, f, d) + validar(f, d, pecas, uso(f, por.get(f.id, [])))
    probs += conflito_protensao(db, f, pecas, por.get(f.id, []))
    probs += desenho_bloqueado(db, pecas)
    if probs and not forcar:
        return {"ok": False, "problemas": probs, "pode_forcar": True}
    for p in pecas:
        db.add(ItemProg(revisao_id=rev.id, data=d, nave=f.nave, forma=f.nome_completo, obra=p.obra.nome, peca=p.peca, codigo=p.codigo,
                        produto=p.produto, secao=p.secao, comprimento=p.comprimento, qtd=1, volume=p.volume, tipo="PLANEJADA",
                        obs=("Forçado: " + " ".join(probs))[:300] if probs else ""))
    datas = [rev.inicio, rev.fim, d]
    rev.inicio, rev.fim = min(x for x in datas if x), max(x for x in datas if x)
    db.commit()
    return {"ok": True, "n": len(pecas), "avisos": probs}


def copiar_revisao(db: Session, rev: RevisaoProg, nome: str) -> RevisaoProg:
    """Nova revisão com os mesmos itens (sem apontamentos), para replanejar sem perder o histórico."""
    nova = RevisaoProg(nome=nome[:120], inicio=rev.inicio, fim=rev.fim)
    for i in rev.itens:
        nova.itens.append(ItemProg(data=i.data, nave=i.nave, forma=i.forma, obra=i.obra, peca=i.peca, codigo=i.codigo, produto=i.produto,
                                   secao=i.secao, comprimento=i.comprimento, qtd=i.qtd, volume=i.volume, tipo=i.tipo, obs=i.obs))
    db.add(nova)
    db.commit()
    return nova


def desenho_bloqueado(db: Session, pecas: list[Peca]) -> list[str]:
    """Peça cujo desenho vigente no Autodoc não está aprovado (ex.: bloqueado para conferência)."""
    from . import autodoc
    idx = autodoc.indice(db)
    out = []
    for nome in sorted({(p.obra_id, p.peca) for p in pecas}, key=lambda x: x[1]):
        x = autodoc.de(idx, *nome)
        if x and x.bloqueado:
            out.append(f"{nome[1]}: desenho {x.status.lower()} no Autodoc (R{x.revisao}, {x.data:%d/%m})"
                       + (f", {x.observacoes}" if x.observacoes else "") + ".")
    return out


def conflito_protensao(db: Session, f: Forma, novas: list[Peca], ja: list[ItemProg]) -> list[str]:
    """Na mesma pista (ou fôrma com peça protendida) no mesmo dia, as cordoalhas correm de ponta a ponta: as peças
    precisam ter o mesmo arranjo (nº e Ø). Vale para peças com cadastro de prancha."""
    from . import cadastro as cad
    idx = cad.indice(db)
    cods = [i.codigo for i in ja if i.codigo]
    existentes = list(db.scalars(select(Peca).where(Peca.codigo.in_(cods)))) if cods else []
    def arranjo(p):
        c = cad.de_peca(idx, p)
        return (c.protensao if c else None)
    lado_a = {(p.peca, arranjo(p)) for p in existentes if arranjo(p) is not None}
    lado_b = {(p.peca, arranjo(p)) for p in novas if arranjo(p) is not None}
    if not lado_a or not lado_b:
        return []
    protendida = f.tipo == "pista" or any(a for _, a in lado_a | lado_b)
    arr_a, arr_b = {a for _, a in lado_a}, {a for _, a in lado_b}
    if protendida and len(arr_a | arr_b) > 1:
        fmt = lambda s: ", ".join(f"{n} ({a or 'sem protensão'})" for n, a in sorted(s))
        return [f"Protensão diferente na {f.nome}: {fmt(lado_b)} x já programadas {fmt(lado_a)}."]
    return []


# ---------------------------------------------------------------- quadro do período (fôrmas x dias)

def dias_periodo(de: date, ate: date) -> list[date]:
    from datetime import timedelta
    out, d = [], de
    while d <= ate and len(out) < 45:
        if d.weekday() != 6:
            out.append(d)
        d += timedelta(days=1)
    return out


def quadro(db: Session, rev: RevisaoProg, dias: list[date]) -> dict:
    """Linhas = fôrmas vigentes em algum dia do período (por nave); colunas = dias. Cada célula: itens agrupados por
    peça, uso e capacidade. Itens cuja fôrma não está no cadastro vão para a linha 'sem fôrma' da nave."""
    formas = [f for f in db.scalars(select(Forma).order_by(Forma.nave, Forma.y, Forma.x)) if any(f.vigente(d) for d in dias)]
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.data >= dias[0],
                                                   ItemProg.data <= dias[-1]))) if dias else []
    fab = {c for (c,) in db.execute(select(Peca.codigo).where(Peca.fabricada.is_(True),
                                                              Peca.codigo.in_([i.codigo for i in itens if i.codigo] or [""])))}
    cel: dict = defaultdict(list)
    sem: dict = defaultdict(list)
    cache: dict = {}
    for i in itens:
        if i.forma not in cache:
            cache[i.forma] = next((f.id for f in formas if i.forma and fm.item_e_da_forma(i.forma, f)), None)
        fid = cache[i.forma]
        (cel[(fid, i.data)] if fid else sem[(i.nave or "-", i.data)]).append(i)

    def grupos(its):
        g: dict = {}
        for i in its:
            k = (i.obra, i.peca)
            x = g.setdefault(k, {"obra": i.obra, "peca": i.peca, "ids": [], "qtd": 0, "fab": 0, "comp": i.comprimento,
                                 "secao": i.secao, "obs": i.obs})
            x["ids"].append(i.id)
            x["qtd"] += i.qtd
            x["fab"] += i.qtd if i.codigo in fab else 0
        return sorted(g.values(), key=lambda x: (x["obra"], chave_natural(x["peca"])))
    from . import premissas as pm
    paradas = [pr for pr in pm.ativas(db) if pr.tipo == "parar_forma"]
    naves = []
    for n, nome in fm.NAVES.items():
        linhas = []
        for f in (f for f in formas if f.nave == n):
            cs = []
            for d in dias:
                its = cel.get((f.id, d), [])
                pr = parada(paradas, f, d)
                cap = 0 if pr else capacidade(f, d)
                cs.append({"d": d, "g": grupos(its), "uso": uso(f, its), "cap": cap, "parada": pr})
            linhas.append({"f": f, "cels": cs, "un": "pç" if f.tipo == "unidade" else "m"})
        if any(sem.get((n, d)) for d in dias):
            linhas.append({"f": None, "un": "", "cels": [{"d": d, "g": grupos(sem.get((n, d), [])), "uso": 0, "cap": 0} for d in dias]})
        naves.append({"cod": n, "nome": nome, "linhas": linhas})
    tot = {d: sum(i.qtd for i in itens if i.data == d) for d in dias}
    vol = {d: sum((i.volume or 0) for i in itens if i.data == d) for d in dias}
    return {"naves": naves, "tot": tot, "vol": vol}


def parada(paradas: list, f: Forma, d: date):
    """Premissa 'parar fôrma' (setup, ajuste, manutenção) que vale para esta fôrma neste dia."""
    from . import premissas as pm
    return next((pr for pr in paradas if pm.forma_parada([pr], f.codigo, d)), None)


def aviso_parada(db: Session, f: Forma, d: date) -> list[str]:
    from . import premissas as pm
    pr = parada([x for x in pm.ativas(db) if x.tipo == "parar_forma"], f, d)
    return [f"{f.nome} parada em {d:%d/%m}: {pr.obs or 'parar fôrma'}."] if pr else []


def mover(db: Session, rev: RevisaoProg, ids: list[int], f: Forma, d: date, forcar: bool = False) -> dict:
    """Leva itens já programados para outra fôrma/dia, com a mesma validação de quando se arrasta da lista."""
    its = [i for i in db.scalars(select(ItemProg).where(ItemProg.id.in_(ids), ItemProg.revisao_id == rev.id))]
    if not its:
        return {"ok": False, "problemas": ["Itens não encontrados nesta revisão."], "pode_forcar": False}
    pecas = list(db.scalars(select(Peca).where(Peca.codigo.in_([i.codigo for i in its if i.codigo] or [""]))))
    vig = [x for x in db.scalars(select(Forma)) if x.vigente(d)]
    por, _ = itens_por_forma(db, rev, d, vig)
    ja = [i for i in por.get(f.id, []) if i.id not in ids]
    probs = aviso_parada(db, f, d) + validar(f, d, pecas, uso(f, ja))
    probs += conflito_protensao(db, f, pecas, ja)
    if probs and not forcar:
        return {"ok": False, "problemas": probs, "pode_forcar": True}
    for i in its:
        i.data, i.nave, i.forma = d, f.nave, f.nome_completo
        if probs:
            i.obs = ("Forçado: " + " ".join(probs))[:300]
    datas = [rev.inicio, rev.fim, d]
    rev.inicio, rev.fim = min(x for x in datas if x), max(x for x in datas if x)
    db.commit()
    return {"ok": True, "n": len(its), "avisos": probs}
