import hmac
import os
import secrets
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import plannix, plantas
from .db import DATA_DIR, Base, engine, get_db
from .models import Importacao, Obra, Peca, Planta
from .status import CORES, LEGENDA, chave_natural, por_produto, resumo, status_da_obra

BASE = Path(__file__).parent
SENHA = os.getenv("APP_PASSWORD", "")
app = FastAPI(title="PCP Naves")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.globals.update(CORES={k: v[1] for k, v in CORES.items()}, LEGENDA=LEGENDA)
tpl.env.filters["pct"] = lambda v: f"{v:.0%}"
tpl.env.filters["num"] = lambda v, d=1: f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
tpl.env.filters["dm"] = lambda d: d.strftime("%d/%m") if d else ""
Base.metadata.create_all(engine)


def ultima_base(db: Session) -> str:
    imp = db.scalar(select(Importacao).where(Importacao.tipo != "programacao").order_by(Importacao.criada_em.desc()))
    return imp.criada_em.strftime("%d/%m/%Y") if imp else "sem importação"


@app.middleware("http")
async def exige_login(request: Request, call_next):
    livre = request.url.path.startswith(("/static", "/login", "/saude"))
    if SENHA and not livre and not request.session.get("ok"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# a sessão precisa envolver o middleware de login (o último adicionado roda primeiro)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32)), max_age=60 * 60 * 24 * 30)


@app.get("/saude")
def saude():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return tpl.TemplateResponse(request, "login.html", {"erro": None})


@app.post("/login")
def login(request: Request, senha: str = Form(...)):
    if SENHA and hmac.compare_digest(senha, SENHA):
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    return tpl.TemplateResponse(request, "login.html", {"erro": "Senha incorreta."}, status_code=401)


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
async def importar(arquivo: UploadFile = File(...), db: Session = Depends(get_db)):
    conteudo = await arquivo.read()
    nome = arquivo.filename or "arquivo"
    (DATA_DIR / "importacoes" / f"{date.today():%Y%m%d}_{secrets.token_hex(3)}_{Path(nome).name}").write_bytes(conteudo)
    try:
        if nome.lower().endswith(".xlsx"):
            imp = plannix.importar_programacao(db, conteudo, nome)
            msg = f"Programação importada: {imp.resumo['linhas']} peças."
        else:
            imp = plannix.importar_exportacao(db, conteudo, nome)
            r = imp.resumo
            msg = (f"{'Lista a realizar' if imp.tipo == 'a_realizar' else 'Exportação completa'}: {r['linhas']} linhas, "
                   f"{r['novas']} peças novas, {r['saiu_lista']} marcadas como fabricadas por terem saído da lista.")
    except ValueError as e:
        db.rollback()
        return RedirectResponse(f"/importar?erro={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/importar?msg={quote(msg)}", status_code=303)


@app.get("/obras/{obra_id}", response_class=HTMLResponse)
def obra(request: Request, obra_id: int, db: Session = Depends(get_db)):
    o = db.get(Obra, obra_id) or _404()
    st = status_da_obra(db, o.id)
    pl = []
    for p in o.plantas:
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
    rel = f"plantas/{o.sigla}_{secrets.token_hex(4)}.pdf"
    destino = DATA_DIR / rel
    destino.write_bytes(await arquivo.read())
    try:
        e = plantas.extrair(destino)
    except Exception:
        destino.unlink(missing_ok=True)
        raise HTTPException(400, "Não consegui ler o PDF. Confira se é o PDF vetorial do projeto (não escaneado).")
    ordem = (db.scalar(select(func.max(Planta.ordem)).where(Planta.obra_id == o.id)) or 0) + 1
    db.add(Planta(obra_id=o.id, titulo=titulo.strip()[:120], ordem=ordem, arquivo=rel, largura=e["largura"], altura=e["altura"],
                  rotacao=e["rotacao"], marcas=e["marcas"]))
    db.commit()
    return RedirectResponse(f"/obras/{obra_id}", status_code=303)


@app.post("/plantas/{pid}/excluir")
def planta_excluir(pid: int, db: Session = Depends(get_db)):
    p = db.get(Planta, pid) or _404()
    oid = p.obra_id
    (DATA_DIR / p.arquivo).unlink(missing_ok=True)
    (DATA_DIR / "cache" / f"planta_{pid}.png").unlink(missing_ok=True)
    db.delete(p)
    db.commit()
    return RedirectResponse(f"/obras/{oid}", status_code=303)


@app.get("/plantas/{pid}", response_class=HTMLResponse)
def planta_ver(request: Request, pid: int, db: Session = Depends(get_db)):
    p = db.get(Planta, pid) or _404()
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
    st = status_da_obra(db, p.obra_id)
    pdf = plantas.pdf_a2(DATA_DIR / p.arquivo, p.marcas, st, f"{p.obra.nome.upper()} - {p.titulo.upper()}", ultima_base(db))
    nome = f"Avanco_{p.obra.nome}_{p.titulo}_{date.today():%d-%m}.pdf".replace(" ", "_")
    ascii_ = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"{ascii_}\"; filename*=UTF-8''{quote(nome)}"})


def _404():
    raise HTTPException(404, "Não encontrado.")
