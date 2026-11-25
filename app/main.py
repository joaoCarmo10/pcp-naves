import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import hmac
import io
import os
import re
import pymupdf
import secrets
import threading
import unicodedata
import zipfile
from collections import Counter, defaultdict
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

from . import ocupacao as ocup
from . import premissas as pm
from . import replanejar as rp
from . import acesso, armacao as arm, autodoc, cadastro as cad, formas as fm, pecas_tab as ptab, pxr_rel as pxr, planejar as pl, plannix, plantas, programacao as pg
from .db import DADOS_EFEMEROS, DATA_DIR, Base, SessionLocal, engine, get_db, migrar
from .models import Apontamento, Auditoria, CadastroPeca, Config, DesenhoAutodoc, Divulgacao, Forma, Perfil, Usuario, Importacao, ItemProg, Obra, Peca, Planta, Premissa, RevisaoProg
from .status import (CORES, LEGENDA, LEGENDA_UNID, atribuir_obra, chave_natural, desenhadas, por_produto, resumo,
                     status_da_obra, status_folha, unidades_da_obra)

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
# datas gravadas com datetime.now() estão no fuso do servidor (UTC no Railway): mostra no horário da Bahia
tpl.env.filters["hl"] = lambda d, f="%d/%m %H:%M": d.astimezone(plannix.BAHIA).strftime(f) if d else ""
Base.metadata.create_all(engine)
migrar()
from . import autodoc_robo   # noqa: E402  (robô do Autodoc: agenda própria, 2x por dia)
autodoc_robo.iniciar_agenda()
with SessionLocal() as _db:
    fm.carga_inicial(_db)
    fm.completar(_db)
    pl.completar_prioridades(_db)
    acesso.carga_inicial(_db)


def ultima_base(db: Session) -> str:
    imp = db.scalar(select(Importacao).where(Importacao.tipo != "programacao").order_by(Importacao.criada_em.desc()))
    try:   # o PC da fábrica atualiza de hora em hora: mostra a hora da última mudança que ele mandou
        sync = datetime.fromisoformat(_cfg(db, "sync_mudanca"))
    except ValueError:
        sync = None
    if sync and (not imp or sync.date() >= imp.criada_em.date()):
        return sync.strftime("%d/%m/%Y %H:%M")
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
    if not SENHA or caminho.startswith(("/static", "/login", "/saude", "/armador/", "/api/plannix", "/api/autodoc")):
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


def _cfg(db: Session, chave: str) -> str:
    c = db.get(Config, chave)
    return c.valor if c else ""


def _cfg_grava(db: Session, chave: str, valor: str) -> None:
    c = db.get(Config, chave) or Config(chave=chave)
    c.valor = valor
    db.add(c)


def _sync_status(db: Session) -> dict:
    def dt(v):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    contato = dt(_cfg(db, "sync_contato"))
    agora = datetime.now(plannix.BAHIA).replace(tzinfo=None)
    return {"ativo": bool(_cfg(db, "sync_token")), "contato": contato, "mudanca": dt(_cfg(db, "sync_mudanca")),
            "atrasado": bool(contato and (agora - contato).total_seconds() > 7 * 3600),
            "duracao": _cfg(db, "sync_duracao"), "erro": _cfg(db, "sync_erro")}


@app.get("/importar", response_class=HTMLResponse)
def importar_form(request: Request, db: Session = Depends(get_db)):
    imps = db.scalars(select(Importacao).order_by(Importacao.criada_em.desc()).limit(15)).all()
    u = request.state.usuario
    return tpl.TemplateResponse(request, "importar.html", {"imps": imps, "msg": request.query_params.get("msg"), "erro": request.query_params.get("erro"),
                                                           "sync": _sync_status(db), "admin": (u is None) or u.admin, "ad": _autodoc_status(db),
                                                           "base": ultima_base(db)})


def _autodoc_status(db: Session) -> dict:
    def dt(v):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    n = db.scalar(select(func.count(DesenhoAutodoc.id))) or 0
    nb = sum(1 for x in db.scalars(select(DesenhoAutodoc).where(DesenhoAutodoc.obsoleto.is_(False))) if x.bloqueado and x.pecas)
    return {"configurado": autodoc_robo.configurado(), "rodando": autodoc_robo.ESTADO["rodando"],
            "agora": autodoc_robo.ESTADO["msg"] if autodoc_robo.ESTADO["rodando"] else "",
            "ultima": dt(_cfg(db, "autodoc_ultima")), "msg": _cfg(db, "autodoc_msg"), "erro": _cfg(db, "autodoc_erro"),
            "tentativa": dt(_cfg(db, "autodoc_tentativa")), "desenhos": n, "bloqueados": nb,
            "horarios": " e ".join(f"{h:02d}:{m:02d}" for h, m in autodoc_robo.HORARIOS)}


@app.post("/autodoc/atualizar")
def autodoc_atualizar(request: Request):
    u = request.state.usuario
    if u is not None and not u.admin:
        return RedirectResponse(f"/importar?erro={quote('Só o administrador dispara o robô do Autodoc.')}", status_code=303)
    if not autodoc_robo.configurado():
        return RedirectResponse(f"/importar?erro={quote('Configure AUTODOC_EMAIL e AUTODOC_SENHA nas variáveis do Railway.')}", status_code=303)
    threading.Thread(target=autodoc_robo.rodar, daemon=True).start()
    return RedirectResponse("/importar#autodoc", status_code=303)


@app.post("/importar/token", response_class=HTMLResponse)
def importar_token(request: Request, db: Session = Depends(get_db)):
    """Gera o token do script de sincronização (mostrado uma vez só; o banco guarda apenas o hash)."""
    u = request.state.usuario
    if u is not None and not u.admin:
        return RedirectResponse(f"/importar?erro={quote('Só o administrador gera o token.')}", status_code=303)
    token = secrets.token_urlsafe(32)
    _cfg_grava(db, "sync_token", hashlib.sha256(token.encode()).hexdigest())
    db.commit()
    imps = db.scalars(select(Importacao).order_by(Importacao.criada_em.desc()).limit(15)).all()
    return tpl.TemplateResponse(request, "importar.html", {"imps": imps, "token_novo": token, "sync": _sync_status(db), "admin": True,
                                                           "ad": _autodoc_status(db)})


@app.post("/importar/aco-frouxo")
async def importar_aco_frouxo(request: Request, db: Session = Depends(get_db)):
    """Peso do aço frouxo por código (kg), vindo de uma exportação do Plannix com a coluna "Peso Aço Frouxo(kg)".
    Só grava esse campo; não mexe em fabricação nem em nada mais."""
    dados = await request.json()
    if not isinstance(dados, dict):
        raise HTTPException(400, "Envie {código: kg} ou {\"sigla|peça\": kg}.")
    n = 0
    if any("|" in str(k) for k in dados):
        # por nome: o peso é do projeto da peça, igual em todas as unidades com o mesmo nome na obra
        for p in db.scalars(select(Peca).join(Obra)):
            v = dados.get(f"{p.obra.sigla}|{p.peca}")
            if isinstance(v, (int, float)) and v > 0 and not p.peso_aco_frouxo:
                p.peso_aco_frouxo = float(v)
                n += 1
    else:
        for p in db.scalars(select(Peca).where(Peca.codigo.in_([str(c) for c in dados]))):
            v = dados.get(p.codigo)
            if isinstance(v, (int, float)) and v > 0:
                p.peso_aco_frouxo = float(v)
                n += 1
    db.commit()
    return {"atualizadas": n, "recebidas": len(dados)}


@app.post("/autodoc/linhas")
async def autodoc_linhas(request: Request, db: Session = Depends(get_db)):
    """Desenhos lidos do Autodoc pelo navegador do usuário logado (mesmo formato do robô). Árvore inteira = completo."""
    dados = await request.json()
    linhas = dados.get("linhas") if isinstance(dados, dict) else None
    if not isinstance(linhas, list) or not linhas:
        raise HTTPException(400, "Envie {linhas: [...]}.")
    r = autodoc.importar_linhas(db, linhas, completo=bool(dados.get("completo")))
    _cfg_grava(db, "autodoc_ultima", datetime.now().isoformat(timespec="seconds"))
    _cfg_grava(db, "autodoc_msg", f"Lido pelo navegador: {len(linhas)} arquivo(s).")
    db.commit()
    return {"ok": True, **r}


@app.get("/autodoc/receber", response_class=HTMLResponse)
def autodoc_receber():
    """Ponte: a aba do Autodoc (logada) abre esta página e manda os desenhos por postMessage."""
    return HTMLResponse(r"""<!doctype html><meta charset="utf-8"><title>Autodoc → PCP Naves</title>
<body style="font:14px sans-serif;padding:24px">Aguardando os desenhos do Autodoc…<script>
window.addEventListener('message', async e => {
  if (!/(^|\.)autodoc\.com\.br$/.test(new URL(e.origin).hostname) || !e.data || !e.data.linhas) return;
  document.body.textContent = 'Gravando ' + e.data.linhas.length + ' arquivos…';
  const r = await fetch('/autodoc/linhas', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(e.data)});
  const j = await r.json();
  document.body.textContent = r.ok ? ('Pronto: ' + JSON.stringify(j)) : ('Erro: ' + JSON.stringify(j));
  e.source.postMessage({resultado: j, ok: r.ok}, e.origin);
});
if (window.opener) window.opener.postMessage({pronto: true}, '*');
</script>""")


@app.post("/api/autodoc")
async def autodoc_automatico(request: Request, db: Session = Depends(get_db)):
    """Recebe o relatório 'Exportar' do Autodoc enviado por automação (mesmo token do Plannix automático)."""
    tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    guardado = _cfg(db, "sync_token")
    if not tok or not guardado or not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), guardado):
        return JSONResponse({"ok": False, "erro": "Token inválido."}, status_code=401)
    conteudo = await request.body()
    if not autodoc.detectar(conteudo):
        return JSONResponse({"ok": False, "erro": "Não é um relatório de arquivos do Autodoc."}, status_code=422)
    return {"ok": True, **autodoc.importar(db, conteudo)}


def _token_valido(request: Request, db: Session) -> bool:
    tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    guardado = _cfg(db, "sync_token")
    return bool(tok and guardado and hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), guardado))


@app.get("/api/autodoc/obras")
def api_autodoc_obras(request: Request, db: Session = Depends(get_db)):
    """Para o script do PC da fábrica: siglas das obras ativas (só essas pastas do Autodoc são lidas)."""
    if not _token_valido(request, db):
        return JSONResponse({"ok": False, "erro": "Token inválido."}, status_code=401)
    return {"ok": True, "siglas": sorted(o.sigla for o in db.scalars(select(Obra).where(Obra.ativa.is_(True))) if o.sigla)}


@app.post("/api/autodoc/linhas")
async def api_autodoc_linhas(request: Request, db: Session = Depends(get_db)):
    """Desenhos lidos do Autodoc pelo script do PC da fábrica (gzip + JSON {linhas, completo}). Só chega quando mudou."""
    import gzip
    if not _token_valido(request, db):
        return JSONResponse({"ok": False, "erro": "Token inválido."}, status_code=401)
    corpo = await request.body()
    if request.headers.get("content-encoding", "") == "gzip":
        corpo = gzip.decompress(corpo)
    dados = json.loads(corpo or b"{}")
    linhas = dados.get("linhas") or []
    agora = datetime.now(plannix.BAHIA).replace(tzinfo=None).isoformat(timespec="seconds")
    _cfg_grava(db, "autodoc_ultima", agora)
    if not linhas:
        _cfg_grava(db, "autodoc_msg", "PC da fábrica: sem mudança no Autodoc.")
        db.commit()
        return {"ok": True, "sem_mudanca": True}
    r = autodoc.importar_linhas(db, linhas, completo=False)
    _cfg_grava(db, "autodoc_msg", f"PC da fábrica: {len(linhas)} arquivo(s) lidos; bloqueadas: {', '.join(r.get('bloqueadas', [])[:30])}"[:480])
    _cfg_grava(db, "autodoc_erro", "")
    db.commit()
    return {"ok": True, **r}


@app.post("/api/plannix")
async def plannix_automatico(request: Request, db: Session = Depends(get_db)):
    """Recebe o 'all data' enviado pelo script que roda no PC da fábrica (lê o SQL Server do Plannix).
    Autenticação por token; envio igual ao anterior só registra o contato, sem reprocessar."""
    import gzip
    tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    guardado = _cfg(db, "sync_token")
    if not tok or not guardado or not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), guardado):
        return JSONResponse({"ok": False, "erro": "Token inválido. Gere um novo em Importar dados."}, status_code=401)
    conteudo = await request.body()
    if request.headers.get("content-encoding", "") == "gzip":
        conteudo = gzip.decompress(conteudo)
    agora = datetime.now(plannix.BAHIA).replace(tzinfo=None).isoformat(timespec="seconds")
    _cfg_grava(db, "sync_contato", agora)
    if not conteudo:
        db.commit()
        return {"ok": True, "sem_mudanca": True}
    h = hashlib.sha256(conteudo).hexdigest()
    if h == _cfg(db, "sync_hash"):
        _cfg_grava(db, "sync_erro", "")
        db.commit()
        return {"ok": True, "sem_mudanca": True}
    t0 = time.monotonic()
    try:
        (DATA_DIR / "importacoes" / "plannix_automatico_ultimo.tsv").write_bytes(conteudo)
        imp = plannix.importar_exportacao(db, conteudo, "Plannix (automático)")
    except ValueError as e:
        db.rollback()
        _cfg_grava(db, "sync_contato", agora)
        _cfg_grava(db, "sync_erro", str(e)[:480])
        db.commit()
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=422)
    _cfg_grava(db, "sync_hash", h)
    _cfg_grava(db, "sync_mudanca", agora)
    _cfg_grava(db, "sync_duracao", f"{time.monotonic() - t0:.0f}")
    _cfg_grava(db, "sync_erro", "")
    db.commit()
    return {"ok": True, "tipo": imp.tipo, "resumo": imp.resumo}


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
            if nome.lower().endswith(".xlsx") and autodoc.detectar(conteudo):
                r = autodoc.importar(db, conteudo)
                msgs.append(f"{nome}: Autodoc, {r['arquivos']} desenhos ({r['novos']} novos)"
                            + (f", bloqueados: {', '.join(r['bloqueadas'])}" if r["bloqueadas"] else ", nenhum bloqueado")
                            + (f". Projeto sem obra no Plannix: {', '.join(r['sem_obra'])}" if r["sem_obra"] else "") + ".")
                continue
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


def _atrib_obra(db: Session, obra: Obra) -> dict[int, list[dict]]:
    """Etiquetas de todas as folhas da obra, cada uma com um código único do Plannix (sem repetir entre folhas)."""
    fs = sorted(obra.plantas, key=lambda p: (chave_natural(p.titulo or ""), p.id))
    for p in fs:
        _garantir_marcas(db, p)
    return atribuir_obra([(p.id, plantas.dentro(p.marcas, p.area)) for p in fs], unidades_da_obra(db, obra.id))


@app.get("/obras/{obra_id}", response_class=HTMLResponse)
def obra(request: Request, obra_id: int, db: Session = Depends(get_db)):
    o = db.get(Obra, obra_id) or _404()
    st = status_da_obra(db, o.id)
    ao = _atrib_obra(db, o)
    dz = desenhadas(ao)
    pl = [dict(p=p, r=resumo(status_folha(ao[p.id], st, dz))) for p in o.plantas]
    bruto = unidades_da_obra(db, o.id, escopo=False)
    fora = {n: len(us) - (st[n].total if n in st else 0) for n, us in bruto.items()}
    fora = {n: k for n, k in fora.items() if k > 0}
    return tpl.TemplateResponse(request, "obra.html", {"o": o, "r": resumo(st), "prod": por_produto(st), "plantas": pl,
                                                       "base": ultima_base(db), "fora": fora})


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
    ao = _atrib_obra(db, p.obra)
    atrib = ao[p.id]
    stf = status_folha(atrib, st, desenhadas(ao))
    marcas = plantas.marcas_tela(caminho, atrib, st, zoom)
    nomes = sorted(stf, key=lambda k: (stf[k].produto, chave_natural(k)))
    with pymupdf.open(caminho) as doc:
        pg0 = doc[0]
        mtx = pg0.rotation_matrix * pymupdf.Matrix(zoom, zoom)
        fora = [r * mtx for r in plantas.sombras(pg0.mediabox.width, pg0.mediabox.height, p.area)]
    fora = [dict(x=round(r.x0), y=round(r.y0), w=round(r.width), h=round(r.height)) for r in fora]
    return tpl.TemplateResponse(request, "planta.html", {
        "p": p, "o": p.obra, "marcas": marcas, "w": round(p.largura * zoom), "h": round(p.altura * zoom), "linhas": [stf[k] for k in nomes],
        "r": resumo(stf), "base": ultima_base(db), "fora": fora, "flash": request.session.pop("flash", None),
        "sem": sum(s.sem for s in stf.values()), "nao_desenhadas": sum(s.nao_desenhadas for s in stf.values()), "LEGENDA": LEGENDA_UNID})


@app.post("/plantas/{pid}/area")
def planta_area(request: Request, pid: int, de: str = Form(""), ate: str = Form(""), todas: str = Form(""), remover: str = Form(""),
                db: Session = Depends(get_db)):
    """Área de atuação: só os eixos 'de' a 'ate' são nossos (o resto da planta é de outra empresa)."""
    p = db.get(Planta, pid) or _404()
    alvo = list(p.obra.plantas) if todas else [p]
    if remover:
        for x in alvo:
            x.area = None
        db.commit()
        _flash(request, "Área de atuação removida: a planta inteira volta a contar.")
        return RedirectResponse(f"/plantas/{pid}", status_code=303)
    if not (de.strip().isdigit() and ate.strip().isdigit()):
        _flash(request, "Erro: informe os eixos inicial e final (números).")
        return RedirectResponse(f"/plantas/{pid}", status_code=303)
    d, a = int(de), int(ate)
    ok, ruins = [], []
    for x in alvo:
        if x.eixos is None:
            x.eixos = plantas.eixos(DATA_DIR / x.arquivo)
        f = plantas.faixa(x.eixos or [], d, a)
        if f:
            x.area = {"de": d, "ate": a, "x0": f[0], "x1": f[1]}
            ok.append(x.titulo)
        else:
            ruins.append(x.titulo)
    db.commit()
    _flash(request, (f"Área de atuação: eixos {d} a {a} em {len(ok)} planta(s)." if ok else "")
           + (f" Erro: não achei os eixos {d} e {a} em: {', '.join(ruins)}." if ruins else ""))
    return RedirectResponse(f"/plantas/{pid}", status_code=303)


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
    ao = _atrib_obra(db, p.obra)
    atrib = ao[p.id]
    pdf = plantas.pdf_a2(DATA_DIR / p.arquivo, atrib, status_folha(atrib, st, desenhadas(ao)), f"{p.obra.nome.upper()} - {p.titulo.upper()}",
                         ultima_base(db), p.area)
    nome = f"Avanco_{p.obra.nome}_{p.titulo}_{date.today():%d-%m}.pdf".replace(" ", "_")
    ascii_ = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"{ascii_}\"; filename*=UTF-8''{quote(nome)}"})


def _404():
    raise HTTPException(404, "Não encontrado.")


# ======================================================== programação e programado x realizado
tpl.env.globals.update(CAUSAS=pg.CAUSAS, COR_STATUS=pg.COR_STATUS, DIAS=pg.DIAS, STATUS=pg.STATUS, timedelta=timedelta)


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
    # fabricadas no dia (Plannix) que não estavam programadas para o dia: adiantadas ou fora da programação
    usados = {l.item.codigo for l in todas if l.item.codigo} | {l.troca for l in todas if l.troca}
    prog_rev = {it.codigo: it for it in r.itens if it.codigo}
    fora = []
    for p in db.scalars(select(Peca).where(Peca.data_fabricacao == d).order_by(Peca.peca)):
        if p.codigo in usados:
            continue
        it = prog_rev.get(p.codigo)
        fora.append({"p": p, "obra": p.obra.nome, "prog": it.data if it else None, "forma": it.forma if it else ""})
    fora.sort(key=lambda x: (x["prog"] is None, x["obra"], chave_natural(x["p"].peca)))
    return tpl.TemplateResponse(request, "programacao.html", {
        "r": r, "revs": _revisoes(db), "dias": dias, "d": d, "ant": dias[i - 1] if i else None, "prox": dias[i + 1] if i + 1 < len(dias) else None,
        "ls": ls, "tot": tot, "acum": acum, "semana": semana, "f": f, "opcoes": opcoes, "hoje": date.today(), "base": ultima_base(db),
        "fora": fora, "fora_vol": sum(x["p"].volume or 0 for x in fora)})


def _ocup_ctx(db: Session, rev: int | None, de: str | None, ate: str | None):
    r = db.get(RevisaoProg, rev) if rev else db.scalar(select(RevisaoProg).order_by(RevisaoProg.criada_em.desc(), RevisaoProg.id.desc()))
    if not r:
        return None, None
    datas = list(db.scalars(select(ItemProg.data).where(ItemProg.revisao_id == r.id)))
    ult = max(datas) if datas else r.fim
    fim_plano = date.fromisoformat(r.relatorio["fim"]) if (r.relatorio or {}).get("fim") else (r.fim or ult)
    ini = _data(de) or max(min(date.today(), ult), r.inicio)
    fim = _data(ate) or min(ult, fim_plano)   # rascunho: o período planejado (sem os dias do remanejamento)
    return r, ocup.calcular(db, r, ini, max(fim, ini))


@app.get("/programacao/ocupacao", response_class=HTMLResponse)
def ocupacao_tela(request: Request, rev: int | None = None, de: str | None = None, ate: str | None = None,
                  db: Session = Depends(get_db)):
    r, c = _ocup_ctx(db, rev, de, ate)
    if not r:
        return RedirectResponse("/programacao", status_code=303)
    return tpl.TemplateResponse(request, "ocupacao.html", {"r": r, "revs": _revisoes(db), "c": c, "cor": ocup.cor, "NAVES": ocup.NAVES,
                                                          "base": ultima_base(db), "agora": datetime.now()})


@app.get("/programacao/ocupacao.xlsx")
def ocupacao_planilha(rev: int | None = None, de: str | None = None, ate: str | None = None, db: Session = Depends(get_db)):
    r, c = _ocup_ctx(db, rev, de, ate)
    if not r:
        raise HTTPException(404)
    d0, d1 = (c["dias"][0], c["dias"][-1]) if c["dias"] else (date.today(), date.today())
    return Response(ocup.planilha(c, r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Ocupacao_formas_{d0:%d-%m}_a_{d1:%d-%m}.xlsx"'})


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


def _pxr_ctx(request: Request, db: Session) -> dict | None:
    """Recorte do programado x realizado a partir da URL (revisão, semana/datas, obra, nave, tipo de peça, status)."""
    qp = request.query_params
    per = pxr.periodo(db, qp, _rev(db, None))
    r = per["rev"]
    if not r:
        return None
    ini, fim = per["ini"], per["fim"]
    todas = pg.linhas(db, r, ini, fim)
    f = {k: qp.get(k, "") for k in ("obra", "nave", "produto", "status")}
    ls = pxr.filtrar(todas, f)
    # semana anterior, mesmo recorte, na revisão que cobre aquela data
    ant_ini, ant_fim = ini - timedelta(days=7), fim - timedelta(days=7)
    r_ant = pxr.revisao_para(db, ant_ini)
    ls_ant = pxr.filtrar(pg.linhas(db, r_ant, ant_ini, ant_fim), f) if r_ant else []
    a = pxr.analisar(ls, ls_ant)
    lista = [l for l in pxr.so_status(ls, f["status"] or "atrasadas") if l.status not in ("PENDENTE", "SEM INFORMAÇÃO")]
    lista.sort(key=lambda l: (l.item.data, l.item.nave, l.item.forma, l.item.obra, l.item.peca))
    ad_idx = autodoc.indice(db)
    obra_id = {o.nome: o.id for o in db.scalars(select(Obra))}
    for l in ls:
        l.ad = autodoc.resumo(autodoc.de(ad_idx, obra_id.get(l.item.obra), l.item.peca)) if l.item.obra in obra_id else None
    nomes = {"obra": "Obra", "nave": "Nave", "produto": "Tipo de peça"}
    filtros_txt = ", ".join(f"{nomes[k]}: {v}" for k, v in f.items() if v and k in nomes)
    if f["status"] and f["status"] != "atrasadas":
        filtros_txt = ", ".join(x for x in (filtros_txt, "Lista: " + pxr.FILTROS_STATUS[f["status"]][0]) if x)
    return {"r": r, "revs": _revisoes(db), "ini": ini, "fim": fim, "semanas": per["semanas"], "semana": per["semana"],
            "f": f, "op": pxr.opcoes(todas), "a": a, "tot": a["tot"] or pxr.vazio(), "lista": lista,
            "ant_ini": ant_ini, "ant_fim": ant_fim, "filtros_txt": filtros_txt, "todas": ls,
            "FILTROS_STATUS": pxr.FILTROS_STATUS, "base": ultima_base(db),
            "qs": "&".join(f"{k}={quote(v)}" for k, v in qp.items() if v)}


@app.get("/pxr", response_class=HTMLResponse)
def pxr_tela(request: Request, db: Session = Depends(get_db)):
    ctx = _pxr_ctx(request, db)
    if not ctx:
        return tpl.TemplateResponse(request, "pxr.html", {"r": None, "revs": [], "base": ultima_base(db)})
    return tpl.TemplateResponse(request, "pxr.html", ctx)


@app.get("/pxr/relatorio", response_class=HTMLResponse)
def pxr_relatorio(request: Request, db: Session = Depends(get_db)):
    ctx = _pxr_ctx(request, db)
    if not ctx:
        return RedirectResponse("/pxr", status_code=303)
    ctx["emitido"] = datetime.now(plannix.BAHIA).strftime("%d/%m/%Y %H:%M")
    u = request.state.usuario
    ctx["autor"] = (u.nome if u and getattr(u, "nome", "") else "")
    return tpl.TemplateResponse(request, "pxr_relatorio.html", ctx)


@app.get("/pxr/pendencias", response_class=HTMLResponse)
def pxr_pendencias(request: Request, db: Session = Depends(get_db)):
    """O que ficou para trás: peças não realizadas do recorte, para imprimir."""
    ctx = _pxr_ctx(request, db)
    if not ctx:
        return RedirectResponse("/pxr", status_code=303)
    ordem = "obra" if request.query_params.get("por") == "obra" else "tipo"
    ctx["p"] = pxr.pendencias(ctx["todas"], ordem)
    ctx["emitido"] = datetime.now(plannix.BAHIA).strftime("%d/%m/%Y %H:%M")
    ctx["qs_sem_por"] = "&".join(f"{k}={quote(v)}" for k, v in request.query_params.items() if v and k != "por")
    return tpl.TemplateResponse(request, "pxr_pendencias.html", ctx)


@app.get("/pxr/tabela")
def pxr_tabela(request: Request, db: Session = Depends(get_db)):
    """Programado x realizado no layout da planilha de programação, para a produção."""
    ctx = _pxr_ctx(request, db)
    if not ctx:
        return RedirectResponse("/pxr", status_code=303)
    nome = f"Programado_x_Realizado_{ctx['ini']:%d-%m}_a_{ctx['fim']:%d-%m}.xlsx"
    return Response(pxr.tabela_programacao(ctx), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@app.get("/pxr/planilha")
def pxr_planilha(request: Request, db: Session = Depends(get_db)):
    ctx = _pxr_ctx(request, db)
    if not ctx:
        return RedirectResponse("/pxr", status_code=303)
    nome = f"programado_x_realizado_{ctx['ini']:%d-%m}_a_{ctx['fim']:%d-%m-%Y}.xlsx"
    return Response(pxr.planilha(ctx), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


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
    rp_ini = hoje + timedelta(days=1)
    while rp_ini.weekday() == 6:
        rp_ini += timedelta(days=1)
    rp_fim = max(r.fim, rp_ini) if r and r.fim else rp_ini + timedelta(days=13)
    return tpl.TemplateResponse(request, "planejar.html", {
        "r": r, "revs": _revisoes(db), "d": d, "dias": dias, "ant": dias[i - 1] if i else None, "prox": dias[i + 1] if i + 1 < len(dias) else None,
        "naves": naves, "sobra": sobra, "falt": falt, "obras": sorted({g["obra"] for g in falt}), "produtos": sorted({g["produto"] for g in falt}),
        "tamanho": fm.tamanho, "altura": fm.altura, "hoje": hoje, "base": ultima_base(db), "rp_ini": rp_ini, "rp_fim": rp_fim})


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


@app.get("/planejar/premissas", response_class=HTMLResponse)
def premissas_tela(request: Request, db: Session = Depends(get_db)):
    pm.semear(db)
    prs = db.scalars(select(Premissa).order_by(Premissa.ativa.desc(), Premissa.tipo, Premissa.id)).all()
    formas = sorted({(f.codigo, f.nome_completo) for f in db.scalars(select(Forma))})
    obras = [o.nome for o in db.scalars(select(Obra).order_by(Obra.nome))]
    return tpl.TemplateResponse(request, "premissas.html", {"prs": prs, "TIPOS": pm.TIPOS, "FIXAS": pm.REGRAS_FIXAS, "formas": formas,
                                                            "obras": obras, "base": ultima_base(db)})


@app.post("/planejar/premissas")
def premissas_salvar(pid: str = Form(""), acao: str = Form("salvar"), tipo: str = Form("excluir"), obra: str = Form(""),
                     pecas: str = Form(""), produto: str = Form(""), secoes: str = Form(""), forma: str = Form(""),
                     de: str = Form(""), ate: str = Form(""), volume: str = Form(""), limite: str = Form(""), obs: str = Form(""),
                     db: Session = Depends(get_db)):
    pr = db.get(Premissa, int(pid)) if pid.isdigit() else None
    if acao == "excluir" and pr:
        db.delete(pr)
    elif acao == "alternar" and pr:
        pr.ativa = not pr.ativa
    else:
        if tipo not in pm.TIPOS:
            raise HTTPException(400, "Tipo inválido.")
        pr = pr or Premissa()
        pr.tipo, pr.obra, pr.pecas, pr.produto = tipo, obra.strip()[:80], pecas.strip()[:300], produto.strip().upper()[:40]
        pr.secoes, pr.forma, pr.obs = secoes.strip()[:120], forma.strip()[:30], obs.strip()[:300]
        pr.de, pr.ate, pr.volume = _data(de), _data(ate), _num(volume)
        pr.limite = int(limite) if limite.strip().isdigit() else None
        db.add(pr)
    db.commit()
    return RedirectResponse("/planejar/premissas", status_code=303)


@app.post("/planejar/replanejar")
def planejar_replanejar(rev: int = Form(...), inicio: str = Form(...), fim: str = Form(...), db: Session = Depends(get_db)):
    base = db.get(RevisaoProg, rev) or _404()
    ini, fi = _data(inicio), _data(fim)
    if not ini or not fi or fi < ini or (fi - ini).days > 90:
        raise HTTPException(400, "Período inválido (até 90 dias).")
    nova = rp.replanejar(db, base, ini, fi)
    return RedirectResponse(f"/planejar/replanejamento/{nova.id}", status_code=303)


@app.get("/planejar/replanejamento/{rid}", response_class=HTMLResponse)
def replanejamento_ver(request: Request, rid: int, db: Session = Depends(get_db)):
    r = db.get(RevisaoProg, rid) or _404()
    rel = r.relatorio or {}
    ini = _data(rel.get("inicio")) or r.inicio
    itens = [i for i in r.itens if i.data >= ini]
    por_dia = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for i in itens:
        x = por_dia[i.data][i.nave]
        x[0] += i.qtd or 1
        x[1] += i.volume or 0
    tot_dia = {d: sum(v[1] for v in n.values()) for d, n in por_dia.items()}
    prot = rp.protensao_dos_itens(db, [i for i in itens if i.nave == "N2"])
    prot_dia = defaultdict(set)
    for i in itens:
        if i.nave == "N2" and prot.get(i.codigo or "", {}).get("txt"):
            prot_dia[i.data].add(prot[i.codigo]["txt"])
    return tpl.TemplateResponse(request, "replanejamento.html", {"r": r, "rel": rel, "ini": ini, "por_dia": dict(sorted(por_dia.items())),
                                                                 "tot_dia": tot_dia, "prot_dia": prot_dia,
                                                                 "base": ultima_base(db)})


@app.get("/planejar/replanejamento/{rid}/planilha")
def replanejamento_planilha(rid: int, db: Session = Depends(get_db)):
    r = db.get(RevisaoProg, rid) or _404()
    ini = _data((r.relatorio or {}).get("inicio")) or r.inicio
    nome = f"Programacao_Producao_{ini:%d-%m}_a_{r.fim:%d-%m}_replanejada.xlsx"
    from . import planilha_prog
    return Response(planilha_prog.gerar(db, r, ini), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@app.post("/planejar/replanejamento/{rid}/publicar")
def replanejamento_publicar(rid: int, db: Session = Depends(get_db)):
    r = db.get(RevisaoProg, rid) or _404()
    rp.publicar(db, r)
    return RedirectResponse(f"/planejar/replanejamento/{rid}", status_code=303)


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
    # aço por item (cadastro de peças): kg passivo e protensão
    idx = cad.indice(db)
    cods = [i.codigo for i in itens if i.codigo]
    pecas_cod = {p.codigo: p for p in db.scalars(select(Peca).where(Peca.codigo.in_(cods)))} if cods else {}
    aco, bit_dia, sem_cad = {}, {}, 0
    for i in itens:
        pc = pecas_cod.get(i.codigo)
        c = cad.de_peca(idx, pc) if pc else None
        if c:
            aco[i.id] = {"kg": (c.aco_passivo or 0) * i.qtd, "prot": c.protensao, "cid": c.id, "e2": c.segunda_etapa}
            for b, kg in (c.bitolas or {}).items():
                bit_dia[b] = bit_dia.get(b, 0) + kg * i.qtd
        else:
            sem_cad += i.qtd
    esquema = request.headers.get("x-forwarded-proto", request.url.scheme)   # Railway entrega HTTPS pelo proxy
    link = f"{esquema}://{request.url.netloc}/armador/{arm.token_armador()}"
    return tpl.TemplateResponse(request, "armacao.html", {
        "r": r, "revs": _revisoes(db), "d": d, "dias": dias, "grupos": _grupos_armacao(itens, sit, incluidos), "pecas": pecas,
        "armadas": armadas, "por_nave": dict(sorted(por_nave.items())), "dv": dv, "incluidos": incluidos, "retirados": retirados,
        "link": link, "hoje": date.today(), "aco": aco, "bit_dia": dict(sorted(bit_dia.items(), key=lambda kv: float(kv[0]))),
        "sem_cad": sem_cad, "base": ultima_base(db)})


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


# ---------------------------------------------------------------- cadastro de peças (pranchas de detalhamento)
@app.get("/pecas", response_class=HTMLResponse)
def pecas_modulo(request: Request, db: Session = Depends(get_db)):
    """Características de todas as peças: um tipo por linha (obra + nome + etapa), dados do Plannix
    completados pelo cadastro lido das pranchas."""
    d = ptab.montar(db, request.query_params)
    obras = d["obras"]
    sel = next((o for o in obras.values() if o.nome == d["f"]["obra"]), None)
    recente = lambda st: (datetime.now() - st.get("em", datetime.min)).total_seconds() < 3 * 3600
    lendo = [(obras[oid], st) for oid, st in cad.ANDAMENTO.items()
             if oid in obras and (st["feitos"] < st["total"] or (sel and oid == sel.id) or (not sel and recente(st)))]
    LIMITE = 800
    return tpl.TemplateResponse(request, "pecas.html", {
        **d, "linhas": d["linhas"][:LIMITE], "cortadas": max(0, len(d["linhas"]) - LIMITE), "base": ultima_base(db),
        "obras": sorted(obras.values(), key=lambda o: o.nome), "obras_map": obras, "sel": sel, "lendo": lendo,
        "rodando": any(st["feitos"] < st["total"] for _, st in lendo), "qs": request.url.query,
        "flash": request.session.pop("flash", None)})


@app.get("/pecas/planilha")
def pecas_planilha(request: Request, db: Session = Depends(get_db)):
    d = ptab.montar(db, request.query_params)
    nome = "pecas" + (f"_{d['f']['obra']}" if d["f"]["obra"] else "") + f"_{date.today():%d-%m-%Y}.xlsx"
    nome = re.sub(r"[^\w.\-]+", "_", unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode())
    return Response(ptab.planilha(d), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


def _pecas_url(o: Obra) -> str:
    return f"/pecas?obra={quote(o.nome)}"


@app.get("/obras/{obra_id}/pecas")
def cadastro_tela(obra_id: int, db: Session = Depends(get_db)):
    """Atalho antigo: o cadastro de peças agora vive no módulo Peças."""
    o = db.get(Obra, obra_id) or _404()
    return RedirectResponse(_pecas_url(o), status_code=303)


async def _receber_pranchas(request: Request, o: Obra, arquivo: list[UploadFile]):
    dados = [((a.filename or "x.pdf"), await a.read()) for a in arquivo if a.filename]
    try:
        lote = cad.guardar(o, dados)
    except zipfile.BadZipFile:
        lote = []
    if not lote:
        _flash(request, "Erro: nenhum PDF encontrado nos arquivos enviados.")
        return RedirectResponse(_pecas_url(o) + "#enviar", status_code=303)
    st = cad.ANDAMENTO.get(o.id)
    if not st or st["feitos"] >= st["total"]:
        cad.ANDAMENTO[o.id] = st = {"total": 0, "feitos": 0, "msgs": [], "em": datetime.now()}
    st["total"] += len(lote)
    _ocr_fila.submit(cad.processar, o.id, lote)
    _flash(request, f"{o.nome}: {len(lote)} prancha(s) recebida(s). A leitura roda em segundo plano: as com texto saem na hora, "
                    f"as desenhadas levam uns 20 segundos cada.")
    return RedirectResponse(_pecas_url(o), status_code=303)


@app.post("/pecas/reler")
def pecas_reler(request: Request, obra_id: int = Form(0), todas: str = Form(""), db: Session = Depends(get_db)):
    """Lê de novo as pranchas que não se ligaram a nenhuma peça (útil depois de melhorar o leitor ou de importar
    um 'all data' novo). Com 'todas', relê também as já cadastradas da obra (perde correções feitas à mão)."""
    if todas and obra_id:
        alvo = list(db.scalars(select(CadastroPeca).where(CadastroPeca.obra_id == obra_id)))
    else:
        alvo = ptab.montar(db, {"obra": db.get(Obra, obra_id).nome} if obra_id else {})["orfaos"]
    por_obra: dict[int, list] = {}
    vistos = set()
    for c in alvo:
        caminho = DATA_DIR / c.arquivo if c.arquivo else None
        if caminho and caminho.exists():
            if c.arquivo not in vistos:   # prancha de peças iguais: um arquivo, vários cadastros
                vistos.add(c.arquivo)
                por_obra.setdefault(c.obra_id, []).append((c.nome_arquivo or caminho.name, caminho))
            db.delete(c)
    db.commit()
    for oid, lote in por_obra.items():
        st = cad.ANDAMENTO.get(oid)
        if not st or st["feitos"] >= st["total"]:
            cad.ANDAMENTO[oid] = st = {"total": 0, "feitos": 0, "msgs": [], "em": datetime.now()}
        st["total"] += len(lote)
        _ocr_fila.submit(cad.processar, oid, lote)
    n = sum(len(v) for v in por_obra.values())
    _flash(request, f"{n} prancha(s) sendo lida(s) de novo." if n else "Nenhuma prancha para reler.")
    o = db.get(Obra, obra_id) if obra_id else None
    return RedirectResponse(_pecas_url(o) if o else "/pecas", status_code=303)


@app.post("/pecas/pranchas")
async def pecas_enviar(request: Request, obra_id: int = Form(...), arquivo: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    return await _receber_pranchas(request, db.get(Obra, obra_id) or _404(), arquivo)


@app.post("/obras/{obra_id}/pranchas")
async def cadastro_enviar(request: Request, obra_id: int, arquivo: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    return await _receber_pranchas(request, db.get(Obra, obra_id) or _404(), arquivo)


@app.get("/cadastro/{cid}", response_class=HTMLResponse)
def cadastro_detalhe(request: Request, cid: int, db: Session = Depends(get_db)):
    c = db.get(CadastroPeca, cid) or _404()
    idx = cad.indice(db, [c.obra_id])
    unidades = sorted((p for p in db.scalars(select(Peca).where(Peca.obra_id == c.obra_id)) if cad.de_peca(idx, p) is c),
                      key=lambda p: (p.item, p.codigo))
    return tpl.TemplateResponse(request, "cadastro_peca.html", {"c": c, "unidades": unidades, "flash": request.session.pop("flash", None),
                                                                 "base": ultima_base(db)})


@app.post("/cadastro/{cid}")
async def cadastro_salvar(request: Request, cid: int, db: Session = Depends(get_db)):
    c = db.get(CadastroPeca, cid) or _404()
    fd = await request.form()
    num = lambda k: _num(str(fd.get(k, "") or ""))
    c.quantidade, c.volume_un, c.fck = num("quantidade"), num("volume_un"), num("fck")
    c.aco_passivo, c.aco_ativo = num("aco_passivo"), num("aco_ativo")
    n = num("cordoalhas")
    c.cordoalhas = int(n) if n else None
    c.cord_diam = str(fd.get("cord_diam", "") or "").strip()[:20]
    c.forca = num("forca")
    c.icamento = str(fd.get("icamento", "") or "").strip()[:120]
    c.neoprene = str(fd.get("neoprene", "") or "").strip()[:120]
    e2 = str(fd.get("etapa2", "") or "")
    c.etapa2 = e2 if e2 in ("sim", "nao") else ""
    c.etapa2_obs = str(fd.get("etapa2_obs", "") or "").strip()[:120]
    if fd.get("conferido"):
        c.situacao = "conferido"
    elif c.situacao == "conferido":
        c.situacao = "revisar" if c.alertas else "ok"
    c.atualizado_em = datetime.now()
    db.commit()
    _flash(request, f"{c.peca} salva.")
    return RedirectResponse(f"/cadastro/{c.id}", status_code=303)


@app.get("/cadastro/{cid}/pdf")
def cadastro_pdf(cid: int, db: Session = Depends(get_db)):
    c = db.get(CadastroPeca, cid) or _404()
    caminho = DATA_DIR / c.arquivo
    if not c.arquivo or not caminho.exists():
        raise HTTPException(404, "Prancha não encontrada.")
    return Response(caminho.read_bytes(), media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=prancha_{c.id}.pdf"})


@app.get("/cadastro/{cid}/texto")
def cadastro_texto(request: Request, cid: int, db: Session = Depends(get_db)):
    """Diagnóstico do leitor: linhas de texto da prancha com a posição (y) e o x de cada palavra.
    Filtros: y0, y1, x0, x1 (pontos do PDF) e busca (só linhas que contêm o texto)."""
    import pymupdf
    from . import pranchas as pr
    c = db.get(CadastroPeca, cid) or _404()
    qp = request.query_params
    f = lambda k, d: float(qp.get(k) or d)
    y0, y1, x0, x1 = f("y0", -1), f("y1", 1e9), f("x0", -1), f("x1", 1e9)
    busca = pr._n(qp.get("busca", ""))
    with pymupdf.open(DATA_DIR / c.arquivo) as doc:
        pg = doc[0]
        if qp.get("modo") == "texto":
            toks, fonte = pr._palavras(pg), "texto (forçado)"
        else:
            toks, fonte = pr._tokens(pg)
        cab = f"{c.nome_arquivo} | página {pg.rect.width:.0f}x{pg.rect.height:.0f} rot {pg.rotation} | {len(toks)} palavras | leitura: {fonte}"
    saida = [cab]
    for l in pr._linhas(toks):
        l = [t for t in l if x0 <= t.xc <= x1]
        if not l or not (y0 <= l[0].yc <= y1):
            continue
        if busca and busca not in pr._n(pr._texto(l)):
            continue
        saida.append(f"{l[0].yc:6.0f} | " + "  ".join(f"{t.t}@{t.xc:.0f}" for t in l))
    return Response("\n".join(saida), media_type="text/plain; charset=utf-8")


@app.post("/cadastro/{cid}/excluir")
def cadastro_excluir(request: Request, cid: int, db: Session = Depends(get_db)):
    c = db.get(CadastroPeca, cid) or _404()
    oid = c.obra_id
    if c.arquivo:
        (DATA_DIR / c.arquivo).unlink(missing_ok=True)
    db.delete(c)
    db.commit()
    _flash(request, f"Cadastro de {c.peca} excluído.")
    return RedirectResponse(_pecas_url(db.get(Obra, oid)), status_code=303)
