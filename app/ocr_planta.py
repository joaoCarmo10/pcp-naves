"""Leitura de etiquetas em plantas cujo texto foi exportado como desenho (AutoCAD sem PDFSHX, fonte SHX vetorizada).

Ideia: no PDF, cada palavra vira um caminho preenchido de preto, do tamanho de um texto. Palavras iguais (todas as
"LA6") têm exatamente o mesmo desenho. Então:
1. separa os caminhos pretos com tamanho de texto (são as palavras);
2. agrupa os desenhos idênticos (assinatura geométrica) e lê só um de cada grupo;
3. redesenha esse representante numa página limpa (sem linhas de cota, eixos ou hexágonos) e lê com OCR;
4. aceita só leituras que são nomes de peça da obra, corrigindo trocas comuns do OCR (S/5, O/0, I/1, B/8...).
"""
import collections
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf

LISTA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.()*,"
PARA_DIGITO = str.maketrans({"S": "5", "O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "B": "8", "Z": "2", "G": "6", "T": "7"})
PARA_LETRA = str.maketrans({"5": "S", "0": "O", "1": "I", "8": "B", "2": "Z", "6": "G"})


def disponivel() -> bool:
    return shutil.which("tesseract") is not None


def _preto(c) -> bool:
    return c is not None and all(v < 0.25 for v in c[:3])


def _assinatura(x) -> int:
    r, o = x["rect"], []
    for it in x["items"]:
        for pt in it[1:]:
            if isinstance(pt, pymupdf.Point):
                o.append((round(pt.x - r.x0, 1), round(pt.y - r.y0, 1)))
            elif isinstance(pt, pymupdf.Rect):
                o.append((round(pt.x0 - r.x0, 1), round(pt.y0 - r.y0, 1), round(pt.width, 1), round(pt.height, 1)))
            elif isinstance(pt, pymupdf.Quad):
                o.append(tuple(round(v, 1) for v in (pt.ul.x - r.x0, pt.ul.y - r.y0, pt.lr.x - r.x0, pt.lr.y - r.y0)))
    return hash(tuple(o))


def _candidatos(t: str) -> list[str]:
    """Leitura do OCR e variantes com trocas comuns: prefixo em letras, final em números (LAS -> LA5, VRI2 -> VR12)."""
    t = re.sub(r"[^A-Z0-9\-]", "", t.upper())
    if not t:
        return []
    out = [t]
    for k in range(1, min(4, len(t)) + 1):
        pre, resto = t[:k].translate(PARA_LETRA), t[k:]
        if resto:
            out.append(pre + re.sub(r"[A-Z]", lambda g: g.group(0).translate(PARA_DIGITO), resto))
    return list(dict.fromkeys(out))


def _ocr(imagens, pasta: Path, n: int, lista: str | None = LISTA) -> list[str]:
    from PIL import Image
    larg = max(i.width for i in imagens) + 40
    alt = sum(i.height + 30 for i in imagens) + 30
    pilha = Image.new("L", (larg, alt), 255)
    y, pos = 15, []
    for im in imagens:
        pilha.paste(im, (20, y))
        pos.append((y, y + im.height))
        y += im.height + 30
    arq = pasta / f"pilha_{n}.png"
    pilha.save(arq)
    cmd = ["tesseract", str(arq), "-", "--psm", "6"] + (["-c", f"tessedit_char_whitelist={lista}"] if lista else []) + ["tsv"]
    tsv = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    res = [""] * len(imagens)
    for linha in tsv.splitlines()[1:]:
        c = linha.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        yc = int(c[7]) + int(c[9]) / 2
        for k, (a, b) in enumerate(pos):
            if a - 15 <= yc <= b + 15:
                res[k] += (" " if res[k] and not lista else "") + c[11]
                break
    return res


def palavras_vetoriais(pg, regiao=None) -> dict[int, list]:
    """Caminhos pretos do tamanho de texto (cada um é uma palavra desenhada), agrupados por desenho idêntico."""
    grupos: dict[int, list] = collections.defaultdict(list)
    for x in pg.get_drawings():
        r = x["rect"]
        if "f" in x["type"] and _preto(x.get("fill")) and 1.0 <= min(r.width, r.height) <= 14 and max(r.width, r.height) <= 200:
            if regiao is None or r.intersects(regiao):
                grupos[_assinatura(x)].append(x)
    return grupos


def _pagina_limpa(pg, reps):
    limpo = pymupdf.open()
    q = limpo.new_page(width=pg.rect.width, height=pg.rect.height)
    sh = q.new_shape()
    for x in reps:
        for it in x["items"]:
            if it[0] == "l":
                sh.draw_line(it[1], it[2])
            elif it[0] == "c":
                sh.draw_bezier(it[1], it[2], it[3], it[4])
            elif it[0] == "re":
                sh.draw_rect(it[1])
            elif it[0] == "qu":
                sh.draw_quad(it[1])
        sh.finish(color=None, fill=(0, 0, 0), even_odd=True, closePath=True)
    sh.commit()
    return limpo, q


def ler_tudo(pg, regiao=None) -> list[tuple[str, "pymupdf.Rect"]]:
    """OCR de todas as palavras desenhadas (texto livre, com minúsculas e números): [(texto, retângulo)]."""
    if not disponivel():
        return []
    from PIL import Image
    grupos = palavras_vetoriais(pg, regiao)
    chaves = list(grupos)
    reps = [grupos[k][0] for k in chaves]
    if not reps:
        return []
    limpo, q = _pagina_limpa(pg, reps)

    def recorte(x, alt=40):
        r = x["rect"] + (-1.2, -1.2, 1.2, 1.2)
        vertical = r.height > r.width * 1.3 and r.height > 9
        z = min(alt / (r.width if vertical else r.height), 40)
        pm = q.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=r, colorspace=pymupdf.csGRAY)
        im = Image.frombytes("L", (pm.width, pm.height), pm.samples)
        return im.rotate(-90, expand=True) if vertical else im

    textos = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(0, len(reps), 120):
            textos += _ocr([recorte(x) for x in reps[i:i + 120]], Path(tmp), i, lista=None)
        # palavras de um caractere só (quantidades nas tabelas): o OCR em bloco erra muito; relê como dígito isolado
        unicos = [i for i, x in enumerate(reps) if x["rect"].width < x["rect"].height * 0.85 and x["rect"].height >= 2]
        for i in unicos:
            d = _digito(recorte(reps[i], 60), Path(tmp), i)
            if d:
                textos[i] = d
    out = []
    for k, t in zip(chaves, textos):
        if t.strip():
            for x in grupos[k]:
                out.append((t.strip(), x["rect"]))
    return out


def _digito(im, pasta: Path, n: int) -> str:
    from PIL import Image, ImageOps
    im = ImageOps.expand(im, border=30, fill=255)
    arq = pasta / f"dig_{n}.png"
    im.save(arq)
    t = subprocess.run(["tesseract", str(arq), "-", "--psm", "10", "-c", "tessedit_char_whitelist=0123456789"],
                       capture_output=True, text=True, timeout=30).stdout.strip()
    return t if re.fullmatch(r"\d", t) else ""


def ler(caminho: Path, nomes: set[str]) -> list[dict]:
    """Marcas [{t, r}] (r em coordenadas da página, como as do texto normal) das peças encontradas."""
    if not nomes or not disponivel():
        return []
    from PIL import Image
    canon = {re.sub(r"(?<=[A-Z])-(?=\d)", "", n.upper()): n for n in nomes}
    doc = pymupdf.open(caminho)
    pg = doc[0]
    palavras = [x for x in pg.get_drawings() if "f" in x["type"] and _preto(x.get("fill"))
                and 1.5 <= min(x["rect"].width, x["rect"].height) <= 14 and max(x["rect"].width, x["rect"].height) <= 160]
    grupos: dict[int, list] = collections.defaultdict(list)
    for x in palavras:
        grupos[_assinatura(x)].append(x)
    reps = [g[0] for g in grupos.values()]
    if not reps:
        return []
    limpo = pymupdf.open()
    q = limpo.new_page(width=pg.rect.width, height=pg.rect.height)
    sh = q.new_shape()
    for x in reps:
        for it in x["items"]:
            if it[0] == "l":
                sh.draw_line(it[1], it[2])
            elif it[0] == "c":
                sh.draw_bezier(it[1], it[2], it[3], it[4])
            elif it[0] == "re":
                sh.draw_rect(it[1])
            elif it[0] == "qu":
                sh.draw_quad(it[1])
        sh.finish(color=None, fill=(0, 0, 0), even_odd=True, closePath=True)
    sh.commit()

    def recorte(x, alt):
        r = x["rect"] + (-1.2, -1.2, 1.2, 1.2)
        vertical = r.height > r.width * 1.3
        z = min(alt / (r.width if vertical else r.height), 40)
        pm = q.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=r, colorspace=pymupdf.csGRAY)
        im = Image.frombytes("L", (pm.width, pm.height), pm.samples)
        return im.rotate(-90, expand=True) if vertical else im

    rotulo: dict[int, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        pasta = Path(tmp)
        for n, alt in enumerate((40, 60, 28, 50)):
            falta = [k for k in range(len(reps)) if k not in rotulo]
            if not falta:
                break
            textos = []
            for i in range(0, len(falta), 120):
                textos += _ocr([recorte(reps[k], alt) for k in falta[i:i + 120]], pasta, n * 1000 + i)
            for k, t in zip(falta, textos):
                for c in _candidatos(t):
                    if c in canon:
                        rotulo[k] = canon[c]
                        break
    marcas = []
    for k, g in enumerate(grupos.values()):
        if k in rotulo:
            for x in g:
                r = x["rect"]
                marcas.append({"t": rotulo[k], "r": [round(v, 2) for v in (r.x0, r.y0, r.x1, r.y1)]})
    return marcas
