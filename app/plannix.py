"""Leitura das exportações do Plannix (.xls que na verdade é texto separado por tabulação, em latin-1)
e da planilha de programação gerada pelo PCP."""
import io
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Importacao, Obra, Peca

NOMES_CURTOS = {
    "8243": "Farol (Hot Park)", "8246": "Elite", "8249": "Bay", "8206": "Cau Biglia", "8239": "Gandú (CBS)",
    "8198": "Aratu (Dona)", "8240": "QPC", "8245": "7Log", "8250": "New Jersey",
}


def _num(v):
    try:
        return float(str(v).strip().replace(".", "").replace(",", ".")) if "," in str(v) else float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _texto(v) -> str:
    t = str(v if v is not None else "").strip()
    return "" if t in ("", "-", "nan") else t[:60]


def _data(v) -> date | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(v).strip())
    return date(int(m[3]), int(m[2]), int(m[1])) if m else None


def _data_hora(v) -> datetime | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2})", str(v).strip())
    return datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5])) if m else None


def ler_exportacao(conteudo: bytes) -> pd.DataFrame:
    for enc in ("latin-1", "utf-8"):
        try:
            texto = conteudo.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    df = pd.read_csv(io.StringIO(texto), sep="\t", dtype=str).fillna("")
    faltando = {"Peça", "Código", "Nome Obra", "Produto"} - set(df.columns)
    if faltando:
        raise ValueError(f"Arquivo não parece uma exportação do Plannix. Faltam as colunas: {', '.join(sorted(faltando))}")
    # data de concretagem é o registro de produção: se a exportação trouxer essa coluna, ela vale como data de fabricação
    concreto = [c for c in df.columns if "CONCRET" in c.upper() and "DATA" in c.upper()]
    if concreto:
        c = df[concreto[0]]
        base = df["Data Fabricação"] if "Data Fabricação" in df.columns else ""
        df["Data Fabricação"] = c.where(c.map(_data).notna(), base)
    return df


BAHIA = timezone(timedelta(hours=-3))


def data_da_baixa(agora: datetime | None = None) -> date:
    """Lista 'a realizar' exportada de manhã (antes das 12h) reflete o que foi feito até o dia anterior:
    a peça que saiu da lista recebe o último dia de produção (domingo não conta)."""
    agora = agora or datetime.now(BAHIA)
    d = agora.date()
    if agora.hour < 12:
        d -= timedelta(days=1)
        if d.weekday() == 6:
            d -= timedelta(days=1)
    return d


def _sem_acento(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()


def _sigla(nome_plannix: str) -> str:
    return nome_plannix.strip().split("-")[0].strip()[:10]


def _obra_da_etapa(db: Session, nome_plannix: str, etapa: str, principal: str, multi: bool = True) -> Obra:
    """Cada etapa do Plannix é uma obra no app (ex.: 8243 Hot Park: Farol e Barracão). A obra que já existia (sem
    etapa) fica com a etapa principal (a que o nome dela cita, senão a de mais peças); as outras viram obras novas com
    a mesma prioridade e situação."""
    sigla = _sigla(nome_plannix)
    obras = list(db.scalars(select(Obra).where(Obra.sigla == sigla).order_by(Obra.id)))
    o = next((x for x in obras if x.etapa == etapa), None)
    if o:
        return o
    velha = next((x for x in obras if not x.etapa), None)
    if velha and etapa == principal:
        velha.etapa = etapa
        return velha
    base = velha or (obras[0] if obras else _obra(db, nome_plannix))
    if not base.etapa and base is not velha:
        base.etapa = principal
    o = Obra(sigla=sigla, etapa=etapa, nome_plannix=nome_plannix.strip(), nome=f"{etapa.title()} ({base.nome})"[:80],
             ativa=base.ativa, prioridade=base.prioridade)
    db.add(o)
    db.flush()
    return o


_MINUSC = {"de", "da", "do", "dos", "das", "e", "em"}


def etapa_legivel(etapa: str) -> str:
    """'BLOCO AMP. 24 SALAS - ARATU' -> 'Bloco AMP. 24 Salas - Aratu'; siglas curtas e códigos ficam em maiúsculas."""
    def um(w: str, primeiro: bool) -> str:
        if w.lower() in _MINUSC and not primeiro:
            return w.lower()
        if any(ch.isdigit() for ch in w) or len(w) <= 4:
            return w
        return w[:1] + w[1:].lower()
    out = []
    for w in (etapa or "").replace("_", " ").split(" "):
        out.append("-".join(um(x, not out and k == 0) for k, x in enumerate(w.split("-"))))
    return " ".join(out)


def base_da_sigla(os_: list) -> str:
    """Nome da obra sem a etapa: o que vem antes do " | ", senão o nome curto da primeira obra da sigla
    ('Farol (Hot Park)', que já cita a etapa, vira 'Hot Park')."""
    for o in os_:
        if " | " in o.nome:
            return o.nome.split(" | ")[0]
    orig = os_[0].nome.split(" · ")[0]
    m = re.search(r"\(([^)]+)\)\s*$", orig)
    et = _sem_acento((os_[0].etapa or "").split("-")[0].strip())
    if m and et and et in _sem_acento(orig):
        return m.group(1)
    return orig


def renomear_obra(db: Session, antigo: str, novo: str) -> None:
    """O nome da obra também está gravado nos itens de programação e nas premissas: troca em todos."""
    from sqlalchemy import update
    from .models import ItemProg, Premissa
    if antigo and novo and antigo != novo:
        db.execute(update(ItemProg).where(ItemProg.obra == antigo).values(obra=novo))
        db.execute(update(Premissa).where(Premissa.obra == antigo).values(obra=novo))


def nomes_das_etapas(db: Session) -> None:
    """Toda obra com etapa no Plannix se chama "Obra | Etapa" (ex.: "Hot Park | Farol", "Hot Park | Barracão").
    Os itens de programação seguem a obra (etapa) em que a peça está."""
    from .models import ItemProg
    por: dict[str, list] = {}
    for o in db.scalars(select(Obra).order_by(Obra.id)):
        por.setdefault(o.sigla, []).append(o)
    for os_ in por.values():
        base = base_da_sigla(os_)
        for o in os_:
            if not o.etapa:
                continue
            novo = f"{base} | {etapa_legivel(o.etapa)}"[:80]
            if o.nome != novo:
                renomear_obra(db, o.nome, novo)
                o.nome = novo
    db.flush()
    nome_obra = {o.id: o.nome for o in db.scalars(select(Obra))}
    obra_cod = dict(db.execute(select(Peca.codigo, Peca.obra_id)).all())
    for it in db.scalars(select(ItemProg).where(ItemProg.codigo.is_not(None))):
        oid = obra_cod.get(it.codigo)
        if oid and it.obra != nome_obra[oid]:
            it.obra = nome_obra[oid]


def _obra(db: Session, nome_plannix: str) -> Obra:
    sigla = _sigla(nome_plannix)
    obra = db.scalar(select(Obra).where(Obra.sigla == sigla).order_by(Obra.id))
    if not obra:
        nome = NOMES_CURTOS.get(sigla) or nome_plannix.split("-", 1)[-1].strip()[:80]
        obra = Obra(sigla=sigla, nome_plannix=nome_plannix.strip(), nome=nome, ativa=sigla in NOMES_CURTOS)
        db.add(obra)
        db.flush()
    return obra


def importar_exportacao(db: Session, conteudo: bytes, arquivo: str) -> Importacao:
    """Geral ('all data': com e sem data), completa (só com data) ou lista 'a realizar' (todas sem data).
    Na lista 'a realizar', peça da mesma obra que não está na lista conta como fabricada."""
    df = ler_exportacao(conteudo)
    datas = df["Data Fabricação"].map(_data) if "Data Fabricação" in df.columns else None
    com = int(datas.notna().sum()) if datas is not None else 0
    # geral ("all data"): fabricadas com data + pendentes sem data, retrato completo do Plannix
    # completa: só fabricadas (realizado do dia) | a_realizar: só pendentes
    tipo = "geral" if com and com < len(df) else ("completa" if com else "a_realizar")
    hoje = date.today()
    baixa = data_da_baixa()
    obras_no_arquivo: dict[int, Obra] = {}
    codigos = set()
    novas = atualizadas = 0
    existentes = {p.codigo: p for p in db.scalars(select(Peca))}
    cache_obras: dict[tuple, Obra] = {}
    tem_etapa = "Etapa" in df.columns
    principal: dict[str, str] = {}
    multi: dict[str, bool] = {}
    if tem_etapa:   # etapa principal de cada sigla: a que o nome da obra cita, senão a de mais peças
        from collections import Counter
        cont: dict[str, Counter] = {}
        for no, et in zip(df["Nome Obra"].astype(str), df["Etapa"].map(lambda v: (_texto(v) or "").upper())):
            cont.setdefault(_sigla(no), Counter())[et] += 1
        for sg, c in cont.items():
            o0 = db.scalar(select(Obra).where(Obra.sigla == sg, Obra.etapa == "").order_by(Obra.id))
            citada = [e for e in c if e and o0 and _sem_acento(e) in _sem_acento(o0.nome)]
            principal[sg] = citada[0] if citada else c.most_common(1)[0][0]
            multi[sg] = len([e for e in c if e]) > 1
    obras_id = {o.id: o for o in db.scalars(select(Obra))}
    for r in df.to_dict("records"):
        codigo = str(r["Código"]).strip()
        if not codigo:
            continue
        nome_obra = str(r["Nome Obra"])
        p = existentes.get(codigo)
        if tem_etapa:
            et = (_texto(r.get("Etapa")) or "").upper()
            k = (nome_obra, et)
            if k not in cache_obras:
                cache_obras[k] = _obra_da_etapa(db, nome_obra, et, principal.get(_sigla(nome_obra), et), multi.get(_sigla(nome_obra), False)) if et else _obra(db, nome_obra)
            obra = cache_obras[k]
        else:   # arquivo sem etapa: a peça fica na obra (etapa) em que já está
            obra = cache_obras.get((nome_obra, None)) or cache_obras.setdefault((nome_obra, None), _obra(db, nome_obra))
            atual = obras_id.get(p.obra_id) if p else None
            if atual and atual.sigla == obra.sigla:
                obra = atual
        obras_no_arquivo[obra.id] = obra
        codigos.add(codigo)
        if not p:
            p = Peca(obra_id=obra.id, codigo=codigo)
            db.add(p)
            existentes[codigo] = p
            novas += 1
        else:
            atualizadas += 1
            p.obra_id = obra.id   # peça transferida de obra no Plannix
        p.peca = str(r["Peça"]).strip()
        p.produto = str(r["Produto"]).strip()
        p.secao = str(r.get("Seção", "")).strip()
        p.comprimento = _num(r.get("Comprimento (m)")) or p.comprimento
        p.volume = _num(r.get("Volume (m³) Total de Peças (Lanç. Final)")) or p.volume
        if str(r.get("Item", "") or "").strip() not in ("", "nan"):
            p.item = str(r.get("Item")).strip()[:20]
        p.vol_projeto = _num(r.get("Volume Teórico de Projeto")) or p.vol_projeto
        if "Etapa" in r:
            p.etapa = (_texto(r.get("Etapa")) or "")[:80]
        p.hora_armacao = _data_hora(r.get("Hora da Armação", "")) or p.hora_armacao
        p.hora_corte_dobra = _data_hora(r.get("Hora do Corte e Dobra", "")) or p.hora_corte_dobra
        p.traco = _texto(r.get("Traço Real")) or p.traco or ""
        p.modelo_protensao = _texto(r.get("Modelo de Protensão")) or p.modelo_protensao or ""
        taxa = next((r[c] for c in r if str(c).startswith("Taxa Aço Protendido")), "")
        p.taxa_aco_prot = _num(taxa) if _num(taxa) else p.taxa_aco_prot
        frouxo = next((r[c] for c in r if str(c).startswith("Peso Aço Frouxo")), "")
        p.peso_aco_frouxo = _num(frouxo) if _num(frouxo) else p.peso_aco_frouxo
        df_ = _data(r.get("Data Fabricação", ""))
        if df_:
            p.fabricada, p.data_fabricacao, p.origem_fabricacao = True, df_, "plannix"
        elif tipo in ("a_realizar", "geral"):   # sem data no Plannix = pendente (o Plannix manda)
            p.fabricada, p.data_fabricacao, p.origem_fabricacao, p.data_baixa = False, None, None, None
        p.atualizada_em = datetime.now()
    if tem_etapa:
        nomes_das_etapas(db)
        for o in db.scalars(select(Obra).where(Obra.id.not_in(list(obras_id) or [0]))):   # etapa nova já toda feita: fora do painel
            ps = [p for p in existentes.values() if p.obra_id == o.id]
            if ps and all(p.fabricada for p in ps):
                o.ativa = False
    saiu_lista = removidas = 0
    if tipo == "geral":
        # retrato completo da obra: peça que sumiu do Plannix (desativada, trocada de código) sai do app
        for obra in obras_no_arquivo.values():
            velhas = [p for p in existentes.values() if p.obra_id == obra.id and p.codigo not in codigos and p.id]
            no_arquivo = sum(1 for p in existentes.values() if p.obra_id == obra.id and p.codigo in codigos)
            if velhas and no_arquivo >= 2 * len(velhas):   # proteção contra arquivo parcial
                for p in velhas:
                    db.delete(p)
                removidas += len(velhas)
    if tipo == "a_realizar":
        for obra in obras_no_arquivo.values():
            for p in db.scalars(select(Peca).where(Peca.obra_id == obra.id, Peca.fabricada.is_(False))):
                if p.codigo not in codigos:
                    p.fabricada, p.origem_fabricacao = True, "saiu_lista"
                    p.data_baixa = baixa
                    p.atualizada_em = datetime.now()
                    saiu_lista += 1
    # aço frouxo é do projeto da peça: unidades com o mesmo nome na obra herdam o peso das que já têm
    if any(str(c).startswith("Peso Aço Frouxo") for c in df.columns):
        from statistics import median
        for obra in obras_no_arquivo.values():
            ps = list(db.scalars(select(Peca).where(Peca.obra_id == obra.id)))
            pesos: dict[str, list[float]] = {}
            for p in ps:
                if p.peso_aco_frouxo:
                    pesos.setdefault(p.peca, []).append(p.peso_aco_frouxo)
            for p in ps:
                if not p.peso_aco_frouxo and p.peca in pesos:
                    p.peso_aco_frouxo = round(median(pesos[p.peca]), 2)
    imp = Importacao(arquivo=arquivo, tipo=tipo, resumo=dict(
        linhas=len(df), novas=novas, atualizadas=atualizadas, saiu_lista=saiu_lista, removidas=removidas,
        obras=sorted(o.nome for o in obras_no_arquivo.values()), data=hoje.isoformat()))
    db.add(imp)
    db.commit()
    return imp
