"""Plantas de montagem: extração das etiquetas das peças, imagem para a tela e PDF A2 marcado."""
import re
from datetime import date
from pathlib import Path

import pymupdf

from .db import DATA_DIR
from .status import CORES, LEGENDA, StatusPeca, chave_natural, resumo

ETIQUETA = re.compile(r"^[A-Z][A-Z0-9.\-]{0,24}\d[A-Z0-9.\-]*$")
LARGURA_TELA = 3200  # px da imagem base


def _norm(t: str) -> str:
    return re.sub(r"\(.*", "", t).strip().rstrip(".,;:")


def extrair(caminho: Path) -> dict:
    doc = pymupdf.open(caminho)
    pg = doc[0]
    palavras = []
    for w in pg.get_text("words"):
        t = _norm(w[4])
        if ETIQUETA.match(t):
            palavras.append({"t": t, "r": [round(v, 2) for v in w[:4]]})
    # AutoCAD com fonte SHX desenha o texto como linhas; com PDFSHX=1 ele grava cada texto também como comentário
    # (anotação) com o conteúdo e a posição. É assim que as etiquetas aparecem nesses PDFs.
    for a in pg.annots() or []:
        t = _norm((a.info.get("content") or "").strip())
        if t and " " not in t and ETIQUETA.match(t):
            r = a.rect
            palavras.append({"t": t, "r": [round(v, 2) for v in (r.x0, r.y0, r.x1, r.y1)]})
    return dict(largura=pg.rect.width, altura=pg.rect.height, rotacao=pg.rotation, marcas=palavras)


def imagem_base(caminho: Path, planta_id: int) -> tuple[Path, float]:
    """PNG da prancha (sem marcas), gerado uma vez. Devolve caminho e zoom usado."""
    destino = DATA_DIR / "cache" / f"planta_{planta_id}.png"
    doc = pymupdf.open(caminho)
    pg = doc[0]
    zoom = LARGURA_TELA / pg.rect.width
    if not destino.exists():
        pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(destino)
    return destino, zoom


def marcas_tela(caminho: Path, marcas: list, status: dict[str, StatusPeca], zoom: float) -> list[dict]:
    """Retângulos das etiquetas no sistema de coordenadas da imagem (já considerando a rotação da página)."""
    doc = pymupdf.open(caminho)
    pg = doc[0]
    m = pg.rotation_matrix * pymupdf.Matrix(zoom, zoom)
    out = []
    for mk in marcas:
        s = status.get(mk["t"])
        if not s:
            continue
        r = (pymupdf.Rect(mk["r"]) + (-2, -2, 2, 2)) * m
        out.append(dict(p=mk["t"], s=s.status, x=round(r.x0), y=round(r.y0), w=round(r.width), h=round(r.height)))
    return out


def pdf_a2(caminho: Path, marcas: list, status: dict[str, StatusPeca], titulo: str, data_ref: str) -> bytes:
    src = pymupdf.open(caminho)
    pg0 = src[0]
    nomes = set()
    for mk in marcas:
        s = status.get(mk["t"])
        if not s:
            continue
        nomes.add(mk["t"])
        cor = CORES[s.status][0]
        pg0.draw_rect(pymupdf.Rect(mk["r"]) + (-1.5, -1.5, 1.5, 1.5), color=cor, fill=cor, fill_opacity=0.35, width=1.2, overlay=True)
    if pg0.rotation:
        # folha girada no CAD: desfaz a rotação (marcas incluídas) para a folha caber inteira no A2
        pg0.remove_rotation()
    out = pymupdf.open()
    W, H = pymupdf.paper_size("a2-l")
    pg = out.new_page(width=W, height=H)
    M, PW = 24, 430
    azul = (0.12, 0.22, 0.39)
    pg.show_pdf_page(pymupdf.Rect(M, M + 40, W - PW - 2 * M, H - M), src, 0, keep_proportion=True)
    rs = resumo(status, nomes)
    pg.insert_text((M, M + 18), f"AVANÇO FÍSICO DA PRODUÇÃO | {titulo}", fontsize=18, fontname="helv", color=azul)
    pg.insert_text((M, M + 36),
                   f"Base: Plannix {data_ref}  |  Peças nesta planta: {rs['pecas']}  |  Fabricadas: {rs['fabricadas']} ({rs['pct']:.0%})  |  "
                   f"Programadas: {rs['programadas']}  |  Volume fabricado: {rs['volume_fab']:.1f} de {rs['volume']:.1f} m³ ({rs['pct_vol']:.0%})",
                   fontsize=10, fontname="helv", color=(0.35, 0.35, 0.35))
    x0, y = W - PW - M, M + 40
    pg.draw_rect(pymupdf.Rect(x0, y, W - M, H - M), color=(0.75, 0.75, 0.75), width=0.6)
    y += 14
    for k, (s, lab) in enumerate(LEGENDA):
        xx, yy = x0 + 8 + (k % 2) * 200, y + (k // 2) * 14
        pg.draw_rect(pymupdf.Rect(xx, yy - 8, xx + 10, yy + 1), color=CORES[s][0], fill=CORES[s][0], fill_opacity=0.6)
        pg.insert_text((xx + 14, yy), lab, fontsize=8, fontname="helv")
    y += 34
    cols = [("PEÇA", 0), ("QTD", 70), ("FAB.", 98), ("PROG.", 126), ("FALTA", 158), ("DATAS PROG.", 192)]
    ordem = sorted(nomes, key=lambda k: (status[k].produto, chave_natural(k)))
    rowh = 9.2
    maxrows = int((H - M - y - 20) / rowh)
    ncol = 1 if len(ordem) <= maxrows else 2
    cw, sc = (PW - 16) / ncol, (0.70 if ncol == 2 else 1)
    for c in range(ncol):
        cx = x0 + 8 + c * cw
        for t, dx in cols:
            pg.insert_text((cx + dx * sc, y), t, fontsize=7, fontname="hebo", color=azul)
        pg.draw_line((cx, y + 2.5), (cx + cw - 6, y + 2.5), color=azul, width=0.6)
    for i, k in enumerate(ordem[: maxrows * ncol]):
        c, ri = divmod(i, maxrows) if ncol == 2 else (0, i)
        cx, yy = x0 + 8 + c * cw, y + rowh * (ri + 1)
        s = status[k]
        pg.draw_rect(pymupdf.Rect(cx - 4, yy - 6.5, cx - 1, yy + 1), color=CORES[s.status][0], fill=CORES[s.status][0])
        ds = ", ".join(d.strftime("%d/%m") for d in s.datas[:3]) + ("…" if len(s.datas) > 3 else "")
        vals = [k, str(s.total), str(s.fabricadas or "-"), str(s.programadas or "-"), str(s.falta or "-"), ds]
        for (t, dx), v in zip(cols, vals):
            pg.insert_text((cx + dx * sc, yy), v, fontsize=6.6 if ncol == 2 else 7.2, fontname="helv")
    if len(ordem) > maxrows * ncol:
        pg.insert_text((x0 + 8, H - M - 16), f"+ {len(ordem) - maxrows * ncol} peças (lista completa no sistema)", fontsize=7, fontname="helv")
    pg.insert_text((x0 + 8, H - M - 6), f"PCP | Emitido em {date.today():%d/%m/%Y} | Formato A2", fontsize=7, fontname="helv", color=(0.45, 0.45, 0.45))
    return out.tobytes(garbage=3, deflate=True)
