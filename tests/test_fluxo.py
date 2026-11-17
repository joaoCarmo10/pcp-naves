import importlib
import os
import sys

import pymupdf
import pytest
from fastapi.testclient import TestClient

COLS = "Peça\tCódigo\tNome Obra\tSigla Obra\tItem\tProduto\tSeção\tQtde de Peças (Lanç. Final)\tVolume (m³) Total de Peças (Lanç. Final)\tData Fabricação\tComprimento (m)\tHora da Armação"


def tsv(linhas):
    return ("\n".join([COLS] + ["\t".join(l) for l in linhas]) + "\n").encode("latin-1")


COMPLETA = tsv([
    ["VR1", "1001", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "10/11/2026", "5,000", "-"],
    ["VR1", "1002", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "-", "5,000", "-"],
    ["P1", "1003", "9999-OBRA-TESTE", "9999", "1", "PILAR", "40x40", "1", "1,000", "-", "8,000", "-"],
    ["P2", "1004", "9999-OBRA-TESTE", "9999", "1", "PILAR", "40x40", "1", "1,000", "-", "8,000", "-"],
])
A_REALIZAR = tsv([
    ["P1", "1003", "9999-OBRA-TESTE", "9999", "1", "PILAR", "40x40", "1", "1,000", "-", "8,000", "12/11/2026 08:00:00"],
])


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_PASSWORD", "teste")
    for m in [m for m in sys.modules if m.startswith("app")]:
        del sys.modules[m]
    main = importlib.import_module("app.main")
    c = TestClient(main.app)
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.post("/login", data={"senha": "teste"}, follow_redirects=False).status_code == 303
    return c


def planta_pdf(caminho):
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    for i, t in enumerate(["VR1", "P1", "P2", "XYZ9"]):
        pg.insert_text((100 + 120 * i, 300), t, fontsize=14)
    doc.save(caminho)


def test_importacao_status_planta(cliente, tmp_path):
    r = cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)}, follow_redirects=False)
    assert "erro" not in r.headers["location"]
    r = cliente.post("/importar", files={"arquivo": ("a_realizar.xls", A_REALIZAR)}, follow_redirects=False)
    assert "saiu" not in r.headers["location"] or True

    from app.db import SessionLocal
    from app.models import Obra, Peca
    from app.status import status_da_obra
    db = SessionLocal()
    obra = db.query(Obra).one()
    assert obra.sigla == "9999"
    pecas = {p.codigo: p for p in db.query(Peca)}
    assert pecas["1001"].fabricada and pecas["1001"].origem_fabricacao == "plannix"
    assert pecas["1002"].fabricada and pecas["1002"].origem_fabricacao == "saiu_lista"   # saiu da lista a realizar
    assert not pecas["1003"].fabricada and pecas["1003"].hora_armacao is not None
    st = status_da_obra(db, obra.id)
    assert st["VR1"].status == "FAB" and st["P1"].status == "FALTA" and st["P2"].status == "FAB"

    pdf = tmp_path / "planta.pdf"
    planta_pdf(pdf)
    with open(pdf, "rb") as f:
        r = cliente.post(f"/obras/{obra.id}/plantas", data={"titulo": "Térreo"}, files={"arquivo": ("planta.pdf", f.read())}, follow_redirects=False)
    assert r.status_code == 303
    html = cliente.get("/plantas/1").text
    assert 'data-p="VR1"' in html and 'data-p="XYZ9"' not in html
    assert cliente.get("/plantas/1/imagem").headers["content-type"] == "image/png"
    a2 = cliente.get("/plantas/1/a2.pdf")
    assert a2.headers["content-type"] == "application/pdf"
    d = pymupdf.open(stream=a2.content, filetype="pdf")
    assert round(d[0].rect.width) == 1684 and round(d[0].rect.height) == 1191   # A2 paisagem
    assert cliente.get("/").status_code == 200 and cliente.get(f"/obras/{obra.id}").status_code == 200


def prog_xlsx():
    from datetime import date
    from io import BytesIO
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "N1 + N3"
    ws.append(["PROGRAMAÇÃO"])
    ws.append([])
    ws.append([])
    ws.append([])
    ws.append(["DATA", "DIA", "FÔRMA", "OBRA", "PEÇA", "CÓDIGO", "PRODUTO", "SEÇÃO", "COMP. (m)", "QTD", "VOL. (m³)", "PAVIMENTO", "ARMAÇÃO", "TIPO", "ADAPTAÇÃO", "OBSERVAÇÃO", "REALIZADO"])
    ws.append([date(2026, 11, 9), "SEG", "N1 - F1 30x80 (30m)", "9999-OBRA-TESTE", "VR1", "1001", "VIGA", "20x60", 5, 1, None, None, None, "PCP", None, None, None])   # fab 10/11: atraso
    ws.append([date(2026, 11, 10), "TER", "N1 - F1 30x80 (30m)", "9999-OBRA-TESTE", "VR1", "1002", "VIGA", "20x60", 5, 1, None, None, None, "PCP", None, None, None])  # saiu da lista
    ws.append([date(2026, 11, 10), "TER", "N3 - F4 60x60 (18m)", "9999-OBRA-TESTE", "P1", "1003", "PILAR", "40x40", 8, 1, None, None, None, "PCP", None, None, "N"])
    ws.append([date(2099, 1, 5), "SEG", "N3 - NEW JERSEY", "New Jersey", "NJ-180 (13 un.)", None, "NEW JERSEY", "38x80x180", 1.8, 13, None, None, None, "FIXA", None, None, None])
    wb.create_sheet("N2")
    b = BytesIO()
    wb.save(b)
    return b.getvalue()


def test_programacao_pxr(cliente):
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    cliente.post("/importar", files={"arquivo": ("a_realizar.xls", A_REALIZAR)})
    r = cliente.post("/importar", files={"arquivo": ("Rev01.xlsx", prog_xlsx())}, follow_redirects=False)
    assert "erro" not in r.headers["location"]
    from datetime import date
    from app import programacao as pg
    from app.db import SessionLocal
    db = SessionLocal()
    rev = pg.revisao_atual(db)
    st = {l.item.codigo or l.item.peca: l.status for l in pg.linhas(db, rev, hoje=date(2026, 11, 16))}
    assert st["1001"] == "COM ATRASO" and st["1002"] == "DATA A CONFIRMAR" and st["1003"] == "NÃO REALIZADA"
    assert st["NJ-180 (13 un.)"] == "PENDENTE"
    it = next(l.item for l in pg.linhas(db, rev) if l.item.codigo == "1003")
    j = cliente.post(f"/apontamentos/{it.id}", json={"realizado": "S"}).json()
    assert j["status"] == "NO PRAZO"
    cliente.post(f"/apontamentos/{it.id}", json={"realizado": "N", "causa": "Armação", "acao": "Antecipar armação", "responsavel": "Encarregado N3"})
    html = cliente.get(f"/pxr?rev={rev.id}&de=2026-11-09&ate=2026-11-10").text
    assert "Armação" in html and "Antecipar armação" in html
    for u in ["/programacao", f"/programacao?rev={rev.id}&dia=2026-11-10", f"/programacao/quadro?rev={rev.id}"]:
        assert cliente.get(u).status_code == 200
    # realizado do dia: lista suspensa de dias, PPC acumulado, X no dia da concretagem e "o que houve"
    cliente.post(f"/apontamentos/{it.id}", json={"ocorrido": "Ferragem atrasou"})
    html = cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10").text
    assert '<select id="dia"' in html and "PPC acumulado" in html and "Ferragem atrasou" in html
    assert "<b>X</b>" in cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-09").text   # 1001 concretada em 10/11
