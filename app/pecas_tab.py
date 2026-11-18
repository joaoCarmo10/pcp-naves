"""Tabela do módulo Peças: um tipo por linha (obra + nome + etapa), dados do Plannix completados pela prancha.
Usada pela tela e pela planilha baixada, para as duas mostrarem exatamente o mesmo."""
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cadastro as cad
from .models import Obra, Peca
from .status import chave_natural

FILTROS = ("obra", "produto", "situacao", "q", "pend", "prot")


def dim_secao(secao: str) -> tuple[float | None, float | None]:
    """(largura, altura) em cm a partir da seção do Plannix: 'h20' -> (None, 20); '20x70' -> (20, 70)."""
    s = (secao or "").strip().upper().replace(",", ".")
    m = re.fullmatch(r"H\s*(\d+(?:\.\d+)?)", s)
    if m:
        return None, float(m[1])
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)", s)
    if m:
        return float(m[1]), float(m[2])
    return None, None


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
    obras = {o.id: o for o in db.scalars(select(Obra))}
    idx = cad.indice(db)
    tipos: dict = {}
    for p in db.scalars(select(Peca)):
        k = (p.obra_id, cad._canon(p.peca), p.item or "")
        t = tipos.get(k)
        if not t:
            larg, alt = dim_secao(p.secao)
            t = tipos[k] = {"o": obras[p.obra_id], "nome": p.peca, "item": p.item or "", "produto": p.produto, "secao": p.secao,
                            "larg": larg, "alt": alt, "comp": p.comprimento, "vol": p.vol_projeto or p.volume,
                            "n": 0, "fab": 0, "c": cad.de_peca(idx, p), "_traco": Counter(), "_taxa": [], "_modelo": Counter()}
        t["n"] += 1
        t["fab"] += 1 if p.fabricada else 0
        if p.traco:
            t["_traco"][p.traco] += 1
        if p.taxa_aco_prot:
            t["_taxa"].append(p.taxa_aco_prot)
        if p.modelo_protensao:
            t["_modelo"][p.modelo_protensao] += 1
    todos = list(tipos.values())
    opcoes = {"obra": sorted({t["o"].nome for t in todos}), "produto": sorted({t["produto"] for t in todos if t["produto"]})}
    busca = cad._canon(f["q"].strip())

    def passa(t):
        c = t["c"]
        return not ((f["obra"] and t["o"].nome != f["obra"]) or (f["produto"] and t["produto"] != f["produto"])
                    or (f["situacao"] == "sem" and c) or (f["situacao"] == "com" and not c)
                    or (f["situacao"] in ("ok", "revisar") and (not c or c.situacao != f["situacao"]))
                    or (busca and busca not in cad._canon(t["nome"])) or (f["pend"] and t["fab"] >= t["n"])
                    or (f["prot"] and not (c and c.cordoalhas)))

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
           "barras_pend": sum((t["barras"] or 0) * t["pend"] for t in linhas)}
    return {"linhas": linhas, "f": f, "opcoes": opcoes, "obras": obras, "tot": tot, "diams": sorted(diams, key=chave),
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
           "Aço passivo (kg/pç)", "Taxa passiva (kg/m³)", "Nº barras/pç", "Nº estribos/pç"]
    for d in dados["diams"]:
        cab += [f"Ø{d} (kg/pç)", f"Ø{d} (nº/pç)"]
    cab += ["Cordoalhas/pç", "Protensão", "Força (kgf/cord.)", "Aço ativo (kg/pç)", "Contraflecha (cm)",
            "Taxa aço protendido Plannix (kg/m³)", "Modelo de protensão", "Içamento", "Neoprene",
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
                round(t["taxa_prot_plannix"], 2) if t["taxa_prot_plannix"] else None, t["modelo"], g("icamento") or "", g("neoprene") or "",
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
    ws3.append(["Bitola (mm)", "Aço passivo pendente (kg)", "Nº de barras pendentes"])
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
