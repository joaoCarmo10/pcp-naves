"""Leitura das pranchas de detalhamento (quadro "LISTA DE MATERIAL POR PEÇA"): concreto, armadura passiva,
protensão, içamento, neoprene. Funciona com texto do PDF e, quando o texto veio desenhado, com OCR das palavras.
As contas da própria prancha (quantidade x unitário, soma das posições) servem de conferência da leitura."""
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

NOME = re.compile(r"^[A-Z]{1,5}\d+[A-Z0-9.\-]*$")


def _n(s: str) -> str:
    """Normaliza para comparar cabeçalhos: sem acento, maiúsculo, com as trocas típicas do OCR (ç->g)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return s


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in (".", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class Tok:
    t: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def xc(self):
        return (self.x0 + self.x1) / 2

    @property
    def yc(self):
        return (self.y0 + self.y1) / 2


@dataclass
class Leitura:
    peca: str = ""
    quantidade: float | None = None
    volume_un: float | None = None
    volume_tot: float | None = None
    peso: float | None = None
    area: float | None = None
    fck: float | None = None
    fcj: float | None = None
    cobrimento: float | None = None
    peso_aco: float | None = None          # peso unitário de aço da prancha (todas as posições)
    aco_ativo: float | None = None         # kg/peça de protensão
    contraflecha: float | None = None
    neoprene: str = ""
    itens: list = field(default_factory=list)   # {pos, diam, quant, material, comp, peso_un, peso_tot, forca, tipo}
    revisao: str = ""
    prancha: str = ""
    fonte: str = "texto"
    etapa2: list = field(default_factory=list)   # indícios de concretagem em 2ª etapa
    alertas: list = field(default_factory=list)


def _tokens(pg) -> tuple[list[Tok], str]:
    ws = pg.get_text("words")
    toks = [Tok(w[4], *w[:4]) for w in ws]
    legiveis = sum(1 for t in toks if re.search(r"[A-Za-z0-9]", t.t))
    if len(toks) > 30 and legiveis > 0.8 * len(toks) and (any("MATERIAL" in _n(t.t) for t in toks) or legiveis > 150):
        return toks, "texto"
    from . import ocr_planta
    out = []
    for texto, r in ocr_planta.ler_tudo(pg):
        partes = texto.split()
        larg = (r.x1 - r.x0) / max(1, sum(len(p) + 1 for p in partes))
        x = r.x0
        for p in partes:   # divide frases do OCR em palavras com posição aproximada
            out.append(Tok(p, x, r.y0, x + larg * len(p), r.y1))
            x += larg * (len(p) + 1)
    return out, "ocr"


def _linhas(toks: list[Tok], tol=3.2) -> list[list[Tok]]:
    linhas: list[list[Tok]] = []
    for t in sorted(toks, key=lambda t: t.yc):
        if linhas and abs(linhas[-1][0].yc - t.yc) <= tol:
            linhas[-1].append(t)
        else:
            linhas.append([t])
    return [sorted(l, key=lambda t: t.x0) for l in linhas]


def _texto(linha) -> str:
    return " ".join(t.t for t in linha)


def _milhar(s: str) -> float | None:
    """'14.500' (kgf, padrão brasileiro) = 14500."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        return float(s.replace(".", ""))
    return _num(s)


def _diam(s: str) -> float | None:
    s = _n(s).replace("MM", "")
    s = re.sub(r"^(O|0|@|4\)|\(|Ø)", "", s.strip())
    return _num(s)


def ler(caminho: Path, nomes: set[str] | None = None) -> Leitura:
    doc = pymupdf.open(caminho)
    pg = doc[0]
    toks, fonte = _tokens(pg)
    L = Leitura(fonte=fonte)
    m = re.search(r"[-_]R(\d{1,2})(?:\.pdf)?$", Path(caminho).stem, re.I)
    L.revisao = f"R{int(m.group(1)):02d}" if m else ""
    m = re.search(r"-(\d{3})-[A-Z]{3}-R\d", Path(caminho).stem)
    L.prancha = m.group(1) if m else ""
    linhas = _linhas(toks)
    textos = [_n(_texto(l)) for l in linhas]
    canon = {}
    for n in nomes or []:
        canon[re.sub(r"(?<=[A-Z])-(?=\d)", "", n.upper())] = n

    # ---------------- cabeçalho da peça: valores casados pela coluna (Peça | Quantidade | Volume unit. | Volume total | Peso | Área)
    for i, tx in enumerate(textos):
        if "QUANTIDADE" in tx and "VOLUME" in tx:
            cab = linhas[i]
            xs = {}
            vols = [t for t in cab if _n(t.t).startswith("VOLUME")]
            for t in cab:
                n = _n(t.t)
                if n.startswith("QUANTIDADE"):
                    xs["quantidade"] = t.xc
                elif n.startswith(("UNITARIO",)):
                    xs["volume_un"] = t.xc
                elif n.startswith("TOTAL") and "volume_tot" not in xs:
                    xs["volume_tot"] = t.xc
                elif n.startswith("PESO") and "peso" not in xs:
                    xs["peso"] = t.xc + 25
                elif n.startswith("AREA"):
                    xs["area"] = t.xc + 20
            if len(vols) >= 2 and "volume_un" not in xs:
                xs["volume_un"], xs["volume_tot"] = vols[0].xc + 20, vols[1].xc + 20
            area_l = [l for l in linhas if "AREA" in _n(_texto(l)) and abs(l[0].yc - cab[0].yc) < 12]
            for t in (area_l[0] if area_l else []):
                if _n(t.t).startswith("AREA"):
                    xs["area"] = t.xc + 20
            x_peca = min((t.x0 for t in cab if _n(t.t) == "PECA"), default=None)
            for l in linhas[i + 1:i + 6]:
                vals = [t for t in l if not re.match(r"^(FC[KJ])", _n(t.t)) and (x_peca is None or t.xc >= x_peca - 15)]
                # o nome é o primeiro token com cara de peça (pode haver lixo à esquerda, ex.: "kg" de outra tabela)
                k0 = next((k for k, t in enumerate(vals) if NOME.match(_n(t.t))), None)
                if k0 is not None and k0 <= 2:
                    vals = vals[k0:]
                if vals and NOME.match(_n(vals[0].t)):
                    nome = _n(vals[0].t)
                    L.peca = canon.get(re.sub(r"(?<=[A-Z])-(?=\d)", "", nome), nome)
                    brutos = {}
                    for t in vals[1:]:
                        if _num(t.t) is None or not xs:
                            continue
                        campo = min(xs, key=lambda k: abs(xs[k] - t.xc))
                        if abs(xs[campo] - t.xc) < 60 and campo not in brutos:
                            brutos[campo] = t.t
                    for campo, b in brutos.items():
                        setattr(L, campo, _num(b))
                    # peso com ponto de milhar ("4.500" = 4500 kg): concreto pesa ~2.500 kg/m³
                    if L.peso and L.volume_un and L.peso < L.volume_un * 500 and re.fullmatch(r"\d{1,3}\.\d{3}", brutos.get("peso", "")):
                        L.peso = _milhar(brutos["peso"])
                    if L.quantidade is not None and (L.quantidade != int(L.quantidade) or L.quantidade <= 0):
                        L.quantidade = None
                    break
            break
    tudo = " ".join(textos)
    m = re.search(r"FCK\s*-?\s*(\d+)", tudo)
    L.fck = float(m.group(1)) if m else None
    m = re.search(r"FCJ\s*-?\s*(\d+)", tudo)
    L.fcj = float(m.group(1)) if m else None
    m = re.search(r"COBRIMENTO:?\s*([\d.,]+)", tudo)
    L.cobrimento = _num(m.group(1)) if m else None
    m = re.search(r"PESO UNITARIO \(KG\)\s*([\d.,]+)", tudo)
    L.peso_aco = _num(m.group(1)) if m else _peso_aco_ao_lado(linhas)
    m = re.search(r"ARMADURA ATIVA\s*([\d.,]+)\s*KG", tudo)
    L.aco_ativo = _num(m.group(1)) if m else None
    m = re.search(r"CONTRA-?FLECHA[^:]*:\s*([\d.,]+)", tudo)
    L.contraflecha = _num(m.group(1)) if m else None
    for l, tx in zip(linhas, textos):
        if "DESCRICAO" in tx or "LISTA" in tx:
            continue
        k = next((k for k, t in enumerate(l) if _n(t.t) == "NEOPRENE"), None)
        if k is not None and len(l) > k + 2:   # pode haver textos do desenho à esquerda na mesma altura
            L.neoprene = " ".join(t.t for t in l[k + 1:])[:80]
            break
    L.etapa2 = indicios_segunda_etapa(textos)

    # ---------------- tabelas de armadura (passiva e de fios/cabos/cordoalhas)
    for i, tx in enumerate(textos):
        if "MATERIAL" in tx and ("QUANT" in tx or "QUANT" in " ".join(textos[max(0, i - 2):i + 3])) and "LISTA DE MATERIAL" not in tx:
            _tabela(L, linhas, textos, i)
    _conferir(L, nomes)
    return L


def _peso_aco_ao_lado(linhas) -> float | None:
    """'Peso unitário (kg)' com o valor numa linha vizinha (layout em que rótulo e número não ficam na mesma linha)."""
    toks = [t for l in linhas for t in l]
    for k, t in enumerate(toks):
        if _n(t.t) != "UNITARIO" or k == 0 or _n(toks[k - 1].t) != "PESO":
            continue
        fim = next((x for x in toks[k:k + 3] if _n(x.t) == "(KG)"), t)
        tot = [x.x0 for x in toks if abs(x.yc - t.yc) < 6 and _n(x.t).startswith("TOTAL") and x.x0 > fim.x1]
        cands = [x for x in toks if abs(x.yc - t.yc) < 6 and fim.x1 - 5 < x.x0 < fim.x1 + 90
                 and (not tot or x.x0 < min(tot)) and re.fullmatch(r"\d+[.,]\d+", x.t)]
        if cands:
            return _num(min(cands, key=lambda x: x.x0 - fim.x1).t)
    return None


# Frases do quadro padrão de tolerâncias (aparecem em toda prancha e não indicam nada)
_TOLERANCIAS = re.compile(r"\S*\s*DO CONSOL[EO]S?( ?/ ?DENTE GERBER)?|CONSOL[EO] ?/ ?DENTE GERBER")   # tolera OCR ("DIMENSDO DO CONSOLO")
_ETAPA2_TEXTO = re.compile(r"\b(2 ?[AªO°º]? ?(ETAPA|FASE|CONCRETAGEM)|SEGUNDA (ETAPA|FASE|CONCRETAGEM)|CONCRETAGEM POSTERIOR|"
                           r"CONCRETAR (APOS|DEPOIS|POSTERIORMENTE)|MOLDAD[OA] POSTERIORMENTE|CONCRETAD[OA] POSTERIORMENTE)\b")


def indicios_segunda_etapa(textos: list[str]) -> list[str]:
    """Sinais na prancha de que a peça leva uma segunda concretagem (ex.: consolo lateral concretado depois).
    Devolve as frases encontradas; vazio = nenhum indício."""
    out = []
    for tx in textos:
        if "+/-" in tx:   # linhas do quadro de tolerâncias
            continue
        limpo = _TOLERANCIAS.sub("", tx)
        m = _ETAPA2_TEXTO.search(limpo)
        if m:
            out.append(f"nota \"{m.group(0).lower()}\"")
        if re.search(r"ATENCAO PARA CONSOL[EO]", limpo):
            out.append("nota \"atenção para consolo\"")
        elif re.search(r"\bCONSOL[EO]S?\b", limpo):
            out.append("desenho com consolo")
    return list(dict.fromkeys(out))


def _tabela(L: Leitura, linhas, textos, i_mat: int) -> None:
    y_mat = linhas[i_mat][0].yc
    dado = re.compile(r"^(CA-?\d|CORD|CP-?\d|FIO)")
    # cabeçalho = palavras até 14 pt acima/abaixo da linha de "Material", só na faixa horizontal da lista
    # (tabelas vizinhas, como o resumo de aço, ficam na mesma altura e não podem entrar)
    perto = [t for l in linhas if abs(l[0].yc - y_mat) <= 14 for t in l if not dado.match(_n(t.t))]
    x_pos = [t.x0 for t in perto if _n(t.t).startswith(("POSICAO", "POSIGAO", "POS.")) or _n(t.t) == "POS"]
    cab = [t for t in perto if not x_pos or t.xc >= min(x_pos) - 30]
    ativa = any("PROTENSAO" in _n(t.t) or "FORCA" in _n(t.t) for t in cab)
    col = {}
    for t in cab:
        n = _n(t.t)
        chave = None
        if n.startswith(("POSICAO", "POSIGAO", "POS.")) or n == "POS":
            chave = "pos"
        elif n.startswith(("DIAMETRO", "DIAM")):
            chave = "diam"
        elif n.startswith("QUANT") and "QUANTIDADE" not in n:
            chave = "quant"
        elif n.startswith("MATERIAL"):
            chave = "material"
        elif n.startswith(("COMPR", "COMP.")):
            chave = "comp"
        elif n.startswith("UNITARIO"):
            col["unit"] = t.xc   # "Unitário" vale mais que "Peso"
            continue
        elif n.startswith("PESO") and "unit" not in col and not any(_n(x.t).startswith("UNITARIO") for x in cab):
            chave = "unit"
        elif n.startswith("TOTAL"):
            chave = "tot"
        elif n.startswith(("FORCA", "PROTENSAO")):
            chave = "forca"
        elif n.startswith("DESENHO"):
            chave = "desenho"   # cotas do desenho da barra: coluna ignorada, só para não cair no Total
        if chave and chave not in col:
            col[chave] = t.xc
    if "material" not in col or "pos" not in col:
        return
    if "unit" in col and "tot" in col and col["unit"] > col["tot"]:
        col["unit"], col["tot"] = col["tot"], col["unit"]
    xmin = max(min(col.values()) - 40, (min(x_pos) - 30) if x_pos else -1e9)
    xmax = max(col.values()) + 25
    chaves = ("POSIC", "POSIG", "POS", "DIAM", "QUANT", "MATERIAL", "COMPR", "COMP.", "UNITARIO", "PESO", "TOTAL", "FORCA", "PROTENSAO", "DESENHO")
    y_cab = max(t.y1 for t in cab if _n(t.t).startswith(chaves))
    # fim da tabela
    fim = None
    for l, tx in zip(linhas, textos):
        if l[0].yc > y_cab + 2 and any(k in tx for k in ("COBRIMENTO", "ARMADURA ATIVA", "RESUMO DE A", "LISTA DE NEOPRENE")):
            fim = min(t.y0 for t in l)
            break
    corpo = [t for l in linhas for t in l if y_cab + 1 < t.yc < (fim or 1e9) and xmin <= t.xc <= xmax]
    ancoras = [t for t in corpo if re.match(r"^(CA-?\d|CORD|CP-?\d|FIO)", _n(t.t)) and abs(t.xc - col["material"]) < 45]
    if not ancoras:
        return
    ancoras.sort(key=lambda t: t.yc)
    linhas_tab = [{"y": a.yc, "c": {}} for a in ancoras]
    for t in corpo:
        r = min(linhas_tab, key=lambda r: abs(r["y"] - t.yc))
        if abs(r["y"] - t.yc) > 14:
            continue
        c = min(col, key=lambda k: abs(col[k] - t.xc))
        r["c"].setdefault(c, []).append(t)
    for r in linhas_tab:
        c = {k: " ".join(t.t for t in sorted(v, key=lambda t: t.x0)) for k, v in r["c"].items()}
        pos = next((_n(t.t) for t in sorted(r["c"].get("pos", []), key=lambda t: t.x0) if re.match(r"^[A-Z]{1,4}\d+", _n(t.t))), "")
        material = _n(c.get("material", "")).replace(" ", "")
        it = {"pos": pos, "diam": _diam(c.get("diam", "")), "quant": _num(c.get("quant", "")), "material": material,
              "comp": _num(c.get("comp", "")), "peso_un": _num(c.get("unit", "")), "peso_tot": _num(c.get("tot", "")),
              "forca": _milhar(c.get("forca", "").split()[0] if c.get("forca") else "") if ativa else None}
        cord = material.startswith(("CORD", "CP", "FIO"))
        it["tipo"] = "ativa" if (cord and ativa and not pos.startswith("IC")) else ("icamento" if cord or pos.startswith("IC") else "passiva")
        L.itens.append(it)


# kg/m nominal por diâmetro (CA-50/60 e cordoalha CP-190)
KG_M = {5.0: 0.154, 6.3: 0.245, 8.0: 0.395, 10.0: 0.617, 12.5: 0.963, 16.0: 1.578, 20.0: 2.466, 25.0: 3.853, 32.0: 6.313}
KG_M_CORD = {9.5: 0.432, 12.7: 0.792, 15.2: 1.102}


def _deduzir(it: dict) -> None:
    """Completa pela própria tabela o que o OCR não leu: quantidade = total / unitário; diâmetro pelo kg/m
    (unitário / comprimento). Se o valor lido existir e discordar do calculado, fica o lido e vira alerta."""
    un, tot, comp = it["peso_un"], it["peso_tot"], it["comp"]
    if un and tot and it["quant"] is None:
        q = tot / un
        if abs(q - round(q)) <= max(0.1, 0.025 * q) and round(q) >= 1:
            it["quant"], it["deduzido"] = float(round(q)), "quantidade"
    if un and comp:
        kgm = un / (comp / 100)
        tabela = KG_M_CORD if it["material"].startswith(("CORD", "CP")) else KG_M
        d, ref = min(tabela.items(), key=lambda kv: abs(kv[1] - kgm))
        if abs(ref - kgm) <= 0.08 * ref:
            if it["diam"] is None:
                it["diam"], it["deduzido"] = d, (it.get("deduzido", "") + " diâmetro").strip()
            elif abs(it["diam"] - d) > 0.05:
                it["diam_calc"] = d


def _conferir(L: Leitura, nomes: set[str] | None) -> None:
    for it in L.itens:
        _deduzir(it)
    a = L.alertas
    if not L.peca:
        a.append("Não achei o nome da peça na lista de material.")
    elif nomes is not None and L.peca not in nomes:
        a.append(f"{L.peca} não existe nesta obra no Plannix.")
    if L.quantidade and L.volume_un and L.volume_tot and abs(L.quantidade * L.volume_un - L.volume_tot) > 0.03 * L.volume_tot + 0.01:
        a.append("Volume total da prancha não bate com quantidade x volume unitário.")
    for it in L.itens:
        if it["quant"] and it["peso_un"] and it["peso_tot"] and abs(it["quant"] * it["peso_un"] - it["peso_tot"]) > 0.02 * it["peso_tot"] + 0.05:
            a.append(f"{it['pos'] or '?'}: quantidade x peso unitário não bate com o total.")
        if not it["diam"] or not it["quant"]:
            a.append(f"{it['pos'] or '?'}: faltou ler diâmetro ou quantidade.")
        if it.get("diam_calc"):
            a.append(f"{it['pos'] or '?'}: diâmetro lido Ø{it['diam']:g} não bate com o peso por metro (Ø{it['diam_calc']:g}).")
    passiva = [it for it in L.itens if it["tipo"] != "ativa"]
    if L.peso_aco and passiva:
        soma = sum(it["peso_tot"] or 0 for it in passiva)
        if abs(soma - L.peso_aco) > 0.01 * L.peso_aco + 0.2:
            a.append(f"Soma das posições ({soma:.1f} kg) não bate com o peso de aço da prancha ({L.peso_aco:.1f} kg).")
    ativas = [it for it in L.itens if it["tipo"] == "ativa"]
    if L.aco_ativo and ativas:
        soma = sum(it["peso_tot"] or 0 for it in ativas)
        if abs(soma - L.aco_ativo) > 0.01 * L.aco_ativo + 0.2:
            a.append(f"Soma das cordoalhas ({soma:.1f} kg) não bate com a armadura ativa ({L.aco_ativo:.1f} kg).")
    if not L.itens:
        a.append("Não achei a tabela de armadura.")


def resumo(L_itens: list) -> dict:
    """Protensão (nº x Ø, força), içamento e aço passivo por bitola."""
    ativas = [i for i in L_itens if i["tipo"] == "ativa"]
    ica = [i for i in L_itens if i["tipo"] == "icamento"]
    bitolas: dict[str, float] = {}
    for i in L_itens:
        if i["tipo"] == "passiva" and i["diam"]:
            k = f"{i['diam']:g}"
            bitolas[k] = round(bitolas.get(k, 0) + (i["peso_tot"] or 0), 2)
    cord_n = sum(int(i["quant"] or 0) for i in ativas)
    diams = sorted({i["diam"] for i in ativas if i["diam"]})
    forca = max((i["forca"] or 0) for i in ativas) if ativas else None
    return {
        "cordoalhas": cord_n, "cord_diam": "/".join(f"{d:g}" for d in diams), "forca": forca or None,
        "protensao": f"{cord_n}×Ø{'/'.join(f'{d:g}' for d in diams)}" if ativas else "",
        "icamento": ", ".join(f"{int(i['quant'] or 0)}×Ø{i['diam']:g} {i['material']}" for i in ica if i["diam"]),
        "aco_passivo": round(sum(i["peso_tot"] or 0 for i in L_itens if i["tipo"] == "passiva"), 2),
        "bitolas": dict(sorted(bitolas.items(), key=lambda kv: float(kv[0]))),
    }
