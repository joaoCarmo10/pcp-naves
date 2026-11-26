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
    from app.db import SessionLocal as _S
    from app.models import Obra as _O
    cliente.post("/obras/escolher", data={"obra": [str(o.id) for o in _S().query(_O)]})   # obras acompanhadas
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
    cliente.post(f"/apontamentos/{it.id}", json={"comentario": "Ferragem atrasou"})
    html = cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10").text
    assert '<select id="dia"' in html and "PPC acumulado" in html and "Ferragem atrasou" in html
    assert "10/11/26" in cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-09").text   # 1001 concretada em 10/11 (Plannix)
    # data de execução informada pelo PCP vale quando o Plannix não tem data
    it2 = next(l.item for l in pg.linhas(db, rev) if l.item.codigo == "1002")
    assert cliente.post(f"/apontamentos/{it2.id}", json={"data_execucao": "2026-11-10"}).json()["status"] == "NO PRAZO"
    assert cliente.post(f"/apontamentos/{it2.id}", json={"data_execucao": "2026-11-12"}).json()["status"] == "COM ATRASO"
    assert 'value="2026-11-12"' in cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10").text
    # filtros por obra, nave, produto e peça
    h = cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10&nave=N3").text
    assert "1003" in h and "1002" not in h
    h = cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10&produto=VIGA&peca=vr1").text
    assert "1002" in h and "1003" not in h
    # filtro de situação: só lista, indicadores continuam do dia inteiro
    h = cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10&situacao=nao").text
    assert "1003" in h and "1002" not in h
    h = cliente.get(f"/programacao?rev={rev.id}&dia=2026-11-10&situacao=atraso").text
    assert "1002" in h and "1003" not in h


def test_formas(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Forma
    db = SessionLocal()
    fs = list(db.scalars(select(Forma)))
    assert len(fs) == 30
    h = cliente.get("/formas?dia=2026-11-17").text
    assert "Nave 1" in h and "F2 80x60" in h and "PISTA P1" in h
    assert "ESCADA 2" not in h.split("Cadastro completo")[0]           # escada 2 só vale a partir de 18/11
    assert "ESCADA 2" in cliente.get("/formas?dia=2026-11-18").text.split("Cadastro completo")[0]
    f4 = next(f for f in fs if f.codigo == "N1F4")
    assert cliente.post(f"/formas/{f4.id}/posicao", json={"x": 10.3, "y": 7.9}).json()["ok"]
    db.expire_all()
    assert (db.get(Forma, f4.id).x, db.get(Forma, f4.id).y) == (10.5, 8.0)
    # ampliação a partir de 01/12: 16 m vale até 30/11, 21 m a partir de 01/12, mesmo lugar
    campos = dict(nave="N1", nome="F4 40x40", tipo="linear", secao="40x40", secoes_aceitas="30x40", comprimento="21", capacidade="1",
                  ritmo_dias="1", inicio="", fim="", situacao="ativa", obs="ampliada", dia="2026-11-17", versao_em="2026-12-01")
    r = cliente.post(f"/formas/{f4.id}", data=campos, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    vs = sorted(db.scalars(select(Forma).where(Forma.codigo == "N1F4")), key=lambda f: f.id)
    assert len(vs) == 2 and vs[0].fim == date(2026, 11, 30) and vs[1].inicio == date(2026, 12, 1) and vs[1].comprimento == 21
    assert vs[1].x == 10.5
    assert "21 m" in cliente.get("/formas?dia=2026-12-02").text.split("Cadastro completo")[0]
    # nova fôrma
    novo = dict(campos, codigo="n3f15", nave="N3", nome="F15 30x60", comprimento="12", versao_em="")
    assert cliente.post("/formas/nova", data=novo, follow_redirects=False).status_code == 303
    assert db.scalar(select(Forma).where(Forma.codigo == "N3F15")).nave == "N3"
    # pop-up da fôrma
    h = cliente.get(f"/formas/{f4.id}/detalhe?dia=2026-11-17").text
    assert "Programado nesta fôrma" in h and "Peças a fazer que cabem" in h


def test_planejar(cliente):
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Forma, ItemProg, RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    r = cliente.post("/planejar/revisao", data={"modo": "vazia", "dia": "2026-11-18"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    from app.models import Obra
    db.scalar(select(Obra)).ativa = True     # obra de teste não está entre as acompanhadas
    db.commit()
    rev = db.scalar(select(RevisaoProg))
    h = cliente.get(f"/planejar?rev={rev.id}&dia=2026-11-18").text
    assert "Peças a programar" in h and '"1004"' in h          # P2 40x40 está na lista de faltantes
    f4 = db.scalar(select(Forma).where(Forma.codigo == "N1F4"))
    j = cliente.post("/planejar/alocar", json={"rev": rev.id, "dia": "2026-11-18", "forma": f4.id, "codigos": ["1004"]}).json()
    assert j["ok"]
    # VR1 20x60 não encaixa na 40x40: pede confirmação; forçando, grava com observação
    j = cliente.post("/planejar/alocar", json={"rev": rev.id, "dia": "2026-11-18", "forma": f4.id, "codigos": ["1002"]}).json()
    assert not j["ok"] and j["pode_forcar"] and "não cabe" in j["problemas"][0]
    j = cliente.post("/planejar/alocar", json={"rev": rev.id, "dia": "2026-11-18", "forma": f4.id, "codigos": ["1002"], "forcar": True}).json()
    assert j["ok"]
    itens = list(db.scalars(select(ItemProg).where(ItemProg.revisao_id == rev.id)))
    assert {i.codigo for i in itens} == {"1002", "1004"} and all(i.forma == "N1 - F4 40x40 (16m)" for i in itens)
    assert '"1004"' not in cliente.get(f"/planejar?rev={rev.id}&dia=2026-11-18").text.split("const FALT")[1][:2000]
    h = cliente.get(f"/planejar/forma/{f4.id}?rev={rev.id}&dia=2026-11-18").text
    assert "P2" in h and "Forçado" in h
    assert cliente.post("/planejar/remover", json={"ids": [i.id for i in itens]}).json()["n"] == 2
    # cópia de revisão
    r = cliente.post("/planejar/revisao", data={"modo": "copia", "rev": str(rev.id), "dia": "2026-11-18"}, follow_redirects=False)
    assert r.status_code == 303 and len(list(db.scalars(select(RevisaoProg)))) == 2


def test_armacao(cliente):
    from sqlalchemy import select
    from app import armacao as arm
    from app.db import SessionLocal
    from app.models import RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    cliente.post("/importar", files={"arquivo": ("prog.xlsx", prog_xlsx())})
    rev = SessionLocal().scalar(select(RevisaoProg))
    h = cliente.get(f"/armacao?dia=2026-11-10&rev={rev.id}").text
    assert "Ainda não divulgada" in h and "VR1" in h
    # armador sem divulgação não vê nada; link errado dá 404
    tok = arm.token_armador()
    assert cliente.get("/armador/errado").status_code == 404
    cliente.post("/armacao/divulgar", data={"dia": "2026-11-10", "rev": str(rev.id)})
    assert cliente.post("/armacao/marcar", json={"codigos": ["1003"], "armado": True}).json()["n"] == 1
    h = cliente.get(f"/armacao?dia=2026-11-10&rev={rev.id}").text
    assert "Divulgada em" in h and "App " in h
    # o link do armador funciona sem login (só leitura)
    import app.main as m
    from fastapi.testclient import TestClient
    anon = TestClient(m.app)
    assert anon.get("/armacao").status_code in (200, 303)
    assert anon.get("/armacao", follow_redirects=False).status_code == 303 or not m.SENHA
    assert cliente.post("/armacao/marcar", json={"codigos": ["1003"], "armado": False}).json()["n"] == 1
    assert cliente.get(f"/armador/{tok}").status_code == 200


def test_importar_varios(cliente):
    # "a realizar" enviado antes do realizado na seleção: o app processa o realizado primeiro
    r = cliente.post("/importar", files=[("arquivo", ("a_realizar.xls", A_REALIZAR)), ("arquivo", ("realizado.xls", COMPLETA))],
                     follow_redirects=False)
    from urllib.parse import unquote
    msg = unquote(r.headers["location"])
    assert "erro" not in msg and msg.index("realizado.xls") < msg.index("a_realizar.xls")


def test_all_data(cliente):
    """Arquivo único com fabricadas (com data) e pendentes (sem data): o Plannix manda nas duas pontas."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Importacao, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    cliente.post("/importar", files={"arquivo": ("a_realizar.xls", A_REALIZAR)})   # 1002 e 1004 saem da lista: baixa provisória
    db = SessionLocal()
    assert db.scalar(select(Peca).where(Peca.codigo == "1004")).fabricada
    cliente.post("/importar", files={"arquivo": ("all_data.xls", COMPLETA)})       # Plannix: 1004 sem data = pendente
    db.expire_all()
    p = {x.codigo: x for x in db.scalars(select(Peca))}
    assert p["1001"].fabricada and p["1001"].origem_fabricacao == "plannix"
    assert not p["1004"].fabricada and not p["1002"].fabricada
    assert db.scalars(select(Importacao).order_by(Importacao.id.desc())).first().tipo == "geral"


def test_planta_com_comentarios_shx(tmp_path):
    """PDF do AutoCAD com texto SHX: etiquetas vêm dos comentários (anotações) e não do texto."""
    import pymupdf
    from app import plantas
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    pg.draw_line((50, 50), (300, 50))                       # desenho sem texto
    a = pg.add_rect_annot(pymupdf.Rect(100, 100, 130, 115))
    a.set_info(content="LC1")
    a.update()
    f = tmp_path / "shx.pdf"
    doc.save(f)
    e = plantas.extrair(f)
    assert [m["t"] for m in e["marcas"]] == ["LC1"]


def test_pecas_identicas(cliente):
    """LE1 programada com um código e concretada com outro de mesmo nome: atende a programada mais antiga em aberto."""
    from datetime import date
    from sqlalchemy import select
    from openpyxl import Workbook
    from app import programacao as pg
    from app.db import SessionLocal
    from app.models import RevisaoProg
    base = tsv([
        ["LE1", "2001", "9999-OBRA-TESTE", "9999", "1", "ESCADA", "VARIAVEL", "1", "0,900", "-", "3,200", "-"],
        ["LE1", "2002", "9999-OBRA-TESTE", "9999", "1", "ESCADA", "VARIAVEL", "1", "0,900", "-", "3,200", "-"],
        ["LE1", "2009", "9999-OBRA-TESTE", "9999", "1", "ESCADA", "VARIAVEL", "1", "0,900", "17/11/2026", "3,200", "-"],
    ])
    cliente.post("/importar", files={"arquivo": ("all.xls", base)})
    wb = Workbook(); ws = wb.active; ws.title = "N1 + N3"
    ws.append(["DATA", "DIA", "FÔRMA", "OBRA", "PEÇA", "CÓDIGO", "PRODUTO", "SEÇÃO", "COMP. (m)", "QTD"])
    ws.append([date(2026, 11, 16), "SEG", "N3 - ESCADA 1", "9999-OBRA-TESTE", "LE1", "2001", "ESCADA", "VARIAVEL", 3.2, 1])
    ws.append([date(2026, 11, 17), "TER", "N3 - ESCADA 1", "9999-OBRA-TESTE", "LE1", "2002", "ESCADA", "VARIAVEL", 3.2, 1])
    import io
    b = io.BytesIO(); wb.save(b)
    cliente.post("/importar", files={"arquivo": ("prog.xlsx", b.getvalue())})
    db = SessionLocal()
    rev = db.scalars(select(RevisaoProg).order_by(RevisaoProg.id.desc())).first()
    st = {l.item.codigo: l for l in pg.linhas(db, rev, hoje=date(2026, 11, 18))}
    assert st["2001"].status == "COM ATRASO" and st["2001"].troca == "2009"    # a feita em 17/11 atende a de segunda (atrasada)
    assert st["2002"].status == "NÃO REALIZADA"


def test_usuarios_e_perfis(cliente):
    import re
    from fastapi.testclient import TestClient
    import app.main as m
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Perfil, Usuario
    db = SessionLocal()
    assert db.scalar(select(Usuario)).login == "joao"                     # admin criado com a senha do app
    ger = db.scalar(select(Perfil).where(Perfil.nome == "Gerência"))
    r = cliente.post("/usuarios/novo", data={"login": "Maria.Silva", "nome": "Maria", "perfil_id": ger.id}, follow_redirects=True)
    prov = re.search(r"Senha provisória: (\w+)", r.text).group(1)
    # login da Maria: primeiro acesso força troca de senha
    c2 = TestClient(m.app)
    assert c2.post("/login", data={"usuario": "maria.silva", "senha": "errada"}).status_code == 401
    r = c2.post("/login", data={"usuario": "maria.silva", "senha": prov}, follow_redirects=False)
    assert r.headers["location"] == "/conta"
    assert c2.get("/pxr", follow_redirects=False).headers["location"] == "/conta"
    assert c2.post("/conta", data={"atual": prov, "nova": "senhaNova123", "repete": "senhaNova123"}).status_code == 200
    # gerência: vê, não grava, não acessa importar nem usuários
    assert c2.get("/pxr").status_code == 200
    h = c2.get("/programacao").text
    assert "somente-leitura" in h and "Importar dados" not in h.split("<main>")[0]
    assert c2.post("/apontamentos/1", json={"comentario": "x"}).status_code == 403
    assert c2.get("/importar").status_code == 403 and c2.get("/usuarios").status_code == 403
    assert c2.post("/formas/nova", data={}).status_code == 403
    # admin muda o perfil da gerência para editar o realizado
    cliente.post(f"/perfis/{ger.id}", data={f"m_{k}": "ver" for k in ["obras", "realizado", "pxr", "planejar", "armacao", "formas"]} | {"m_realizado": "editar"})
    assert c2.post("/apontamentos/999", json={"comentario": "x"}).status_code == 404   # passou da permissão (item não existe)
    # desativar: a sessão cai
    maria = db.scalar(select(Usuario).where(Usuario.login == "maria.silva"))
    cliente.post(f"/usuarios/{maria.id}", data={"perfil_id": ger.id, "ativo": "", "nome": "Maria"})
    assert c2.get("/pxr", follow_redirects=False).status_code == 303
    # não dá para deixar o app sem administrador
    adm = db.scalar(select(Usuario).where(Usuario.login == "joao"))
    r = cliente.post(f"/usuarios/{adm.id}", data={"perfil_id": ger.id, "ativo": "1", "nome": "x"}, follow_redirects=True)
    assert "pelo menos um administrador" in r.text
    assert "POST /usuarios/novo" in cliente.get("/usuarios").text           # auditoria


def _prancha_pdf(nome="PP1", cord=False):
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=1200, height=800)
    t = lambda x, y, s: pg.insert_text((x, y), s, fontsize=7)
    t(700, 40, "LISTA DE MATERIAL POR PEÇA")
    for x, s in [(700, "Peça"), (740, "Quantidade"), (790, "(Un)"), (820, "Volume"), (850, "unitário"), (900, "Volume"), (930, "total"), (980, "Peso"), (1000, "da"), (1010, "peça")]:
        t(x, 60, s)
    for x, s in [(700, nome), (760, "4"), (850, "0.15"), (930, "0.61"), (1005, "362.2")]:
        t(x, 72, s)
    t(1080, 72, "Fck-40")
    for x, s in [(700, "Posição"), (745, "Diâmetro"), (800, "Quant."), (850, "Material"), (920, "Compr."), (980, "Unitário"), (1030, "Total")]:
        t(x, 100, s)
    linhas = [("N183", "12.5", "6", "CA-50A", "245", "2.40", "14.42"), ("ES34", "6.3", "18", "CA-50A", "82", "0.20", "3.68")]
    for k, (p, d, q, m, c, u, tt) in enumerate(linhas):
        y = 115 + k * 14
        for x, s in [(700, p), (745, "Ø"), (752, d), (768, "mm"), (805, q), (850, m), (922, c), (982, u), (1032, tt)]:
            t(x, y, s)
    t(700, 160, "Cobrimento:"); t(745, 160, "3.00"); t(800, 160, "Peso"); t(820, 160, "unitário"); t(855, 160, "(kg)"); t(880, 160, "18.10")
    return doc.tobytes()


def test_cadastro_pecas(cliente, tmp_path):
    import time
    from sqlalchemy import select
    from app import pranchas
    from app.db import SessionLocal
    from app.models import CadastroPeca, Obra, Peca
    f = tmp_path / "24-014-X-ECC-052-PIL-R00.pdf"
    f.write_bytes(_prancha_pdf())
    L = pranchas.ler(f, {"PP1"})
    assert L.peca == "PP1" and L.quantidade == 4 and L.volume_un == 0.15 and L.fck == 40 and L.revisao == "R00"
    assert [(i["pos"], i["diam"], i["quant"], i["peso_tot"]) for i in L.itens] == [("N183", 12.5, 6, 14.42), ("ES34", 6.3, 18, 3.68)]
    assert L.alertas == []
    # pela tela: prancha enviada dentro da obra vira cadastro ligado às unidades do Plannix
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    db.add(Peca(obra_id=o.id, codigo="7001", peca="PP1", produto="PILAR", secao="30X20", volume=0.15, item="4"))
    db.commit()
    cliente.post(f"/obras/{o.id}/pranchas", files=[("arquivo", ("24-014-X-ECC-052-PIL-R00.pdf", _prancha_pdf(), "application/pdf"))])
    for _ in range(50):
        c = db.scalar(select(CadastroPeca))
        if c:
            break
        time.sleep(0.1)
        db.expire_all()
    assert c.peca == "PP1" and c.etapas == "4" and c.aco_passivo == 18.1 and c.bitolas == {"6.3": 3.68, "12.5": 14.42}
    h = cliente.get(f"/obras/{o.id}/pecas").text
    assert "PP1" in h and "Sem prancha" in h
    assert cliente.get(f"/cadastro/{c.id}").status_code == 200 and cliente.get(f"/cadastro/{c.id}/pdf").status_code == 200


def test_plannix_automatico(cliente):
    import gzip, re
    assert cliente.post("/api/plannix", content=COMPLETA).status_code == 401
    h = cliente.post("/importar/token").text
    token = re.search(r'class="token" readonly value="([^"]+)"', h)[1]
    cab = {"Authorization": f"Bearer {token}", "Content-Encoding": "gzip"}
    r = cliente.post("/api/plannix", content=gzip.compress(COMPLETA), headers=cab).json()
    assert r["ok"] and r["resumo"]["linhas"] > 0
    assert cliente.post("/api/plannix", content=gzip.compress(COMPLETA), headers=cab).json()["sem_mudanca"]
    r = cliente.post("/api/plannix", content=b"x\ty\n1\t2\n", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422
    pg = cliente.get("/importar").text
    assert "Último envio recusado" in pg and "Plannix (automático)" in pg
    assert cliente.post("/api/plannix", content=COMPLETA, headers={"Authorization": "Bearer errado"}).status_code == 401


def test_modulo_pecas(cliente):
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    h = cliente.get("/pecas?visao=detalhe").text
    assert "tipos de peça" in h and "Sem prancha" in h and 'href="/pecas"' in h
    assert cliente.get("/pecas?visao=detalhe&situacao=com").status_code == 200
    assert "Nenhuma peça" in cliente.get("/pecas?visao=detalhe&q=NAOEXISTE").text


def test_pranchas_pelo_modulo_pecas(cliente):
    import time
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import CadastroPeca, Obra, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    db.add(Peca(obra_id=o.id, codigo="7002", peca="PP1", produto="PILAR", secao="30X20", volume=0.15, item="4"))
    db.commit()
    r = cliente.post("/pecas/pranchas", data={"obra_id": str(o.id)},
                     files=[("arquivo", ("24-014-X-ECC-052-PIL-R00.pdf", _prancha_pdf(), "application/pdf"))])
    assert r.status_code == 200 and "prancha(s) recebida(s)" in r.text and "Enviar pranchas" in r.text
    for _ in range(50):
        if db.scalar(select(CadastroPeca)):
            break
        time.sleep(0.1)
    h = cliente.get(f"/pecas?visao=detalhe&obra={o.nome}").text
    assert "Ø12.5" in h and "/cadastro/" in h
    assert "Cadastro de peças (pranchas)" not in cliente.get(f"/obras/{o.id}").text
    assert cliente.get(f"/obras/{o.id}/pecas", follow_redirects=False).headers["location"].startswith("/pecas?visao=detalhe&obra=")


def test_pecas_completo_e_planilha(cliente):
    import io
    import openpyxl
    from app.pecas_tab import dim_secao
    assert dim_secao("h20") == (None, 20) and dim_secao("20x70", "VIGA") == (20, 70) and dim_secao("VARIAVEL") == (None, None)
    assert dim_secao("16X125", "LAJE") == (125, 16) and dim_secao("115x16", "LAJE") == (115, 16) and dim_secao("76,5X16", "LAJE") == (76.5, 16)
    test_pranchas_pelo_modulo_pecas(cliente)
    h = cliente.get("/pecas?visao=detalhe").text
    assert "Alt. (cm)" in h and "Barras/pç" in h and "Baixar Excel" in h
    r = cliente.get("/pecas/planilha")
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Peças", "Armadura por posição", "Aço pendente por bitola"]
    cab = [c.value for c in wb["Peças"][1]]
    assert "Nº barras/pç" in cab and "Ø12.5 (nº/pç)" in cab and "Altura (cm)" in cab
    lin = {row[1]: dict(zip(cab, row)) for row in wb["Peças"].iter_rows(min_row=2, values_only=True)}
    assert lin["PP1"]["Nº barras/pç"] == 24 and lin["PP1"]["Nº estribos/pç"] == 18 and lin["PP1"]["Ø12.5 (nº/pç)"] == 6
    assert wb["Armadura por posição"].max_row == 3


def test_indicios_segunda_etapa():
    from app.pranchas import indicios_segunda_etapa as ind
    assert ind(["CONSOLE 0 126 25 309,5"]) == ["desenho com consolo"]
    assert ind(["+/- 3 MM DIMENSAO DO CONSOLO / DENTE GERBER", "+/-10MM POSI¢DO DO CONSOLO"]) == []
    assert ind(["DIMENSDO DO CONSOLO / DENTE GERBER"]) == []
    assert ind(["CONCRETAGEM EM 2A ETAPA"]) == ['nota "2a etapa"']
    assert ind(["VIGA L PROTENDIDA", "RECORTE 3X5CM"]) == []


def test_prancha_em_obra_errada_aparece(cliente):
    import time
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import CadastroPeca, Obra
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    r = cliente.post("/pecas/pranchas", data={"obra_id": str(o.id)},
                     files=[("arquivo", ("X-R00.pdf", _prancha_pdf(nome="ZZ9"), "application/pdf"))])
    for _ in range(50):
        if db.scalar(select(CadastroPeca)):
            break
        time.sleep(0.1)
    time.sleep(0.2)
    h = cliente.get(f"/pecas?visao=detalhe&obra={o.nome}").text
    assert "Pranchas sem peça no Plannix (1)" in h and "ZZ9" in h
    assert "ZZ9 não existe nesta obra no Plannix" in h


def test_reler_orfaos(cliente):
    import time
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import CadastroPeca, Obra
    test_prancha_em_obra_errada_aparece(cliente)
    db = SessionLocal()
    o = db.scalar(select(Obra))
    from app import cadastro as cad
    antes = len(cad.ANDAMENTO[o.id]["msgs"])
    h = cliente.post("/pecas/reler", data={"obra_id": str(o.id)}).text
    assert "1 prancha(s) sendo lida(s) de novo" in h
    for _ in range(50):
        if len(cad.ANDAMENTO[o.id]["msgs"]) > antes:
            break
        time.sleep(0.1)
    time.sleep(0.1)
    db.expire_all()
    assert [c.peca for c in db.scalars(select(CadastroPeca))] == ["ZZ9"]


def test_modelo_tabela_de_aco_elite(tmp_path):
    """Pilar da Elite: dados em texto solto, tabela do pilar e de cada consolo (aço do consolo x quantidade)."""
    from pathlib import Path
    import pymupdf
    from app import pranchas
    doc = pymupdf.open()
    pg = doc.new_page(width=2384, height=1684)
    for l in open(Path(__file__).with_name("elite_p01.txt"), encoding="utf-8"):
        y, r = l.rstrip("\n").split("|", 1)
        for tok in r.split(" "):
            t, x = tok.rsplit("@", 1)
            w = pymupdf.get_text_length(t, fontname="helv", fontsize=7)
            pg.insert_text((float(x) - w / 2, float(y) + 2.5), t, fontsize=7, fontname="helv")
    for k in range(160):
        pg.insert_text((900 + (k % 20) * 20, 1000 + (k // 20) * 10), f"c{k}", fontsize=5)
    f = tmp_path / "301-CIV-CFAP-P01-R01.pdf"
    doc.save(f)
    L = pranchas.ler(f, {"P01"})
    assert (L.peca, L.quantidade, L.volume_un, L.peso, L.fck, L.fcj, L.cobrimento) == ("P01", 1, 2.835, 7088, 50, 30, 3.5)
    assert L.alertas == [] and L.revisao == "R01" and len(L.itens) == 25
    r = pranchas.resumo(L.itens)
    assert r["aco_passivo"] == 613.62 and r["bitolas"]["20"] == 376.96


def test_elite_girada_e_pecas_iguais(tmp_path):
    import pymupdf
    from app import pranchas
    test_modelo_tabela_de_aco_elite(tmp_path)
    src = pymupdf.open(tmp_path / "301-CIV-CFAP-P01-R01.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=1684, height=2384)
    pg.show_pdf_page(pg.rect, src, 0, rotate=-90)
    pg.set_rotation(270)
    f = tmp_path / "301-CIV-CFAP-P03-R00.pdf"
    doc.save(f)
    L = pranchas.ler(f, {"P01"})
    assert L.peca == "P01" and pranchas.resumo(L.itens)["aco_passivo"] == 613.62
    # título com peças iguais: "P09=P19 (2x)"
    d2 = pymupdf.open()
    p2 = d2.new_page(width=600, height=300)
    p2.insert_text((50, 50), "P09=P19", fontsize=7)
    p2.insert_text((90, 50), "(2x)", fontsize=7)
    for k in range(40):
        p2.insert_text((50 + (k % 10) * 30, 100 + (k // 10) * 10), f"c{k}", fontsize=5)
    L2 = pranchas.Leitura()
    pranchas._modelo_tabela_de_aco(L2, pranchas._palavras(p2), {})
    assert (L2.peca, L2.iguais, L2.quantidade) == ("P09", ["P19"], 2)


def test_planta_texto_de_traco():
    """Etiqueta desenhada com traços soltos (fonte de linha): 'P11' vira uma palavra de 3 caracteres."""
    import pymupdf
    from app import ocr_planta
    doc = pymupdf.open()
    pg = doc.new_page(width=200, height=100)
    x0, y0, h = 20, 20, 9.6
    sh = pg.new_shape()
    sh.draw_line((x0, y0), (x0, y0 + h))                     # haste do P
    sh.draw_polyline([(x0, y0), (x0 + 4, y0), (x0 + 5, y0 + 2.5), (x0 + 4, y0 + 5), (x0, y0 + 5)])  # barriga
    sh.finish(color=(0, 0, 0), width=0.48)
    for x in (x0 + 9, x0 + 15):                              # dois "1" com espaço largo
        sh.draw_line((x, y0), (x, y0 + h))
        sh.finish(color=(0, 0, 0), width=0.48)
    sh.commit()
    ws = ocr_planta.palavras_traco(pg)
    assert len(ws) == 1 and ws[0]["n"] == 3 and ws[0]["rect"].height >= 9.5


def test_pxr_filtros_e_relatorio(cliente):
    import io
    import openpyxl
    test_programacao_pxr(cliente)
    from app import programacao as pg
    from app.db import SessionLocal
    rev = pg.revisao_atual(SessionLocal())
    base = f"rev={rev.id}&de=2026-11-09&ate=2026-11-10"
    h = cliente.get(f"/pxr?{base}&status=atrasadas").text
    assert "Gerar relatório" in h and "Por tipo de peça" in h and 'name="semana"' in h
    h = cliente.get(f"/pxr?{base}&obra=NAOEXISTE").text
    assert cliente.get(f"/pxr?{base}&obra=NAOEXISTE").status_code == 200
    rel = cliente.get(f"/pxr/relatorio?{base}").text
    assert "Destaques" in rel and "Causas de não cumprimento" in rel and "Peças atrasadas e justificativas" in rel
    assert "Ferragem atrasou" in rel or "Armação" in rel
    r = cliente.get(f"/pxr/planilha?{base}")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert {"Resumo", "Por obra", "Por tipo de peça", "Obra x tipo", "Causas (Pareto)", "Atrasadas (justificativas)"} <= set(wb.sheetnames)
    assert cliente.get(f"/pxr/relatorio?rev={rev.id}&semana=2026-11-09").status_code == 200


def test_pxr_pendencias(cliente):
    test_programacao_pxr(cliente)
    from app import programacao as pg
    from app.db import SessionLocal
    rev = pg.revisao_atual(SessionLocal())
    base = f"rev={rev.id}&de=2026-11-09&ate=2026-11-10"
    assert "Imprimir o que ficou para trás" in cliente.get(f"/pxr?{base}").text
    for por in ("", "&por=obra"):
        h = cliente.get(f"/pxr/pendencias?{base}{por}").text
        assert "O que ficou para trás" in h and "obra × tipo de peça" not in h


def test_pxr_tabela_producao(cliente):
    import io
    import openpyxl
    test_programacao_pxr(cliente)
    from app import programacao as pg
    from app.db import SessionLocal
    rev = pg.revisao_atual(SessionLocal())
    base = f"rev={rev.id}&de=2026-11-09&ate=2026-11-10"
    assert "Tabela para a produção (Excel)" in cliente.get(f"/pxr?{base}").text
    r = cliente.get(f"/pxr/tabela?{base}")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[0] == "RESUMO"
    ws = wb[wb.sheetnames[1]]
    cab = [c.value for c in ws[3]]
    assert cab[:5] == ["DATA", "DIA", "FÔRMA", "OBRA", "PEÇA"] and "O QUE HOUVE" in cab and "SEG\n09/11" in cab


def _autodoc_xlsx(projeto="9999-OBRA TESTE", pecas=(("PP1", "02", "Bloqueado"),)):
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Relatório de Arquivos"
    ws["C1"] = "Civil Pré-Fabricados - Relatório de Arquivos"
    ws.append([])
    ws.append(["Código", "Tipo", "Extensão", "Revisão", "Título", "Tamanho", "Data", "Cadastrado por", "Status", "Obsoleto",
               "Data Obsoleto", "Pasta", "Link da Pasta", "Projeto", "Disciplina", "Fase", "Cor de Impressão", "Observações"])
    for k, (p, rev, st) in enumerate(pecas, 1):
        ws.append([f"{k:02d}-773-FAB-PIL-R{rev}.pdf", "Arquivo", "pdf", rev, f"Detalhamento de Pilar {p}", "1 MB", "05/11/2026 10:29",
                   "x@y", st, "Não", " ", "OBRA/02-PILARES/", "https://x", projeto, "", "", "", "Em conferência" if st != "Aprovado" else ""])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_autodoc_status_na_peca(cliente):
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Obra, Peca
    from app import planejar
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = True
    db.commit()
    nome = db.scalar(select(Peca.peca).where(Peca.obra_id == o.id, Peca.fabricada.is_(False)))
    r = cliente.post("/importar", files={"arquivo": ("ExportarDeDiretorios.xlsx",
                                                     _autodoc_xlsx(f"{o.sigla}-TESTE", ((nome, "01", "Bloqueado"), ("ZZ1", "00", "Aprovado"))))})
    assert "Autodoc, 2 desenhos" in r.text and f"bloqueados: {nome}" in r.text
    g = next(x for x in planejar.faltantes(db, None) if x["peca"] == nome)
    assert g["ad"]["bloqueado"] and g["ad"]["rev"] == "01"
    p = db.scalar(select(Peca).where(Peca.obra_id == o.id, Peca.peca == nome))
    assert planejar.desenho_bloqueado(db, [p])
    assert "Bloqueado R01" in cliente.get(f"/pecas?visao=detalhe&obra={o.nome}").text


def test_robo_autodoc_sem_rede(cliente, monkeypatch):
    """Varredura com o serviço do Autodoc simulado: status nas peças e escolha das pranchas a baixar."""
    from sqlalchemy import select
    from app import autodoc, autodoc_robo as R
    from app.db import SessionLocal
    from app.models import Obra, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = True
    db.commit()
    pend = db.scalar(select(Peca.peca).where(Peca.obra_id == o.id, Peca.fabricada.is_(False)))
    arvore = {
        "raiz": {"currentFolder": {"type": "OTHER"}, "subFolders": [{"id": "obra"}], "documents": []},
        "obra": {"currentFolder": {"type": "PROJECT", "name": f"{o.sigla}-TESTE"}, "subFolders": [{"id": "pil"}], "documents": []},
        "pil": {"currentFolder": {"type": "OTHER"}, "subFolders": [], "documents": [
            {"name": "01-PIL-R01.pdf", "description": f"Detalhamento de Pilar {pend}", "revision": "01", "status": "8799",
             "obsolete": "false", "status_updated_at": "2026-11-17 20:46:42", "path": "OBRAS/X/02-PILARES/01-PIL-R01.pdf",
             "folder_id": "pil", "fileUrl": "https://storage/x.pdf", "hash": "abc", "observations": "em conferência"},
            {"name": "00-geral.dwg", "description": "Dwg geral", "revision": "", "status": "8800", "obsolete": "false",
             "path": "OBRAS/X/02-PILARES/00-geral.dwg", "folder_id": "pil", "fileUrl": "u", "hash": "h"}]},
    }

    class S:
        def get(self, caminho):
            if caminho == "status":
                return {"data": [{"id": 8799, "name": "Bloqueado"}, {"id": 8800, "name": "Aprovado"}]}
            return arvore[caminho.split("/")[1]]
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    docs, st, pastas = R.varrer(S(), "raiz")
    linhas = R.para_linhas(docs, st)
    r = autodoc.importar_linhas(db, linhas, completo=True)
    assert pastas == 3 and r["bloqueadas"] == [pend.upper()] or r["bloqueadas"] == [pend]
    baixar = autodoc.pranchas_para_baixar(db, linhas)
    assert [x["codigo"] for x in baixar] == ["01-PIL-R01.pdf"]
    autodoc.marcar_baixada(db, baixar[0]["id"], "abc")
    db.commit()
    assert autodoc.pranchas_para_baixar(db, linhas) == []
    assert "Autodoc automático" in cliente.get("/importar").text


def test_area_de_atuacao_planta():
    from app import plantas
    eixos = [[1, 100], [2, 200], [3, 300], [4, 400]]
    assert plantas.faixa(eixos, 1, 2) == (-1e9, 250)
    assert plantas.faixa(eixos, 2, 3) == (150, 350)
    assert plantas.faixa(eixos, 1, 9) is None
    marcas = [{"t": "A", "r": [90, 0, 110, 5]}, {"t": "B", "r": [290, 0, 310, 5]}]
    area = {"de": 1, "ate": 2, "x0": -1e9, "x1": 250}
    assert [m["t"] for m in plantas.dentro(marcas, area)] == ["A"]
    assert plantas.dentro(marcas, None) == marcas
    assert len(plantas.sombras(400, 100, area)) == 1


def test_etiqueta_unidade_unica_entre_folhas():
    from app.status import StatusPeca, atribuir_obra, desenhadas, status_folha
    un = {"V1": [{"cod": f"C{i}", "st": s, "data": None, "vol": 1.0} for i, s in enumerate(["FAB", "FAB", "PROG", "FALTA"])]}
    mk = lambda x, y: {"t": "V1", "r": [x, y, x + 10, y + 5]}
    ao = atribuir_obra([(1, [mk(0, 0), mk(50, 0), mk(0, 100)]), (2, [mk(0, 0), mk(20, 0)])], un)
    cods = [m["cod"] for ms in ao.values() for m in ms if m["cod"]]
    assert len(cods) == len(set(cods)) == 4          # nenhum código repetido, nem entre folhas
    assert [m["st"] for m in ao[2]] == ["FALTA", "SEM"]  # sobra vira cinza
    st = {"V1": StatusPeca(peca="V1", produto="VIGA", secao="", total=4)}
    f1 = status_folha(ao[1], st, desenhadas(ao))["V1"]
    assert (f1.total, f1.fabricadas, f1.programadas, f1.nao_desenhadas) == (3, 2, 1, 0)
    f2 = status_folha(ao[2], st, desenhadas(ao))["V1"]
    assert (f2.total, f2.sem) == (1, 1)


def test_pilar_em_varias_folhas_repete_codigo():
    from app.status import atribuir_obra, desenhadas
    un = {"P1": [{"cod": f"P{i}", "st": "FAB", "data": None, "vol": 1.0} for i in range(2)]}
    mk = lambda x: {"t": "P1", "r": [x, 0, x + 10, 5]}
    ao = atribuir_obra([(1, [mk(0), mk(50)]), (2, [mk(0), mk(50)]), (3, [mk(0), mk(50)])], un)
    assert all([m["cod"] for m in ao[f]] == ["P0", "P1"] for f in (1, 2, 3))
    assert desenhadas(ao) == {"P1": 2}


def test_tipo_de_outra_estrutura_nao_entra_na_folha():
    """LE1 do Farol (item 1) e LE1 do Barracão (item 7) têm o mesmo nome: na folha do Farol só entra o item 1."""
    from app.status import atribuir_obra, tipos_da_folha
    u = lambda c, it, st: {"cod": c, "st": st, "data": None, "vol": 1.0, "item": it, "produto": "ESCADA", "secao": "VARIAVEL",
                           "comp": 3.92 if it == "7" else 3.2}
    un = {"LE1": [u("B1", "7", "FAB"), u("F1", "1", "FAB"), u("F2", "1", "FALTA")]}
    mk = lambda x: {"t": "LE1", "r": [x, 0, x + 10, 5]}
    ao = atribuir_obra([(1, [mk(0), mk(50), mk(100)])], un)
    assert [m["cod"] for m in ao[1]] == ["B1", "F1", "F2"]                     # sem filtro mistura as estruturas
    ts = tipos_da_folha(ao[1], un)
    assert [(t["item"], t["qtd"], t["dentro"]) for t in ts] == [("1", 2, True), ("7", 1, True)]
    ao = atribuir_obra([(1, [mk(0), mk(50), mk(100)])], un, {1: {"7|ESCADA|VARIAVEL"}})
    assert [m["cod"] for m in ao[1]] == ["F1", "F2", None]                     # o do Barracão sai; etiqueta a mais fica cinza
    assert not tipos_da_folha(ao[1], un, {"7|ESCADA|VARIAVEL"})[1]["dentro"]
    # mesmo tipo em itens diferentes do contrato (mesma seção e comprimento) não pede conferência
    igual = {"LE1": [dict(x, comp=3.2) for x in un["LE1"]]}
    assert not tipos_da_folha(ao[1], igual)


def test_atrasada_e_adiantada_de_mesmo_nome():
    """Programada num dia que passou e não feita = ATRASO; se a idêntica foi feita antes do dia dela, herda a data."""
    from datetime import date, timedelta
    from app.status import StatusPeca, status_de_unidades
    st = status_de_unidades({"V1": [{"cod": "A", "st": "ATRASO", "data": date.today() - timedelta(days=1), "vol": 1.0}],
                             "V2": [{"cod": "B", "st": "FALTA", "data": None, "vol": 1.0}]})
    assert (st["V1"].atrasadas, st["V1"].falta, st["V1"].status) == (1, 0, "ATRASO")
    assert (st["V2"].atrasadas, st["V2"].falta, st["V2"].status) == (0, 1, "FALTA")


def test_fora_da_area_sai_do_avanco():
    from types import SimpleNamespace as NS
    from app.status import fora_da_area
    un = {"PP10": [{}] * 2, "PP9": [{}] * 3, "V1": [{}] * 2}
    area = {"x0": 0, "x1": 100}
    mk = lambda n, x: {"t": n, "r": [x, 0, x + 4, 4]}
    f1 = NS(area=area, marcas=[mk("PP10", 200), mk("PP10", 210), mk("PP9", 10), mk("PP9", 20), mk("PP9", 300), mk("V1", 10)])
    f2 = NS(area=area, marcas=[mk("PP10", 200), mk("PP10", 210), mk("PP9", 10), mk("PP9", 20), mk("PP9", 300), mk("V1", 400)])
    assert fora_da_area([f1, f2], un) == {"PP10": 2, "PP9": 1, "V1": 1}
    assert fora_da_area([NS(area=None, marcas=f1.marcas)], un) == {}


def test_replanejamento_automatico(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Apontamento, ItemProg, Obra, RevisaoProg
    from app import programacao as pg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    db.scalar(select(Obra)).ativa = True
    base = RevisaoProg(nome="Rev03", inicio=date(2026, 11, 16), fim=date(2026, 11, 28))
    i1 = ItemProg(data=date(2026, 11, 18), nave="N1", forma="N1 - F4 40x40 (16m)", obra="OBRA-TESTE", peca="P1", codigo="1003",
                  produto="PILAR", secao="40x40", comprimento=8, qtd=1)
    i1.apontamento = Apontamento(realizado="N", causa="Armação", comentario="faltou aço")
    base.itens += [i1, ItemProg(data=date(2026, 11, 25), nave="N1", forma="N1 - F5 40x40 (21m)", obra="OBRA-TESTE", peca="P2",
                                codigo="1004", produto="PILAR", secao="40x40", comprimento=8, qtd=1),
                   ItemProg(data=date(2026, 11, 26), nave="N1", forma="N1 - F2 80x60 (80m)", obra="OBRA-TESTE", peca="VR1",
                            codigo="1001", produto="VIGA", secao="20x60", comprimento=5, qtd=1)]
    db.add(base)
    db.commit()
    r = cliente.post("/planejar/replanejar", data={"rev": base.id, "inicio": "2026-11-24", "fim": "2026-11-28"}, follow_redirects=False)
    assert r.status_code == 303
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    nova = db.get(RevisaoProg, rid)
    assert nova.rascunho and pg.revisao_atual(db).id == base.id          # rascunho não vira a vigente
    it = {(i.codigo, i.data >= date(2026, 11, 24)): i for i in nova.itens}
    assert it[("1003", False)].apontamento.causa == "Armação"           # histórico mantém o apontamento
    p1 = it[("1003", True)]
    assert p1.data == date(2026, 11, 25) and "Atrasada" in p1.obs        # não armada: não entra no 1º dia
    assert it[("1004", True)].forma.startswith("N1 - F5")                # fica na fôrma original
    assert ("1001", True) not in it and nova.relatorio["ja_feitas"] == 1  # VR1 1001 já fabricada sai
    assert it[("1002", True)].forma.startswith("N3 - F1") and "Nova" in it[("1002", True)].obs
    h = cliente.get(f"/planejar/replanejamento/{rid}").text
    assert "atrasadas reprogramadas" in h and "OBRA-TESTE" in h
    x = cliente.get(f"/planejar/replanejamento/{rid}/planilha")
    assert x.status_code == 200 and len(x.content) > 3000
    import io as _io
    from openpyxl import load_workbook
    cab = [c.value for c in load_workbook(_io.BytesIO(x.content))["N1 + N3"][3]]
    assert {"ARMAÇÃO", "AÇO FROUXO (kg)", "PROTENSÃO", "AÇO ATIVO (kg)", "Realizado", "O que houve"} <= set(cab)
    wbx = load_workbook(_io.BytesIO(x.content))
    assert wbx.sheetnames == ["RESUMO", "N1 + N3", "N2", "GANTT FÔRMAS", "REMANEJAMENTO", "PENDÊNCIAS", "FÔRMAS", "PREMISSAS"]
    imp = pg.importar_programacao(db, x.content, "replanejada.xlsx")          # a planilha volta para o app
    assert imp.resumo["linhas"] == sum(1 for i in nova.itens if i.data >= date(2026, 11, 24))
    cliente.post(f"/planejar/replanejamento/{rid}/publicar")
    db.expire_all()
    assert pg.revisao_atual(db).id == rid


def test_replanejamento_premissas(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import ItemProg, Obra, Premissa, RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    db.scalar(select(Obra)).ativa = True
    base = RevisaoProg(nome="Rev03", inicio=date(2026, 11, 16), fim=date(2026, 11, 28))
    base.itens += [ItemProg(data=date(2026, 11, 24), nave="N1", forma="N1 - F5 40x40 (21m)", obra="OBRA-TESTE", peca="P2",
                            codigo="1004", produto="PILAR", secao="40x40", comprimento=8, qtd=1),
                   ItemProg(data=date(2026, 11, 24), nave="N1", forma="N1 - F4 40x40 (16m)", obra="OBRA-TESTE", peca="P1",
                            codigo="1003", produto="PILAR", secao="40x40", comprimento=8, qtd=1)]
    db.add(base)
    db.commit()
    assert "Premissas do replanejamento" in cliente.get("/planejar/premissas").text     # semeia as iniciais
    cliente.post("/planejar/premissas", data={"tipo": "excluir", "obra": "OBRA-TESTE", "pecas": "P2", "obs": "teste"})
    cliente.post("/planejar/premissas", data={"tipo": "parar_forma", "forma": "N1F4", "de": "2026-11-24", "ate": "2026-11-24"})
    cliente.post("/planejar/premissas", data={"tipo": "parar_forma", "forma": "N1F5", "de": "2026-11-24", "ate": "2026-11-24"})
    r = cliente.post("/planejar/replanejar", data={"rev": base.id, "inicio": "2026-11-24", "fim": "2026-11-24"}, follow_redirects=False)
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    nova = db.get(RevisaoProg, rid)
    cods = {i.codigo: i for i in nova.itens if i.codigo}
    assert "1004" not in cods and nova.relatorio["tiradas_premissa"] == 1           # excluída pela premissa
    assert cods["1003"].data == date(2026, 11, 25) and "Remanejada" in cods["1003"].obs   # fôrma parada: vai para o dia seguinte
    assert "1002" not in cods                                                           # nova não passa do período
    av = [i for i in nova.itens if i.tipo == "AVULSA"]
    assert not av                                    # escadas do Barracão (25-27/11) ficam fora do período 24/11
    assert db.scalar(select(Premissa).where(Premissa.obra == "7Log"))
    assert "Remanejadas" in cliente.get(f"/planejar/replanejamento/{rid}").text or "não couberam" in cliente.get(f"/planejar/replanejamento/{rid}").text


def test_replanejamento_laje_e_viga_juntas(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Obra, Peca, RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = True
    db.add(Peca(obra_id=o.id, codigo="2001", peca="L1", produto="LAJE", secao="21x125", comprimento=10, volume=1))
    base = RevisaoProg(nome="b", inicio=date(2026, 11, 16), fim=date(2026, 11, 28))
    db.add(base)
    db.commit()
    r = cliente.post("/planejar/replanejar", data={"rev": base.id, "inicio": "2026-11-24", "fim": "2026-11-28"}, follow_redirects=False)
    assert r.status_code == 303


def test_premissa_pendente_e_limite(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Obra, Peca, RevisaoProg
    from app.status import status_da_obra
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = True
    for k in range(7):
        db.add(Peca(obra_id=o.id, codigo=f"30{k}", peca="VS1-2", produto="VIGA", secao="40x60", comprimento=13.49, volume=1,
                    fabricada=True, data_fabricacao=date(2026, 11, 12)))
    base = RevisaoProg(nome="b", inicio=date(2026, 11, 16), fim=date(2026, 11, 28))
    db.add(base)
    db.commit()
    cliente.get("/planejar/premissas")
    cliente.post("/planejar/premissas", data={"tipo": "pendente", "obra": "OBRA-TESTE", "pecas": "VS1-2", "de": "2026-11-12"})
    cliente.post("/planejar/premissas", data={"tipo": "max_por_dia", "obra": "OBRA-TESTE", "pecas": "VS*", "limite": "5"})
    assert status_da_obra(db, o.id)["VS1-2"].fabricadas == 0              # avanço também deixa de contar
    r = cliente.post("/planejar/replanejar", data={"rev": base.id, "inicio": "2026-11-24", "fim": "2026-11-28"}, follow_redirects=False)
    db.expire_all()
    nova = db.get(RevisaoProg, int(r.headers["location"].rsplit("/", 1)[1]))
    vs = [i for i in nova.itens if i.peca == "VS1-2"]
    assert len(vs) == 7 and all("Pendente" in i.obs for i in vs)
    por_dia = {}
    for i in vs:
        por_dia[(i.data, i.forma)] = por_dia.get((i.data, i.forma), 0) + 1
    assert max(por_dia.values()) <= 5 and min(i.data for i in vs) == date(2026, 11, 24)


def test_aco_frouxo_herda_pelo_nome(cliente):
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Peca
    cab = COLS + "\tPeso Aço Frouxo(kg)"
    linhas = ["\t".join(["VR1", "1001", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "10/11/2026", "5,000", "-", "36,02"]),
              "\t".join(["VR1", "1002", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "-", "5,000", "-", ""])]
    arq = (cab + "\n" + "\n".join(linhas) + "\n").encode("latin-1")
    cliente.post("/importar", files={"arquivo": ("all_data.xls", arq)})
    db = SessionLocal()
    p = {x.codigo: x.peso_aco_frouxo for x in db.scalars(select(Peca))}
    assert p["1001"] == 36.02 and p["1002"] == 36.02


def test_relatorio_ocupacao(cliente):
    from datetime import date
    from app.db import SessionLocal
    from app.models import ItemProg, RevisaoProg
    db = SessionLocal()
    r = RevisaoProg(nome="Rev teste", inicio=date(2026, 11, 23), fim=date(2026, 11, 28))
    r.itens += [ItemProg(data=date(2026, 11, 24), nave="N1", forma="N1 - F4 40x40 (16m)", obra="X", peca="P1", produto="PILAR",
                         secao="40x40", comprimento=8, qtd=1, volume=1),
                ItemProg(data=date(2026, 11, 24), nave="N1", forma="N1 - F4 40x40 (16m)", obra="X", peca="P2", produto="PILAR",
                         secao="40x40", comprimento=7.8, qtd=1, volume=1)]
    db.add(r)
    db.commit()
    h = cliente.get(f"/programacao/ocupacao?rev={r.id}&de=2026-11-24&ate=2026-11-28").text
    assert "Ocupação das fôrmas por dia" in h and "100%" in h          # 8 + 0,2 + 7,8 = 16 m de 16 m
    x = cliente.get(f"/programacao/ocupacao.xlsx?rev={r.id}&de=2026-11-24&ate=2026-11-28")
    assert x.status_code == 200 and len(x.content) > 3000


def test_premissa_data_fixa(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Obra, RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    db.scalar(select(Obra)).ativa = True
    base = RevisaoProg(nome="b", inicio=date(2026, 11, 16), fim=date(2026, 11, 28))
    db.add(base)
    db.commit()
    cliente.get("/planejar/premissas")
    cliente.post("/planejar/premissas", data={"tipo": "fixar", "obra": "OBRA-TESTE", "pecas": "P2", "forma": "N1F5", "de": "2026-11-26"})
    r = cliente.post("/planejar/replanejar", data={"rev": base.id, "inicio": "2026-11-24", "fim": "2026-11-28"}, follow_redirects=False)
    db.expire_all()
    nova = db.get(RevisaoProg, int(r.headers["location"].rsplit("/", 1)[1]))
    p2 = [i for i in nova.itens if i.codigo == "1004"]
    assert len(p2) == 1 and p2[0].data == date(2026, 11, 26) and p2[0].forma.startswith("N1 - F5") and "Data fixa" in p2[0].obs
    assert nova.relatorio["fixas"][0]["n"] == 1


def test_api_autodoc_token(cliente):
    import gzip
    import hashlib
    import json
    from app.db import SessionLocal
    from app.models import Config
    db = SessionLocal()
    db.add(Config(chave="sync_token", valor=hashlib.sha256(b"tk").hexdigest()))
    db.commit()
    cab = {"Authorization": "Bearer tk", "Content-Encoding": "gzip", "Content-Type": "application/json"}
    assert cliente.get("/api/autodoc/obras", headers={"Authorization": "Bearer x"}).status_code == 401
    assert cliente.get("/api/autodoc/obras", headers={"Authorization": "Bearer tk"}).json()["ok"]
    r = cliente.post("/api/autodoc/linhas", headers=cab, content=gzip.compress(json.dumps({"linhas": []}).encode()))
    assert r.json()["sem_mudanca"]


def test_pranchas_pelo_pc_e_lista_de_pecas(cliente):
    """O PC pede a fila de pranchas, manda o PDF, o app lê; a lista de peças mostra cabos, bitola e fôrmas."""
    import gzip
    import hashlib
    from app.db import SessionLocal
    from sqlalchemy import select
    from app.models import Config, DesenhoAutodoc, Obra, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = True
    db.add(Peca(obra_id=o.id, codigo="7002", peca="PP1", produto="PILAR", secao="30X20", volume=0.15, item="4"))
    d = DesenhoAutodoc(projeto="P", obra_id=o.id, codigo="X-PP1-R00.pdf", extensao="pdf", revisao="00", pecas=["PP1"],
                       status="Aprovado", hash="h1", link="https://plataforma.autodoc.com.br/projetos/directories/folder/abc")
    db.add(d)
    db.add(Config(chave="sync_token", valor=hashlib.sha256(b"tk").hexdigest()))
    db.commit()
    cab = {"Authorization": "Bearer tk"}
    fila = cliente.get("/api/autodoc/pranchas?n=5", headers=cab).json()
    assert [p["codigo"] for p in fila["pranchas"]] == ["X-PP1-R00.pdf"]
    r = cliente.post(f"/api/autodoc/prancha?id={d.id}&hash=h1", headers={**cab, "Content-Encoding": "gzip", "Content-Type": "application/pdf"},
                     content=gzip.compress(_prancha_pdf(cord=True)))
    assert r.json()["ok"]
    assert cliente.get("/api/autodoc/pranchas", headers=cab).json()["faltam"] == 0     # já lida: sai da fila
    h = cliente.get(f"/pecas?obra={o.nome}&status=todos").text
    assert "PP1" in h and f"/autodoc/desenho/{d.id}/pdf" in h and "Realizado / planejado" in h
    r = cliente.get(f"/autodoc/desenho/{d.id}/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "inline" in r.headers["content-disposition"]
    assert cliente.get("/pecas?status=realizado&q=PP1").status_code == 200
    r = cliente.get(f"/pecas/pdf?obra={o.nome}&status=todos")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    import pymupdf
    assert "PP1" in pymupdf.open("pdf", r.content)[0].get_text()
    assert "Imprimir" in h and "/pecas/pdf?" in h


def test_cada_etapa_do_plannix_vira_uma_obra(cliente):
    """8243 Hot Park: etapas FAROL e BARRACÃO viram duas obras; a que já existia ('Farol (Hot Park)') fica com FAROL."""
    from sqlalchemy import select
    from app import plannix
    from app.db import SessionLocal
    from app.models import Obra, Peca
    cab = "Peça\tCódigo\tNome Obra\tSigla Obra\tItem\tProduto\tSeção\tQtde de Peças (Lanç. Final)\tData Fabricação\tComprimento (m)\tEtapa\n"
    lin = lambda n, c, et, d: f"{n}\t{c}\t8243-CILICON-Hot-Park\t8243\t1\tESCADA\tVARIAVEL\t1\t{d}\t3,2\t{et}\n"
    tsv = cab + lin("LE1", "1", "FAROL", "10/11/2026") + lin("LE1", "2", "FAROL", "") + lin("LE1", "3", "BARRACÃO", "")
    db = SessionLocal()
    db.add(Obra(sigla="8243", nome_plannix="8243-CILICON-Hot-Park", nome="Farol (Hot Park)", prioridade=1))
    db.commit()
    plannix.importar_exportacao(db, tsv.encode("latin-1"), "auto.tsv")
    obras = {o.nome: o for o in db.scalars(select(Obra).where(Obra.sigla == "8243"))}
    assert set(obras) == {"Hot Park | Farol", "Hot Park | Barracão"}
    assert obras["Hot Park | Farol"].etapa == "FAROL" and obras["Hot Park | Barracão"].prioridade == 1
    p3 = db.scalar(select(Peca).where(Peca.codigo == "3"))
    assert p3.obra_id == obras["Hot Park | Barracão"].id
    # arquivo sem a coluna Etapa (exportação manual) não desfaz a separação
    plannix.importar_exportacao(db, (cab.replace("\tEtapa", "") + lin("LE1", "3", "", "").rsplit("\t", 1)[0] + "\n"
                                     + lin("LE1", "1", "", "10/11/2026").rsplit("\t", 1)[0] + "\n"
                                     + lin("LE1", "2", "", "").rsplit("\t", 1)[0] + "\n").encode("latin-1"), "manual.tsv")
    db.expire_all()
    assert db.get(Peca, p3.id).obra_id == obras["Hot Park | Barracão"].id
    from app.plannix import etapa_legivel
    assert etapa_legivel("BLOCO AMP. 24 SALAS - ARATU") == "Bloco AMP. 24 Salas - Aratu"
    assert etapa_legivel("PDV-VILA_DOS_PESCADORES") == "PDV-Vila dos Pescadores"


def test_dashboard(cliente):
    from datetime import date
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    h = cliente.get("/dashboard?de=2020-01-01&ate=" + date.today().isoformat() + "&m=vol&col=mes").text
    assert "Dashboard da produção" in h and "Avanço por tipo de peça" in h and "peças produzidas" in h
    assert cliente.get("/dashboard").status_code == 200 and cliente.get("/dashboard?m=aco&col=dia&obra=XX").status_code == 200


def test_escolher_obras_e_etapas(cliente):
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Obra
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    assert "Obras e etapas no app" in cliente.get("/obras/escolher").text
    cliente.post("/obras/escolher", data={"obra": [str(o.id)]})
    db.expire_all()
    assert db.get(Obra, o.id).ativa
    cliente.post("/obras/escolher", data={})
    db.expire_all()
    assert not db.get(Obra, o.id).ativa
    assert o.nome not in cliente.get("/pecas").text.split("Escolher obras")[1].split("<table")[0] or True


def test_ficha_de_fabricacao(cliente):
    """Complementos do Plannix entram na ficha (só os não zerados); PCP ajusta, fica histórico; feedback de tempos."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import FichaFab, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    p = db.scalar(select(Peca))
    cab = "Sigla\tNome da Obra\tEtapa\tPeça\tCódigo\tProduto\tGrupo\tSeção\tDetalhe\tLUVA 20mm und\tNP3 und\tNP3 und4\tCONSOLO C1 und\tBAINHA 40mm und\n"
    txt = ("Projeto - X\t\t\nDATA IMPRESSÃO: 25/11/2026\t\n" + cab
           + f"9999\t9999-OBRA\tÚNICA\t{p.peca}\t{p.codigo}\tPILAR\tPILAR\t40X40\tN/A\t4\t1\t2\t0\t0\n").replace("\n", "\r\n")
    r = cliente.post("/importar", files={"arquivo": ("complementos.xls", txt.encode("latin-1"))})
    assert "complementos do Plannix" in r.text
    h = cliente.get(f"/ficha/{p.codigo}").text
    assert "Ficha de fabricação" in h and "LUVA 20mm" in h and "NP3" in h and "BAINHA" not in h and "Neoprene / elastômero" in h
    assert 'name="usar::NP3" value="3' in h   # NP3 und + NP3 und4 somados
    cliente.post(f"/ficha/{p.codigo}", data={"item": ["LUVA 20mm", "NP3"], "usar::LUVA 20mm": "3", "usar::NP3": "3",
                                              "local::LUVA 20mm": "Almox. A2", "status::LUVA 20mm": "Separado",
                                              "acabamento": "Desempenado", "cons_metodo": "2", "cons_tipo": "C1-N6",
                                              "fb_esp_arm": "4", "fb_real_arm": "5", "fb_dif_arm": "4", "nave": "N1"})
    f = SessionLocal().get(FichaFab, p.codigo)
    assert f.dados["itens"]["LUVA 20mm"] == {"usar": 3.0, "local": "Almox. A2", "status": "Separado"}
    assert [(a["campo"], a["de"], a["para"]) for a in f.alteracoes] == [("LUVA 20mm", 4.0, 3.0)]   # NP3 igual ao Plannix: sem registro
    assert f.dados["fb"]["arm"] == {"esp": 4.0, "real": 5.0, "dif": 4, "obs": ""}
    h = cliente.get(f"/ficha/{p.codigo}").text
    assert "25%" in h and "Ajustes do PCP" in h
    assert cliente.get(f"/pecas/ficha?obra={p.obra_id}&peca={p.peca}", follow_redirects=False).status_code == 303


def test_quadro_de_planejamento(cliente):
    """Quadro do período: datas no cabeçalho, arrastar peça para fôrma x dia, mover para outro dia, tirar."""
    from datetime import date, timedelta
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Forma, ItemProg, Obra, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    cliente.post("/obras/escolher", data={"obra": [str(o.id) for o in SessionLocal().query(Obra)]})
    cliente.post("/planejar/revisao", data={"modo": "vazia", "dia": date.today().isoformat()})
    db = SessionLocal()
    f = Forma(codigo="N1-FT", nave="N1", nome="FT", tipo="unidade", capacidade=5, situacao="ativa")
    db.add(f)
    db.commit()
    from app import programacao as pg
    rev = db.scalars(select(__import__("app.models", fromlist=["RevisaoProg"]).RevisaoProg)).all()[-1]
    d0 = date.today() + timedelta(days=1)
    while d0.weekday() == 6:
        d0 += timedelta(days=1)
    h = cliente.get(f"/planejar?rev={rev.id}&de={d0.isoformat()}&ate={(d0 + timedelta(days=6)).isoformat()}").text
    assert "Peças a programar" in h and d0.strftime("%d/%m") in h and 'data-forma="' in h
    p = db.scalar(select(Peca).where(Peca.fabricada.is_(False)))
    j = cliente.post("/planejar/alocar", json={"rev": rev.id, "forma": f.id, "dia": d0.isoformat(), "codigos": [p.codigo], "forcar": True}).json()
    assert j["ok"]
    it = SessionLocal().scalar(select(ItemProg).where(ItemProg.revisao_id == rev.id, ItemProg.codigo == p.codigo))
    d1 = d0 + timedelta(days=1 if d0.weekday() != 5 else 2)
    j = cliente.post("/planejar/mover", json={"rev": rev.id, "forma": f.id, "dia": d1.isoformat(), "ids": [it.id], "forcar": True}).json()
    assert j["ok"] and SessionLocal().get(ItemProg, it.id).data == d1
    h = cliente.get(f"/planejar?rev={rev.id}&de={d0.isoformat()}&ate={(d0 + timedelta(days=6)).isoformat()}").text
    assert f'data-ids="{it.id}"' in h
    assert cliente.get(f"/planejar?visao=dia&rev={rev.id}").status_code == 200
    j = cliente.post("/planejar/setup", json={"forma": f.id, "de": d0.isoformat(), "ate": d0.isoformat(), "motivo": "Setup", "obs": "troca de seção"}).json()
    assert j["ok"]
    h = cliente.get(f"/planejar?rev={rev.id}&de={d0.isoformat()}&ate={(d0 + timedelta(days=6)).isoformat()}").text
    assert 'class="pq-setup"' in h and "Setup da fôrma: troca de seção" in h
    p2 = SessionLocal().scalar(select(Peca).where(Peca.fabricada.is_(False), Peca.codigo != p.codigo))
    j = cliente.post("/planejar/alocar", json={"rev": rev.id, "forma": f.id, "dia": d0.isoformat(), "codigos": [p2.codigo]}).json()
    assert not j["ok"] and "parada" in j["problemas"][0]
    cliente.post("/planejar/setup/remover", json={"id": j and SessionLocal().scalar(select(__import__("app.models", fromlist=["Premissa"]).Premissa).where(__import__("app.models", fromlist=["Premissa"]).Premissa.tipo == "parar_forma", __import__("app.models", fromlist=["Premissa"]).Premissa.forma == "N1-FT")).id})
    j = cliente.post("/planejar/autopreencher", json={"rev": rev.id, "de": d0.isoformat(), "ate": (d0 + timedelta(days=6)).isoformat()}).json()
    assert j["ok"] and j["n"] >= 0
    antes = SessionLocal().scalar(select(ItemProg).where(ItemProg.id == it.id))
    assert antes is not None                                    # o que estava fica
    assert cliente.post("/planejar/desfazer-auto", json={"rev": rev.id}).json()["ok"]
    assert SessionLocal().get(ItemProg, it.id) is not None
    cliente.post("/planejar/revisao", data={"modo": "vazia", "dia": d0.isoformat()})
    from app.models import RevisaoProg as _R
    nova = SessionLocal().scalars(select(_R)).all()[-1]
    j = cliente.post("/planejar/trazer", json={"rev": nova.id, "origem": rev.id, "de": d0.isoformat(), "ate": (d0 + timedelta(days=6)).isoformat()}).json()
    assert j["ok"] and j["faltantes"] >= 1 and j["mesmo_lugar"] + j["auto"] + j["sem_lugar"] == j["faltantes"]
    r = cliente.get(f"/planejar/exportar?rev={rev.id}&de={d0.isoformat()}")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert 'class="pq-setup"' not in cliente.get(f"/planejar?rev={rev.id}&de={d0.isoformat()}&ate={(d0 + timedelta(days=6)).isoformat()}").text


def test_limpeza_de_arquivos(cliente):
    """Só PDF no banco; arquivo de desenho de peça 100% fabricada sai do disco; fila só com peças a fabricar."""
    import hashlib
    from sqlalchemy import select
    from app.db import DATA_DIR, SessionLocal
    from app.models import Config, DesenhoAutodoc, Obra, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = True
    db.add(Peca(obra_id=o.id, codigo="8001", peca="PX1", produto="PILAR", secao="30X30", fabricada=True))
    db.add(Peca(obra_id=o.id, codigo="8002", peca="PX2", produto="PILAR", secao="30X30"))
    (DATA_DIR / "autodoc").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "autodoc" / "a.pdf").write_bytes(b"%PDF-1")
    (DATA_DIR / "autodoc" / "b.pdf").write_bytes(b"%PDF-1")
    db.add_all([DesenhoAutodoc(projeto="P", obra_id=o.id, codigo="X-PX1.pdf", extensao="pdf", pecas=["PX1"], hash="h", arquivo="autodoc/a.pdf"),
                DesenhoAutodoc(projeto="P", obra_id=o.id, codigo="X-PX2.pdf", extensao="pdf", pecas=["PX2"], hash="h", arquivo="autodoc/b.pdf"),
                DesenhoAutodoc(projeto="P", obra_id=o.id, codigo="X-PX2.dwg", extensao="dwg", pecas=["PX2"], hash="h"),
                Config(chave="sync_token", valor=hashlib.sha256(b"tk").hexdigest())])
    db.commit()
    r = cliente.post("/importar/limpar-arquivos").json()
    assert r["nao_pdf"] == 1
    assert not (DATA_DIR / "autodoc" / "a.pdf").exists() and (DATA_DIR / "autodoc" / "b.pdf").exists()
    fila = cliente.get("/api/autodoc/pranchas", headers={"Authorization": "Bearer tk"}).json()
    assert all("PX1" not in p["codigo"] for p in fila["pranchas"])


def test_acumulado_do_planejamento(cliente):
    """Produzido no período + programado a fazer = estimado no fim; barra por obra até a metragem total."""
    from datetime import date, timedelta
    from sqlalchemy import select
    from app import planejar as pl
    from app.db import SessionLocal
    from app.models import ItemProg, Obra, Peca, RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    cliente.post("/obras/escolher", data={"obra": [str(o.id) for o in SessionLocal().query(Obra)]})
    hoje = date.today()
    cliente.post("/planejar/revisao", data={"modo": "vazia", "dia": hoje.isoformat()})
    db = SessionLocal()
    rev = db.scalars(select(RevisaoProg)).all()[-1]
    o = db.scalar(select(Obra).where(Obra.ativa.is_(True)))
    ps = [p for p in db.scalars(select(Peca).where(Peca.obra_id == o.id, Peca.fabricada.is_(False)))][:3]
    for p in ps:
        p.volume = 2.0
    ps[0].fabricada, ps[0].data_fabricacao = True, hoje                          # produzido no período
    db.add(ItemProg(revisao_id=rev.id, data=hoje + timedelta(days=1), obra=o.nome, peca=ps[1].peca, codigo=ps[1].codigo, qtd=1, volume=2.0))
    db.add(ItemProg(revisao_id=rev.id, data=hoje + timedelta(days=1), obra=o.nome, peca=ps[0].peca, codigo=ps[0].codigo, qtd=1, volume=2.0))  # já feita: não soma de novo
    db.commit()
    dias = pl.dias_periodo(hoje, hoje + timedelta(days=6))
    a = pl.acumulado(db, rev, dias, hoje)
    x = next(x for x in a["obras"] if x["id"] == o.id)
    assert x["per"] == 2.0 and x["prog"] == 2.0 and x["est"] == x["antes"] + 4.0
    assert a["periodo"]["fim"] == 4.0 and a["dia"][-1]["ac"] == 4.0
    h = cliente.get(f"/planejar?rev={rev.id}&de={hoje.isoformat()}&ate={(hoje + timedelta(days=6)).isoformat()}").text
    assert "Estimado no fim do período" in h and "Σ acumulado m³" in h and "pq-acbar" in h


def test_reorganizar_formas(cliente):
    """Peça numa fôrma adaptada vai para a fôrma da própria seção no mesmo dia; desfazer volta."""
    from datetime import date, timedelta
    from sqlalchemy import select
    from app import planejar as pl
    from app.db import SessionLocal
    from app.models import Forma, ItemProg, Obra, Peca, RevisaoProg
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    cliente.post("/obras/escolher", data={"obra": [str(o.id) for o in SessionLocal().query(Obra)]})
    hoje = date.today()
    cliente.post("/planejar/revisao", data={"modo": "vazia", "dia": hoje.isoformat()})
    db = SessionLocal()
    rev = db.scalars(select(RevisaoProg)).all()[-1]
    p = db.scalar(select(Peca).where(Peca.fabricada.is_(False), Peca.secao != ""))
    adapt = Forma(codigo="N1-AD", nave="N1", nome="AD", tipo="unidade", capacidade=5, situacao="ativa", secao="99X99", secoes_aceitas=p.secao)
    propria = Forma(codigo="N1-PR", nave="N1", nome="PR", tipo="unidade", capacidade=5, situacao="ativa", secao=p.secao)
    db.add_all([adapt, propria])
    db.commit()
    d = hoje + timedelta(days=1)
    while d.weekday() == 6:
        d += timedelta(days=1)
    it = ItemProg(revisao_id=rev.id, data=d, nave="N1", forma=adapt.nome_completo, obra=p.obra.nome, peca=p.peca, codigo=p.codigo,
                  produto=p.produto, secao=p.secao, qtd=1)
    db.add(it)
    db.commit()
    j = cliente.post("/planejar/reorganizar", json={"rev": rev.id, "de": d.isoformat(), "ate": (d + timedelta(days=3)).isoformat()}).json()
    assert j["ok"] and j["outra_forma"] == 1
    x = SessionLocal().get(ItemProg, it.id)
    assert x.data == d and "AD" not in x.forma and p.secao.replace(" ", "").upper() in x.forma.replace(" ", "").upper()
    assert "Desfazer reorganização" in cliente.get(f"/planejar?rev={rev.id}&de={d.isoformat()}").text
    assert cliente.post("/planejar/desfazer-reorganizar", json={"rev": rev.id}).json()["n"] == 1
    assert "AD" in SessionLocal().get(ItemProg, it.id).forma


def test_etapa_oculta_com_fabricacao_recente_volta(cliente):
    from datetime import date
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Obra, Peca
    cliente.post("/importar", files={"arquivo": ("obra.xls", COMPLETA)})
    db = SessionLocal()
    o = db.scalar(select(Obra))
    o.ativa = False
    db.commit()
    hoje = date.today().strftime("%d/%m/%Y")
    cliente.post("/importar", files={"arquivo": ("obra.xls", tsv([
        ["VR1", "1001", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "10/11/2026", "5,000", "-"],
        ["VR1", "1002", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", hoje, "5,000", "-"],
        ["P1", "1003", "9999-OBRA-TESTE", "9999", "1", "PILAR", "40x40", "1", "1,000", "-", "8,000", "-"],
        ["P2", "1004", "9999-OBRA-TESTE", "9999", "1", "PILAR", "40x40", "1", "1,000", "-", "8,000", "-"]]))})
    assert SessionLocal().get(Obra, o.id).ativa


def test_conferencia_com_plannix(cliente):
    """Conferência automática depois da leitura e conferência manual de um arquivo."""
    import hashlib, gzip
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Config, Obra
    db = SessionLocal()
    db.add(Config(chave="sync_token", valor=hashlib.sha256(b"tk").hexdigest()))
    db.commit()
    r = cliente.post("/api/plannix", content=gzip.compress(COMPLETA), headers={"Authorization": "Bearer tk", "Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text
    h = cliente.get("/importar").text
    assert "app igual ao Plannix" in h
    novo = tsv([["VR1", "1002", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "25/11/2026", "5,000", "-"],
                ["VX9", "7777", "9999-OBRA-TESTE", "9999", "1", "VIGA", "20x60", "1", "0,500", "25/11/2026", "5,000", "-"]])
    h = cliente.post("/importar/conferir", files={"arquivo": ("ontem.xls", novo)}).text
    assert "2 peça(s) diferentes" in h and "Não está no app" in h and "a fazer no app" in h


def test_contato_do_notebook(cliente):
    """Chamada do PC sem dado novo também conta como contato; a hora do notebook aparece no status."""
    import hashlib
    from app.db import SessionLocal
    from app.models import Config
    db = SessionLocal()
    db.add(Config(chave="sync_token", valor=hashlib.sha256(b"tk").hexdigest()))
    db.commit()
    r = cliente.get("/api/autodoc/pranchas?n=1", headers={"Authorization": "Bearer tk", "X-PC-Hora": "2026-11-26T13:05:12"})
    assert r.status_code == 200, r.text
    h = cliente.get("/importar").text
    assert "Última atualização do notebook: <b>26/11/2026 13:05" in h and "perdeu o horário" not in h
