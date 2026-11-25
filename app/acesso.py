"""Usuários, perfis e permissões por módulo (nenhum, ver, editar). As obras são visíveis para todos;
o controle é só por módulo do app."""
import hashlib
import hmac
import os
import secrets
import string
from collections import defaultdict
from time import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Perfil, Usuario

NIVEIS = {"nenhum": 0, "ver": 1, "editar": 2}

# módulo: (nome na tela, prefixos de endereço)
MODULOS = {
    "obras": ("Avanço das obras, plantas, peças e dashboard", ("/obras", "/plantas", "/cadastro", "/pecas", "/dashboard", "/ficha")),
    "realizado": ("Realizado do dia", ("/programacao", "/apontamentos")),
    "pxr": ("Programado x realizado", ("/pxr",)),
    "planejar": ("Planejar", ("/planejar",)),
    "armacao": ("Armação", ("/armacao",)),
    "formas": ("Fôrmas e naves", ("/formas",)),
    "importar": ("Importar dados", ("/importar",)),
    "usuarios": ("Usuários e perfis", ("/usuarios", "/perfis")),
}

PERFIS_PADRAO = {
    "Administrador": {m: "editar" for m in MODULOS},
    "PCP": {**{m: "editar" for m in MODULOS}, "usuarios": "nenhum"},
    "Gerência": {**{m: "ver" for m in MODULOS}, "importar": "nenhum", "usuarios": "nenhum"},
    "Produção": {**{m: "ver" for m in MODULOS}, "armacao": "editar", "importar": "nenhum", "usuarios": "nenhum"},
    "Logística": {**{m: "nenhum" for m in MODULOS}, "obras": "ver"},
}

LIVRES = ("/static", "/login", "/saude", "/armador/", "/sair", "/conta")


def modulo_do_caminho(caminho: str) -> str | None:
    if caminho == "/":
        return "obras"
    for m, (_, prefixos) in MODULOS.items():
        if any(caminho == p or caminho.startswith(p + "/") or caminho.startswith(p + "?") for p in prefixos):
            return m
    return None


# ---------------------------------------------------------------- senhas
def gerar_hash(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, 200_000)
    return f"pbkdf2$200000${sal.hex()}${h.hex()}"


def confere(senha: str, guardado: str) -> bool:
    try:
        _, it, sal, h = guardado.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, AttributeError):
        return False


def senha_provisoria() -> str:
    alf = string.ascii_letters + string.digits
    alf = "".join(c for c in alf if c not in "0O1lI")
    return "".join(secrets.choice(alf) for _ in range(10))


# ---------------------------------------------------------------- carga inicial
def carga_inicial(db: Session) -> None:
    """Cria os perfis padrão e, sem nenhum usuário, o administrador com a senha atual do app (APP_PASSWORD)."""
    existentes = {p.nome for p in db.scalars(select(Perfil))}
    for nome, perms in PERFIS_PADRAO.items():
        if nome not in existentes:
            db.add(Perfil(nome=nome, permissoes=perms, sistema=(nome == "Administrador")))
    db.commit()
    # perfis antigos ganham módulos novos como "nenhum" (admin sempre tudo)
    for p in db.scalars(select(Perfil)):
        faltam = {m: ("editar" if p.sistema else "nenhum") for m in MODULOS if m not in (p.permissoes or {})}
        if faltam:
            p.permissoes = {**(p.permissoes or {}), **faltam}
    db.commit()
    senha = os.getenv("APP_PASSWORD", "")
    if senha and not db.scalar(select(Usuario.id).limit(1)):
        adm = db.scalar(select(Perfil).where(Perfil.sistema.is_(True)))
        db.add(Usuario(login=os.getenv("APP_ADMIN", "joao").lower(), nome="Administrador", senha_hash=gerar_hash(senha),
                       perfil_id=adm.id, trocar_senha=False))
        db.commit()


def nivel(usuario: Usuario | None, modulo: str | None) -> int:
    if modulo is None:
        return NIVEIS["editar"]
    if usuario is None:
        return NIVEIS["editar"]   # app sem senha (uso local)
    if usuario.perfil.sistema:
        return NIVEIS["editar"]
    return NIVEIS.get((usuario.perfil.permissoes or {}).get(modulo, "nenhum"), 0)


# ---------------------------------------------------------------- tentativas de login
_falhas: dict[str, list[float]] = defaultdict(list)


def bloqueado(chave: str) -> bool:
    agora = time()
    _falhas[chave] = [t for t in _falhas[chave] if agora - t < 900]
    return len(_falhas[chave]) >= 8


def falhou(chave: str) -> None:
    _falhas[chave].append(time())


def autenticar(db: Session, login: str, senha: str) -> Usuario | None:
    login = (login or "").strip().lower()
    if not login:
        # compatibilidade: só a senha do app entra como administrador inicial
        u = db.scalar(select(Usuario).where(Usuario.login == os.getenv("APP_ADMIN", "joao").lower()))
        app_senha = os.getenv("APP_PASSWORD", "")
        return u if u and u.ativo and app_senha and hmac.compare_digest(senha, app_senha) and confere(senha, u.senha_hash) else None
    u = db.scalar(select(Usuario).where(Usuario.login == login))
    if u and u.ativo and confere(senha, u.senha_hash):
        return u
    return None
