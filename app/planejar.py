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


def hoje_bahia() -> date:
    from datetime import datetime
    from .plannix import BAHIA
    return datetime.now(BAHIA).date()


def programados(db: Session, rev: RevisaoProg, hoje: date | None = None) -> set[str]:
    """Códigos com lugar nesta revisão de hoje em diante. Peça programada num dia que já passou e não foi feita
    volta a ser 'a programar' (fica no dia antigo só como histórico do PPC)."""
    hoje = hoje or hoje_bahia()
    return {c for (c,) in db.execute(select(ItemProg.codigo).where(ItemProg.revisao_id == rev.id, ItemProg.codigo.is_not(None),
                                                                   ItemProg.data >= hoje))}


def faltantes(db: Session, rev: RevisaoProg | None) -> list[dict]:
    """Peças não fabricadas das obras ativas sem lugar nesta revisão de hoje em diante (as que ficaram para trás em dia
    passado voltam, marcadas como atrasadas), agrupadas por peça igual e ordenadas pelos critérios do PCP:
    prioridade da obra, armadas primeiro, produto e nome da peça. Lido do banco a cada abertura: fabricada no
    Plannix sai da lista na próxima leitura do notebook."""
    ja, atras = set(), set()
    if rev:
        ja = programados(db, rev)
        atras = {c for (c,) in db.execute(select(ItemProg.codigo).where(ItemProg.revisao_id == rev.id, ItemProg.codigo.is_not(None),
                                                                        ItemProg.data < hoje_bahia()))} - ja
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
                             "produto": p.produto, "codigos": [], "armadas": 0, "atrasadas": 0,
                             "prot": c.protensao if c else None, "aco": round(c.aco_passivo) if c and c.aco_passivo else None,
                             "e2": (c.etapa2_obs or "sim") if c and c.segunda_etapa else None,
                             "ad": autodoc.resumo(autodoc.de(ad, o.id, p.peca))}
        g["codigos"].append(p.codigo)
        if p.codigo in atras:
            g["atrasadas"] += 1
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
    ja = programados(db, rev) & set(codigos)
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


# ---------------------------------------------------------------- autopreencher (completa sem tirar o que já está)

def autopreencher(db: Session, rev: RevisaoProg, dias: list[date], so_formas: set[int] | None = None,
                  so_codigos: set[str] | None = None, lote: str | None = None,
                  dias_forma: dict[int, set[date]] | None = None) -> dict:
    """Completa a capacidade livre das fôrmas no período com as peças que faltam, na ordem de prioridade do PCP
    (fura-fila, prioridade da obra, armadas primeiro, produto, nome). Não tira nem move nada do que já está.
    Respeita: encaixe (produto, seção, comprimento), capacidade, fôrma parada (setup), premissas (não programar,
    só a partir de, máximo por dia), desenho bloqueado no Autodoc e uma seção por fôrma por dia. Itens marcados
    com o lote para poder desfazer."""
    import secrets
    from . import autodoc
    from . import premissas as pm
    prs = pm.ativas(db)
    paradas = [pr for pr in prs if pr.tipo == "parar_forma"]
    hoje = hoje_bahia()
    dias = [d for d in dias if d >= hoje]
    ja = programados(db, rev)
    armadas = {c for (c,) in db.execute(select(Armacao.codigo).where(Armacao.codigo.is_not(None)))}
    adx = autodoc.indice(db)
    cands = []
    for p, o in db.execute(select(Peca, Obra).join(Obra).where(Peca.fabricada.is_(False), Obra.ativa.is_(True))):
        if p.codigo in ja or pm.motivo_fora(prs, p) or (so_codigos is not None and p.codigo not in so_codigos):
            continue
        x = autodoc.de(adx, p.obra_id, p.peca)
        if x and x.bloqueado:
            continue
        cands.append(p)
    cands.sort(key=lambda p: (0 if pm.fura_fila(prs, p) else 1, p.obra.prioridade or 99,
                              0 if (p.hora_armacao or p.codigo in armadas) else 1, p.produto, chave_natural(p.peca), p.codigo))
    usados: set[str] = set()
    lote = lote or "AUTO-" + secrets.token_hex(3)
    novos, cont = 0, defaultdict(int)
    formas = [f for f in db.scalars(select(Forma).order_by(Forma.nave, Forma.y, Forma.x)) if not so_formas or f.id in so_formas]
    for d in dias:
        vig = [f for f in formas if f.vigente(d)]
        por, _ = itens_por_forma(db, rev, d, vig)
        for f in vig:
            if parada(paradas, f, d) or (dias_forma and f.id in dias_forma and d not in dias_forma[f.id]):
                continue   # dias_forma: a fôrma só produz nesses dias (ex.: P1 seg/qua/sex, P2 ter/qui/sáb)
            cap = capacidade(f, d)
            if not cap:
                continue
            atual = por.get(f.id, [])
            livre = cap - uso(f, atual)
            secao = next((i.secao for i in atual if i.secao), None)   # uma seção por fôrma por dia
            for p in cands:
                if livre <= 1e-6:
                    break
                if p.codigo in usados or not fm.aceita(f, p.produto, p.secao, p.comprimento):
                    continue
                if secao and (p.secao or "").upper() != secao.upper():
                    continue
                na = pm.nao_antes(prs, p)
                if na and d < na:
                    continue
                lim = pm.limites(prs, p)
                if any(cont[(pid, f.id, d)] >= l for pid, l in lim):
                    continue
                ocupa = 1 if f.tipo == "unidade" else (p.comprimento or 0)
                if not ocupa or ocupa > livre + 1e-6:
                    continue
                if f.tipo == "pista" and conflito_protensao(db, f, [p], atual):
                    continue
                it = ItemProg(revisao_id=rev.id, data=d, nave=f.nave, forma=f.nome_completo, obra=p.obra.nome, peca=p.peca,
                              codigo=p.codigo, produto=p.produto, secao=p.secao, comprimento=p.comprimento, qtd=1,
                              volume=p.volume, tipo="PLANEJADA", obs=f"Autopreenchido ({lote})")
                db.add(it)
                atual.append(it)
                usados.add(p.codigo)
                secao = secao or p.secao
                livre -= ocupa
                novos += 1
                for pid, _ in lim:
                    cont[(pid, f.id, d)] += 1
    if novos:
        rev.inicio = min(x for x in [rev.inicio, dias[0]] if x)
        rev.fim = max(x for x in [rev.fim, dias[-1]] if x)
    db.commit()
    return {"ok": True, "n": novos, "lote": lote, "sobram": len(cands) - len(usados)}


def desfazer_auto(db: Session, rev: RevisaoProg, lote: str | None = None) -> int:
    """Tira os itens do último autopreenchimento (ou do lote informado). O que foi arrastado à mão fica."""
    its = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.obs.like("Autopreenchido (AUTO-%"))))
    if not its:
        return 0
    import re
    alvo = lote or re.search(r"AUTO-[0-9a-f]+", max(its, key=lambda i: i.id).obs).group(0)
    n = 0
    for i in its:
        if alvo in i.obs:
            db.delete(i)
            n += 1
    db.commit()
    return n


def trazer_faltantes(db: Session, rev: RevisaoProg, origem: RevisaoProg, dias: list[date],
                     so_formas: set[int] | None = None, lote: str | None = None,
                     dias_forma: dict[int, set[date]] | None = None) -> dict:
    """Peças de outra revisão (ex.: a última programação) programadas até o fim do período e ainda não fabricadas, que
    não estão nesta revisão: primeiro tenta o mesmo dia e fôrma de lá (se o dia está no período e cabe); o resto entra
    pelo autopreenchimento, na ordem de prioridade. Nada do que já está nesta revisão é tirado."""
    import secrets
    from . import premissas as pm
    hoje = hoje_bahia()
    dias = [d for d in dias if d >= hoje]
    lote = lote or "AUTO-" + secrets.token_hex(3)
    ja = programados(db, rev)
    its = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == origem.id, ItemProg.data <= dias[-1]).order_by(ItemProg.data)))
    sem_codigo = sorted({f"{i.obra} {i.peca}" for i in its if not i.codigo})
    cods = [i.codigo for i in its if i.codigo and i.codigo not in ja]
    feitas = {c for (c,) in db.execute(select(Peca.codigo).where(Peca.codigo.in_(cods or [""]), Peca.fabricada.is_(True)))}
    pend = [i for i in its if i.codigo and i.codigo not in ja and i.codigo not in feitas]
    paradas = [pr for pr in pm.ativas(db) if pr.tipo == "parar_forma"]
    formas = list(db.scalars(select(Forma)))
    if so_formas:   # só o que estava nessas fôrmas na outra revisão (ex.: lajes da Nave 2)
        alvo = [f for f in formas if f.id in so_formas]
        pend = [i for i in pend if i.forma and any(fm.item_e_da_forma(i.forma, f) for f in alvo)]
        formas = alvo
    mesmo = 0
    for i in pend:
        if i.data not in dias:
            continue
        f = next((f for f in formas if f.vigente(i.data) and i.forma and fm.item_e_da_forma(i.forma, f)), None)
        if not f or parada(paradas, f, i.data):
            continue
        p = db.scalar(select(Peca).where(Peca.codigo == i.codigo))
        if not p:
            continue
        por, _ = itens_por_forma(db, rev, i.data, [f])
        atual = por.get(f.id, [])
        if validar(f, i.data, [p], uso(f, atual)) or conflito_protensao(db, f, [p], atual):
            continue
        db.add(ItemProg(revisao_id=rev.id, data=i.data, nave=f.nave, forma=f.nome_completo, obra=p.obra.nome, peca=p.peca,
                        codigo=p.codigo, produto=p.produto, secao=p.secao, comprimento=p.comprimento, qtd=1, volume=p.volume,
                        tipo="PLANEJADA", obs=f"Autopreenchido ({lote}) · mesmo lugar da {origem.nome[:40]}"))
        db.flush()
        mesmo += 1
    db.commit()
    resto = {i.codigo for i in pend} - programados(db, rev)
    r = autopreencher(db, rev, dias, so_formas=so_formas, so_codigos=resto, lote=lote, dias_forma=dias_forma) if resto else {"n": 0, "sobram": 0}
    return {"ok": True, "lote": lote, "faltantes": len({i.codigo for i in pend}), "mesmo_lugar": mesmo, "auto": r["n"],
            "sem_lugar": len(resto) - r["n"], "sem_codigo": sem_codigo, "ja_feitas": len(feitas)}


def acumulado(db: Session, rev: RevisaoProg, dias: list[date], hoje: date) -> dict:
    """Quanto cada obra/etapa (ativa) vai somando até a metragem total: fabricado antes do período + produzido no
    período (até hoje) + programado a fazer (de hoje ao fim do período, não fabricado). Programado em dia que já passou
    e não foi feito não entra na estimativa (fica como 'para trás')."""
    vazio = {"obras": [], "tot": {}, "dia": [], "periodo": {}}
    if not dias:
        return vazio
    d0, d1 = dias[0], dias[-1]
    obras = {o.id: o for o in db.scalars(select(Obra).where(Obra.ativa.is_(True)))}
    por_nome = {o.nome: o.id for o in obras.values()}
    pecas = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.obra_id.in_(list(obras) or [0])))}
    vol = lambda p: p.volume or p.vol_projeto or 0.0
    quando = lambda p: p.data_fabricacao or p.data_baixa

    def novo(oid):
        o = obras[oid]
        return {"id": oid, "nome": o.nome, "pri": o.prioridade if o.prioridade is not None else 999,
                "vol": 0.0, "pc": 0, "antes": 0.0, "antes_pc": 0, "per": 0.0, "per_pc": 0,
                "prog": 0.0, "prog_pc": 0, "tras": 0.0, "tras_pc": 0}
    t: dict = {}
    real_dia: dict = defaultdict(float)
    for p in pecas.values():
        x = t.setdefault(p.obra_id, novo(p.obra_id))
        x["vol"] += vol(p)
        x["pc"] += 1
        if not p.fabricada:
            continue
        d = quando(p)
        if d is None or d < d0:
            x["antes"] += vol(p); x["antes_pc"] += 1
        elif d <= d1:
            x["per"] += vol(p); x["per_pc"] += 1
            real_dia[d] += vol(p)
    prog_dia: dict = defaultdict(float)
    vistos: set = set()
    for i in db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.data >= d0, ItemProg.data <= d1)):
        p = pecas.get(i.codigo) if i.codigo else None
        if i.codigo and i.codigo in vistos:
            continue
        if p is not None:
            vistos.add(i.codigo)
            if p.fabricada:
                continue
            oid, v, n = p.obra_id, vol(p), 1
        else:
            oid = por_nome.get(i.obra)
            if oid is None or (i.codigo and db.scalar(select(Peca.fabricada).where(Peca.codigo == i.codigo))):
                continue                      # obra desmarcada ou peça de fora já feita
            v, n = (i.volume or 0.0), (i.qtd or 1)
        x = t.setdefault(oid, novo(oid))
        if i.data < hoje:
            x["tras"] += v; x["tras_pc"] += n
        else:
            x["prog"] += v; x["prog_pc"] += n
            prog_dia[i.data] += v
    lin = []
    for x in t.values():
        if not (x["per"] or x["prog"] or x["tras"]):
            continue
        base = max(x["vol"], x["antes"] + x["per"] + x["prog"]) or 1.0
        x["est"] = x["antes"] + x["per"] + x["prog"]
        x["est_pc"] = x["antes_pc"] + x["per_pc"] + x["prog_pc"]
        x["falta"] = max(0.0, x["vol"] - x["est"])
        x["falta_pc"] = max(0, x["pc"] - x["est_pc"])
        x["pct"] = {k: 100 * x[k] / base for k in ("antes", "per", "prog")}
        x["pct_est"] = 100 * x["est"] / x["vol"] if x["vol"] else 0
        lin.append(x)
    lin.sort(key=lambda x: (x["pri"], x["nome"]))
    soma = lambda k: sum(x[k] for x in lin)
    tot = {k: soma(k) for k in ("vol", "pc", "antes", "antes_pc", "per", "per_pc", "prog", "prog_pc", "tras", "tras_pc", "est", "est_pc", "falta")}
    dia, ac = [], 0.0
    for d in dias:
        v = (real_dia.get(d, 0.0) if d <= hoje else 0.0) + (prog_dia.get(d, 0.0) if d >= hoje else 0.0)
        ac += v
        dia.append({"d": d, "v": v, "ac": ac, "real": d < hoje})
    return {"obras": lin, "tot": tot, "dia": dia, "periodo": {"per": tot["per"], "prog": tot["prog"], "fim": tot["per"] + tot["prog"]}}


def _arquivo_reorg(rev_id: int):
    from .db import DATA_DIR
    d = DATA_DIR / "reorganizar"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"rev_{rev_id}.json"


def tem_reorg(rev_id: int) -> bool:
    return _arquivo_reorg(rev_id).exists()


def reorganizar(db: Session, rev: RevisaoProg, dias: list[date], hoje: date | None = None) -> dict:
    """Recoloca as peças programadas (de hoje ao fim do período, não fabricadas) na fôrma que faz mais sentido.
    Cada peça fica no mesmo dia quando há fôrma boa nele; senão vai para o próximo dia com lugar; sem lugar, fica onde
    está. Ordem: peças com menos fôrmas possíveis primeiro, depois a prioridade do PCP. Preferência de fôrma:
    1) fôrma da própria seção (não adaptada); 2) fôrma que já tem a mesma seção no dia (junta e abre menos fôrmas);
    3) a fôrma onde a peça já estava; 4) a que fica mais cheia (menos sobra). Respeita encaixe, capacidade, uma seção
    por fôrma por dia, protensão igual na pista, fôrma parada e premissas. Guarda a posição anterior para desfazer."""
    import json
    from . import cadastro as cad
    from . import premissas as pm
    hoje = hoje or date.today()
    dias = [d for d in dias if d >= hoje]
    if not dias:
        return {"ok": False, "erro": "O período não tem dia de hoje em diante."}
    prs = pm.ativas(db)
    paradas = [pr for pr in prs if pr.tipo == "parar_forma"]
    formas = list(db.scalars(select(Forma).order_by(Forma.nave, Forma.y, Forma.x)))
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.data >= dias[0], ItemProg.data <= dias[-1])))
    pecas = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_([i.codigo for i in itens if i.codigo] or [""])))}
    moveis = [i for i in itens if i.codigo in pecas and not pecas[i.codigo].fabricada and (i.qtd or 1) == 1 and i.data in dias]
    ids_moveis = {i.id for i in moveis}
    antes = {i.id: [i.data.isoformat(), i.forma, i.nave] for i in moveis}
    cache: dict = {}

    def forma_de(i):
        if i.forma not in cache:
            cache[i.forma] = next((f for f in formas if i.forma and fm.item_e_da_forma(i.forma, f)), None)
        return cache[i.forma]
    origem = {i.id: forma_de(i) for i in moveis}
    cel: dict = defaultdict(list)                     # (forma.id, dia) -> itens que ficam (fixos) + já recolocados
    for i in itens:
        if i.id not in ids_moveis and (f := forma_de(i)):
            cel[(f.id, i.data)].append(i)
    idx = cad.indice(db)
    arr: dict = {}

    def arranjo(cod):
        if cod not in arr:
            p = pecas.get(cod) or db.scalar(select(Peca).where(Peca.codigo == cod))
            c = cad.de_peca(idx, p) if p else None
            arr[cod] = c.protensao if c else None
        return arr[cod]
    possiveis = {i.id: [f for f in formas if fm.aceita(f, pecas[i.codigo].produto, pecas[i.codigo].secao, pecas[i.codigo].comprimento)]
                 for i in moveis}
    armadas = {c for (c,) in db.execute(select(Armacao.codigo).where(Armacao.codigo.is_not(None)))}

    def prio(i):
        p = pecas[i.codigo]
        return (len(possiveis[i.id]) or 999, 0 if pm.fura_fila(prs, p) else 1, p.obra.prioridade or 99,
                0 if (p.hora_armacao or p.codigo in armadas) else 1, chave_natural(p.peca), p.codigo)
    cont: dict = defaultdict(int)

    def melhor(i, d):
        p = pecas[i.codigo]
        na = pm.nao_antes(prs, p)
        if na and d < na:
            return None
        lim = pm.limites(prs, p)
        ops = []
        for f in possiveis[i.id]:
            if not f.vigente(d) or parada(paradas, f, d):
                continue
            cap = capacidade(f, d)
            if not cap or any(cont[(pid, f.id, d)] >= l for pid, l in lim):
                continue
            atual = cel[(f.id, d)]
            ocupa = 1 if f.tipo == "unidade" else (p.comprimento or 0)
            sobra = cap - uso(f, atual) - ocupa
            if not ocupa or sobra < -1e-6:
                continue
            secs = {fm._norm(x.secao) for x in atual if x.secao}
            if secs and secs != {fm._norm(p.secao)}:
                continue
            a = arranjo(p.codigo)
            if a is not None and (f.tipo == "pista" or a):
                outros = {arranjo(x.codigo) for x in atual if x.codigo} - {None}
                if outros and outros != {a}:
                    continue
            propria = fm._norm(f.secao) == fm._norm(p.secao)
            ops.append(((0 if propria else 1, 0 if secs else 1, 0 if origem[i.id] is f else 1, sobra / cap), f))
        return min(ops, key=lambda x: x[0])[1] if ops else None
    res = {"mesmo_lugar": 0, "outra_forma": 0, "outro_dia": 0, "sem_lugar": 0}
    for i in sorted(moveis, key=lambda i: (i.data, prio(i))):
        f, d = None, i.data
        for dd in [x for x in dias if x >= i.data]:
            f = melhor(i, dd)
            if f:
                d = dd
                break
        if not f:
            res["sem_lugar"] += 1
            if (fo := origem[i.id]):
                cel[(fo.id, i.data)].append(i)
            continue
        if f is origem[i.id] and d == i.data:
            res["mesmo_lugar"] += 1
        elif d == i.data:
            res["outra_forma"] += 1
        else:
            res["outro_dia"] += 1
        i.data, i.nave, i.forma = d, f.nave, f.nome_completo
        cel[(f.id, d)].append(i)
        for pid, _ in pm.limites(prs, pecas[i.codigo]):
            cont[(pid, f.id, d)] += 1
    _arquivo_reorg(rev.id).write_text(json.dumps(antes))
    db.commit()
    return {"ok": True, "n": len(moveis), **res}


def desfazer_reorg(db: Session, rev: RevisaoProg) -> int:
    """Volta cada peça para o dia e a fôrma de antes da última reorganização."""
    import json
    arq = _arquivo_reorg(rev.id)
    if not arq.exists():
        return 0
    antes = json.loads(arq.read_text())
    n = 0
    for i in db.scalars(select(ItemProg).where(ItemProg.id.in_([int(k) for k in antes] or [0]))):
        d, forma, nave = antes[str(i.id)]
        i.data, i.forma, i.nave = date.fromisoformat(d), forma, nave
        n += 1
    db.commit()
    arq.unlink()
    return n
