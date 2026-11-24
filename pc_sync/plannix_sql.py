"""Sincroniza o Plannix (SQL Server da fábrica) com o PCP Naves.

Só LÊ o banco do Plannix, sem travar ninguém (leitura suja, timeout curto). Roda num PC da rede da fábrica,
agendado no Windows. Três comandos:

    py plannix_sync.py descobrir [--procurar 8239]   mapeia tabelas, colunas e consultas salvas -> plannix_esquema.txt
    py plannix_sync.py testar                         roda consulta.sql e confere as colunas (nada é enviado)
    py plannix_sync.py enviar                         roda consulta.sql e manda para o app (é o que o agendamento chama)

Configuração em plannix_sync.ini, ao lado deste arquivo. A senha do banco fica só nesse arquivo, neste PC.
"""
import argparse
import configparser
import datetime as dt
import decimal
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
INI = AQUI / "plannix_sync.ini"
SQL = AQUI / "consulta.sql"
ESTADO = AQUI / ".ultimo_envio.json"
ESQUEMA = AQUI / "plannix_esquema.txt"

# colunas que o app usa do "all data" (o nome tem que sair igual da consulta, com AS [Nome])
OBRIGATORIAS = ["Peça", "Código", "Nome Obra", "Produto"]
DESEJADAS = ["Sigla Obra", "Item", "Seção", "Qtde", "Volume Total", "Data Fabricação", "Comprimento", "Traço Real",
             "Hora da Armação", "Hora do Corte e Dobra", "Modelo de Protensão", "Taxa Aço Protendido",
             "Volume Teórico de Projeto"]
PISTAS = ["fabric", "concret", "peca", "peça", "obra", "protens", "armac", "teoric", "traco", "traço", "forma", "volume"]


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%d/%m/%Y %H:%M:%S}  {msg}", flush=True)


def config() -> configparser.SectionProxy:
    if not INI.exists():
        sys.exit(f"Falta o arquivo {INI.name}. Copie plannix_sync.exemplo.ini para {INI.name} e preencha.")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(INI, encoding="utf-8")
    return cp["plannix"]


def conectar(cfg):
    try:
        import pyodbc
    except ImportError:
        sys.exit("Falta o pyodbc. Rode: py -m pip install pyodbc")
    preferidos = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 13 for SQL Server",
                  "SQL Server Native Client 11.0", "SQL Server"]
    drv = next((d for d in preferidos if d in pyodbc.drivers()), None)
    if not drv:
        sys.exit("Nenhum driver ODBC do SQL Server neste PC. Instale o 'ODBC Driver 18 for SQL Server' da Microsoft.")
    partes = [f"DRIVER={{{drv}}}", f"SERVER={cfg['servidor']}", f"DATABASE={cfg['banco']}", "APP=PCP Naves (leitura)"]
    if cfg.get("usuario", "").strip():
        partes += [f"UID={cfg['usuario'].strip()}", f"PWD={{{cfg.get('senha', '')}}}"]
    else:
        partes.append("Trusted_Connection=yes")   # login do Windows
    if drv.startswith("ODBC Driver 18"):
        partes.append("TrustServerCertificate=yes")   # servidor interno costuma ter certificado próprio
    cx = pyodbc.connect(";".join(partes), timeout=15, readonly=True)
    cx.timeout = int(cfg.get("timeout_consulta", "120"))
    cur = cx.cursor()
    # não disputar bloqueio com o Plannix: lê o que estiver gravado e desiste rápido se algo estiver travado
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; SET LOCK_TIMEOUT 5000; SET NOCOUNT ON;")
    return cx


def _celula(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        return v.strftime("%d/%m/%Y %H:%M") if (v.hour or v.minute) else v.strftime("%d/%m/%Y")
    if isinstance(v, dt.date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, (float, decimal.Decimal)):
        return format(v, "f").rstrip("0").rstrip(".") if "." in format(v, "f") else format(v, "f")
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def rodar_consulta(cfg) -> tuple[list[str], list[list[str]]]:
    if not SQL.exists():
        sys.exit(f"Falta o arquivo {SQL.name} com a consulta do 'all data'.")
    sql = SQL.read_text(encoding="utf-8")
    with conectar(cfg) as cx:
        cur = cx.cursor()
        cur.execute(sql)
        while cur.description is None and cur.nextset():   # pula resultados vazios de SETs no início do arquivo
            pass
        if cur.description is None:
            sys.exit("A consulta não devolveu nenhuma tabela.")
        cols = [c[0] for c in cur.description]
        linhas = [[_celula(v) for v in r] for r in cur.fetchall()]
    return cols, linhas


def tsv(cols, linhas) -> bytes:
    texto = "\n".join("\t".join(r) for r in [cols, *linhas]) + "\n"
    return texto.encode("latin-1", errors="replace")


def conferir(cols) -> list[str]:
    avisos = []
    falta = [c for c in OBRIGATORIAS if c not in cols]
    if falta:
        avisos.append("FALTAM colunas obrigatórias: " + ", ".join(falta))
    falta = [c for c in DESEJADAS if c not in cols]
    if falta:
        avisos.append("Faltam (o app funciona, mas perde detalhe): " + ", ".join(falta))
    return avisos


# ---------------------------------------------------------------- comandos

def cmd_descobrir(cfg, procurar: str | None) -> None:
    """Mapa do banco para montar a consulta: nada de dados de produção sai daqui, só nomes de tabelas e colunas."""
    out = []
    with conectar(cfg) as cx:
        cur = cx.cursor()
        cur.execute("SELECT @@VERSION, DB_NAME()")
        v, banco = cur.fetchone()
        out += [f"Banco: {banco}", f"Versão: {v.splitlines()[0]}", ""]

        cur.execute("""SELECT s.name, t.name, SUM(p.rows) FROM sys.tables t
                       JOIN sys.schemas s ON s.schema_id = t.schema_id
                       JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
                       GROUP BY s.name, t.name ORDER BY SUM(p.rows) DESC""")
        tabelas = [(s, t, int(n or 0)) for s, t, n in cur.fetchall()]
        out.append(f"== TABELAS ({len(tabelas)}) : linhas aproximadas")
        out += [f"{n:>10}  {s}.{t}" for s, t, n in tabelas]

        cur.execute("""SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                       ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION""")
        colunas: dict[str, list[tuple[str, str]]] = {}
        for s, t, c, tipo in cur.fetchall():
            colunas.setdefault(f"{s}.{t}", []).append((c, tipo))
        out += ["", "== COLUNAS QUE PARECEM DE PEÇA / OBRA / FABRICAÇÃO"]
        for tab, cs in colunas.items():
            achou = [f"{c} ({tipo})" for c, tipo in cs if any(p in c.lower() for p in PISTAS)]
            if achou:
                out.append(f"{tab}: " + ", ".join(achou))

        cur.execute("""SELECT s.name, o.name, o.type_desc, m.definition FROM sys.sql_modules m
                       JOIN sys.objects o ON o.object_id = m.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id""")
        mods = cur.fetchall()
        sem_def = sum(1 for m in mods if m[3] is None)
        relevantes = [m for m in mods if m[3] and sum(p in m[3].lower() for p in PISTAS) >= 3]
        out += ["", f"== VIEWS E PROCEDURES COM CARA DE RELATÓRIO DE PRODUÇÃO ({len(relevantes)} de {len(mods)})"]
        if sem_def:
            out.append(f"({sem_def} sem definição visível: o usuário não tem VIEW DEFINITION)")
        for s, n, tipo, d in relevantes:
            out += ["", f"--- {tipo} {s}.{n}", d.strip()]

        if procurar:
            out += ["", f"== ONDE APARECE O VALOR '{procurar}'"]
            numero = procurar.isdigit()
            for s, t, n in tabelas:
                if not n:
                    continue
                cs = [c for c, tipo in colunas.get(f"{s}.{t}", [])
                      if tipo in ("varchar", "nvarchar", "char", "nchar") or (numero and tipo in ("int", "bigint", "smallint", "numeric", "decimal"))]
                for c in cs:
                    try:
                        cur.execute(f"SELECT TOP 1 1 FROM [{s}].[{t}] WITH (NOLOCK) WHERE [{c}] = ?", int(procurar) if numero and not _texto(colunas, s, t, c) else procurar)
                        if cur.fetchone():
                            out.append(f"{s}.{t}.{c}")
                    except Exception:   # noqa: BLE001  coluna que não aceita a comparação: segue
                        continue

        out += ["", "== TODAS AS COLUNAS"]
        for tab, cs in colunas.items():
            out.append(f"{tab}: " + ", ".join(f"{c} ({tipo})" for c, tipo in cs))
    ESQUEMA.write_text("\n".join(out), encoding="utf-8")
    log(f"Mapa salvo em {ESQUEMA.name} ({len(tabelas)} tabelas, {len(relevantes)} consultas candidatas). Mande esse arquivo.")


def _texto(colunas, s, t, c) -> bool:
    return dict(colunas.get(f"{s}.{t}", [])).get(c) in ("varchar", "nvarchar", "char", "nchar")


def cmd_testar(cfg) -> None:
    t0 = time.time()
    cols, linhas = rodar_consulta(cfg)
    log(f"{len(linhas)} linhas, {len(cols)} colunas em {time.time() - t0:.1f}s")
    log("Colunas: " + " | ".join(cols))
    for a in conferir(cols):
        log(a)
    datas = sum(1 for r in linhas if "Data Fabricação" in cols and r[cols.index("Data Fabricação")])
    log(f"Com data de fabricação: {datas}  |  pendentes: {len(linhas) - datas}")
    (AQUI / "amostra.tsv").write_bytes(tsv(cols, linhas[:30]))
    log("30 primeiras linhas em amostra.tsv (só neste PC). Compare com o 'all data' exportado do Plannix.")


def cmd_enviar(cfg, forcar: bool) -> int:
    cols, linhas = rodar_consulta(cfg)
    falta = [c for c in OBRIGATORIAS if c not in cols]
    if falta:
        log("Consulta sem as colunas obrigatórias: " + ", ".join(falta))
        return 2
    corpo = tsv(cols, linhas)
    h = hashlib.sha256(corpo).hexdigest()
    est = json.loads(ESTADO.read_text()) if ESTADO.exists() else {}
    batimento = int(cfg.get("batimento_min", "60")) * 60
    if not forcar and est.get("hash") == h and time.time() - est.get("quando", 0) < batimento:
        log(f"Sem mudança ({len(linhas)} linhas). Nada enviado.")
        return 0
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/api/plannix", data=gzip.compress(corpo), method="POST", headers={
        "Authorization": f"Bearer {cfg['token'].strip()}", "Content-Type": "text/tab-separated-values; charset=latin-1",
        "Content-Encoding": "gzip", "User-Agent": "plannix-sync"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        log(f"App recusou ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}")
        return 3
    except urllib.error.URLError as e:
        log(f"Sem conexão com o app: {e.reason}")
        return 4
    ESTADO.write_text(json.dumps({"hash": h, "quando": time.time()}))
    if resp.get("sem_mudanca"):
        log(f"Enviado, app já tinha esses dados ({len(linhas)} linhas).")
    else:
        r = resp.get("resumo", {})
        log(f"Enviado: {r.get('linhas')} linhas, {r.get('novas')} peças novas.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Plannix -> PCP Naves")
    ap.add_argument("comando", choices=["descobrir", "testar", "enviar"])
    ap.add_argument("--procurar", help="valor conhecido (ex.: código de uma peça) para achar em que tabela ele mora")
    ap.add_argument("--forcar", action="store_true", help="envia mesmo sem mudança")
    a = ap.parse_args()
    cfg = config()
    try:
        if a.comando == "descobrir":
            cmd_descobrir(cfg, a.procurar)
        elif a.comando == "testar":
            cmd_testar(cfg)
        else:
            sys.exit(cmd_enviar(cfg, a.forcar))
    except Exception as e:   # noqa: BLE001  o agendador só vê o log: registra e sai com erro
        log(f"ERRO: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
