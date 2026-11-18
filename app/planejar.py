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
    from . import cadastro as cad
    idx = cad.indice(db)
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
                             "prot": c.protensao if c else None, "aco": round(c.aco_passivo) if c and c.aco_passivo else None}
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
    probs = validar(f, d, pecas, uso(f, por.get(f.id, [])))
    probs += conflito_protensao(db, f, pecas, por.get(f.id, []))
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
