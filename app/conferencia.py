"""Conferência Plannix x app, peça por peça (pelo código). Serve para duas coisas:
1) depois de cada leitura automática, confere se o app ficou igual ao arquivo que acabou de chegar;
2) o PCP sobe qualquer exportação do Plannix (ex.: realizado de ontem) e vê se o app bate com ela.
Divergência = peça que falta no app, status ou data de fabricação diferente, ou fabricada recente em etapa oculta."""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import plannix
from .models import Config, Obra, Peca

MOTIVOS = {
    "falta": "Não está no app",
    "nao_fab": "Fabricada no Plannix, a fazer no app",
    "fab_app": "A fazer no Plannix, fabricada no app",
    "data": "Data de fabricação diferente",
    "oculta": "Fabricada nos últimos 7 dias, mas a etapa está oculta no app",
}


def conferir(db: Session, df, geral: bool | None = None, hoje: date | None = None) -> dict:
    """geral=True: o arquivo traz também as pendentes (linha sem data = a fazer). Arquivo só com fabricadas
    (ex.: realizado do dia) não acusa 'a fazer no Plannix'."""
    hoje = hoje or datetime.now(plannix.BAHIA).date()
    datas = df["Data Fabricação"].map(plannix._data) if "Data Fabricação" in df.columns else None
    if geral is None:
        com = int(datas.notna().sum()) if datas is not None else 0
        geral = bool(com and com < len(df))
    cods = [str(c).strip() for c in df["Código"]]
    app = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_([c for c in cods if c] or [""])))}
    obras = {o.id: o for o in db.scalars(select(Obra))}
    div, por_dia = [], defaultdict(lambda: {"plannix": 0, "app": 0})
    for k, c in enumerate(cods):
        if not c:
            continue
        d = datas.iloc[k] if datas is not None else None
        nome = str(df["Peça"].iloc[k]).strip()
        p = app.get(c)
        if d:
            por_dia[d]["plannix"] += 1
            if p is not None and p.fabricada and p.data_fabricacao == d:
                por_dia[d]["app"] += 1
        o = obras.get(p.obra_id) if p is not None else None
        linha = {"codigo": c, "peca": nome, "obra": o.nome if o else str(df["Nome Obra"].iloc[k]).strip(),
                 "plannix": d.strftime("%d/%m/%Y") if d else "a fazer",
                 "app": ("fabricada " + (p.data_fabricacao.strftime("%d/%m/%Y") if p.data_fabricacao else "sem data")
                         if p.fabricada else "a fazer") if p is not None else "-"}
        if p is None:
            div.append({**linha, "motivo": "falta"})
        elif d and not p.fabricada:
            div.append({**linha, "motivo": "nao_fab"})
        elif d and p.data_fabricacao != d:
            div.append({**linha, "motivo": "data"})
        elif not d and geral and p.fabricada and p.origem_fabricacao == "plannix":
            div.append({**linha, "motivo": "fab_app"})
        elif d and o is not None and not o.ativa and d >= hoje - timedelta(days=7):
            div.append({**linha, "motivo": "oculta"})
    dias = sorted(por_dia.items(), reverse=True)[:10]
    return {"linhas": len(df), "codigos": len({c for c in cods if c}), "divergencias": div,
            "por_motivo": {m: sum(1 for x in div if x["motivo"] == m) for m in MOTIVOS if any(x["motivo"] == m for x in div)},
            "dias": [{"d": d.strftime("%d/%m/%Y"), **v} for d, v in dias], "quando": datetime.now(plannix.BAHIA).strftime("%d/%m %H:%M")}


def gravar_automatica(db: Session, r: dict) -> None:
    resumo = {"quando": r["quando"], "n": len(r["divergencias"]), "por_motivo": r["por_motivo"],
              "ex": [f"{x['obra']} {x['peca']} ({x['codigo']}): {MOTIVOS[x['motivo']].lower()}" for x in r["divergencias"][:3]]}
    c = db.get(Config, "conferencia_auto") or Config(chave="conferencia_auto")
    c.valor = json.dumps(resumo, ensure_ascii=False)[:500]
    db.add(c)
    db.commit()


def ultima_automatica(db: Session) -> dict | None:
    c = db.get(Config, "conferencia_auto")
    try:
        return json.loads(c.valor) if c and c.valor else None
    except ValueError:
        return None
