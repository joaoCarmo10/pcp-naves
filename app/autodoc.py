"""Autodoc: status e revisão dos desenhos de cada peça, a partir do relatório 'Exportar' de um diretório.

O relatório traz um arquivo por linha (Código = nome do arquivo, Revisão, Título, Data, Status, Obsoleto, Pasta,
Projeto, Observações). A peça vem do título ("Detalhamento de Pilar PP14"); a obra, do número do projeto
("8249-BAY-GALPÃO USIBA" -> obra de sigla 8249 no Plannix).
"""
import io
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import plannix
from .models import DesenhoAutodoc, Obra

COLUNAS = ("Código", "Revisão", "Título", "Status")
NOME = re.compile(r"\b([A-Z]{1,6}\d+[A-Z0-9]*(?:[.\-][A-Z0-9]+)*)\b")
IGNORAR = re.compile(r"^(R\d{1,2}|ETAPA\d*|CA\d+|CP\d+|FCK\d*|N\d{1,2})$")


def _canon(s: str) -> str:
    from .cadastro import _canon as c
    return c(s)


def detectar(conteudo: bytes) -> bool:
    """É um relatório de arquivos exportado do Autodoc?"""
    if not conteudo.startswith(b"PK"):
        return False
    try:
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True).active
        topo = " ".join(str(v) for r in ws.iter_rows(min_row=1, max_row=12, values_only=True) for v in r if v)
        return "Relatório de Arquivos" in topo or all(c in topo for c in COLUNAS + ("Obsoleto", "Pasta"))
    except Exception:   # noqa: BLE001  qualquer xlsx que não abre não é do Autodoc
        return False


def pecas_do_titulo(titulo: str) -> list[str]:
    """'Detalhamento de Pilar PP14' -> ['PP14']; 'Detalhamento de Pilar P09=P19' -> ['P09', 'P19']."""
    t = (titulo or "").upper().replace("=", " ").replace(",", " ").replace(" E ", " ")
    out = []
    for m in NOME.finditer(t):
        n = m.group(1)
        if not IGNORAR.match(n) and n not in out:
            out.append(n)
    return out


def _data(v):
    if isinstance(v, datetime):
        return v
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt)
        except ValueError:
            continue
    return None


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def ler(conteudo: bytes) -> list[dict]:
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True).active
    linhas = list(ws.iter_rows(values_only=True))
    i_cab = next((k for k, r in enumerate(linhas) if r and "Código" in [_txt(x) for x in r] and "Status" in [_txt(x) for x in r]), None)
    if i_cab is None:
        raise ValueError("Relatório do Autodoc sem a linha de cabeçalho (Código, Revisão, Título, Status).")
    cab = [_txt(x) for x in linhas[i_cab]]
    out = []
    for r in linhas[i_cab + 1:]:
        d = {cab[k]: r[k] for k in range(min(len(cab), len(r))) if cab[k]}
        if not _txt(d.get("Código")):
            continue
        out.append(d)
    return out


def importar(db: Session, conteudo: bytes) -> dict:
    """Atualiza os desenhos do(s) diretório(s) exportado(s). Arquivo que sumiu da pasta sai do app também."""
    linhas = ler(conteudo)
    if not linhas:
        raise ValueError("Relatório do Autodoc sem arquivos.")
    return importar_linhas(db, linhas)


def ligador(db: Session):
    """Função (sigla, linha do Autodoc) -> obra. Obra com várias etapas (ex.: Hot Park: Farol e Barracão): a etapa
    citada na pasta/arquivo/título; senão a única etapa que tem as peças do título; senão a principal."""
    por_sigla: dict[str, list] = {}
    for o in db.scalars(select(Obra).order_by(Obra.id)):
        por_sigla.setdefault(o.sigla, []).append(o)
    nomes_obra: dict[int, set] = {}
    if any(len(v) > 1 for v in por_sigla.values()):
        from .models import Peca
        for oid, n in db.execute(select(Peca.obra_id, Peca.peca)):
            nomes_obra.setdefault(oid, set()).add(_canon(n))

    def obra_do_desenho(sigla: str, d: dict):
        os_ = por_sigla.get(sigla) or []
        if len(os_) <= 1:
            return os_[0] if os_ else None
        txt = plannix._sem_acento(" ".join(_txt(d.get(k)) for k in ("Pasta", "Código", "Título", "Projeto")))
        citadas = sorted((o for o in os_ if o.etapa and plannix._sem_acento(o.etapa) in txt), key=lambda o: -len(o.etapa))
        if citadas:
            return citadas[0]
        ps = {_canon(p) for p in pecas_do_titulo(_txt(d.get("Título")))}
        com = [o for o in os_ if ps and ps <= nomes_obra.get(o.id, set())]
        return com[0] if len(com) == 1 else os_[0]
    return obra_do_desenho


def religar_desenhos(db: Session) -> int:
    """Depois que a obra foi separada em etapas: cada desenho já guardado vai para a obra (etapa) certa."""
    f = ligador(db)
    n = 0
    for x in db.scalars(select(DesenhoAutodoc)):
        m = re.match(r"\s*(\d{3,6})", x.projeto or "")
        o = f(m.group(1), {"Pasta": x.pasta, "Código": x.codigo, "Título": x.titulo, "Projeto": x.projeto}) if m else None
        if o and x.obra_id != o.id:
            x.obra_id = o.id
            n += 1
    return n


def importar_linhas(db: Session, linhas: list[dict], completo: bool = False) -> dict:
    """completo=True (robô, árvore inteira de obras ativas): desenho que não apareceu sai do app."""
    obra_do_desenho = ligador(db)
    vistos: dict[tuple, set] = {}
    novos = atualizados = 0
    sem_obra = set()
    for d in linhas:
        projeto = _txt(d.get("Projeto"))
        codigo = _txt(d.get("Código"))[:160]
        m = re.match(r"\s*(\d{3,6})", projeto)
        o = obra_do_desenho(m.group(1), d) if m else None
        if not o:
            sem_obra.add(projeto)
        x = db.scalar(select(DesenhoAutodoc).where(DesenhoAutodoc.projeto == projeto, DesenhoAutodoc.codigo == codigo))
        if not x:
            x = DesenhoAutodoc(projeto=projeto[:120], codigo=codigo)
            db.add(x)
            novos += 1
        else:
            atualizados += 1
        x.obra_id = o.id if o else None
        x.extensao = _txt(d.get("Extensão")).lower()[:10]
        x.revisao = _txt(d.get("Revisão"))[:10]
        if not x.revisao:
            r = re.search(r"-R(\d{1,2})\b", codigo, re.I)
            x.revisao = r.group(1).zfill(2) if r else ""
        x.titulo = _txt(d.get("Título"))[:200]
        x.pecas = pecas_do_titulo(x.titulo)
        x.status = _txt(d.get("Status"))[:30]
        x.data = _data(d.get("Data"))
        x.obsoleto = _txt(d.get("Obsoleto")).lower().startswith("s")
        x.pasta = _txt(d.get("Pasta"))[:300]
        x.link = _txt(d.get("Link da Pasta"))[:300]
        x.observacoes = _txt(d.get("Observações"))[:500]
        if d.get("_url"):
            x.url = _txt(d.get("_url"))[:500]
        if d.get("_hash"):
            x.hash = _txt(d.get("_hash"))[:64]
        x.atualizado_em = datetime.utcnow()
        vistos.setdefault((x.projeto, x.pasta), set()).add(codigo)
    db.flush()
    removidos = 0
    if completo:
        todos = {(p, c) for (p, _), cs in vistos.items() for c in cs}
        for x in db.scalars(select(DesenhoAutodoc)):
            if (x.projeto, x.codigo) not in todos:
                db.delete(x)
                removidos += 1
        vistos = {}
    for (projeto, pasta), cods in vistos.items():
        for x in db.scalars(select(DesenhoAutodoc).where(DesenhoAutodoc.projeto == projeto, DesenhoAutodoc.pasta == pasta)):
            if x.codigo not in cods:
                db.delete(x)
                removidos += 1
    db.commit()
    bloq = sorted({p for d in linhas for p in pecas_do_titulo(_txt(d.get("Título")))
                   if _txt(d.get("Status")).lower() != "aprovado" and not _txt(d.get("Obsoleto")).lower().startswith("s")})
    return {"arquivos": len(linhas), "novos": novos, "atualizados": atualizados, "removidos": removidos,
            "bloqueadas": bloq, "sem_obra": sorted(sem_obra)}


def indice(db: Session) -> dict:
    """{(obra_id, nome canônico): desenho vigente} = o PDF mais recente não obsoleto que cita a peça."""
    sig = {o.id: o.sigla for o in db.scalars(select(Obra))}
    idx: dict = {"_sig": sig}
    for x in db.scalars(select(DesenhoAutodoc).where(DesenhoAutodoc.obsoleto.is_(False), DesenhoAutodoc.obra_id.is_not(None))):
        for p in x.pecas or []:
            # por etapa e, de reserva, pela obra do Plannix inteira (etapas iguais, ex.: blocos repetidos em escolas diferentes)
            for k in ((x.obra_id, _canon(p)), ("s", sig.get(x.obra_id), _canon(p))):
                atual = idx.get(k)
                # prefere PDF; entre iguais, o mais recente
                chave = (x.extensao == "pdf", x.data or datetime.min, x.revisao)
                if not atual or chave > (atual.extensao == "pdf", atual.data or datetime.min, atual.revisao):
                    idx[k] = x
    return idx


def de(idx: dict, obra_id: int, peca: str):
    """Desenho da peça na etapa; se a etapa não tem, o da mesma obra do Plannix (outra etapa com a mesma peça)."""
    c = _canon(peca)
    return idx.get((obra_id, c)) or idx.get(("s", idx.get("_sig", {}).get(obra_id), c))


def resumo(x) -> dict | None:
    """O que a tela mostra de um desenho."""
    if not x:
        return None
    return {"status": x.status, "bloqueado": x.bloqueado, "rev": x.revisao, "data": x.data.strftime("%d/%m/%Y") if x.data else "",
            "obs": x.observacoes, "titulo": x.titulo, "arquivo": x.codigo, "link": x.link}


def pranchas_para_baixar(db: Session, linhas: list[dict], todas: bool = False) -> list[dict]:
    """PDFs de detalhamento vigentes, novos ou alterados, de peças com unidades ainda não fabricadas em obras ativas.
    todas=True: também as de peças já fabricadas, depois das pendentes."""
    from .models import Peca
    pend: dict = {}
    for oid, nome in db.execute(select(Peca.obra_id, Peca.peca).where(Peca.fabricada.is_(False))):
        pend.setdefault(oid, set()).add(_canon(nome))
    obras = {o.id: o for o in db.scalars(select(Obra).where(Obra.ativa.is_(True)))}
    out = []
    for x in db.scalars(select(DesenhoAutodoc).where(DesenhoAutodoc.obsoleto.is_(False), DesenhoAutodoc.obra_id.is_not(None))):
        if x.extensao != "pdf" or not x.hash or x.hash == x.hash_baixado or x.obra_id not in obras or not x.pecas:
            continue
        tem_pend = any(_canon(p) in pend.get(x.obra_id, set()) for p in x.pecas)
        if not tem_pend and not todas:
            continue
        out.append({"id": x.id, "obra": obras[x.obra_id], "codigo": x.codigo, "url": x.url, "hash": x.hash,
                    "projeto": x.projeto, "data": x.data or datetime.min, "pend": tem_pend})
    out.sort(key=lambda d: (d["pend"], d["data"]), reverse=True)   # pendentes primeiro, as mais recentes primeiro
    return out


def marcar_baixada(db: Session, desenho_id: int, h: str) -> None:
    x = db.get(DesenhoAutodoc, desenho_id)
    if x:
        x.hash_baixado = h
