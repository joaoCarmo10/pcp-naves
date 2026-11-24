"""PCP Naves - sincronização do PC da fábrica (Autodoc + Plannix), de hora em hora.

Roda escondido pelo Agendador de Tarefas do Windows (instalar.bat). Cada rodada:
  1. Autodoc: entra com o usuário do ini (só na 1ª vez usa a senha; depois renova o acesso), lê as pastas das obras
     ATIVAS no app e manda os status dos desenhos SÓ SE algo mudou desde a última leitura.
  2. Plannix: modo "sql" (lê o banco do Plannix, somente leitura) ou modo "pasta" (manda o 'all data' que você
     exportar para a pasta vigiada). Também só envia quando o conteúdo muda.
Fora do horário configurado não faz nada. Duas rodadas nunca rodam juntas.

Comandos:
    py pcp_sync.py rodar        o que o agendador chama
    py pcp_sync.py testar       confere ini, login no Autodoc, acesso ao app e ao Plannix (não grava nada no app)
    py pcp_sync.py descobrir    (modo sql) mapeia o banco do Plannix -> plannix_esquema.txt, para montar a consulta
    py pcp_sync.py status       mostra o que foi enviado por último
"""
import argparse
import configparser
import datetime as dt
import fnmatch
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
INI = AQUI / "pcp_sync.ini"
LOG = AQUI / "pcp_sync.log"
ESTADO = AQUI / ".estado.json"
TRAVA = AQUI / ".rodando"
SESSAO_AD = AQUI / ".autodoc_sessao.json"
LINK_PASTA = "https://plataforma.autodoc.com.br/projetos/directories/folder/"
BATIMENTO_H = 6          # sem mudança, avisa o app que está vivo a cada 6 h (uma chamada pequena)


# ---------------------------------------------------------------- base

def log(msg: str) -> None:
    linha = f"{dt.datetime.now():%d/%m/%Y %H:%M:%S}  {msg}"
    print(linha, flush=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:       # log não cresce sem fim
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def config() -> configparser.ConfigParser:
    if not INI.exists():
        sys.exit(f"Falta {INI.name}. Copie pcp_sync.exemplo.ini para {INI.name} e preencha (o instalar.bat faz isso).")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(INI, encoding="utf-8")
    return cp


def estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_estado(e: dict) -> None:
    ESTADO.write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")


def app_req(cp, caminho: str, corpo: bytes | None = None, tipo: str = "application/json", timeout=180):
    url = cp["app"]["url"].rstrip("/") + caminho
    cab = {"Authorization": f"Bearer {cp['app']['token'].strip()}", "User-Agent": "pcp-sync", "Accept": "application/json"}
    if corpo is not None:
        cab.update({"Content-Type": tipo, "Content-Encoding": "gzip"})
        corpo = gzip.compress(corpo)
    req = urllib.request.Request(url, data=corpo, headers=cab, method="POST" if corpo is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"app recusou ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"sem conexão com o app: {e.reason}") from None


def no_horario(cp) -> bool:
    g = cp["geral"] if cp.has_section("geral") else {}
    ini, fim = int(g.get("hora_inicio", 6)), int(g.get("hora_fim", 20))
    h = dt.datetime.now().hour
    dias = g.get("dias", "seg-sab")
    if dias == "seg-sab" and dt.date.today().weekday() == 6:
        return False
    return ini <= h <= fim


# ---------------------------------------------------------------- Autodoc

class Autodoc:
    def __init__(self, cp):
        import autodoc_login as al
        self.al, self.cp, self.tok = al, cp, None

    def entrar(self):
        al = self.al
        try:
            s = json.loads(SESSAO_AD.read_text())
            self.tok = al.login_renovacao(s["refresh"])
            return
        except Exception:   # noqa: BLE001  sem sessão guardada ou expirada: usa a senha
            pass
        a = self.cp["autodoc"]
        self.tok = al.login_senha(a["email"].strip(), a["senha"])
        if self.tok.get("RefreshToken"):
            SESSAO_AD.write_text(json.dumps({"refresh": self.tok["RefreshToken"]}))
            try:
                os.chmod(SESSAO_AD, 0o600)
            except OSError:
                pass

    def get(self, caminho: str):
        al = self.al
        cab = {"Authorization": f"Bearer {self.tok['IdToken']}", "X-Customer-Id": al.CLIENTE_CIVIL,
               "X-Product-value": "projetos", "Accept": "application/json"}
        for t in range(3):
            try:
                return al._req(al.BFF + caminho, cab=cab)
            except al.ErroAutodoc as e:
                if str(e).startswith("401") and t == 0:
                    SESSAO_AD.unlink(missing_ok=True)
                    self.entrar()
                    cab["Authorization"] = f"Bearer {self.tok['IdToken']}"
                    continue
                if t == 2 or str(e).startswith(("401", "403", "404")):
                    raise
                time.sleep(3 * (t + 1))

    def ler(self, siglas: set[str]) -> list[dict]:
        """Só as pastas de projeto das obras ativas no app (sigla = começo do nome da pasta)."""
        st = {str(x["id"]): x["name"] for x in (self.get("status") or {}).get("data", [])}
        raiz = self.get(f"folders/{self.al.PASTA_RAIZ}/subitems/light") or {}
        fila = [(s.get("id") or s.get("_id"), s.get("name") or "") for s in raiz.get("subFolders") or []
                if (s.get("name") or "")[:4] in siglas]
        docs, pastas = [], 0
        while fila:
            fid, projeto = fila.pop(0)
            j = self.get(f"folders/{fid}/subitems/light") or {}
            pastas += 1
            for sub in j.get("subFolders") or []:
                fila.append((sub.get("id") or sub.get("_id"), projeto))
            for d in j.get("documents") or []:
                d["_projeto"] = projeto
                docs.append(d)
            time.sleep(0.1)    # gentil com o servidor deles
        linhas = []
        for d in docs:
            nome, cam = d.get("name") or "", d.get("path") or ""
            linhas.append({"Código": nome, "Extensão": nome.rsplit(".", 1)[-1] if "." in nome else "",
                           "Revisão": d.get("revision") or "", "Título": d.get("description") or "",
                           "Data": (d.get("status_updated_at") or d.get("updated_at") or "")[:16],
                           "Status": st.get(str(d.get("status")), str(d.get("status") or "")),
                           "Obsoleto": "Sim" if str(d.get("obsolete")).lower() == "true" else "Não",
                           "Pasta": cam.rsplit("/", 1)[0] + "/" if "/" in cam else "",
                           "Link da Pasta": LINK_PASTA + str(d.get("folder_id") or ""), "Projeto": d.get("_projeto") or "",
                           "Observações": d.get("observations") or "", "_url": d.get("fileUrl") or "", "_hash": d.get("hash") or ""})
        log(f"Autodoc: {pastas} pastas, {len(linhas)} arquivos ({len(siglas)} obras ativas)")
        return linhas


def rodada_autodoc(cp, e: dict, testar=False) -> None:
    if not cp.has_section("autodoc") or not cp["autodoc"].get("email", "").strip():
        log("Autodoc: desligado (sem e-mail no ini).")
        return
    siglas = set(app_req(cp, "/api/autodoc/obras").get("siglas") or [])
    ad = Autodoc(cp)
    ad.entrar()
    linhas = ad.ler(siglas)
    if testar:
        bloq = sorted({l["Título"][:40] for l in linhas if l["Status"].lower().startswith("bloqueado") and l["Obsoleto"] == "Não"})
        log(f"Autodoc OK. Bloqueados agora: {len(bloq)} desenho(s). Nada enviado (teste).")
        return
    essencial = sorted((l["Projeto"], l["Código"], l["Revisão"], l["Status"], l["Obsoleto"], l["_hash"]) for l in linhas)
    h = hashlib.sha256(json.dumps(essencial, ensure_ascii=False).encode()).hexdigest()
    ult = e.get("autodoc", {})
    if h == ult.get("hash"):
        if time.time() - ult.get("contato", 0) > BATIMENTO_H * 3600:
            app_req(cp, "/api/autodoc/linhas", json.dumps({"linhas": []}).encode())
            ult["contato"] = time.time()
        log("Autodoc: sem mudança, nada enviado.")
    else:
        r = app_req(cp, "/api/autodoc/linhas", json.dumps({"linhas": linhas}, ensure_ascii=False).encode())
        log(f"Autodoc: enviado. Novos {r.get('novos')}, atualizados {r.get('atualizados')}, bloqueadas: {', '.join(r.get('bloqueadas', [])[:20])}")
        ult.update(hash=h, contato=time.time(), enviado=dt.datetime.now().isoformat(timespec="minutes"))
    e["autodoc"] = ult


# ---------------------------------------------------------------- Plannix

def _cfg_sql(cp) -> dict:
    p = dict(cp["plannix"])
    p.update(url=cp["app"]["url"], token=cp["app"]["token"])
    return p


def rodada_plannix(cp, e: dict, testar=False) -> None:
    modo = cp["plannix"].get("modo", "desligado").strip().lower() if cp.has_section("plannix") else "desligado"
    ult = e.setdefault("plannix", {})
    if modo == "sql":
        import plannix_sql as ps
        if not ps.SQL.exists() or "SELECT" not in ps.SQL.read_text(encoding="utf-8").upper():
            log("Plannix (sql): consulta.sql ainda vazia. Rode 'descobrir' e mande o plannix_esquema.txt para montar a consulta.")
            return
        cols, linhas = ps.rodar_consulta(_cfg_sql(cp))
        if testar:
            log(f"Plannix (sql) OK: {len(linhas)} linhas. " + " ".join(ps.conferir(cols)))
            return
        corpo = ps.tsv(cols, linhas)
        _enviar_plannix(cp, ult, "sql", corpo)
    elif modo == "pasta":
        pasta = Path(os.path.expandvars(os.path.expanduser(cp["plannix"].get("pasta", "~/Downloads"))))
        padroes = [x.strip() for x in cp["plannix"].get("arquivos", "all_data*.xls, *a_realizar*.xls").split(",") if x.strip()]
        if not pasta.is_dir():
            log(f"Plannix (pasta): pasta não existe: {pasta}")
            return
        limite = time.time() - 3 * 86400
        arqs = sorted((p for p in pasta.iterdir() if p.is_file() and p.stat().st_mtime > limite
                       and any(fnmatch.fnmatch(p.name.lower(), x.lower()) for x in padroes)), key=lambda p: p.stat().st_mtime)
        if testar:
            log(f"Plannix (pasta) OK: {pasta} · {len(arqs)} exportação(ões) recentes: " + ", ".join(p.name for p in arqs[-5:]))
            return
        enviados = ult.setdefault("arquivos", {})
        novos = 0
        for p in arqs:
            corpo = p.read_bytes()
            h = hashlib.sha256(corpo).hexdigest()
            if enviados.get(p.name) == h:
                continue
            if _enviar_plannix(cp, ult, p.name, corpo, forcar=True):
                enviados[p.name] = h
                novos += 1
        for nome in list(enviados):   # não guarda histórico de arquivo que já sumiu da pasta
            if not (pasta / nome).exists():
                enviados.pop(nome)
        if not novos:
            log("Plannix (pasta): nenhuma exportação nova.")
    else:
        log("Plannix: desligado.")


def _enviar_plannix(cp, ult: dict, nome: str, corpo: bytes, forcar=False) -> bool:
    h = hashlib.sha256(corpo).hexdigest()
    if not forcar and h == ult.get("hash") and time.time() - ult.get("contato", 0) < BATIMENTO_H * 3600:
        log("Plannix: sem mudança, nada enviado.")
        return True
    r = app_req(cp, "/api/plannix", corpo, tipo="text/tab-separated-values; charset=latin-1", timeout=300)
    if r.get("sem_mudanca"):
        log(f"Plannix ({nome}): app já tinha esses dados.")
    else:
        res = r.get("resumo", {})
        log(f"Plannix ({nome}): enviado, {res.get('linhas')} linhas, {res.get('novas')} peças novas.")
    ult.update(hash=h, contato=time.time(), enviado=dt.datetime.now().isoformat(timespec="minutes"))
    return True


# ---------------------------------------------------------------- comandos

def rodar(cp, forcar_horario=False) -> int:
    if not forcar_horario and not no_horario(cp):
        return 0
    if TRAVA.exists() and time.time() - TRAVA.stat().st_mtime < 50 * 60:
        log("Outra rodada ainda em andamento: esta foi pulada.")
        return 0
    TRAVA.write_text(str(os.getpid()))
    e = estado()
    erro = 0
    try:
        for nome, f in (("Autodoc", rodada_autodoc), ("Plannix", rodada_plannix)):
            try:
                f(cp, e)
            except Exception as x:   # noqa: BLE001  um lado com erro não derruba o outro
                log(f"{nome}: ERRO {type(x).__name__}: {x}")
                erro = 1
        guardar_estado(e)
    finally:
        TRAVA.unlink(missing_ok=True)
    return erro


def testar(cp) -> None:
    log(f"App: {cp['app']['url']}")
    try:
        s = app_req(cp, "/api/autodoc/obras")
        log(f"App OK. Obras ativas: {', '.join(s.get('siglas', []))}")
    except Exception as x:   # noqa: BLE001
        log(f"App: ERRO {x}")
        return
    for nome, f in (("Autodoc", rodada_autodoc), ("Plannix", rodada_plannix)):
        try:
            f(cp, {}, testar=True)
        except Exception as x:   # noqa: BLE001
            log(f"{nome}: ERRO {type(x).__name__}: {x}")


def main() -> None:
    ap = argparse.ArgumentParser(description="PCP Naves: Autodoc + Plannix -> app")
    ap.add_argument("comando", nargs="?", default="rodar", choices=["rodar", "testar", "descobrir", "status", "agora"])
    ap.add_argument("--procurar")
    a = ap.parse_args()
    sys.path.insert(0, str(AQUI))
    cp = config()
    if a.comando == "rodar":
        sys.exit(rodar(cp))
    if a.comando == "agora":
        sys.exit(rodar(cp, forcar_horario=True))
    if a.comando == "testar":
        testar(cp)
    elif a.comando == "descobrir":
        import plannix_sql as ps
        ps.cmd_descobrir(_cfg_sql(cp), a.procurar)
    else:
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "arquivos"} for k, v in estado().items()}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
