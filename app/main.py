import hmac
import os
import secrets
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import plannix, plantas, programacao as pg
from .db import DATA_DIR, Base, engine, get_db, migrar
from .models import Apontamento, Importacao, ItemProg, Obra, Peca, Planta, RevisaoProg
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
migrar()


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
            imp = pg.importar_programacao(db, conteudo, nome)
            msg = f"Programação importada como nova revisão: {imp.resumo['linhas']} linhas."
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
def programacao_dia(request: Request, rev: int | None = None, dia: str | None = None, db: Session = Depends(get_db)):
    r = _rev(db, rev)
    if not r:
        return tpl.TemplateResponse(request, "programacao.html", {"r": None, "revs": [], "base": ultima_base(db)})
    dias = sorted(set(db.scalars(select(ItemProg.data).where(ItemProg.revisao_id == r.id))))
    d = _data(dia) or date.today()
    if d not in dias:
        futuros = [x for x in dias if x >= d]
        d = futuros[0] if futuros else dias[-1]
    ls = pg.linhas(db, r, d, d)
    grupos = {}
    for l in ls:
        grupos.setdefault(l.item.nave or "-", {}).setdefault(l.item.forma or "-", []).append(l)
    i = dias.index(d)
    return tpl.TemplateResponse(request, "programacao.html", {
        "r": r, "revs": _revisoes(db), "dias": dias, "d": d, "ant": dias[i - 1] if i else None, "prox": dias[i + 1] if i + 1 < len(dias) else None,
        "grupos": grupos, "tot": pg.totais(ls), "hoje": date.today(), "base": ultima_base(db)})


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
        c["n"] += l.item.qtd
        c["ok"] += l.item.qtd if l.status in ("NO PRAZO", "COM ATRASO", "DATA A CONFIRMAR") else 0
        c["pend"] += l.item.qtd if l.status == "PENDENTE" else 0
    return tpl.TemplateResponse(request, "quadro.html", {"r": r, "revs": _revisoes(db), "dias": dias, "formas": formas, "cel": cel,
                                                         "hoje": date.today(), "base": ultima_base(db)})


@app.get("/pxr", response_class=HTMLResponse)
def pxr(request: Request, rev: int | None = None, de: str | None = None, ate: str | None = None, db: Session = Depends(get_db)):
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
            causas[l.ap.causa] += l.item.qtd
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
    for k, lim in (("causa", 60), ("acao", 300), ("responsavel", 80)):
        if k in dados:
            setattr(ap, k, str(dados[k] or "")[:lim])
    ap.atualizado_em = datetime.now()
    db.add(ap)
    db.commit()
    db.refresh(it)
    peca = db.scalar(select(Peca).where(Peca.codigo == it.codigo)) if it.codigo else None
    acomp = bool(db.scalar(select(Peca.id).join(Obra).where(Obra.nome == it.obra).limit(1)))
    st, _ = pg.classificar(it, peca, ap, date.today(), acomp)
    return {"ok": True, "status": st, "cor": pg.COR_STATUS[st]}


@app.post("/programacao/revisoes/{rid}/excluir")
def revisao_excluir(rid: int, db: Session = Depends(get_db)):
    r = db.get(RevisaoProg, rid) or _404()
    db.delete(r)
    db.commit()
    return RedirectResponse("/programacao", status_code=303)
