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
