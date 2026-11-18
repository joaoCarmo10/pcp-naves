import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import hmac
import io
import os
import re
import secrets
import unicodedata
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import acesso, armacao as arm, formas as fm, planejar as pl, plannix, plantas, programacao as pg
from .db import DADOS_EFEMEROS, DATA_DIR, Base, SessionLocal, engine, get_db, migrar
from .models import Apontamento, Auditoria, Divulgacao, Forma, Perfil, Usuario, Importacao, ItemProg, Obra, Peca, Planta, RevisaoProg
from .status import CORES, LEGENDA, chave_natural, por_produto, resumo, status_da_obra

BASE = Path(__file__).parent
SENHA = os.getenv("APP_PASSWORD", "")
app = FastAPI(title="PCP Naves")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
# versão do CSS na URL: a cada deploy com CSS novo o navegador baixa de novo (evita tela quebrada por cache)
VERSAO_CSS = hashlib.md5((BASE / "static" / "app.css").read_bytes()).hexdigest()[:8]
tpl.env.globals.update(lendo_etiquetas=lambda pid: pid in _ocr_em_andamento, CORES={k: v[1] for k, v in CORES.items()}, LEGENDA=LEGENDA, DADOS_EFEMEROS=DADOS_EFEMEROS, VERSAO_CSS=VERSAO_CSS)
tpl.env.filters["pct"] = lambda v: f"{v:.0%}"
tpl.env.filters["num"] = lambda v, d=1: f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
tpl.env.filters["dm"] = lambda d: d.strftime("%d/%m") if d else ""
Base.metadata.create_all(engine)
migrar()
with SessionLocal() as _db:
    fm.carga_inicial(_db)
    fm.completar(_db)
    pl.completar_prioridades(_db)
    acesso.carga_inicial(_db)


def ultima_base(db: Session) -> str:
    imp = db.scalar(select(Importacao).where(Importacao.tipo != "programacao").order_by(Importacao.criada_em.desc()))
    return imp.criada_em.strftime("%d/%m/%Y") if imp else "sem importação"


def _primeira_pagina(niveis: dict) -> str:
    destinos = {"obras": "/", "realizado": "/programacao", "pxr": "/pxr", "planejar": "/planejar", "armacao": "/armacao",
                "formas": "/formas", "importar": "/importar", "usuarios": "/usuarios"}
    return next((destinos[m] for m in destinos if niveis.get(m)), "/conta")


@app.middleware("http")
async def exige_login(request: Request, call_next):
    """Login por usuário e permissão por módulo: GET pede 'ver', gravação pede 'editar'. O servidor recusa mesmo que
    alguém tente gravar por fora da tela."""
    caminho = request.url.path
    request.state.usuario = None
    request.state.niveis = {m: 2 for m in acesso.MODULOS}
    request.state.nivel_pagina = 2
    if not SENHA or caminho.startswith(("/static", "/login", "/saude", "/armador/")):
        return await call_next(request)
    quer_json = "application/json" in request.headers.get("content-type", "") or request.headers.get("accept", "").startswith("application/json")
    with SessionLocal() as db:
        u = db.get(Usuario, request.session.get("uid")) if request.session.get("uid") else None
        if not u or not u.ativo:
            request.session.clear()
            if quer_json:
                return JSONResponse({"ok": False, "erro": "Sessão expirada. Entre de novo."}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        niveis = {m: acesso.nivel(u, m) for m in acesso.MODULOS}
        request.state.usuario = SimpleNamespace(id=u.id, login=u.login, nome=u.nome or u.login, perfil=u.perfil.nome,
                                                admin=u.perfil.sistema, trocar=u.trocar_senha)
        if not u.ultimo_acesso or (datetime.now() - u.ultimo_acesso).total_seconds() > 300:
            u.ultimo_acesso = datetime.now()
            db.commit()
    request.state.niveis = niveis
    if request.state.usuario.trocar and caminho not in ("/conta", "/sair"):
        return RedirectResponse("/conta", status_code=303)
    mod = acesso.modulo_do_caminho(caminho)
    precisa = 1 if request.method in ("GET", "HEAD") else 2
    if mod:
        request.state.nivel_pagina = niveis[mod]
        if niveis[mod] < precisa:
            if caminho == "/" and request.method == "GET":
                return RedirectResponse(_primeira_pagina(niveis), status_code=303)
            msg = "Seu perfil só pode ver este módulo." if niveis[mod] else "Seu perfil não tem acesso a este módulo."
            if quer_json or request.method != "GET":
                return JSONResponse({"ok": False, "erro": msg}, status_code=403)
            return tpl.TemplateResponse(request, "sem_acesso.html", {"msg": msg, "base": ""}, status_code=403)
    resp = await call_next(request)
    if request.method not in ("GET", "HEAD") and resp.status_code < 400 and caminho not in ("/conta",):
        with SessionLocal() as db:
            db.add(Auditoria(login=request.state.usuario.login, acao=f"{request.method} {caminho}"[:300]))
            db.commit()
    return resp


# a sessão precisa envolver o middleware de login (o último adicionado roda primeiro)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32)), max_age=60 * 60 * 24 * 30)


@app.get("/saude")
def saude():
    from . import ocr_planta
    return {"ok": True, "ocr": ocr_planta.disponivel()}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return tpl.TemplateResponse(request, "login.html", {"erro": None})


@app.post("/login")
def login(request: Request, senha: str = Form(...), usuario: str = Form(""), db: Session = Depends(get_db)):
    chave = request.client.host if request.client else "?"
    if acesso.bloqueado(chave):
        return tpl.TemplateResponse(request, "login.html", {"erro": "Muitas tentativas. Espere 15 minutos.", "usuario": usuario}, status_code=429)
    u = acesso.autenticar(db, usuario, senha)
    if not u:
        acesso.falhou(chave)
        return tpl.TemplateResponse(request, "login.html", {"erro": "Usuário ou senha incorretos.", "usuario": usuario}, status_code=401)
    request.session.clear()
    request.session["uid"] = u.id
    u.ultimo_acesso = datetime.now()
    db.commit()
    return RedirectResponse("/conta" if u.trocar_senha else "/", status_code=303)


@app.get("/sair")
def sair(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def painel(request: Request, db: Session = Depends(get_db)):
    todas = db.scalars(select(Obra).order_by(Obra.prioridade.is_(None), Obra.prioridade, Obra.nome)).all()
    obras = [o for o in todas if o.ativa]
    ocultas = [o for o in todas if not o.ativa]
    linhas = []
    for o in obras:
        st = status_da_obra(db, o.id)
        if not st:
            continue
        linhas.append(dict(obra=o, r=resumo(st), plantas=len(o.plantas)))
    return tpl.TemplateResponse(request, "painel.html", {"linhas": linhas, "ocultas": ocultas, "base": ultima_base(db)})


@app.get("/importar", response_class=HTMLResponse)
def importar_form(request: Request, db: Session = Depends(get_db)):
    imps = db.scalars(select(Importacao).order_by(Importacao.criada_em.desc()).limit(15)).all()
    return tpl.TemplateResponse(request, "importar.html", {"imps": imps, "msg": request.query_params.get("msg"), "erro": request.query_params.get("erro")})


@app.post("/importar")
async def importar(arquivo: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Aceita um ou vários arquivos de uma vez (ex.: realizado + a realizar do dia). Ordem de processamento:
    pacotes .zip, exportações completas (com data de fabricação), listas a realizar e por último programações."""
    arquivos = [((a.filename or "arquivo"), await a.read()) for a in arquivo if a.filename]
    if not arquivos:
        return RedirectResponse(f"/importar?erro={quote('Escolha pelo menos um arquivo.')}", status_code=303)

    def ordem(x):
        nome, conteudo = x
        n = nome.lower()
        if n.endswith(".zip"):
            return 0
        if n.endswith(".xlsx"):
            return 3
        try:
            df = plannix.ler_exportacao(conteudo)
            com = int(df["Data Fabricação"].map(plannix._data).notna().sum()) if "Data Fabricação" in df.columns else 0
            return 1 if com else 2   # geral e completa antes da lista a realizar
        except ValueError:
            return 2

    msgs = []
    for nome, conteudo in sorted(arquivos, key=ordem):
        try:
            if nome.lower().endswith(".zip"):
                msgs.append(importar_pacote(db, conteudo))
                continue
            (DATA_DIR / "importacoes" / f"{date.today():%Y%m%d}_{secrets.token_hex(3)}_{Path(nome).name}").write_bytes(conteudo)
            if nome.lower().endswith(".xlsx"):
                imp = pg.importar_programacao(db, conteudo, nome)
                msgs.append(f"{nome}: programação importada como nova revisão ({imp.resumo['linhas']} linhas).")
            else:
                imp = plannix.importar_exportacao(db, conteudo, nome)
                r = imp.resumo
                msgs.append(f"{nome}: { {'a_realizar': 'lista a realizar', 'completa': 'realizado', 'geral': 'all data (realizado + a realizar)'}.get(imp.tipo, imp.tipo) }, {r['linhas']} linhas, "
                            f"{r['novas']} peças novas, {r['saiu_lista']} saíram da lista.")
        except (ValueError, zipfile.BadZipFile) as e:
            db.rollback()
            msgs.append(f"{nome}: ERRO, {e}")
            return RedirectResponse(f"/importar?erro={quote(' | '.join(msgs))}", status_code=303)
    return RedirectResponse(f"/importar?msg={quote(' | '.join(msgs))}", status_code=303)


_ocr_fila = ThreadPoolExecutor(max_workers=1)   # OCR é pesado: um por vez, fora da requisição
_ocr_em_andamento: set[int] = set()


def _ocr_em_segundo_plano(pid: int) -> None:
    try:
        with SessionLocal() as db:
            p = db.get(Planta, pid)
            if not p:
                return
            nomes = {n for (n,) in db.execute(select(Peca.peca).where(Peca.obra_id == p.obra_id))}
            e = plantas.extrair(DATA_DIR / p.arquivo, nomes, ocr=True)
            marcas, _ = plantas.alinhar_nomes(e["marcas"], nomes)
            if any(m["t"] in nomes for m in marcas):
                p.marcas = marcas
                (DATA_DIR / "cache" / f"planta_{p.id}.png").unlink(missing_ok=True)
                db.commit()
        (DATA_DIR / "cache" / f"ocr_{pid}.feito").write_text("1")
    except Exception as e:   # não derruba o app; tenta de novo só se a planta for reenviada
        (DATA_DIR / "cache" / f"ocr_{pid}.feito").write_text(f"erro: {e}"[:300])
    finally:
        _ocr_em_andamento.discard(pid)


def lendo_etiquetas(pid: int) -> bool:
    return pid in _ocr_em_andamento


def _garantir_marcas(db: Session, p: Planta) -> None:
    """Confere se as etiquetas da planta batem com as peças da obra. Se nenhuma bate: lê de novo o texto e os
    comentários do PDF; se ainda assim nada, agenda a leitura por OCR (uma vez por planta, em segundo plano)."""
    nomes = {n for (n,) in db.execute(select(Peca.peca).where(Peca.obra_id == p.obra_id))}
    marcas = p.marcas or []
    if not any(m["t"] in nomes for m in marcas):
        try:
            marcas = plantas.extrair(DATA_DIR / p.arquivo)["marcas"]
        except Exception:
            return
    marcas, trocas = plantas.alinhar_nomes(marcas, nomes)
    if marcas != (p.marcas or []) and any(m["t"] in nomes for m in marcas):
        p.marcas = marcas
        (DATA_DIR / "cache" / f"planta_{p.id}.png").unlink(missing_ok=True)
        db.commit()
    if (not any(m["t"] in nomes for m in p.marcas or []) and nomes and p.id not in _ocr_em_andamento
            and not (DATA_DIR / "cache" / f"ocr_{p.id}.feito").exists()):
        _ocr_em_andamento.add(p.id)
        _ocr_fila.submit(_ocr_em_segundo_plano, p.id)


def _salvar_planta(db: Session, o: Obra, titulo: str, dados: bytes) -> Planta:
    rel = f"plantas/{o.sigla}_{secrets.token_hex(4)}.pdf"
    destino = DATA_DIR / rel
    destino.write_bytes(dados)
    try:
        e = plantas.extrair(destino)
    except Exception:
        destino.unlink(missing_ok=True)
        raise ValueError(f"Não consegui ler o PDF '{titulo}'. Confira se é o PDF vetorial do projeto (não escaneado).")
    ordem = (db.scalar(select(func.max(Planta.ordem)).where(Planta.obra_id == o.id)) or 0) + 1
    nomes = {n for (n,) in db.execute(select(Peca.peca).where(Peca.obra_id == o.id))}
    marcas, _ = plantas.alinhar_nomes(e["marcas"], nomes)
    p = Planta(obra_id=o.id, titulo=titulo.strip()[:120], ordem=ordem, arquivo=rel, largura=e["largura"], altura=e["altura"],
               rotacao=e["rotacao"], marcas=marcas)
    db.add(p)
    db.commit()
    _garantir_marcas(db, p)
    return p


def importar_pacote(db: Session, conteudo: bytes) -> str:
    """Pacote .zip: plannix/*.xls (completas primeiro, depois a realizar), programacao/*.xlsx (em ordem de nome)
    e plantas/<sigla da obra>/<título>.pdf. Planta com o mesmo título na obra é substituída."""
    z = zipfile.ZipFile(io.BytesIO(conteudo))
    nomes = sorted(n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n)
    xls = [n for n in nomes if n.lower().endswith((".xls", ".txt", ".csv"))]
    progs = [n for n in nomes if n.lower().endswith(".xlsx")]
    pdfs = [n for n in nomes if n.lower().endswith(".pdf")]
    if not (xls or progs or pdfs):
        raise ValueError("O pacote não tem arquivos .xls, .xlsx ou .pdf.")
    completas, listas = [], []
    for n in xls:
        df = plannix.ler_exportacao(z.read(n))
        tem_data = "Data Fabricação" in df.columns and df["Data Fabricação"].map(plannix._data).notna().any()
        (completas if tem_data else listas).append(n)
    feitos = []
    for n in completas + listas:
        plannix.importar_exportacao(db, z.read(n), Path(n).name)
        feitos.append(Path(n).name)
    for n in progs:
        pg.importar_programacao(db, z.read(n), Path(n).name)
        feitos.append(Path(n).name)
    np_ = 0
    for n in pdfs:
        partes = Path(n).parts
        sigla = partes[-2] if len(partes) >= 2 else ""
        o = db.scalar(select(Obra).where(Obra.sigla == sigla))
        if not o:
            raise ValueError(f"Planta '{n}': a pasta deve ter a sigla da obra no Plannix (ex.: plantas/8246/...). Obra '{sigla}' não encontrada.")
        titulo = re.sub(r"^\d+\s*[-_.]\s*", "", Path(n).stem)
        for velha in [p for p in o.plantas if p.titulo == titulo]:
            (DATA_DIR / velha.arquivo).unlink(missing_ok=True)
            (DATA_DIR / "cache" / f"planta_{velha.id}.png").unlink(missing_ok=True)
            db.delete(velha)
        db.commit()
        _salvar_planta(db, o, titulo, z.read(n))
        np_ += 1
    return f"Pacote importado: {len(completas)} exportações completas, {len(listas)} listas a realizar, {len(progs)} programações e {np_} plantas."


@app.get("/obras/{obra_id}", response_class=HTMLResponse)
def obra(request: Request, obra_id: int, db: Session = Depends(get_db)):
    o = db.get(Obra, obra_id) or _404()
    st = status_da_obra(db, o.id)
    pl = []
    for p in o.plantas:
        _garantir_marcas(db, p)
        nomes = {m["t"] for m in p.marcas} & set(st)
        pl.append(dict(p=p, r=resumo(st, nomes)))
    return tpl.TemplateResponse(request, "obra.html", {"o": o, "r": resumo(st), "prod": por_produto(st), "plantas": pl, "base": ultima_base(db)})


@app.post("/obras/{obra_id}")
def obra_editar(obra_id: int, nome: str = Form(...), prioridade: str = Form(""), ativa: str = Form(""), db: Session = Depends(get_db)):
    o = db.get(Obra, obra_id) or _404()
    o.nome = nome.strip()[:80] or o.nome
    o.ativa = bool(ativa)
    o.prioridade = int(prioridade) if prioridade.strip().isdigit() else None
    db.commit()
    return RedirectResponse(f"/obras/{obra_id}", status_code=303)


@app.post("/obras/{obra_id}/plantas")
async def planta_upload(obra_id: int, titulo: str = Form(...), arquivo: UploadFile = File(...), db: Session = Depends(get_db)):
    o = db.get(Obra, obra_id) or _404()
    if not (arquivo.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Envie a planta em PDF.")
    try:
        _salvar_planta(db, o, titulo, await arquivo.read())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/obras/{obra_id}", status_code=303)


@app.post("/plantas/{pid}/excluir")
def planta_excluir(pid: int, db: Session = Depends(get_db)):
    p = db.get(Planta, pid) or _404()
    (DATA_DIR / "cache" / f"ocr_{pid}.feito").unlink(missing_ok=True)
    oid = p.obra_id
    (DATA_DIR / p.arquivo).unlink(missing_ok=True)
    (DATA_DIR / "cache" / f"planta_{pid}.png").unlink(missing_ok=True)
    db.delete(p)
    db.commit()
    return RedirectResponse(f"/obras/{oid}", status_code=303)


@app.get("/plantas/{pid}", response_class=HTMLResponse)
def planta_ver(request: Request, pid: int, db: Session = Depends(get_db)):
    p = db.get(Planta, pid) or _404()
    _garantir_marcas(db, p)
    st = status_da_obra(db, p.obra_id)
    caminho = DATA_DIR / p.arquivo
    png, zoom = plantas.imagem_base(caminho, p.id)
    marcas = plantas.marcas_tela(caminho, p.marcas, st, zoom)
    nomes = sorted({m["p"] for m in marcas}, key=lambda k: (st[k].produto, chave_natural(k)))
    return tpl.TemplateResponse(request, "planta.html", {
        "p": p, "o": p.obra, "marcas": marcas, "w": round(p.largura * zoom), "h": round(p.altura * zoom), "linhas": [st[k] for k in nomes],
        "r": resumo(st, set(nomes)), "base": ultima_base(db)})


@app.get("/plantas/{pid}/imagem")
def planta_imagem(pid: int, db: Session = Depends(get_db)):
    p = db.get(Planta, pid) or _404()
    png, _ = plantas.imagem_base(DATA_DIR / p.arquivo, p.id)
    return FileResponse(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/plantas/{pid}/a2.pdf")
def planta_a2(pid: int, db: Session = Depends(get_db)):
    p = db.get(Planta, pid) or _404()
    _garantir_marcas(db, p)
    st = status_da_obra(db, p.obra_id)
    pdf = plantas.pdf_a2(DATA_DIR / p.arquivo, p.marcas, st, f"{p.obra.nome.upper()} - {p.titulo.upper()}", ultima_base(db))
    nome = f"Avanco_{p.obra.nome}_{p.titulo}_{date.today():%d-%m}.pdf".replace(" ", "_")
    ascii_ = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"{ascii_}\"; filename*=UTF-8''{quote(nome)}"})


def _404():
    raise HTTPException(404, "Não encontrado.")


# ======================================================== programação e programado x realizado
tpl.env.globals.update(CAUSAS=pg.CAUSAS, COR_STATUS=pg.COR_STATUS, DIAS=pg.DIAS, STATUS=pg.STATUS)


def _rev(db: Session, rev: int | None) -> RevisaoProg | None:
    return db.get(RevisaoProg, rev) if rev else pg.revisao_atual(db)


def _revisoes(db: Session):
    return db.scalars(select(RevisaoProg).order_by(RevisaoProg.criada_em.desc(), RevisaoProg.id.desc())).all()


def _data(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


@app.get("/programacao", response_class=HTMLResponse)
def programacao_dia(request: Request, rev: int | None = None, dia: str | None = None, obra: str = "", nave: str = "",
                    produto: str = "", peca: str = "", situacao: str = "", db: Session = Depends(get_db)):
    r = _rev(db, rev)
    if not r:
        return tpl.TemplateResponse(request, "programacao.html", {"r": None, "revs": [], "base": ultima_base(db)})
    dias = sorted(set(db.scalars(select(ItemProg.data).where(ItemProg.revisao_id == r.id))))
    d = _data(dia) or date.today()
    if d not in dias:
        futuros = [x for x in dias if x >= d]
        d = futuros[0] if futuros else dias[-1]
    f = {"obra": obra.strip(), "nave": nave.strip(), "produto": produto.strip(), "peca": peca.strip()}
    busca = f["peca"].upper()

    def passa(l) -> bool:
        i = l.item
        return ((not f["obra"] or i.obra == f["obra"]) and (not f["nave"] or i.nave == f["nave"])
                and (not f["produto"] or i.produto == f["produto"])
                and (not busca or busca in (i.peca or "").upper() or busca in (i.codigo or "")))

    todas = pg.linhas(db, r, d, d)
    opcoes = {"obra": sorted({l.item.obra for l in todas if l.item.obra}), "nave": sorted({l.item.nave for l in todas if l.item.nave}),
              "produto": sorted({l.item.produto for l in todas if l.item.produto})}
    ls = [l for l in todas if passa(l)]
    tot = pg.totais(ls)   # indicadores não dependem do filtro de situação
    feitos = ("NO PRAZO", "COM ATRASO", "DATA A CONFIRMAR")
    sit = {"sim": lambda s: s in feitos, "nao": lambda s: s == "NÃO REALIZADA", "pendente": lambda s: s == "PENDENTE",
           "atraso": lambda s: s == "COM ATRASO", "confirmar": lambda s: s == "DATA A CONFIRMAR"}.get(situacao)
    f["situacao"] = situacao if sit else ""
    if sit:
        ls = [l for l in ls if sit(l.status)]
    acum = pg.totais([l for l in pg.linhas(db, r, r.inicio, d) if passa(l)])
    seg = d - timedelta(days=d.weekday())
    semana = [seg + timedelta(days=k) for k in range(6)]
    i = dias.index(d)
    return tpl.TemplateResponse(request, "programacao.html", {
        "r": r, "revs": _revisoes(db), "dias": dias, "d": d, "ant": dias[i - 1] if i else None, "prox": dias[i + 1] if i + 1 < len(dias) else None,
        "ls": ls, "tot": tot, "acum": acum, "semana": semana, "f": f, "opcoes": opcoes, "hoje": date.today(), "base": ultima_base(db)})


@app.get("/programacao/quadro", response_class=HTMLResponse)
def programacao_quadro(request: Request, rev: int | None = None, db: Session = Depends(get_db)):
    r = _rev(db, rev)
    if not r:
        return RedirectResponse("/programacao", status_code=303)
    ls = pg.linhas(db, r)
    dias = sorted({l.item.data for l in ls})
    formas = sorted({(l.item.nave or "-", l.item.forma or "-") for l in ls})
    cel = {}
    for l in ls:
        c = cel.setdefault((l.item.forma or "-", l.item.data), {"n": 0, "ok": 0, "pend": 0})
        c["n"] += l.qtd
        c["ok"] += l.qtd if l.status in ("NO PRAZO", "COM ATRASO", "DATA A CONFIRMAR") else 0
        c["pend"] += l.qtd if l.status == "PENDENTE" else 0
    return tpl.TemplateResponse(request, "quadro.html", {"r": r, "revs": _revisoes(db), "dias": dias, "formas": formas, "cel": cel,
                                                         "hoje": date.today(), "base": ultima_base(db)})


@app.get("/pxr", response_class=HTMLResponse)
def pxr(request: Request, rev: int | None = None, de: str | None = None, ate: str | None = None, db: Session = Depends(get_db)):
    if rev is None:
        # abre na revisão mais recente que já tem dia passado para medir
        rev = db.scalar(select(RevisaoProg.id).where(RevisaoProg.inicio < date.today())
                        .order_by(RevisaoProg.criada_em.desc(), RevisaoProg.id.desc()))
    r = _rev(db, rev)
    if not r:
        return tpl.TemplateResponse(request, "pxr.html", {"r": None, "revs": [], "base": ultima_base(db)})
    hoje = date.today()
    ini = _data(de) or r.inicio
    fim = _data(ate) or min(r.fim, hoje - timedelta(days=1))
    if fim < ini:
        fim = r.fim
    ls = pg.linhas(db, r, ini, fim)
    causas = Counter()
    for l in ls:
        if l.ap and l.ap.causa and l.status in ("NÃO REALIZADA", "COM ATRASO"):
            causas[l.ap.causa] += l.qtd
    nao = [l for l in ls if l.status in ("NÃO REALIZADA", "COM ATRASO")]
    nao.sort(key=lambda l: (l.status != "NÃO REALIZADA", l.item.obra, l.item.forma, l.item.data))
    return tpl.TemplateResponse(request, "pxr.html", {
        "r": r, "revs": _revisoes(db), "ini": ini, "fim": fim, "tot": pg.totais(ls),
        "por_dia": sorted(pg.agrega(ls, lambda l: l.item.data), key=lambda x: x["chave"]),
        "por_semana": sorted(pg.agrega(ls, lambda l: l.item.data - timedelta(days=l.item.data.weekday())), key=lambda x: x["chave"]),
        "por_nave": sorted(pg.agrega(ls, lambda l: l.item.nave or "-"), key=lambda x: x["chave"]),
        "por_obra": sorted(pg.agrega(ls, lambda l: l.item.obra or "-"), key=lambda x: -x["prog"]),
        "por_forma": sorted([a for a in pg.agrega(ls, lambda l: l.item.forma or "-") if a["prog"] >= 3], key=lambda x: (x["ppc"] or 0, -x["prog"])),
        "causas": causas.most_common(), "nao": nao, "base": ultima_base(db)})


@app.post("/apontamentos/{item_id}")
def apontar(item_id: int, dados: dict = Body(...), db: Session = Depends(get_db)):
    it = db.get(ItemProg, item_id) or _404()
    ap = it.apontamento or Apontamento(item_id=it.id)
    if "realizado" in dados:
        v = (dados["realizado"] or "").upper()
        ap.realizado = v if v in ("S", "N") else None
    if "data_execucao" in dados:
        try:
            ap.data_execucao = date.fromisoformat(dados["data_execucao"]) if dados["data_execucao"] else None
        except (TypeError, ValueError):
            raise HTTPException(400, "Data de execução inválida.")
    for k, lim in (("comentario", 500), ("ocorrido", 300), ("causa", 60), ("acao", 300), ("responsavel", 80)):
        if k in dados:
            setattr(ap, k, str(dados[k] or "")[:lim])
    ap.atualizado_em = datetime.now()
    db.add(ap)
    db.commit()
    db.refresh(it)
    peca = db.scalar(select(Peca).where(Peca.codigo == it.codigo)) if it.codigo else None
    acomp = bool(db.scalar(select(Peca.id).join(Obra).where(Obra.nome == it.obra).limit(1)))
    st, _ = pg.classificar(it, peca, ap, date.today(), acomp)
    return {"ok": True, "status": st, "cor": pg.COR_STATUS[st], "realizado": ap.realizado or ""}


@app.post("/programacao/revisoes/{rid}/excluir")
def revisao_excluir(rid: int, db: Session = Depends(get_db)):
    r = db.get(RevisaoProg, rid) or _404()
    db.delete(r)
    db.commit()
    return RedirectResponse("/programacao", status_code=303)


# ---------------------------------------------------------------- fôrmas e layout das naves
def _num(v: str) -> float | None:
    v = (v or "").strip().replace(",", ".")
    return float(v) if v else None


@app.get("/formas", response_class=HTMLResponse)
def formas_tela(request: Request, dia: str | None = None, sel: int | None = None, db: Session = Depends(get_db)):
    d = _data(dia) or date.today()
    vig = fm.vigentes(db, d)
    naves = []
    for n, nome in fm.NAVES.items():
        fs = [f for f in vig if f.nave == n]
        w, h = fm.dimensoes_nave(fs)
        naves.append({"cod": n, "nome": nome, "formas": fs, "w": w, "h": h, "cap": fm.capacidade_dia(fs, d)})
    todas = list(db.scalars(select(Forma).order_by(Forma.nave, Forma.codigo, Forma.inicio)))
    return tpl.TemplateResponse(request, "formas.html", {
        "d": d, "naves": naves, "todas": todas, "sel": db.get(Forma, sel) if sel else None, "fm": fm,
        "tamanho": fm.tamanho, "altura": fm.altura, "base": ultima_base(db)})


@app.post("/formas/{fid}/posicao")
def forma_posicao(fid: int, dados: dict = Body(...), db: Session = Depends(get_db)):
    f = db.get(Forma, fid) or _404()
    # a posição vale para todas as versões da mesma fôrma (é o mesmo lugar no chão)
    for v in db.scalars(select(Forma).where(Forma.codigo == f.codigo)):
        v.x, v.y = max(0.0, round(float(dados["x"]) * 2) / 2), max(0.0, round(float(dados["y"]) * 2) / 2)
    db.commit()
    return {"ok": True}


def _preencher(f: Forma, campos: dict) -> None:
    f.nave = campos["nave"] if campos["nave"] in fm.NAVES else f.nave
    f.nome = campos["nome"].strip()[:60] or f.nome
    f.tipo = campos["tipo"] if campos["tipo"] in fm.TIPOS else "linear"
    f.secao = campos["secao"].strip()[:40]
    f.secoes_aceitas = campos["secoes_aceitas"].strip()[:120]
    f.produtos = campos["produtos"].strip().upper()[:120]
    f.comprimento = _num(campos["comprimento"])
    f.capacidade = _num(campos["capacidade"]) or 1
    f.ritmo_dias = max(1, int(_num(campos["ritmo_dias"]) or 1))
    f.inicio, f.fim = _data(campos["inicio"]), _data(campos["fim"])
    f.situacao = campos["situacao"] if campos["situacao"] in fm.SITUACOES else "ativa"
    f.obs = campos["obs"].strip()[:300]


async def _campos(request: Request) -> dict:
    fd = await request.form()
    ks = ["codigo", "nave", "nome", "tipo", "secao", "secoes_aceitas", "produtos", "comprimento", "capacidade", "ritmo_dias", "inicio", "fim",
          "situacao", "obs", "dia", "versao_em"]
    return {k: str(fd.get(k, "") or "") for k in ks}


@app.post("/formas/nova")
async def forma_nova(request: Request, db: Session = Depends(get_db)):
    c = await _campos(request)
    codigo = re.sub(r"[^A-Z0-9-]", "", (c["codigo"] or "").upper())[:30] or f"{c['nave']}X{secrets.token_hex(2).upper()}"
    if db.scalar(select(Forma.id).where(Forma.codigo == codigo)):
        raise HTTPException(400, f"Já existe fôrma com o código {codigo}. Use 'nova versão' para mudar uma fôrma existente.")
    f = Forma(codigo=codigo, nave="N1", nome=codigo, x=2, y=2)
    _preencher(f, c)
    vizinhas = [v for v in db.scalars(select(Forma).where(Forma.nave == f.nave))]
    f.y = max([v.y for v in vizinhas] + [-1]) + 3   # entra numa linha nova da nave; depois é só arrastar
    db.add(f)
    db.commit()
    return RedirectResponse(f"/formas?dia={c['dia']}&sel={f.id}", status_code=303)


@app.post("/formas/{fid}")
async def forma_editar(request: Request, fid: int, db: Session = Depends(get_db)):
    f = db.get(Forma, fid) or _404()
    c = await _campos(request)
    em = _data(c["versao_em"])
    if em:
        # nova versão a partir de uma data: a atual vale até a véspera, a nova herda posição e código
        if f.inicio and em <= f.inicio:
            raise HTTPException(400, "A nova versão tem que começar depois do início da versão atual.")
        nova = Forma(codigo=f.codigo, x=f.x, y=f.y)
        _preencher(nova, c)
        nova.inicio, nova.fim = em, f.fim
        f.fim = em - timedelta(days=1)
        db.add(nova)
        db.commit()
        return RedirectResponse(f"/formas?dia={em}&sel={nova.id}", status_code=303)
    _preencher(f, c)
    db.commit()
    return RedirectResponse(f"/formas?dia={c['dia']}&sel={f.id}", status_code=303)


@app.post("/formas/{fid}/excluir")
def forma_excluir(fid: int, dia: str = Form(""), db: Session = Depends(get_db)):
    f = db.get(Forma, fid) or _404()
    db.delete(f)
    db.commit()
    return RedirectResponse(f"/formas?dia={dia}", status_code=303)


@app.get("/formas/{fid}/detalhe", response_class=HTMLResponse)
def forma_detalhe(request: Request, fid: int, dia: str | None = None, db: Session = Depends(get_db)):
    """Conteúdo do pop-up: dados da fôrma, o que está programado nela e peças a fazer que cabem nela."""
    f = db.get(Forma, fid) or _404()
    d = _data(dia) or date.today()
    r = pg.revisao_atual(db)
    prog = [l for l in pg.linhas(db, r, d) if l.item.forma and fm.item_e_da_forma(l.item.forma, f)] if r else []
    por_dia, ocup = {}, {}
    for l in prog:
        i = l.item
        ocup[i.data] = ocup.get(i.data, 0) + ((i.comprimento or 0) * l.qtd if f.tipo != "unidade" else l.qtd)
        g = por_dia.setdefault(i.data, {})
        k = (i.obra, i.peca, i.secao, i.comprimento, l.status)   # peças iguais numa linha só
        if k not in g:
            g[k] = {"obra": i.obra, "peca": i.peca, "secao": i.secao, "comp": i.comprimento, "status": l.status, "qtd": 0}
        g[k]["qtd"] += l.qtd
    por_dia = {k: list(v.values()) for k, v in por_dia.items()}
    # peças a fazer que cabem: seção da fôrma (ou adaptação) e comprimento até o útil
    programados = {l.item.codigo for l in prog if l.item.codigo}
    q = select(Peca).join(Obra).where(Peca.fabricada.is_(False), Obra.ativa.is_(True))
    cabem = [p for p in db.scalars(q) if fm.aceita(f, p.produto, p.secao, p.comprimento)]
    cabem.sort(key=lambda p: (p.obra.prioridade or 99, p.obra.nome, p.produto, chave_natural(p.peca)))
    resumo_cabem = Counter(p.obra.nome for p in cabem)
    return tpl.TemplateResponse(request, "_forma_detalhe.html", {
        "f": f, "d": d, "r": r, "por_dia": dict(sorted(por_dia.items())), "ocup": ocup, "cabem": cabem[:80], "n_cabem": len(cabem),
        "resumo_cabem": resumo_cabem.most_common(), "programados": programados, "fm": fm})


# ---------------------------------------------------------------- quadro de planejamento (arrastar peças para as fôrmas)
@app.get("/planejar", response_class=HTMLResponse)
def planejar_tela(request: Request, rev: int | None = None, dia: str | None = None, db: Session = Depends(get_db)):
    r = _rev(db, rev)
    hoje = date.today()
    d = _data(dia) or hoje
    # dias oferecidos: período da revisão e as próximas 3 semanas, sem domingos
    base = {hoje + timedelta(days=k) for k in range(-3, 22)}
    if r and r.inicio and r.fim:
        base |= {r.inicio + timedelta(days=k) for k in range((r.fim - r.inicio).days + 1)}
    base.add(d)
    dias = sorted(x for x in base if x.weekday() != 6)
    vig = [f for f in db.scalars(select(Forma).order_by(Forma.nave, Forma.y, Forma.x)) if f.vigente(d)]
    por, sobra = pl.itens_por_forma(db, r, d, vig) if r else ({}, [])
    naves = []
    for n, nome in fm.NAVES.items():
        fs = [f for f in vig if f.nave == n]
        w, h = fm.dimensoes_nave(fs)
        linhas = []
        for f in fs:
            cap, us = pl.capacidade(f, d), pl.uso(f, por.get(f.id, []))
            linhas.append({"f": f, "cap": cap, "uso": us, "n": sum(i.qtd for i in por.get(f.id, [])),
                           "secoes": sorted(fm.secoes(f)), "produtos": [fm._norm(x) for x in (f.produtos or "").split(",") if x.strip()]})
        tot_cap = sum(x["cap"] for x in linhas if x["f"].tipo != "unidade")
        tot_uso = sum(x["uso"] for x in linhas if x["f"].tipo != "unidade")
        naves.append({"cod": n, "nome": nome, "linhas": linhas, "w": w, "h": h, "cap": tot_cap, "uso": tot_uso})
    falt = pl.faltantes(db, r) if r else []
    i = dias.index(d)
    return tpl.TemplateResponse(request, "planejar.html", {
        "r": r, "revs": _revisoes(db), "d": d, "dias": dias, "ant": dias[i - 1] if i else None, "prox": dias[i + 1] if i + 1 < len(dias) else None,
        "naves": naves, "sobra": sobra, "falt": falt, "obras": sorted({g["obra"] for g in falt}), "produtos": sorted({g["produto"] for g in falt}),
        "tamanho": fm.tamanho, "altura": fm.altura, "hoje": hoje, "base": ultima_base(db)})


@app.post("/planejar/alocar")
def planejar_alocar(dados: dict = Body(...), db: Session = Depends(get_db)):
    r = db.get(RevisaoProg, int(dados["rev"])) or _404()
    f = db.get(Forma, int(dados["forma"])) or _404()
    d = _data(dados.get("dia")) or _404()
    cods = [str(c) for c in dados.get("codigos", [])][:500]
    return pl.alocar(db, r, f, d, cods, bool(dados.get("forcar")))


@app.post("/planejar/remover")
def planejar_remover(dados: dict = Body(...), db: Session = Depends(get_db)):
    ids = [int(x) for x in dados.get("ids", [])]
    n = 0
    for it in db.scalars(select(ItemProg).where(ItemProg.id.in_(ids))):
        db.delete(it)
        n += 1
    db.commit()
    return {"ok": True, "n": n}


@app.post("/planejar/revisao")
def planejar_revisao(rev: str = Form(""), dia: str = Form(""), modo: str = Form("copia"), db: Session = Depends(get_db)):
    agora = datetime.now()
    if modo == "copia" and rev:
        r = db.get(RevisaoProg, int(rev)) or _404()
        nova = pl.copiar_revisao(db, r, f"Planejamento {agora:%d/%m %H:%M} (cópia de {r.nome})")
    else:
        d = _data(dia) or date.today()
        nova = RevisaoProg(nome=f"Planejamento {agora:%d/%m %H:%M}", inicio=d, fim=d)
        db.add(nova)
        db.commit()
    return RedirectResponse(f"/planejar?rev={nova.id}&dia={dia}", status_code=303)


@app.get("/planejar/forma/{fid}", response_class=HTMLResponse)
def planejar_forma(request: Request, fid: int, rev: int, dia: str, db: Session = Depends(get_db)):
    f = db.get(Forma, fid) or _404()
    r = db.get(RevisaoProg, rev) or _404()
    d = _data(dia) or _404()
    por, _ = pl.itens_por_forma(db, r, d, [f])
    itens = sorted(por.get(f.id, []), key=lambda i: (i.obra, chave_natural(i.peca)))
    return tpl.TemplateResponse(request, "_planejar_forma.html", {"f": f, "d": d, "itens": itens, "cap": pl.capacidade(f, d),
                                                                  "uso": pl.uso(f, itens)})


# ---------------------------------------------------------------- armação (lista D-1 do armador)
def _grupos_armacao(itens, sit, incluidos=frozenset()):
    grupos = {}
    for i in itens:
        g = grupos.setdefault((i.nave, i.forma), [])
        g.append({"i": i, "s": sit[i.id], "novo": i.id in incluidos})
    return grupos


@app.get("/armacao", response_class=HTMLResponse)
def armacao_tela(request: Request, dia: str | None = None, rev: int | None = None, db: Session = Depends(get_db)):
    r = _rev(db, rev)
    d = _data(dia) or arm.proximo_dia_util(date.today())
    itens = arm.itens_do_dia(db, r, d) if r else []
    sit = arm.situacao(db, itens)
    dv = arm.ultima_divulgacao(db, d)
    incluidos, retirados = arm.diferencas(dv, itens)
    pecas = sum(i.qtd for i in itens)
    armadas = sum(i.qtd for i in itens if sit[i.id]["armado"])
    por_nave = {}
    for i in itens:
        x = por_nave.setdefault(i.nave, [0, 0])
        x[0] += i.qtd
        x[1] += i.qtd if sit[i.id]["armado"] else 0
    dias = sorted({date.today() + timedelta(days=k) for k in range(-2, 8)} | {d})
    dias = [x for x in dias if x.weekday() != 6]
    esquema = request.headers.get("x-forwarded-proto", request.url.scheme)   # Railway entrega HTTPS pelo proxy
    link = f"{esquema}://{request.url.netloc}/armador/{arm.token_armador()}"
    return tpl.TemplateResponse(request, "armacao.html", {
        "r": r, "revs": _revisoes(db), "d": d, "dias": dias, "grupos": _grupos_armacao(itens, sit, incluidos), "pecas": pecas,
        "armadas": armadas, "por_nave": dict(sorted(por_nave.items())), "dv": dv, "incluidos": incluidos, "retirados": retirados,
        "link": link, "hoje": date.today(), "base": ultima_base(db)})


@app.post("/armacao/marcar")
def armacao_marcar(dados: dict = Body(...), db: Session = Depends(get_db)):
    n = arm.marcar(db, [str(c) for c in dados.get("codigos", []) if c], [int(x) for x in dados.get("item_ids", [])], bool(dados.get("armado")))
    return {"ok": True, "n": n}


@app.post("/armacao/divulgar")
def armacao_divulgar(dia: str = Form(...), rev: str = Form(""), db: Session = Depends(get_db)):
    r = _rev(db, int(rev) if rev else None) or _404()
    d = _data(dia) or _404()
    arm.divulgar(db, r, d)
    return RedirectResponse(f"/armacao?dia={d}&rev={r.id}&divulgado=1", status_code=303)


@app.get("/armador/{token}", response_class=HTMLResponse)
def armador(request: Request, token: str, db: Session = Depends(get_db)):
    if not hmac.compare_digest(token, arm.token_armador()):
        raise HTTPException(404)
    hoje = date.today()
    listas = []
    for d in sorted({x for (x,) in db.execute(select(Divulgacao.dia).where(Divulgacao.dia >= hoje))})[:3]:
        dv = arm.ultima_divulgacao(db, d)
        sit = arm.situacao(db, dv.itens)
        grupos = {}
        for x in sorted(dv.itens, key=lambda x: (x["nave"], x["forma"], x["obra"], chave_natural(x["peca"]))):
            grupos.setdefault((x["nave"], x["forma"]), []).append({"x": x, "s": sit[x["id"]]})
        listas.append({"d": d, "dv": dv, "grupos": grupos, "pecas": sum(x["qtd"] for x in dv.itens),
                       "armadas": sum(x["qtd"] for x in dv.itens if sit[x["id"]]["armado"])})
    return tpl.TemplateResponse(request, "armador.html", {"listas": listas, "hoje": hoje})


# ---------------------------------------------------------------- minha conta, usuários e perfis
@app.get("/conta", response_class=HTMLResponse)
def conta(request: Request):
    return tpl.TemplateResponse(request, "conta.html", {"erro": None, "ok": None, "base": ""})


@app.post("/conta", response_class=HTMLResponse)
def conta_salvar(request: Request, atual: str = Form(...), nova: str = Form(...), repete: str = Form(...), db: Session = Depends(get_db)):
    u = db.get(Usuario, request.session.get("uid")) if request.session.get("uid") else None
    erro = None
    if not u:
        return RedirectResponse("/login", status_code=303)
    if not acesso.confere(atual, u.senha_hash):
        erro = "Senha atual incorreta."
    elif len(nova) < 8:
        erro = "A nova senha precisa ter pelo menos 8 caracteres."
    elif nova != repete:
        erro = "A confirmação não bate com a nova senha."
    elif nova == atual:
        erro = "A nova senha tem que ser diferente da atual."
    if erro:
        return tpl.TemplateResponse(request, "conta.html", {"erro": erro, "ok": None, "base": ""}, status_code=400)
    u.senha_hash, u.trocar_senha = acesso.gerar_hash(nova), False
    db.commit()
    request.state.usuario.trocar = False
    return tpl.TemplateResponse(request, "conta.html", {"erro": None, "ok": "Senha alterada.", "base": ""})


def _flash(request: Request, msg: str) -> None:
    request.session["flash"] = msg


@app.get("/usuarios", response_class=HTMLResponse)
def usuarios_tela(request: Request, db: Session = Depends(get_db)):
    flash = request.session.pop("flash", None)
    return tpl.TemplateResponse(request, "usuarios.html", {
        "usuarios": list(db.scalars(select(Usuario).order_by(Usuario.ativo.desc(), Usuario.login))),
        "perfis": list(db.scalars(select(Perfil).order_by(Perfil.sistema.desc(), Perfil.nome))),
        "modulos": acesso.MODULOS, "flash": flash,
        "log": list(db.scalars(select(Auditoria).order_by(Auditoria.quando.desc()).limit(80))), "base": ultima_base(db)})


def _admins_ativos(db: Session) -> int:
    return len([u for u in db.scalars(select(Usuario).where(Usuario.ativo.is_(True))) if u.perfil.sistema])


@app.post("/usuarios/novo")
def usuario_novo(request: Request, login: str = Form(...), nome: str = Form(""), perfil_id: int = Form(...), db: Session = Depends(get_db)):
    login = re.sub(r"[^a-z0-9._@-]", "", login.strip().lower())[:60]
    if not login:
        _flash(request, "Erro: informe um login (letras, números, ponto, hífen).")
    elif db.scalar(select(Usuario.id).where(Usuario.login == login)):
        _flash(request, f"Erro: já existe o usuário {login}.")
    elif not db.get(Perfil, perfil_id):
        _flash(request, "Erro: perfil inválido.")
    else:
        prov = acesso.senha_provisoria()
        db.add(Usuario(login=login, nome=nome.strip()[:120], senha_hash=acesso.gerar_hash(prov), perfil_id=perfil_id, trocar_senha=True))
        db.commit()
        _flash(request, f"Usuário {login} criado. Senha provisória: {prov} (aparece só agora; no primeiro acesso a pessoa troca).")
    return RedirectResponse("/usuarios", status_code=303)


@app.post("/usuarios/{uid}")
def usuario_editar(request: Request, uid: int, perfil_id: int = Form(...), ativo: str = Form(""), nome: str = Form(""),
                   db: Session = Depends(get_db)):
    u = db.get(Usuario, uid) or _404()
    novo_perfil = db.get(Perfil, perfil_id) or _404()
    ficaria_admin = novo_perfil.sistema and bool(ativo)
    if u.perfil.sistema and u.ativo and not ficaria_admin and _admins_ativos(db) <= 1:
        _flash(request, "Erro: precisa ficar pelo menos um administrador ativo.")
        return RedirectResponse("/usuarios", status_code=303)
    if u.id == request.session.get("uid") and not ativo:
        _flash(request, "Erro: você não pode desativar o seu próprio usuário.")
        return RedirectResponse("/usuarios", status_code=303)
    u.perfil_id, u.ativo, u.nome = perfil_id, bool(ativo), nome.strip()[:120] or u.nome
    db.commit()
    _flash(request, f"Usuário {u.login} atualizado.")
    return RedirectResponse("/usuarios", status_code=303)


@app.post("/usuarios/{uid}/senha")
def usuario_senha(request: Request, uid: int, db: Session = Depends(get_db)):
    u = db.get(Usuario, uid) or _404()
    prov = acesso.senha_provisoria()
    u.senha_hash, u.trocar_senha = acesso.gerar_hash(prov), True
    db.commit()
    _flash(request, f"Nova senha provisória de {u.login}: {prov} (aparece só agora; no próximo acesso a pessoa troca).")
    return RedirectResponse("/usuarios", status_code=303)


@app.post("/perfis/novo")
def perfil_novo(request: Request, nome: str = Form(...), base_id: int = Form(0), db: Session = Depends(get_db)):
    nome = nome.strip()[:60]
    if not nome or db.scalar(select(Perfil.id).where(Perfil.nome == nome)):
        _flash(request, "Erro: nome de perfil vazio ou repetido.")
        return RedirectResponse("/usuarios", status_code=303)
    base_p = db.get(Perfil, base_id) if base_id else None
    perms = dict(base_p.permissoes) if base_p and not base_p.sistema else {m: "ver" for m in acesso.MODULOS}
    perms["usuarios"] = "nenhum" if not (base_p and base_p.sistema) else perms.get("usuarios", "nenhum")
    db.add(Perfil(nome=nome, permissoes=perms))
    db.commit()
    _flash(request, f"Perfil {nome} criado. Ajuste os módulos na tabela.")
    return RedirectResponse("/usuarios#perfis", status_code=303)


@app.post("/perfis/{pid}")
async def perfil_salvar(request: Request, pid: int, db: Session = Depends(get_db)):
    p = db.get(Perfil, pid) or _404()
    if p.sistema:
        _flash(request, "O perfil Administrador tem acesso a tudo e não muda.")
        return RedirectResponse("/usuarios#perfis", status_code=303)
    fd = await request.form()
    p.permissoes = {m: (fd.get(f"m_{m}") if fd.get(f"m_{m}") in acesso.NIVEIS else "nenhum") for m in acesso.MODULOS}
    db.commit()
    _flash(request, f"Perfil {p.nome} salvo.")
    return RedirectResponse("/usuarios#perfis", status_code=303)


@app.post("/perfis/{pid}/excluir")
def perfil_excluir(request: Request, pid: int, db: Session = Depends(get_db)):
    p = db.get(Perfil, pid) or _404()
    if p.sistema or p.usuarios:
        _flash(request, "Erro: só dá para excluir perfil sem usuários (e o Administrador não sai).")
    else:
        db.delete(p)
        db.commit()
        _flash(request, f"Perfil {p.nome} excluído.")
    return RedirectResponse("/usuarios#perfis", status_code=303)
