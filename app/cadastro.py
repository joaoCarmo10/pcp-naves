"""Cadastro de peças: pranchas enviadas por obra, lidas em segundo plano e conferidas com o Plannix."""
import dataclasses
import io
import re
import secrets
import statistics
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pranchas
from .db import DATA_DIR, SessionLocal
from .models import CadastroPeca, Obra, Peca
from .status import chave_natural

# andamento da leitura por obra (em memória: some se o app reiniciar, os dados já gravados ficam)
ANDAMENTO: dict[int, dict] = {}


def _canon(n: str) -> str:
    return re.sub(r"(?<=[A-Z])-(?=\d)", "", (n or "").upper())


def guardar(obra: Obra, arquivos: list[tuple[str, bytes]]) -> list[tuple[str, Path]]:
    """Salva os PDFs (soltos ou dentro de .zip) na pasta da obra. Devolve [(nome original, caminho)]."""
    pasta = DATA_DIR / "pranchas" / obra.sigla
    pasta.mkdir(parents=True, exist_ok=True)
    out = []

    def um(nome: str, dados: bytes):
        nome = Path(nome).name
        if not nome.lower().endswith(".pdf") or not dados.startswith(b"%PDF"):
            return
        destino = pasta / f"{secrets.token_hex(3)}_{re.sub(r'[^A-Za-z0-9._-]', '_', nome)[:150]}"
        destino.write_bytes(dados)
        out.append((nome, destino))

    for nome, dados in arquivos:
        if nome.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(dados)) as z:
                for n in z.namelist():
                    if n.lower().endswith(".pdf") and "__MACOSX" not in n:
                        um(n, z.read(n))
        else:
            um(nome, dados)
    return out


def _rev_num(r: str) -> int:
    m = re.search(r"(\d+)", r or "")
    return int(m.group(1)) if m else -1


def processar(obra_id: int, lote: list[tuple[str, Path]]) -> None:
    """Lê cada prancha e grava/atualiza o cadastro (roda fora da requisição)."""
    st = ANDAMENTO.setdefault(obra_id, {"total": 0, "feitos": 0, "msgs": []})
    for nome, caminho in lote:
        try:
            with SessionLocal() as db:
                _uma(db, obra_id, nome, caminho, st)
        except Exception as e:   # uma prancha ruim não para as outras
            st["msgs"].append(f"{nome}: erro na leitura ({str(e)[:120]})")
        st["feitos"] += 1


def _vols(ps: list[Peca]) -> list[float]:
    vp = [p.vol_projeto for p in ps if p.vol_projeto]
    return vp or [p.volume for p in ps if p.volume]


def etapas_da_prancha(pecas: list[Peca], nome: str, volume_un: float | None) -> tuple[str, list[str]]:
    """Em quais itens (etapas/blocos) do Plannix esta prancha se aplica: o mesmo nome pode ter medidas diferentes
    em cada bloco da obra. Casa pelo volume unitário: igual (±4%) em uma ou mais etapas; se nenhuma, a mais próxima
    (até 12%) quando ela for claramente a mais próxima, com alerta."""
    alertas = []
    grupos: dict[str, list[Peca]] = {}
    for p in pecas:
        if _canon(p.peca) == _canon(nome):
            grupos.setdefault(p.item or "", []).append(p)
    if not grupos:
        return "", alertas
    if volume_un:
        dif = {k: min((abs(v - volume_un) / v for v in _vols(ps) if v), default=None) for k, ps in grupos.items()}
        dif = {k: d for k, d in dif.items() if d is not None}
        bate = sorted((k for k, d in dif.items() if d <= 0.04), key=chave_natural)
        if bate:
            return ",".join(bate), alertas
        if dif:
            ordem = sorted(dif.items(), key=lambda kv: kv[1])
            k, d = ordem[0]
            if d <= 0.12 and (len(ordem) == 1 or ordem[1][1] >= 2 * d):
                alertas.append(f"Etapa {k or '-'} escolhida pelo volume mais próximo ({d:.0%} de diferença). Confira.")
                return k, alertas
        vs = ", ".join(f"etapa {k or '-'}: {min(_vols(ps) or [0]):.2f} m³" for k, ps in sorted(grupos.items(), key=lambda kv: chave_natural(kv[0])))
        alertas.append(f"Volume da prancha ({volume_un:.2f} m³) não bate com nenhuma etapa do Plannix ({vs}).")
    if len(grupos) == 1:
        return next(iter(grupos)), alertas
    alertas.append("Não consegui identificar a etapa/bloco do Plannix desta prancha: o nome existe em várias etapas.")
    return "?", alertas


def _uma(db: Session, obra_id: int, nome: str, caminho: Path, st: dict) -> None:
    pecas = list(db.scalars(select(Peca).where(Peca.obra_id == obra_id)))
    nomes = {p.peca for p in pecas}
    L0 = pranchas.ler(caminho, nomes)
    todas = [L0.peca, *L0.iguais] if L0.peca else [""]
    for p in todas:   # prancha de peças iguais ("P09=P19"): o mesmo cadastro para cada nome
        L = L0 if p == L0.peca else dataclasses.replace(
            L0, peca=p, alertas=[a for a in L0.alertas if "não existe nesta obra" not in a]
            + ([f"{p} não existe nesta obra no Plannix."] if p not in nomes else []))
        _gravar(db, obra_id, nome, caminho, st, L, pecas, nomes)


def _gravar(db: Session, obra_id: int, nome: str, caminho: Path, st: dict, L, pecas: list, nomes: set) -> None:
    chave = L.peca or f"?{Path(nome).stem[:36]}"
    etapas, alertas_et = etapas_da_prancha(pecas, L.peca, L.volume_un) if L.peca else ("", [])
    c = db.scalar(select(CadastroPeca).where(CadastroPeca.obra_id == obra_id, CadastroPeca.peca == chave, CadastroPeca.etapas == etapas))
    if c and _rev_num(c.revisao) > _rev_num(L.revisao) >= 0:
        st["msgs"].append(f"{nome}: {chave} já está cadastrada na {c.revisao}; a {L.revisao} enviada é mais antiga e foi ignorada.")
        caminho.unlink(missing_ok=True)
        return
    if c:
        c.historico = (c.historico or []) + [{"revisao": c.revisao, "arquivo": c.nome_arquivo, "protensao": c.protensao,
                                              "aco_passivo": c.aco_passivo, "em": c.atualizado_em.isoformat(timespec="minutes")}]
        if c.arquivo and c.arquivo != str(caminho.relative_to(DATA_DIR)):
            (DATA_DIR / c.arquivo).unlink(missing_ok=True)
    else:
        c = CadastroPeca(obra_id=obra_id, peca=chave, etapas=etapas)
        db.add(c)
    r = pranchas.resumo(L.itens)
    alertas = list(L.alertas) + alertas_et
    c.prancha, c.revisao, c.fonte = L.prancha, L.revisao, L.fonte
    c.arquivo, c.nome_arquivo = str(caminho.relative_to(DATA_DIR)), nome[:200]
    c.quantidade, c.volume_un, c.peso, c.area = L.quantidade, L.volume_un, L.peso, L.area
    c.fck, c.fcj, c.cobrimento, c.contraflecha = L.fck, L.fcj, L.cobrimento, L.contraflecha
    c.aco_passivo = r["aco_passivo"] or None
    c.aco_ativo = L.aco_ativo or (round(sum(i["peso_tot"] or 0 for i in L.itens if i["tipo"] == "ativa"), 2) or None)
    c.cordoalhas, c.cord_diam, c.forca = (r["cordoalhas"] or None), r["cord_diam"], r["forca"]
    c.icamento, c.neoprene, c.bitolas, c.itens = r["icamento"][:120], (L.neoprene or "")[:120], r["bitolas"], L.itens
    c.alertas = alertas
    c.etapa2_indicios = list(L.etapa2)
    c.situacao = "revisar" if alertas else "ok"
    if not L.peca:
        st["msgs"].append(f"{nome}: não achei o nome da peça na prancha. Ficou em 'Pranchas sem peça no Plannix' para corrigir.")
    elif L.peca not in nomes:
        st["msgs"].append(f"{nome}: {L.peca} não existe nesta obra no Plannix. Confira se escolheu a obra certa.")
    else:
        st["msgs"].append(f"{nome}: {L.peca} cadastrada{f' (etapa {etapas})' if etapas and etapas != '?' else ''}"
                          f"{', com alertas para revisar' if alertas else ''}{', 2ª etapa' if L.etapa2 else ''}.")
    c.atualizado_em = datetime.now()
    db.commit()


def indice(db: Session, obra_ids=None) -> dict[tuple[int, str], list[CadastroPeca]]:
    """Cadastros por (obra_id, nome canônico); cada um vale para as etapas listadas."""
    q = select(CadastroPeca)
    if obra_ids:
        q = q.where(CadastroPeca.obra_id.in_(list(obra_ids)))
    out: dict[tuple[int, str], list[CadastroPeca]] = {}
    for c in db.scalars(q):
        out.setdefault((c.obra_id, _canon(c.peca)), []).append(c)
    return out


def de_peca(idx: dict, p: Peca) -> CadastroPeca | None:
    """Cadastro que vale para esta unidade do Plannix (mesma obra, mesmo nome, etapa coberta pela prancha)."""
    cs = idx.get((p.obra_id, _canon(p.peca)))
    if not cs:
        return None
    for c in cs:
        if p.item and p.item in (c.etapas or "").split(","):
            return c
    soltos = [c for c in cs if c.etapas in ("", "?")]
    return soltos[0] if len(cs) == 1 and soltos else None
