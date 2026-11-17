"""Cadastro de fôrmas: carga inicial com as fôrmas usadas na programação e montagem do layout esquemático."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Forma

NAVES = {"N1": "Nave 1", "N2": "Nave 2", "N3": "Nave 3"}
TIPOS = {"linear": "Linear (comprimento)", "unidade": "Por unidade", "pista": "Pista de protensão"}
TIPO_CURTO = {"linear": "Linear", "unidade": "Unidade", "pista": "Pista"}
SITUACOES = {"ativa": "Ativa", "manutencao": "Em manutenção", "desativada": "Desativada"}
LARG_BARRA = 1.6   # largura da barra no desenho (m)
UNID_COMP = 8.0    # comprimento desenhado de fôrma por unidade (m)
UNID_ALT = 2.8     # altura desenhada de fôrma por unidade (m)

# (código, nave, nome, tipo, seção, aceitas, comprimento, capacidade, ritmo, início, obs)
INICIAIS = [
    ("N1F1", "N1", "F1 30x80", "linear", "30x80", "", 30, 1, 1, None, ""),
    ("N1F2", "N1", "F2 80x60", "linear", "80x60", "", 80, 1, 1, None, ""),
    ("N1F3", "N1", "F3 50x40", "linear", "50x40", "", 12, 1, 1, None, "Peça 40x50 não entra (orientação b x h)"),
    ("N1F4", "N1", "F4 40x40", "linear", "40x40", "30x40", 16, 1, 1, None, ""),
    ("N1F5", "N1", "F5 40x40", "linear", "40x40", "30x40", 21, 1, 1, None, "Ampliada de 17 para 21 m"),
    ("N1F6", "N1", "F6 50x50", "unidade", "50x50", "40x60, 40x50", None, 1, 1, None, "Fôrma dupla com a F7"),
    ("N1F7", "N1", "F7 50x50", "unidade", "50x50", "40x60, 40x50", None, 1, 1, None, "Fôrma dupla com a F6"),
    ("N2P1", "N2", "PISTA P1", "pista", "", "", 130, 1, 1, None, "Lajes alveolares"),
    ("N2P2", "N2", "PISTA P2", "pista", "", "", 103, 1, 1, None, "Lajes alveolares"),
    ("N3F1", "N3", "F1 20x60", "linear", "20x60", "", 18, 1, 1, None, ""),
    ("N3F2", "N3", "F2 20x80", "linear", "20x80", "", 12, 1, 1, None, ""),
    ("N3F3", "N3", "F3 20x95", "linear", "20x95", "", 6, 1, 1, None, ""),
    ("N3F4", "N3", "F4 60x60", "linear", "60x60", "", 18, 1, 1, None, ""),
    ("N3F5", "N3", "F5 25x74", "linear", "25x74", "", 12, 1, 1, None, ""),
    ("N3F6", "N3", "F6 80x40", "linear", "80x40", "", 18, 1, 1, None, "40 de altura x 80 de base"),
    ("N3F7", "N3", "F7 15x45", "linear", "15x45", "", 12, 1, 1, None, ""),
    ("N3F8", "N3", "F8 15x60", "linear", "15x60", "", 18, 1, 1, None, ""),
    ("N3F9", "N3", "F9 20x45", "linear", "20x45", "", 9, 1, 1, None, ""),
    ("N3F10", "N3", "F10 20x40", "linear", "20x40", "", 6, 1, 1, None, ""),
    ("N3F11", "N3", "F11 30x50", "linear", "30x50", "", 16, 1, 1, None, ""),
    ("N3F12", "N3", "F12 50x50", "linear", "50x50", "", 32, 1, 1, None, ""),
    ("N3F13", "N3", "F13 30x50", "linear", "30x50", "", 10, 1, 1, None, ""),
    ("N3F14", "N3", "F14 30x40", "linear", "30x40", "", 12, 1, 1, None, ""),
    ("N3F40x70", "N3", "40x70", "unidade", "40x70", "", None, 1, 1, None, ""),
    ("N3VL", "N3", "VL 30x43x87", "unidade", "30x43x87", "", None, 1, 1, None, ""),
    ("N3ESC1", "N3", "ESCADA 1", "unidade", "", "", None, 1, 1, None, ""),
    ("N3ESC2", "N3", "ESCADA 2", "unidade", "", "", None, 1, 2, date(2026, 11, 18), "Uma a cada dois dias"),
    ("N3LAJE", "N3", "LAJE (MADEIRA)", "unidade", "", "", None, 2, 1, date(2026, 11, 17), "Lajes Hot Park"),
    ("N3NJ", "N3", "NEW JERSEY", "unidade", "38x80x180", "", None, 13, 1, None, ""),
    ("N3SAPATA", "N3", "SAPATA", "unidade", "", "", None, 1, 1, None, ""),
]


# seções que cada fôrma já produziu nas programações (rev5 e Rev07) e restrição por produto
COMPLEMENTO = {
    "N1F1": ("", "30x60", ""), "N1F2": ("", "40x60", ""), "N3F1": ("", "20x50", ""), "N3F2": ("", "20x70", ""),
    "N3F3": ("", "20x87", ""), "N3F4": ("", "40x60", ""), "N3F6": ("", "40x80", ""),
    "N2P1": ("21x125", "", "LAJE"), "N2P2": ("21x125", "", "LAJE"),
    "N3ESC1": ("", "", "ESCADA"), "N3ESC2": ("", "", "ESCADA"), "N3LAJE": ("VARIAVEL", "", "LAJE"),
    "N3SAPATA": ("", "", "SAPATA"), "N3NJ": ("", "", "NEW JERSEY"),
}


def completar(db: Session) -> None:
    """Preenche seção, adaptações e produtos das fôrmas da carga inicial, só onde o campo ainda está vazio."""
    for f in db.scalars(select(Forma).where(Forma.codigo.in_(list(COMPLEMENTO)))):
        sec, aceitas, prods = COMPLEMENTO[f.codigo]
        if sec and not f.secao:
            f.secao = sec
        if aceitas and not f.secoes_aceitas:
            f.secoes_aceitas = aceitas
        if prods and not f.produtos:
            f.produtos = prods
    db.commit()


def aceita(f: Forma, produto: str, secao: str, comprimento: float | None) -> bool:
    """A peça cabe na fôrma? Produto (se a fôrma restringe), seção (nome ou adaptação) e comprimento útil."""
    prods = {_norm(x) for x in (f.produtos or "").split(",") if _norm(x)}
    if prods and _norm(produto) not in prods:
        return False
    secs = secoes(f)
    if secs and _norm(secao) not in secs:
        return False
    if not prods and not secs:
        return False
    if f.tipo != "unidade" and f.comprimento and (comprimento or 0) > f.comprimento:
        return False
    return True


def tamanho(f: Forma) -> float:
    return f.comprimento if f.tipo in ("linear", "pista") and f.comprimento else UNID_COMP


def altura(f: Forma) -> float:
    return UNID_ALT if f.tipo == "unidade" else LARG_BARRA


def carga_inicial(db: Session) -> int:
    """Cria as fôrmas atuais se o cadastro estiver vazio, já posicionadas: lineares uma por linha e unitárias em grade."""
    if db.scalar(select(Forma.id).limit(1)):
        return 0
    por_nave: dict[str, list] = {}
    for t in INICIAIS:
        por_nave.setdefault(t[1], []).append(t)
    n = 0
    for nave, itens in por_nave.items():
        lineares = [t for t in itens if t[3] != "unidade"]
        unitarias = [t for t in itens if t[3] == "unidade"]
        maior = max([t[6] or 0 for t in lineares] + [0])
        colunas = 2 if maior <= 35 and len(lineares) > 6 else 1   # fôrmas curtas e muitas: duas fileiras lado a lado
        por_col = -(-len(lineares) // colunas) if lineares else 0
        for k, t in enumerate(lineares):
            c, r = divmod(k, por_col)
            db.add(_forma(t, x=2.0 + c * (maior + 6), y=2.0 + r * 3.0))
            n += 1
        y0 = 2.0 + por_col * 3.0 + 1
        for k, t in enumerate(unitarias):
            db.add(_forma(t, x=2.0 + (k % 6) * (UNID_COMP + 1.5), y=y0 + (k // 6) * (UNID_ALT + 1.2)))
            n += 1
    db.commit()
    return n


def _forma(t, x: float, y: float) -> Forma:
    cod, nave, nome, tipo, sec, aceitas, comp, cap, ritmo, ini, obs = t
    return Forma(codigo=cod, nave=nave, nome=nome, tipo=tipo, secao=sec, secoes_aceitas=aceitas, comprimento=comp,
                 capacidade=cap, ritmo_dias=ritmo, inicio=ini, obs=obs, x=x, y=y)


def vigentes(db: Session, d: date) -> list[Forma]:
    return [f for f in db.scalars(select(Forma).order_by(Forma.nave, Forma.y, Forma.x)) if f.vigente(d)]


def dimensoes_nave(formas: list[Forma]) -> tuple[float, float]:
    """Tamanho do desenho da nave (m): cresce com as fôrmas, mínimo 40 x 12."""
    w = max([f.x + tamanho(f) for f in formas] + [40]) + 2
    h = max([f.y + altura(f) for f in formas] + [12]) + 2
    return w, h


def capacidade_dia(formas: list[Forma], d: date) -> dict:
    """Resumo do que a nave consegue produzir no dia: metros lineares e peças por unidade (respeitando o ritmo)."""
    out = {"metros": 0.0, "pecas": 0.0, "formas": 0}
    for f in formas:
        if f.situacao != "ativa":
            continue
        if f.ritmo_dias > 1 and f.inicio and (d - f.inicio).days % f.ritmo_dias:
            continue
        out["formas"] += 1
        if f.tipo == "unidade":
            out["pecas"] += f.capacidade
        else:
            out["metros"] += (f.comprimento or 0) * f.capacidade
    return out


def _norm(s: str) -> str:
    return (s or "").upper().replace("×", "X").replace(" ", "")


def secoes(f: Forma) -> set[str]:
    return {_norm(s) for s in [f.secao] + (f.secoes_aceitas or "").split(",") if _norm(s)}


def item_e_da_forma(forma_txt: str, f: Forma) -> bool:
    """A programação guarda a fôrma como texto ('N1 - F2 80x60 (80m)'); casa pelo prefixo nave + nome."""
    t = _norm(forma_txt)
    alvo = _norm(f"{f.nave}-{f.nome}")
    return t == alvo or t.startswith(alvo + "(") or t.startswith(alvo) and not t[len(alvo):len(alvo) + 1].isdigit()
