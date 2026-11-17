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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import formas as fm, plannix, plantas, programacao as pg
from .db import DADOS_EFEMEROS, DATA_DIR, Base, SessionLocal, engine, get_db, migrar
from .models import Apontamento, Forma, Importacao, ItemProg, Obra, Peca, Planta, RevisaoProg
from .status import CORES, LEGENDA, chave_natural, por_produto, resumo, status_da_obra

BASE = Path(__file__).parent
SENHA = os.getenv("APP_PASSWORD", "")
app = FastAPI(title="PCP Naves")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.globals.update(CORES={k: v[1] for k, v in CORES.items()}, LEGENDA=LEGENDA, DADOS_EFEMEROS=DADOS_EFEMEROS)
tpl.env.filters["pct"] = lambda v: f"{v:.0%}"
tpl.env.filters["num"] = lambda v, d=1: f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
tpl.env.filters["dm"] = lambda d: d.strftime("%d/%m") if d else ""
Base.metadata.create_all(engine)
migrar()
with SessionLocal() as _db:
    fm.carga_inicial(_db)


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
    if nome.lower().endswith(".zip"):
        try:
            msg = importar_pacote(db, conteudo)
        except (ValueError, zipfile.BadZipFile) as e:
            db.rollback()
            return RedirectResponse(f"/importar?erro={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/importar?msg={quote(msg)}", status_code=303)
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
    p = Planta(obra_id=o.id, titulo=titulo.strip()[:120], ordem=ordem, arquivo=rel, largura=e["largura"], altura=e["altura"],
               rotacao=e["rotacao"], marcas=e["marcas"])
    db.add(p)
    db.commit()
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
    f.comprimento = _num(campos["comprimento"])
    f.capacidade = _num(campos["capacidade"]) or 1
    f.ritmo_dias = max(1, int(_num(campos["ritmo_dias"]) or 1))
    f.inicio, f.fim = _data(campos["inicio"]), _data(campos["fim"])
    f.situacao = campos["situacao"] if campos["situacao"] in fm.SITUACOES else "ativa"
    f.obs = campos["obs"].strip()[:300]


async def _campos(request: Request) -> dict:
    fd = await request.form()
    ks = ["codigo", "nave", "nome", "tipo", "secao", "secoes_aceitas", "comprimento", "capacidade", "ritmo_dias", "inicio", "fim",
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
