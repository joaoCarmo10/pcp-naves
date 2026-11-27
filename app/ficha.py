"""Ficha de fabricação da peça: dados do Plannix/Autodoc, programação, itens e embutidos (complementos do cadastro
de produtos do Plannix, editáveis pelo PCP), acabamento, consolo e feedback de tempos e dificuldade."""
import io
import re
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import autodoc
from . import cadastro as cad
from . import programacao as pg
from .models import ComplementoPeca, FichaFab, ItemProg, Peca

FAMILIAS = [  # ordem na ficha
    ("Consolo", r"^(CONSOLO|C\d|CT\d)"),
    ("Luva", r"^LUVA"),
    ("Bainha", r"^BAINHA"),
    ("Neoprene / elastômero", r"^(NP|ELAST)"),
    ("Chumbador", r"^(CHUMBADOR|CH\d)"),
    ("Inserto", r"^(INS|INSERT)"),
    ("SPDA / aterramento", r"^(ATERRAMENTO|BANDEIRA)"),
    ("Chapa / cantoneira", r"^(CP|CHAPA|CANTONEIRA)"),
    ("Elétrica", r"^(ELETRODUTO|CAIXA DE PASSAGEM|PVC)"),
    ("Enchimento / EPS", r"^(ENCHIMENTO|EPS)"),
]
ACABAMENTOS = ["Liso de fôrma", "Desempenado", "Alisado", "Ranhurado", "Aparente", "Outro"]
STATUS_ITEM = ["Separado", "Na fôrma", "Falta"]
ARMACAO = "Armação (aço frouxo, kg)"
CONSOLOS = [("C1-N3", "C1 nível 3 · EPS"), ("C1-N6", "C1 nível 6 · luva"), ("C23-N9", "C2/C3 nível 9 · bainha")]
ETAPAS_FB = [("arm", "Armação"), ("forma", "Fôrma"), ("prod", "Produção (concretagem)")]
INFO = {"Sigla", "Nome da Obra", "Etapa", "Peça", "Código", "Produto", "Grupo", "Seção", "Detalhe"}


def chave_tipo(p) -> str:
    """Registro dos itens incluídos que valem para todas as unidades da peça (mesma obra/etapa e mesmo nome)."""
    return f"tipo:{p.obra_id}:{p.peca}"[:60]


def familia(coluna: str) -> str:
    u = coluna.upper().strip()
    return next((f for f, pat in FAMILIAS if re.match(pat, u)), "Outros")


def _nome_col(c: str) -> str:
    return re.sub(r"\s*und\d*\s*$", "", str(c)).strip()


# ---------------------------------------------------------------- importação da tabela de complementos

def detectar(conteudo: bytes) -> bool:
    cab = conteudo[:6000].decode("latin-1", "ignore")
    return cab.startswith("Projeto - ") and "\tDetalhe\t" in cab and " und" in cab


def importar(db: Session, conteudo: bytes) -> dict:
    """Relatório de complementos do Plannix (texto com tabulação): guarda, por código, só os itens não zerados.
    Códigos do arquivo que não têm mais nada ficam vazios (o Plannix zerou)."""
    linhas = conteudo.decode("latin-1").replace("\r\n", "\n").split("\n")
    i = next(k for k, l in enumerate(linhas[:10]) if l.startswith("Sigla\t"))
    df = pd.read_csv(io.StringIO("\n".join(linhas[i:])), sep="\t", dtype=str, keep_default_na=False)
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
    df = df[df["Código"].str.strip().str.match(r"^\d+$")]
    cols = [c for c in df.columns if c not in INFO]
    atuais = {c.codigo: c for c in db.scalars(select(ComplementoPeca))}
    n = com = 0
    for r in df.to_dict("records"):
        itens: dict[str, float] = {}
        for c in cols:
            try:
                v = float(str(r[c]).replace(",", ".") or 0)
            except ValueError:
                continue
            if v:
                k = _nome_col(c)
                itens[k] = itens.get(k, 0) + v
        cod = r["Código"].strip()
        x = atuais.get(cod) or ComplementoPeca(codigo=cod)
        x.sigla, x.peca, x.itens, x.atualizado_em = r["Sigla"].strip()[:10], r["Peça"].strip()[:60], itens, datetime.now()
        if cod not in atuais:
            db.add(x)
        n += 1
        com += 1 if itens else 0
    db.commit()
    return {"linhas": n, "com_itens": com, "colunas": len(cols)}


# ---------------------------------------------------------------- ficha

def _prog(db: Session, codigo: str) -> ItemProg | None:
    rev = pg.revisao_atual(db)
    if not rev:
        return None
    its = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.codigo == codigo).order_by(ItemProg.data)))
    hoje = date.today()
    return next((i for i in its if i.data >= hoje), its[-1] if its else None)


def montar(db: Session, codigo: str) -> dict | None:
    p = db.scalar(select(Peca).where(Peca.codigo == codigo))
    if not p:
        return None
    f = db.get(FichaFab, codigo)
    d = dict(f.dados) if f else {}
    c = cad.de_peca(cad.indice(db), p)
    ad = autodoc.de(autodoc.indice(db), p.obra_id, p.peca)
    it = _prog(db, codigo)
    comp = db.get(ComplementoPeca, codigo)
    plannix = dict(comp.itens) if comp else {}
    salvo = d.get("itens", {})
    tipo_f = db.get(FichaFab, chave_tipo(p))
    extras = {**{k: {**v, "escopo": "todas"} for k, v in ((tipo_f.dados or {}).get("extras", {}) if tipo_f else {}).items()},
              **{k: {**v, "escopo": "unidade"} for k, v in d.get("extras", {}).items()}}
    grupos: dict[str, list] = {}
    aco = p.peso_aco_frouxo or (c.aco_passivo if c and c.aco_passivo else None)
    vazio = {"usar": None, "local": "", "status": "", "incluido": None}
    grupos.setdefault("Armação", []).append({"chave": ARMACAO, "nome": "Armadura", "plannix": round(aco, 1) if aco else None,
                                             "un": "kg", **vazio, **salvo.get(ARMACAO, {})})
    antigos = {k for k in salvo if k != ARMACAO and k not in plannix and k not in extras}   # incluídos na versão anterior
    for nome in sorted(set(plannix) | set(extras) | antigos, key=lambda k: (familia(k), k)):
        ex = extras.get(nome)
        fam = (ex or {}).get("familia") or familia(nome)
        x = {"chave": nome, "nome": nome, "plannix": plannix.get(nome), "un": "", **vazio, **salvo.get(nome, {})}
        if ex:
            x["incluido"] = ex
            if x["usar"] is None:
                x["usar"] = ex.get("qtd")
        elif nome in antigos:
            x["incluido"] = {"escopo": "unidade", "quem": "", "quando": ""}
        grupos.setdefault(fam, []).append(x)
    ordem = ["Armação"] + [fm for fm, _ in FAMILIAS] + ["Outros"]
    grupos_l = []
    for fm in ordem:
        if fm not in grupos:
            continue
        its = grupos[fm]
        for x in its:
            x["efetivo"] = x["usar"] if x["usar"] is not None else x["plannix"]
        un = sum(x["efetivo"] or 0 for x in its) if fm != "Armação" else None
        grupos_l.append((fm, its, un))
    nao_leva = [fm for fm, _ in FAMILIAS if fm not in grupos]
    n_itens = sum(len(its) for _, its, _ in grupos_l)
    unidades = list(db.scalars(select(Peca).where(Peca.obra_id == p.obra_id, Peca.peca == p.peca).order_by(Peca.codigo)))
    prot = "Não"
    if p.modelo_protensao or (c and c.cordoalhas):
        prot = " · ".join(x for x in [p.modelo_protensao,
                                      f"{c.cordoalhas} cordoalhas Ø{c.cord_diam}" if c and c.cordoalhas else ""] if x)
    info = [x for x in [ad.observacoes if ad and ad.observacoes else "", it.obs if it and it.obs else "",
                        c.etapa2_obs if c and c.etapa2_obs else ""] if x]
    cons_padrao = "2" if (c and c.etapa2 == "sim") else ("1" if c and c.etapa2 == "nao" else "")
    return {
        "p": p, "f": f, "d": d, "c": c, "ad": ad, "it": it, "grupos": grupos_l, "unidades": unidades,
        "n_itens": n_itens, "nao_leva": nao_leva, "FAMILIAS_TODAS": [fm for fm, _ in FAMILIAS] + ["Outros"],
        "pos": next((k + 1 for k, u in enumerate(unidades) if u.codigo == p.codigo), 1),
        "prot": prot, "info": " · ".join(info), "cons_padrao": cons_padrao, "tem_complemento": comp is not None,
        "nave": d.get("nave") or (it.nave if it else ""), "forma": d.get("forma") or (it.forma if it else ""),
        "data": d.get("data") or (it.data.strftime("%d/%m/%Y") if it else ""),
        "ACABAMENTOS": ACABAMENTOS, "STATUS_ITEM": STATUS_ITEM, "CONSOLOS": CONSOLOS, "ETAPAS_FB": ETAPAS_FB,
    }


def _num(v):
    try:
        return float(str(v).replace(",", ".")) if str(v).strip() != "" else None
    except ValueError:
        return None


def salvar(db: Session, codigo: str, form, quem: str, pode_qtd: bool) -> FichaFab:
    """Grava o formulário. A quantidade "usar" só muda para PCP/administrador; toda mudança de quantidade fica no
    histórico com o valor anterior."""
    f = db.get(FichaFab, codigo) or FichaFab(codigo=codigo, dados={}, alteracoes=[])
    if f not in db:
        db.add(f)
    d = dict(f.dados or {})
    alt = list(f.alteracoes or [])
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    for k in ("nave", "forma", "data", "acabamento", "acab_obs", "cons_metodo", "cons_tipo", "obs"):
        d[k] = (form.get(k) or "").strip()[:300]
    itens = dict(d.get("itens", {}))
    comp = db.get(ComplementoPeca, codigo)
    base = dict(comp.itens) if comp else {}
    p = db.scalar(select(Peca).where(Peca.codigo == codigo))
    if p and p.peso_aco_frouxo:
        base[ARMACAO] = round(p.peso_aco_frouxo, 1)
    t0 = db.get(FichaFab, chave_tipo(p)) if p else None
    for k, v in {**((t0.dados or {}).get("extras", {}) if t0 else {}), **d.get("extras", {})}.items():
        base.setdefault(k, v.get("qtd"))
    for chave in form.getlist("item"):
        x = dict(itens.get(chave, {}))
        x["local"] = (form.get(f"local::{chave}") or "").strip()[:80]
        st = (form.get(f"status::{chave}") or "").strip()
        x["status"] = st if st in STATUS_ITEM else ""
        if pode_qtd:
            novo = _num(form.get(f"usar::{chave}"))
            efetivo = x.get("usar") if x.get("usar") is not None else base.get(chave)
            if novo != efetivo:   # só o que o PCP mudou de fato; o resto continua seguindo o Plannix
                alt.append({"quando": agora, "quem": quem, "campo": chave, "de": efetivo, "para": novo})
                x["usar"] = novo
        itens[chave] = x
    nova = (form.get("novo_item") or "").strip()[:60]
    if nova and pode_qtd and p:
        fam = form.get("novo_familia") or "Outros"
        ex = {"familia": fam if fam in [f for f, _ in FAMILIAS] + ["Outros"] else "Outros", "qtd": _num(form.get("novo_qtd")),
              "quem": quem, "quando": agora}
        if form.get("novo_escopo") == "todas":
            t = db.get(FichaFab, chave_tipo(p)) or FichaFab(codigo=chave_tipo(p), dados={}, alteracoes=[])
            if t not in db:
                db.add(t)
            td = dict(t.dados or {})
            td["extras"] = {**td.get("extras", {}), nova: ex}
            t.dados, t.atualizado_em, t.atualizado_por = td, datetime.now(), quem
        else:
            d["extras"] = {**d.get("extras", {}), nova: ex}
        alt.append({"quando": agora, "quem": quem, "campo": f"incluído: {nova}", "de": None, "para": ex["qtd"]})
    rem = form.get("remover") or ""
    if rem and pode_qtd and p and "::" in rem:
        escopo, chave = rem.split("::", 1)
        if escopo == "todas" and (t := db.get(FichaFab, chave_tipo(p))):
            td = dict(t.dados or {})
            td["extras"] = {k: v for k, v in td.get("extras", {}).items() if k != chave}
            t.dados = td
        else:
            d["extras"] = {k: v for k, v in d.get("extras", {}).items() if k != chave}
        itens.pop(chave, None)
        alt.append({"quando": agora, "quem": quem, "campo": f"removido: {chave}", "de": None, "para": None})
    d["itens"] = itens
    fb = {}
    for k, _ in ETAPAS_FB:
        dif = _num(form.get(f"fb_dif_{k}"))
        fb[k] = {"esp": _num(form.get(f"fb_esp_{k}")), "real": _num(form.get(f"fb_real_{k}")),
                 "dif": int(dif) if dif and 1 <= dif <= 5 else None, "obs": (form.get(f"fb_obs_{k}") or "").strip()[:200]}
    d["fb"] = fb
    f.dados, f.alteracoes, f.atualizado_em, f.atualizado_por = d, alt[-200:], datetime.now(), quem
    db.commit()
    return f
