"""Leitura das exportações do Plannix (.xls que na verdade é texto separado por tabulação, em latin-1)
e da planilha de programação gerada pelo PCP."""
import io
import re
from datetime import date, datetime

import pandas as pd
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Importacao, Obra, Peca, Programacao

NOMES_CURTOS = {
    "8243": "Farol (Hot Park)", "8246": "Elite", "8249": "Bay", "8206": "Cau Biglia", "8239": "Gandú (CBS)",
    "8198": "Aratu (Dona)", "8240": "QPC", "8245": "7Log", "8250": "New Jersey",
}


def _num(v):
    try:
        return float(str(v).strip().replace(".", "").replace(",", ".")) if "," in str(v) else float(str(v).strip())
    except (TypeError, ValueError):
        return None


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
    return df


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
    """Completa (com 'Data Fabricação' preenchida) ou lista 'a realizar' (todas sem data).
    Na lista 'a realizar', peça da mesma obra que não está na lista conta como fabricada."""
    df = ler_exportacao(conteudo)
    tem_data = df["Data Fabricação"].map(_data).notna().any() if "Data Fabricação" in df.columns else False
    tipo = "completa" if tem_data else "a_realizar"
    hoje = date.today()
    obras_no_arquivo: dict[int, Obra] = {}
    codigos = set()
    novas = atualizadas = 0
    existentes = {p.codigo: p for p in db.scalars(select(Peca))}
    for r in df.to_dict("records"):
        codigo = str(r["Código"]).strip()
        if not codigo:
            continue
        obra = _obra(db, r["Nome Obra"])
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
        p.peca = str(r["Peça"]).strip()
        p.produto = str(r["Produto"]).strip()
        p.secao = str(r.get("Seção", "")).strip()
        p.comprimento = _num(r.get("Comprimento (m)")) or p.comprimento
        p.volume = _num(r.get("Volume (m³) Total de Peças (Lanç. Final)")) or p.volume
        p.hora_armacao = _data_hora(r.get("Hora da Armação", "")) or p.hora_armacao
        df_ = _data(r.get("Data Fabricação", ""))
        if df_:
            p.fabricada, p.data_fabricacao, p.origem_fabricacao = True, df_, "plannix"
        elif tipo == "a_realizar":
            p.fabricada, p.data_fabricacao, p.origem_fabricacao = False, None, None
        p.atualizada_em = datetime.now()
    saiu_lista = 0
    if tipo == "a_realizar":
        for obra in obras_no_arquivo.values():
            for p in db.scalars(select(Peca).where(Peca.obra_id == obra.id, Peca.fabricada.is_(False))):
                if p.codigo not in codigos:
                    p.fabricada, p.origem_fabricacao = True, "saiu_lista"
                    p.data_fabricacao = p.data_fabricacao or None
                    p.atualizada_em = datetime.now()
                    saiu_lista += 1
    imp = Importacao(arquivo=arquivo, tipo=tipo, resumo=dict(
        linhas=len(df), novas=novas, atualizadas=atualizadas, saiu_lista=saiu_lista,
        obras=sorted(o.nome for o in obras_no_arquivo.values()), data=hoje.isoformat()))
    db.add(imp)
    db.commit()
    return imp


def importar_programacao(db: Session, conteudo: bytes, arquivo: str) -> Importacao:
    """Planilha de programação do PCP (abas 'N1 + N3' e 'N2'): DATA na coluna A, FÔRMA na C, CÓDIGO na F."""
    wb = load_workbook(io.BytesIO(conteudo), data_only=True, read_only=True)
    abas = [a for a in ("N1 + N3", "N2") if a in wb.sheetnames]
    if not abas:
        raise ValueError("Planilha sem as abas 'N1 + N3' e 'N2' da programação.")
    linhas = []
    for aba in abas:
        ws = wb[aba]
        cab = None
        for row in ws.iter_rows(values_only=True):
            if cab is None:
                if row and row[0] == "DATA" and "CÓDIGO" in row:
                    cab = {k: i for i, k in enumerate(row) if k}
                continue
            cod = row[cab["CÓDIGO"]]
            if not cod:
                continue
            d = row[cab["DATA"]]
            linhas.append((str(cod).strip(), d.date() if isinstance(d, datetime) else d, str(row[cab["FÔRMA"]] or "")))
    if not linhas:
        raise ValueError("Não encontrei linhas de programação (cabeçalho DATA ... CÓDIGO).")
    db.query(Programacao).delete()
    vistos = set()
    for cod, d, f in linhas:
        if cod in vistos:
            continue
        vistos.add(cod)
        db.add(Programacao(codigo=cod, data=d, forma=f, revisao=arquivo))
    imp = Importacao(arquivo=arquivo, tipo="programacao", resumo=dict(linhas=len(vistos), data=date.today().isoformat()))
    db.add(imp)
    db.commit()
    return imp
