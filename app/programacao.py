"""Revisões da programação e programado x realizado."""
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, datetime

from openpyxl import load_workbook
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from .models import Apontamento, Importacao, ItemProg, Obra, Peca, RevisaoProg

CAUSAS = ["Projeto / liberação", "Armação", "Fôrma", "Concreto / central", "Mão de obra", "Equipamento / ponte",
          "Qualidade / retrabalho", "Mudança de prioridade", "Apontamento no Plannix", "Material / insumo", "Clima", "Outros"]
STATUS = ["NO PRAZO", "COM ATRASO", "DATA A CONFIRMAR", "NÃO REALIZADA", "PENDENTE", "SEM INFORMAÇÃO"]
COR_STATUS = {"NO PRAZO": "#4caf50", "COM ATRASO": "#ffbf00", "DATA A CONFIRMAR": "#4582d9", "NÃO REALIZADA": "#e53935", "PENDENTE": "#c9d1db", "SEM INFORMAÇÃO": "#e7e9ed"}
DIAS = ["SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM"]


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def _float(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ".")) if not isinstance(v, (int, float)) else float(v)
    except ValueError:
        return None


def _nave(forma: str, aba: str) -> str:
    m = re.match(r"\s*(N[123])", forma or "")
    if m:
        return m[1]
    return "N2" if aba.strip().upper() == "N2" else ""


# ---------------------------------------------------------------- importação
def _cabecalho(ws):
    """Procura a linha de cabeçalho nas 10 primeiras linhas. Aceita a planilha do PCP (DATA / CÓDIGO)
    e a do engenheiro anterior (Código / DATA PROG.)."""
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), 1):
        nomes = [_txt(v).upper() for v in row]
        if ("CÓDIGO" in nomes or "CODIGO" in nomes) and ("DATA" in nomes or "DATA PROG." in nomes):
            return i, {n: j for j, n in enumerate(nomes) if n}
    return None, None


def importar_programacao(db: Session, conteudo: bytes, arquivo: str) -> Importacao:
    wb = load_workbook(io.BytesIO(conteudo), data_only=True, read_only=True)
    abas = [a for a in wb.sheetnames if a.strip().upper() in ("N1 + N3", "N2")]
    if not abas:
        raise ValueError("Planilha sem as abas 'N1 + N3' e 'N2' da programação.")
    obras = {o.sigla: o.nome for o in db.scalars(select(Obra))}
    rev = RevisaoProg(nome=arquivo)
    realizados = []
    n = 0
    for aba in abas:
        ws = wb[aba]
        hr, c = _cabecalho(ws)
        if not hr:
            continue
        g = lambda row, *ks: next((row[c[k]] for k in ks if k in c and c[k] < len(row)), None)
        for row in ws.iter_rows(min_row=hr + 1, values_only=True):
            d = g(row, "DATA", "DATA PROG.")
            if isinstance(d, datetime):
                d = d.date()
            if not isinstance(d, date):
                continue
            forma = _txt(g(row, "FÔRMA", "FORMA"))
            obra = _txt(g(row, "OBRA", "NOME OBRA", "NOME DA OBRA (PLANNIX)"))
            sigla = obra.split("-")[0].strip()
            obra = obras.get(sigla, obra[:80])
            cod = _txt(g(row, "CÓDIGO", "CODIGO")) or None
            if not obra:   # planilha sem coluna de obra: usa a obra da peça no Plannix (ou do lote, pelo nome)
                pc = db.scalar(select(Peca).where(Peca.codigo == cod)) if cod else None
                if not pc and not cod:
                    base = _txt(g(row, "PEÇA")).split(" (")[0].strip()
                    pc = db.scalar(select(Peca).where(Peca.peca == base).limit(1)) if base else None
                obra = pc.obra.nome if pc else ""
            qtd = g(row, "QTD")
            it = ItemProg(data=d, nave=_nave(forma, aba), forma=forma, obra=obra, peca=_txt(g(row, "PEÇA")), codigo=cod,
                          produto=_txt(g(row, "PRODUTO")), secao=_txt(g(row, "SEÇÃO")),
                          comprimento=_float(g(row, "COMP. (M)", "COMPRIMENTO (M)")), qtd=int(qtd) if isinstance(qtd, (int, float)) and qtd else 1,
                          volume=_float(g(row, "VOL. (M³)", "VOLUME (M³) TOTAL DE PEÇAS (LANÇ. FINAL)")), tipo=_txt(g(row, "TIPO"))[:30],
                          obs=_txt(g(row, "OBSERVAÇÃO", "OBS"))[:300])
            rev.itens.append(it)
            r = _txt(g(row, "REALIZADO")).upper()
            if r in ("S", "N"):
                realizados.append((it, r))
            n += 1
    if not n:
        raise ValueError("Não encontrei linhas programadas (cabeçalho com DATA e CÓDIGO).")
    datas = [i.data for i in rev.itens]
    rev.inicio, rev.fim = min(datas), max(datas)
    db.add(rev)
    db.flush()
    for it, r in realizados:
        db.add(Apontamento(item_id=it.id, realizado=r))
    imp = Importacao(arquivo=arquivo, tipo="programacao", resumo=dict(linhas=n, data=date.today().isoformat(),
                                                                       obras=sorted({i.obra for i in rev.itens if i.obra})[:12]))
    db.add(imp)
    db.commit()
    return imp


def revisao_atual(db: Session) -> RevisaoProg | None:
    return db.scalar(select(RevisaoProg).where(RevisaoProg.rascunho.is_not(True))
                     .order_by(RevisaoProg.criada_em.desc(), RevisaoProg.id.desc()))


def codigos_programados(db: Session) -> dict[str, date]:
    rev = revisao_atual(db)
    if not rev:
        return {}
    # só o que está no plano de hoje em diante: dia passado sem fabricação já foi reprogramado ou saiu do plano
    return {c: d for c, d in db.execute(select(ItemProg.codigo, ItemProg.data).where(
        ItemProg.revisao_id == rev.id, ItemProg.codigo.is_not(None), ItemProg.data >= date.today()))}


# ---------------------------------------------------------------- programado x realizado
@dataclass
class Linha:
    item: ItemProg
    status: str
    data_fab: date | None
    desvio: int | None
    ap: Apontamento | None
    peca: Peca | None = None
    qtd: int | None = None   # parte do item (lote sem código dividido em feito e não feito)
    troca: str | None = None  # código da peça idêntica que foi concretada no lugar da programada
    origem: str | None = None  # de onde vem a data: plannix | manual

    def __post_init__(self):
        if self.qtd is None:
            self.qtd = self.item.qtd



def classificar(item: ItemProg, peca: Peca | None, ap: Apontamento | None, hoje: date, obra_acompanhada: bool = True) -> tuple[str, date | None]:
    """Registro de produção = data de fabricação (concretagem) do Plannix. Sem ela: data de execução do PCP > apontamento S/N > data a confirmar > pendente.
    Código que não existe na base, de obra acompanhada, já tinha saído da lista a realizar: conta como feito, data a confirmar."""
    manual = ap.data_execucao if ap else None
    if peca is None and item.codigo:
        if manual:
            return ("NO PRAZO" if manual <= item.data else "COM ATRASO"), manual
        if ap and ap.realizado in ("S", "N"):
            return ("NO PRAZO" if ap.realizado == "S" else "NÃO REALIZADA"), None
        return ("DATA A CONFIRMAR" if obra_acompanhada else "SEM INFORMAÇÃO"), None
    if peca and peca.fabricada and peca.data_fabricacao:
        return ("NO PRAZO" if peca.data_fabricacao <= item.data else "COM ATRASO"), peca.data_fabricacao
    if manual:   # data de execução informada pelo PCP (vale até o Plannix ter a data)
        return ("NO PRAZO" if manual <= item.data else "COM ATRASO"), manual
    if peca and peca.fabricada:
        # saiu da lista 'a realizar' sem data de concretagem no Plannix: feita, mas o prazo só se mede com a data
        if ap and ap.realizado == "S":
            return "NO PRAZO", None
        return "DATA A CONFIRMAR", None
    if ap and ap.realizado == "S":
        return "NO PRAZO", None
    if item.data >= hoje and not (ap and ap.realizado == "N"):
        return "PENDENTE", None
    return "NÃO REALIZADA", None


def linhas(db: Session, rev: RevisaoProg, inicio: date | None = None, fim: date | None = None, hoje: date | None = None) -> list[Linha]:
    hoje = hoje or date.today()
    q = select(ItemProg).where(ItemProg.revisao_id == rev.id).options(selectinload(ItemProg.apontamento))
    if inicio:
        q = q.where(ItemProg.data >= inicio)
    if fim:
        q = q.where(ItemProg.data <= fim)
    itens = list(db.scalars(q.order_by(ItemProg.data, ItemProg.nave, ItemProg.forma, ItemProg.obra, ItemProg.peca)))
    cods = {i.codigo for i in itens if i.codigo}
    pecas = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_(cods)))} if cods else {}
    acompanhadas = {o.nome for o in db.scalars(select(Obra)) if o.pecas}
    lotes = _lotes_sem_codigo(db, [i for i in itens if not i.codigo and i.qtd > 1])
    subst = substitutas(db, rev)
    out = []
    for i in itens:
        feitos = lotes.get(i.id)
        if feitos is not None and not (i.apontamento and i.apontamento.data_execucao):
            # lote sem código (ex.: New Jersey): conta no Plannix as peças da obra com o mesmo nome concretadas no dia
            n = min(feitos, i.qtd)
            if n:
                out.append(Linha(i, "NO PRAZO", i.data, 0, i.apontamento, None, n, origem="plannix"))
            if i.qtd - n:
                st = "PENDENTE" if i.data >= hoje else "NÃO REALIZADA"
                out.append(Linha(i, st, None, None, i.apontamento, None, i.qtd - n))
            continue
        st, df = classificar(i, pecas.get(i.codigo), i.apontamento, hoje, i.obra in acompanhadas)
        troca = None
        if i.id in subst:   # peça idêntica: vale a fila de concretagens do grupo
            sp = subst[i.id]
            if sp:
                df, troca = sp.data_fabricacao, (sp.codigo if sp.codigo != i.codigo else None)
                st = "NO PRAZO" if df <= i.data else "COM ATRASO"
            else:
                df = None
                st = "NO PRAZO" if i.apontamento and i.apontamento.realizado == "S" else ("PENDENTE" if i.data >= hoje else "NÃO REALIZADA")
        pc = pecas.get(i.codigo)
        if not df:
            origem = None
        elif i.id in subst or (pc and pc.data_fabricacao == df):
            origem = "plannix"
        else:
            origem = "manual" if i.apontamento and i.apontamento.data_execucao == df else "plannix"
        out.append(Linha(i, st, df, (df - i.data).days if df else None, i.apontamento, pc, troca=troca, origem=origem))
    return out


def substitutas(db: Session, rev: RevisaoProg) -> dict[int, Peca | None]:
    """Peças com o mesmo nome na mesma obra são iguais: a produção pode concretar qualquer uma delas. Para cada grupo,
    as concretagens (por data) atendem os itens programados na ordem das datas, sem olhar o código: o que ficou
    atrasado é atendido primeiro. Devolve, por item,
    a peça que o atendeu (None = ainda não atendido). Calculado sobre a revisão inteira."""
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.codigo.is_not(None))
                            .options(selectinload(ItemProg.apontamento)).order_by(ItemProg.data, ItemProg.id)))
    if not itens:
        return {}
    na_rev = {i.codigo for i in itens}
    pecas = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_(na_rev)))}
    grupos: dict[tuple, list[ItemProg]] = {}
    for i in itens:
        p = pecas.get(i.codigo)
        # fora da fila: data informada pelo PCP e peça que saiu da lista sem data (feita, data a confirmar)
        if p and not (i.apontamento and i.apontamento.data_execucao) and not (p.fabricada and not p.data_fabricacao):
            grupos.setdefault((p.obra_id, p.peca), []).append(i)
    grupos = {k: v for k, v in grupos.items() if len(v) > 1 or not pecas[v[0].codigo].data_fabricacao}
    if not grupos:
        return {}
    desde = (rev.inicio or itens[0].data) - timedelta(days=7)
    fabs: dict[tuple, list[Peca]] = {}
    for p in db.scalars(select(Peca).where(Peca.data_fabricacao.is_not(None), Peca.peca.in_({k[1] for k in grupos}))):
        k = (p.obra_id, p.peca)
        if k in grupos and (p.codigo in na_rev or p.data_fabricacao >= desde):
            fabs.setdefault(k, []).append(p)
    out = {}
    for k, its in grupos.items():
        fila = sorted(fabs.get(k, []), key=lambda p: (p.data_fabricacao, p.codigo))
        if not fila and all(not pecas[i.codigo].data_fabricacao for i in its):
            continue
        # fila: a concretagem mais antiga atende o item programado mais antigo (o atrasado é feito primeiro)
        for n, i in enumerate(its):
            out[i.id] = fila[n] if n < len(fila) else None
    return out


def _lotes_sem_codigo(db: Session, itens: list[ItemProg]) -> dict[int, int]:
    """Para itens programados em lote sem código (\"NJ-180 (13 un.)\"), quantas peças da mesma obra e mesmo nome
    o Plannix registra como concretadas na data programada."""
    if not itens:
        return {}
    obras = {o.nome: o.id for o in db.scalars(select(Obra))}
    out = {}
    for i in itens:
        oid = obras.get(i.obra)
        base = (i.peca or "").split(" (")[0].strip()
        if not base:
            continue
        q = select(func.count(Peca.id)).where(Peca.peca == base, Peca.data_fabricacao == i.data)
        if oid:   # sem obra na planilha, procura o nome da peça em todas as obras
            q = q.where(Peca.obra_id == oid)
        out[i.id] = db.scalar(q) or 0
    return out


def agrega(ls: list[Linha], chave) -> list[dict]:
    g = defaultdict(Counter)
    for l in ls:
        g[chave(l)][l.status] += l.qtd
    out = []
    for k, c in g.items():
        prog = sum(c.values()) - c["PENDENTE"] - c["SEM INFORMAÇÃO"]
        out.append(dict(chave=k, prog=prog, pend=c["PENDENTE"], **{s: c[s] for s in STATUS},
                        ppc=(c["NO PRAZO"] / prog if prog else None),
                        feito=((c["NO PRAZO"] + c["COM ATRASO"] + c["DATA A CONFIRMAR"]) / prog if prog else None)))
    return out


def totais(ls: list[Linha]) -> dict:
    a = agrega(ls, lambda l: "total")
    return a[0] if a else dict(prog=0, pend=0, ppc=None, feito=None, **{s: 0 for s in STATUS})
