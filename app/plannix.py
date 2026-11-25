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


def _obra(db: Session, nome_plannix: str) -> Obra:
    sigla = nome_plannix.strip().split("-")[0].strip()[:10]
    obra = db.scalar(select(Obra).where(Obra.sigla == sigla))
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
    cache_obras: dict[str, Obra] = {}
    for r in df.to_dict("records"):
        codigo = str(r["Código"]).strip()
        if not codigo:
            continue
        nome_obra = str(r["Nome Obra"])
        obra = cache_obras.get(nome_obra) or cache_obras.setdefault(nome_obra, _obra(db, nome_obra))
        obras_no_arquivo[obra.id] = obra
        codigos.add(codigo)
        p = existentes.get(codigo)
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
