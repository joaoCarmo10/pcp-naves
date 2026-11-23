"""Robô do Autodoc: entra com um usuário do Autodoc (variáveis AUTODOC_EMAIL e AUTODOC_SENHA no Railway), percorre
'1-OBRAS ATIVAS' e atualiza status/revisão dos desenhos no app. Baixa as pranchas novas ou alteradas de peças que
ainda têm unidades pendentes e manda para o leitor de pranchas.

Não é uma integração oficial: usa o mesmo serviço que o site do Autodoc usa. Se o site mudar, o robô avisa e para.
Login: AWS Cognito com SRP (o mesmo protocolo do site; a senha nunca trafega). Depois do primeiro login, o robô
usa o token de renovação guardado no banco e só volta a usar a senha quando ele expira.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

REGIAO = "us-east-1"
POOL = os.environ.get("AUTODOC_POOL", "us-east-1_wzg5eXXLU")
CLIENTE_APP = os.environ.get("AUTODOC_CLIENT_ID", "2qepdfbpe43utn1mkvbakdu78e")
CLIENTE_CIVIL = os.environ.get("AUTODOC_CUSTOMER_ID", "66047f7eb7e6b6d2570499ab")
PASTA_RAIZ = os.environ.get("AUTODOC_PASTA_RAIZ", "6918bc4ea5122aea6f0f2fc3")   # 1-OBRAS ATIVAS
BFF = "https://projetos-ng-bff.autodoc.com.br/v1/"
LINK_PASTA = "https://plataforma.autodoc.com.br/projetos/directories/folder/"
HORARIOS = [(6, 10), (12, 10)]          # horário da Bahia
MAX_PRANCHAS_POR_RODADA = int(os.environ.get("AUTODOC_MAX_PRANCHAS", "60"))
BAHIA = ZoneInfo("America/Bahia")

ESTADO = {"rodando": False, "ultima": None, "msg": "", "erro": "", "pastas": 0, "arquivos": 0, "pranchas": 0}
_trava = threading.Lock()


class ErroAutodoc(Exception):
    pass


def configurado() -> bool:
    return bool(os.environ.get("AUTODOC_EMAIL") and os.environ.get("AUTODOC_SENHA"))


# ------------------------------------------------------------------ HTTP

def _req(url, dados=None, cab=None, metodo=None, timeout=60, bruto=False):
    req = urllib.request.Request(url, data=dados, headers=cab or {}, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            corpo = r.read()
            return corpo if bruto else (json.loads(corpo) if corpo else {})
    except urllib.error.HTTPError as e:
        texto = e.read().decode("utf-8", "replace")[:300]
        raise ErroAutodoc(f"{e.code} em {url.split('?')[0]}: {texto}") from None
    except urllib.error.URLError as e:
        raise ErroAutodoc(f"sem conexão com {url.split('/')[2]}: {e.reason}") from None


def _cognito(acao: str, corpo: dict) -> dict:
    return _req(f"https://cognito-idp.{REGIAO}.amazonaws.com/", json.dumps(corpo).encode(),
                {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": f"AWSCognitoIdentityProviderService.{acao}"}, "POST")


# ------------------------------------------------------------------ SRP (AWS Cognito)

N_HEX = ("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
         "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
         "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
         "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
         "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
         "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
         "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
         "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF")
G_HEX = "2"
N = int(N_HEX, 16)
G = int(G_HEX, 16)


def _hash(b: bytes) -> str:
    v = hashlib.sha256(b).hexdigest()
    return "0" * (64 - len(v)) + v


def _hex_hash(h: str) -> str:
    return _hash(bytes.fromhex(h))


def _pad(v) -> str:
    h = v if isinstance(v, str) else f"{v:x}"
    if len(h) % 2 == 1:
        h = "0" + h
    elif h[0] in "89abcdefABCDEF":
        h = "00" + h
    return h


K = int(_hex_hash("00" + N_HEX + "0" + G_HEX), 16)


def _hkdf(ikm: bytes, salt: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, b"Caldera Derived Key" + bytes([1]), hashlib.sha256).digest()[:16]


class _SRP:
    def __init__(self, usuario: str, senha: str):
        self.usuario, self.senha = usuario, senha
        self.a = int.from_bytes(os.urandom(128), "big") % N
        self.A = pow(G, self.a, N)
        if self.A % N == 0:
            raise ErroAutodoc("SRP: valor A inválido")

    def chave(self, user_id: str, B: int, salt_hex: str) -> bytes:
        u = int(_hex_hash(_pad(self.A) + _pad(B)), 16)
        if u == 0:
            raise ErroAutodoc("SRP: u = 0")
        up = _hash(f"{POOL.split('_')[1]}{user_id}:{self.senha}".encode())
        x = int(_hex_hash(_pad(salt_hex) + up), 16)
        s = pow(B - K * pow(G, x, N), self.a + u * x, N)
        return _hkdf(bytes.fromhex(_pad(s)), bytes.fromhex(_pad(f"{u:x}")))

    def resposta(self, desafio: dict) -> dict:
        p = desafio["ChallengeParameters"]
        user_id = p["USER_ID_FOR_SRP"]
        agora = datetime.utcnow()
        ts = agora.strftime("%a %b ") + str(agora.day) + agora.strftime(" %H:%M:%S UTC %Y")
        chave = self.chave(user_id, int(p["SRP_B"], 16), p["SALT"])
        msg = POOL.split("_")[1].encode() + user_id.encode() + base64.b64decode(p["SECRET_BLOCK"]) + ts.encode()
        assinatura = base64.b64encode(hmac.new(chave, msg, hashlib.sha256).digest()).decode()
        return {"TIMESTAMP": ts, "USERNAME": user_id, "PASSWORD_CLAIM_SECRET_BLOCK": p["SECRET_BLOCK"],
                "PASSWORD_CLAIM_SIGNATURE": assinatura}


def login_senha(email: str, senha: str) -> dict:
    srp = _SRP(email, senha)
    d = _cognito("InitiateAuth", {"AuthFlow": "USER_SRP_AUTH", "ClientId": CLIENTE_APP,
                                  "AuthParameters": {"USERNAME": email, "SRP_A": f"{srp.A:x}"}})
    if d.get("ChallengeName") != "PASSWORD_VERIFIER":
        raise ErroAutodoc(f"Autodoc pediu um passo de login inesperado: {d.get('ChallengeName')}")
    r = _cognito("RespondToAuthChallenge", {"ChallengeName": "PASSWORD_VERIFIER", "ClientId": CLIENTE_APP,
                                            "ChallengeResponses": srp.resposta(d)})
    if "AuthenticationResult" not in r:
        raise ErroAutodoc(f"Login exige um passo extra ({r.get('ChallengeName')}), por exemplo troca de senha ou código. "
                          "Entre uma vez no site com esse usuário e resolva.")
    return r["AuthenticationResult"]


def login_renovacao(refresh: str) -> dict:
    r = _cognito("InitiateAuth", {"AuthFlow": "REFRESH_TOKEN_AUTH", "ClientId": CLIENTE_APP,
                                  "AuthParameters": {"REFRESH_TOKEN": refresh}})
    return r["AuthenticationResult"]


# ------------------------------------------------------------------ sessão

class Sessao:
    def __init__(self, db):
        from .main import _cfg, _cfg_grava   # evita import circular no carregamento
        self.db, self._cfg, self._grava = db, _cfg, _cfg_grava
        self.tok = None

    def entrar(self):
        refresh = self._cfg(self.db, "autodoc_refresh")
        if refresh:
            try:
                self.tok = login_renovacao(refresh)
                return
            except ErroAutodoc:
                pass
        if not configurado():
            raise ErroAutodoc("Faltam as variáveis AUTODOC_EMAIL e AUTODOC_SENHA no Railway.")
        self.tok = login_senha(os.environ["AUTODOC_EMAIL"].strip(), os.environ["AUTODOC_SENHA"])
        if self.tok.get("RefreshToken"):
            self._grava(self.db, "autodoc_refresh", self.tok["RefreshToken"][:4000])
            self.db.commit()

    def _cab(self):
        return {"Authorization": f"Bearer {self.tok['IdToken']}", "X-Customer-Id": CLIENTE_CIVIL,
                "X-Product-value": "projetos", "Accept": "application/json"}

    def get(self, caminho: str):
        for tentativa in range(3):
            try:
                return _req(BFF + caminho, cab=self._cab())
            except ErroAutodoc as e:
                if str(e).startswith("401") and tentativa == 0:
                    self._grava(self.db, "autodoc_refresh", "")
                    self.entrar()
                    continue
                if tentativa == 2 or str(e).startswith(("401", "403", "404")):
                    raise
                time.sleep(2 * (tentativa + 1))

    def baixar(self, url: str) -> bytes:
        cookie = f"idToken={self.tok['IdToken']}; accessToken={self.tok['AccessToken']}"
        return _req(url, cab={"Cookie": cookie}, timeout=120, bruto=True)


# ------------------------------------------------------------------ varredura

def varrer(s: Sessao, raiz: str = PASTA_RAIZ) -> tuple[list[dict], dict, int]:
    """Percorre a árvore a partir da raiz. Devolve (documentos, nomes de status, nº de pastas)."""
    st = {str(x["id"]): x["name"] for x in (s.get("status") or {}).get("data", [])}
    docs, fila, pastas = [], [(raiz, None)], 0
    while fila:
        fid, projeto = fila.pop(0)
        j = s.get(f"folders/{fid}/subitems/light") or {}
        pastas += 1
        atual = j.get("currentFolder") or {}
        if atual.get("type") == "PROJECT":
            projeto = atual.get("name")
        for sub in j.get("subFolders") or []:
            fila.append((sub.get("id") or sub.get("_id"), projeto))
        for d in j.get("documents") or []:
            d["_projeto"] = projeto or ""
            docs.append(d)
        time.sleep(0.15)   # gentil com o servidor deles
    return docs, st, pastas


def para_linhas(docs: list[dict], st: dict) -> list[dict]:
    """Mesmo formato do relatório 'Exportar' (o importador do app é um só)."""
    out = []
    for d in docs:
        nome = d.get("name") or ""
        caminho = d.get("path") or ""
        out.append({"Código": nome, "Extensão": nome.rsplit(".", 1)[-1] if "." in nome else "", "Revisão": d.get("revision") or "",
                    "Título": d.get("description") or "", "Data": (d.get("status_updated_at") or d.get("updated_at") or "")[:16],
                    "Status": st.get(str(d.get("status")), str(d.get("status") or "")),
                    "Obsoleto": "Sim" if str(d.get("obsolete")).lower() == "true" else "Não",
                    "Pasta": caminho.rsplit("/", 1)[0] + "/" if "/" in caminho else "",
                    "Link da Pasta": LINK_PASTA + str(d.get("folder_id") or ""), "Projeto": d.get("_projeto") or "",
                    "Observações": d.get("observations") or "", "_url": d.get("fileUrl") or "", "_hash": d.get("hash") or ""})
    return out


def rodar(forcar: bool = False) -> dict:
    """Uma rodada completa. Seguro chamar de mais de um lugar: só uma roda por vez."""
    if not _trava.acquire(blocking=False):
        return ESTADO
    from . import autodoc
    from . import cadastro as cad
    from .db import SessionLocal
    ESTADO.update(rodando=True, erro="", msg="Entrando no Autodoc…")
    t0 = time.time()
    try:
        with SessionLocal() as db:
            s = Sessao(db)
            s.entrar()
            ESTADO["msg"] = "Lendo as pastas das obras ativas…"
            docs, st, pastas = varrer(s)
            linhas = para_linhas(docs, st)
            r = autodoc.importar_linhas(db, linhas)
            novas = autodoc.pranchas_para_baixar(db, linhas)[:MAX_PRANCHAS_POR_RODADA]
            baixadas = 0
            por_obra: dict = {}
            for x in novas:
                try:
                    dados = s.baixar(x["url"])
                except ErroAutodoc as e:
                    ESTADO["erro"] = f"Download de {x['codigo']}: {e}"
                    continue
                lote = cad.guardar(x["obra"], [(x["codigo"], dados)])
                if lote:
                    por_obra.setdefault(x["obra"].id, []).extend(lote)
                    autodoc.marcar_baixada(db, x["id"], x["hash"])
                    baixadas += 1
            db.commit()
            ESTADO.update(pastas=pastas, arquivos=len(linhas), pranchas=baixadas)
            ESTADO["msg"] = (f"{len(linhas)} arquivos em {pastas} pastas ({r['novos']} novos, {r['removidos']} removidos), "
                             f"{len(r['bloqueadas'])} peças com desenho bloqueado, {baixadas} pranchas novas para leitura. "
                             f"{time.time() - t0:.0f} s.")
            from .main import _cfg_grava
            _cfg_grava(db, "autodoc_ultima", datetime.now(BAHIA).replace(tzinfo=None).isoformat(timespec="seconds"))
            _cfg_grava(db, "autodoc_msg", ESTADO["msg"][:480])
            _cfg_grava(db, "autodoc_erro", ESTADO["erro"][:480])
            db.commit()
        for oid, lote in por_obra.items():   # leitura das pranchas fica na fila do OCR, uma por vez
            stt = cad.ANDAMENTO.setdefault(oid, {"total": 0, "feitos": 0, "msgs": [], "em": datetime.now()})
            stt["total"] += len(lote)
            from .main import _ocr_fila
            _ocr_fila.submit(cad.processar, oid, lote)
    except Exception as e:   # noqa: BLE001  o robô nunca derruba o app
        ESTADO["erro"] = str(e)[:400]
        ESTADO["msg"] = "Falhou."
        try:
            from .db import SessionLocal
            from .main import _cfg_grava
            with SessionLocal() as db:
                _cfg_grava(db, "autodoc_erro", ESTADO["erro"])
                _cfg_grava(db, "autodoc_tentativa", datetime.now(BAHIA).replace(tzinfo=None).isoformat(timespec="seconds"))
                db.commit()
        except Exception:   # noqa: BLE001
            pass
    finally:
        ESTADO["rodando"] = False
        ESTADO["ultima"] = datetime.now(BAHIA)
        _trava.release()
    return ESTADO


def _agenda():
    """Roda nos horários fixos (Bahia). Uma vez por horário por dia."""
    feitos = set()
    while True:
        try:
            agora = datetime.now(BAHIA)
            for h, m in HORARIOS:
                chave = (agora.date(), h)
                alvo = agora.replace(hour=h, minute=m, second=0, microsecond=0)
                if configurado() and chave not in feitos and alvo <= agora < alvo + timedelta(minutes=50):
                    feitos.add(chave)
                    rodar()
        except Exception:   # noqa: BLE001
            pass
        time.sleep(300)


def iniciar_agenda():
    if os.environ.get("AUTODOC_DESLIGADO") or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    threading.Thread(target=_agenda, daemon=True, name="autodoc").start()
