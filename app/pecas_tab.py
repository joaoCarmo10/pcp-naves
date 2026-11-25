"""Tabela do módulo Peças: um tipo por linha (obra + nome + etapa), dados do Plannix completados pela prancha.
Usada pela tela e pela planilha baixada, para as duas mostrarem exatamente o mesmo."""
import io
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cadastro as cad
from .models import Obra, Peca
from .status import chave_natural

FILTROS = ("obra", "produto", "situacao", "q", "pend", "prot", "e2")


def dim_secao(secao: str, produto: str = "") -> tuple[float | None, float | None]:
    """(largura, altura) em cm a partir da seção do Plannix. 'h20' -> (None, 20).
    Laje, placa e painel: a menor medida é a espessura ('16X125' -> (125, 16)). Viga: a maior é a altura.
    Demais (pilar etc.): como vem escrito."""
    s = (secao or "").strip().upper().replace(",", ".")
    m = re.fullmatch(r"H\s*(\d+(?:\.\d+)?)", s)
    if m:
        return None, float(m[1])
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)", s)
    if not m:
        return None, None
    a, b = float(m[1]), float(m[2])
    prod = (produto or "").upper()
    if any(x in prod for x in ("LAJE", "PLACA", "PAINEL")):
        return max(a, b), min(a, b)
    if "VIGA" in prod:
        return min(a, b), max(a, b)
    return a, b


def _estribo(pos: str) -> bool:
    return bool(re.match(r"(ES|E|EST)\d", (pos or "").upper()))


def _da_prancha(c) -> dict:
    """Números de armadura tirados dos itens da prancha (kg e nº de barras por bitola)."""
    bit: dict[str, list[float]] = {}
    barras = estribos = 0
    for i in c.itens or []:
        if i.get("tipo") != "passiva" or not i.get("diam"):
            continue
        q = i.get("quant") or 0
        k = f"{i['diam']:g}"
        b = bit.setdefault(k, [0.0, 0])
        b[0] += i.get("peso_tot") or 0
        b[1] += q
        barras += q
        estribos += q if _estribo(i.get("pos")) else 0
    if not bit:   # cadastro antigo ou editado à mão: só os kg por bitola
        bit = {k: [kg, 0] for k, kg in (c.bitolas or {}).items()}
    return {"bit": {k: (round(v[0], 2), int(v[1])) for k, v in bit.items()}, "barras": int(barras), "estribos": int(estribos)}


def montar(db: Session, qp) -> dict:
    f = {k: (qp.get(k) or "") for k in FILTROS}
    from . import autodoc
    obras = {o.id: o for o in db.scalars(select(Obra))}
    idx = cad.indice(db)
    ad_idx = autodoc.indice(db)
    tipos: dict = {}
    for p in db.scalars(select(Peca)):
        k = (p.obra_id, cad._canon(p.peca), p.item or "")
        t = tipos.get(k)
        if not t:
            larg, alt = dim_secao(p.secao, p.produto)
            t = tipos[k] = {"o": obras[p.obra_id], "nome": p.peca, "item": p.item or "", "produto": p.produto, "secao": p.secao,
                            "larg": larg, "alt": alt, "comp": p.comprimento, "vol": p.vol_projeto or p.volume,
                            "n": 0, "fab": 0, "c": cad.de_peca(idx, p), "_traco": Counter(), "_taxa": [], "_modelo": Counter(),
                            "ad": autodoc.resumo(autodoc.de(ad_idx, p.obra_id, p.peca))}
        t["n"] += 1
        t["fab"] += 1 if p.fabricada else 0
        if p.traco:
            t["_traco"][p.traco] += 1
        if p.taxa_aco_prot:
            t["_taxa"].append(p.taxa_aco_prot)
        if p.modelo_protensao:
            t["_modelo"][p.modelo_protensao] += 1
    todos = list(tipos.values())
    ligados = {id(t["c"]) for t in todos if t["c"]}
    orfaos = sorted((c for cs in idx.values() for c in cs if id(c) not in ligados and c.obra_id in obras),
                    key=lambda c: (obras[c.obra_id].nome, chave_natural(c.peca)))
    opcoes = {"obra": sorted({t["o"].nome for t in todos}), "produto": sorted({t["produto"] for t in todos if t["produto"]})}
    busca = cad._canon(f["q"].strip())

    def passa(t):
        c = t["c"]
        return not ((f["obra"] and t["o"].nome != f["obra"]) or (f["produto"] and t["produto"] != f["produto"])
                    or (f["situacao"] == "sem" and c) or (f["situacao"] == "com" and not c)
                    or (f["situacao"] in ("ok", "revisar") and (not c or c.situacao != f["situacao"]))
                    or (busca and busca not in cad._canon(t["nome"])) or (f["pend"] and t["fab"] >= t["n"])
                    or (f["prot"] and not (c and c.cordoalhas)) or (f["e2"] and not (c and c.segunda_etapa)))

    linhas = sorted((t for t in todos if passa(t)), key=lambda t: (t["o"].nome, t["produto"], chave_natural(t["nome"]), chave_natural(t["item"])))
    diams: set[str] = set()
    pend_bit: dict[str, list[float]] = {}
    for t in linhas:
        t["pend"] = t["n"] - t["fab"]
        t["traco"] = t["_traco"].most_common(1)[0][0] if t["_traco"] else ""
        t["taxa_prot_plannix"] = sum(t["_taxa"]) / len(t["_taxa"]) if t["_taxa"] else None
        t["modelo"] = t["_modelo"].most_common(1)[0][0] if t["_modelo"] else ""
        c = t["c"]
        t["peso_t"], t["peso_est"] = ((c.peso / 1000, False) if c and c.peso else ((t["vol"] * 2.5, True) if t["vol"] else (None, False)))
        if c:
            a = _da_prancha(c)
            t.update(a)
            vol = c.volume_un or t["vol"]
            t["taxa"] = (c.aco_passivo or 0) / vol if vol and c.aco_passivo else None
            diams.update(a["bit"])
            if t["pend"]:
                for d, (kg, n) in a["bit"].items():
                    pb = pend_bit.setdefault(d, [0.0, 0])
                    pb[0] += kg * t["pend"]
                    pb[1] += n * t["pend"]
        else:
            t.update({"bit": {}, "barras": None, "estribos": None, "taxa": None})
    chave = lambda d: float(d)
    tot = {"tipos": len(linhas), "com": sum(1 for t in linhas if t["c"]),
           "rev": sum(1 for t in linhas if t["c"] and t["c"].situacao == "revisar"),
           "pend": sum(t["pend"] for t in linhas), "vol_pend": sum((t["vol"] or 0) * t["pend"] for t in linhas),
           "aco_pend": sum((t["c"].aco_passivo or 0) * t["pend"] for t in linhas if t["c"]),
           "ativo_pend": sum((t["c"].aco_ativo or 0) * t["pend"] for t in linhas if t["c"]),
           "barras_pend": sum((t["barras"] or 0) * t["pend"] for t in linhas),
           "e2": sum(1 for t in linhas if t["c"] and t["c"].segunda_etapa)}
    if f["obra"]:
        orfaos = [c for c in orfaos if obras[c.obra_id].nome == f["obra"]]
    return {"linhas": linhas, "f": f, "opcoes": opcoes, "obras": obras, "tot": tot, "orfaos": orfaos, "diams": sorted(diams, key=chave),
            "pend_bit": {d: pend_bit[d] for d in sorted(pend_bit, key=chave)}}


# ---------------------------------------------------------------- planilha

def planilha(dados: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Peças"
    cab = ["Obra", "Peça", "Etapa", "Produto", "Seção", "Largura (cm)", "Altura (cm)", "Comprimento (m)", "Volume (m³)",
           "Área (m²)", "Peso (t)", "Peso estimado?", "Unidades", "Feitas", "Pendentes",
           "fck (MPa)", "fcj (MPa)", "Cobrimento (cm)", "Traço (Plannix)",
           "Aço frouxo (kg/pç)", "Taxa aço frouxo (kg/m³)", "Nº barras/pç", "Nº estribos/pç"]
    for d in dados["diams"]:
        cab += [f"Ø{d} (kg/pç)", f"Ø{d} (nº/pç)"]
    cab += ["Cordoalhas/pç", "Protensão", "Força (kgf/cord.)", "Aço ativo (kg/pç)", "Contraflecha (cm)",
            "Taxa aço protendido Plannix (kg/m³)", "Modelo de protensão", "2ª etapa", "O que vai na 2ª etapa", "Indícios na prancha",
            "Içamento", "Neoprene",
            "Prancha", "Revisão", "Leitura", "Situação", "Alertas"]
    ws.append(cab)
    for t in dados["linhas"]:
        c = t["c"]
        g = (lambda a: getattr(c, a) if c else None)
        lin = [t["o"].nome, t["nome"], t["item"], t["produto"], t["secao"], t["larg"], t["alt"], t["comp"], t["vol"],
               g("area"), round(t["peso_t"], 3) if t["peso_t"] else None, "sim" if t["peso_est"] else "", t["n"], t["fab"], t["pend"],
               g("fck"), g("fcj"), g("cobrimento"), t["traco"],
               g("aco_passivo"), round(t["taxa"], 1) if t["taxa"] else None, t["barras"], t["estribos"]]
        for d in dados["diams"]:
            kg, n = t["bit"].get(d, (None, None))
            lin += [kg, n or None]
        lin += [g("cordoalhas") or None, g("protensao") or "", g("forca"), g("aco_ativo"), g("contraflecha"),
                round(t["taxa_prot_plannix"], 2) if t["taxa_prot_plannix"] else None, t["modelo"],
                ("sim" if c.segunda_etapa else "não") if c else "", g("etapa2_obs") or "", ", ".join(c.etapa2_indicios or []) if c else "",
                g("icamento") or "", g("neoprene") or "",
                g("prancha") or "", g("revisao") or "", g("fonte") or "", ("sem prancha" if not c else c.situacao),
                " | ".join(c.alertas or []) if c else ""]
        ws.append(lin)

    ws2 = wb.create_sheet("Armadura por posição")
    ws2.append(["Obra", "Peça", "Etapa", "Posição", "Tipo", "Ø (mm)", "Quant./pç", "Material", "Compr. (cm)", "Peso unit. (kg)",
                "Peso total/pç (kg)", "Força (kgf)", "Unidades pendentes", "Peso pendente (kg)", "Observação"])
    vistos = set()
    for t in dados["linhas"]:
        c = t["c"]
        if not c or (c.id, t["item"]) in vistos:
            continue
        vistos.add((c.id, t["item"]))
        for i in c.itens or []:
            ws2.append([t["o"].nome, t["nome"], t["item"], i.get("pos"), i.get("tipo"), i.get("diam"), i.get("quant"), i.get("material"),
                        i.get("comp"), i.get("peso_un"), i.get("peso_tot"), i.get("forca"), t["pend"],
                        round((i.get("peso_tot") or 0) * t["pend"], 2) if t["pend"] else None,
                        f"{i['deduzido']} calculada" if i.get("deduzido") else ""])

    ws3 = wb.create_sheet("Aço pendente por bitola")
    ws3.append(["Bitola (mm)", "Aço frouxo pendente (kg)", "Nº de barras pendentes"])
    for d, (kg, n) in dados["pend_bit"].items():
        ws3.append([float(d), round(kg, 1), int(n)])
    ws3.append(["Cordoalha (aço ativo)", round(dados["tot"]["ativo_pend"], 1), None])

    for w in wb.worksheets:
        for cel in w[1]:
            cel.font = Font(bold=True, color="FFFFFF")
            cel.fill = PatternFill("solid", fgColor="1F3A68")
            cel.alignment = Alignment(wrap_text=True, vertical="center")
        w.freeze_panes = "C2" if w.title != "Aço pendente por bitola" else "A2"
        w.auto_filter.ref = w.dimensions
        for k, col in enumerate(w.iter_cols(min_row=1, max_row=min(w.max_row, 200)), 1):
            larg = max(len(str(x.value or "")) for x in col)
            w.column_dimensions[get_column_letter(k)].width = min(max(9, larg + 2), 40)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- lista enxuta (visão principal do módulo)

def lista(db: Session, qp) -> dict:
    """Um tipo de peça por linha: obra, nome, tipo, realizado/planejado, Autodoc, seção, altura, comprimento, cabos,
    bitola, fôrmas onde cabe e link da prancha. Mesmo nome em itens diferentes do contrato com a mesma seção e
    comprimento vira uma linha só; seção ou comprimento diferente (outra estrutura) fica separado."""
    from datetime import date
    from . import autodoc, formas as fm
    f = {"obra": qp.get("obra") or "", "tipo": qp.get("tipo") or "", "status": qp.get("status") or "a_realizar",
         "q": qp.get("q") or ""}
    obras = {o.id: o for o in db.scalars(select(Obra))}
    idx, ad_idx = cad.indice(db), autodoc.indice(db)
    fs = [x for x in fm.vigentes(db, date.today()) if x.situacao == "ativa"]
    cabe: dict = {}
    g: dict = {}
    for p in db.scalars(select(Peca)):
        k = (p.obra_id, cad._canon(p.peca), p.produto, (p.secao or "").upper(), round(p.comprimento or 0, 2))
        t = g.get(k)
        if not t:
            ck = (p.produto, p.secao, round(p.comprimento or 0, 2))
            if ck not in cabe:
                cabe[ck] = sorted((x.nome_completo for x in fs if fm.aceita(x, p.produto, p.secao, p.comprimento)))
            larg, alt = dim_secao(p.secao, p.produto)
            t = g[k] = {"o": obras[p.obra_id], "nome": p.peca, "produto": p.produto, "secao": p.secao, "alt": alt,
                        "comp": p.comprimento, "n": 0, "fab": 0, "c": cad.de_peca(idx, p), "formas": cabe[ck],
                        "ad": autodoc.de(ad_idx, p.obra_id, p.peca)}
        t["n"] += 1
        t["fab"] += 1 if p.fabricada else 0
    busca = cad._canon(f["q"].strip())
    ativas = {o.id for o in obras.values() if o.ativa}

    def passa(t):
        if t["o"].id not in ativas and not f["obra"]:
            return False
        if f["obra"] and t["o"].nome != f["obra"]:
            return False
        if f["tipo"] and t["produto"] != f["tipo"]:
            return False
        if f["status"] == "a_realizar" and t["fab"] >= t["n"]:
            return False
        if f["status"] == "realizado" and t["fab"] < t["n"]:
            return False
        return not busca or busca in cad._canon(t["nome"])

    todos = list(g.values())
    linhas = sorted((t for t in todos if passa(t)), key=lambda t: (t["o"].nome, t["produto"], chave_natural(t["nome"]), t["comp"] or 0))
    for t in linhas:
        c = t["c"]
        t["cabos"] = c.cordoalhas if c and c.cordoalhas else None
        if t["cabos"]:
            t["bitola"] = f"Ø{c.cord_diam} (cordoalha)" if c.cord_diam else ""
        elif c:
            t["bitola"] = " · ".join(f"Ø{b}" for b in sorted(_da_prancha(c)["bit"], key=float))
        else:
            t["bitola"] = ""
    opcoes = {"obra": sorted({t["o"].nome for t in todos if t["o"].id in ativas}),
              "tipo": sorted({t["produto"] for t in todos if t["produto"]})}
    tot = {"tipos": len(linhas), "pend": sum(t["n"] - t["fab"] for t in linhas), "n": sum(t["n"] for t in linhas),
           "fab": sum(t["fab"] for t in linhas), "com": sum(1 for t in linhas if t["c"]),
           "bloq": sum(1 for t in linhas if t["ad"] and t["ad"].bloqueado)}
    return {"linhas": linhas, "f": f, "opcoes": opcoes, "tot": tot}


def pdf_lista(dados: dict, base: str = "") -> bytes:
    """A tabela da lista enxuta, com os mesmos filtros da tela, em PDF A4 deitado (para mandar ou arquivar)."""
    import html
    from datetime import datetime
    import pymupdf
    f = dados["f"]
    filtros = [f"Obra: {f['obra'] or 'todas'}", f"Tipo: {f['tipo'] or 'todos'}",
               "Status: " + {"a_realizar": "a realizar", "realizado": "realizado"}.get(f["status"], "todos")]
    if f["q"]:
        filtros.append(f"Peça: {f['q']}")
    e = lambda v: html.escape(str(v)) if v not in (None, "") else ""
    num = lambda v, d: (f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")) if v else ""
    linhas = []
    for t in dados["linhas"]:
        ad = t["ad"]
        st_ad = f"{ad.status} R{ad.revisao}" if ad else "sem desenho"
        cor = "#a4161a" if ad and ad.bloqueado else "#333"
        formas = ", ".join(t["formas"]) if t["formas"] else "nenhuma"
        linhas.append(f"<tr><td>{e(t['o'].nome)}</td><td><b>{e(t['nome'])}</b></td><td>{e(t['produto'])}</td>"
                      f"<td class='n'>{t['fab']}/{t['n']}</td><td style='color:{cor}'>{e(st_ad)}</td><td>{e(t['secao'])}</td>"
                      f"<td class='n'>{num(t['alt'], 0)}</td><td class='n'>{num(t['comp'], 2)}</td>"
                      f"<td class='n'>{e(t['cabos'])}</td><td>{e(t['bitola'])}</td><td>{e(formas)}</td></tr>")
    tot = dados["tot"]
    corpo = f"""
<h1>Peças</h1>
<p class="meta">{' · '.join(e(x) for x in filtros)} · {tot['tipos']} tipos · realizado/planejado {tot['fab']}/{tot['n']} ·
{tot['pend']} a realizar{(' · Base Plannix ' + e(base)) if base else ''} · emitido em {datetime.now():%d/%m/%Y %H:%M}</p>
<table><thead><tr><th>Obra</th><th>Peça</th><th>Tipo</th><th>Real./plan.</th><th>Autodoc</th><th>Seção</th><th>Alt. (cm)</th>
<th>Compr. (m)</th><th>Cabos</th><th>Bitola</th><th>Fôrmas onde cabe</th></tr></thead><tbody>{''.join(linhas)}</tbody></table>"""
    css = """* {font-family: sans-serif;} h1 {font-size: 14pt; color: #1f3864; margin: 0 0 2pt 0;}
.meta {font-size: 7.5pt; color: #555; margin: 0 0 6pt 0;}
table {border-collapse: collapse; width: 100%;} th {background-color: #1f3864; color: #fff; font-size: 7pt; text-align: left; padding: 2pt 3pt;}
td {font-size: 7pt; padding: 1.5pt 3pt; border-bottom: 0.5pt solid #ccc;} .n {text-align: right;}"""
    story = pymupdf.Story(html=corpo, user_css=css)
    buf = pymupdf.open()
    w, h = pymupdf.paper_size("a4-l")
    caixa = pymupdf.Rect(28, 28, w - 28, h - 34)
    out = io.BytesIO()
    escritor = pymupdf.DocumentWriter(out)
    mais, pg = True, 0
    while mais:
        pg += 1
        dev = escritor.begin_page(pymupdf.Rect(0, 0, w, h))
        mais, _ = story.place(caixa)
        story.draw(dev)
        escritor.end_page()
    escritor.close()
    doc = pymupdf.open("pdf", out.getvalue())
    for i, p in enumerate(doc):
        p.insert_text((28, h - 16), f"PCP Naves · Peças · página {i + 1} de {len(doc)}", fontsize=7, color=(0.45, 0.45, 0.45))
    buf.close()
    return doc.tobytes(garbage=3, deflate=True)
