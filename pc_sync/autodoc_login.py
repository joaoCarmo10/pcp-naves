"""Login no Autodoc (AWS Cognito, SRP: a senha não trafega) e acesso ao mesmo serviço que o site usa.
Cópia do robô do app (app/autodoc_robo.py), sem dependências além da biblioteca padrão do Python."""
import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

REGIAO = "us-east-1"
POOL = os.environ.get("AUTODOC_POOL", "us-east-1_wzg5eXXLU")
CLIENTE_APP = os.environ.get("AUTODOC_CLIENT_ID", "2qepdfbpe43utn1mkvbakdu78e")
CLIENTE_CIVIL = os.environ.get("AUTODOC_CUSTOMER_ID", "66047f7eb7e6b6d2570499ab")
PASTA_RAIZ = os.environ.get("AUTODOC_PASTA_RAIZ", "6918bc4ea5122aea6f0f2fc3")   # 1-OBRAS ATIVAS
BFF = "https://projetos-ng-bff.autodoc.com.br/v1/"
LINK_PASTA = "https://plataforma.autodoc.com.br/projetos/directories/folder/"



class ErroAutodoc(Exception):
    pass


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


