# PCP Naves - sincronização do PC da fábrica (Autodoc + Plannix), de hora em hora.
# Só usa o que já vem no Windows (PowerShell 5.1 e .NET): não precisa instalar Python nem nada.
#
# Comandos:
#   powershell -ExecutionPolicy Bypass -File pcp_sync.ps1 rodar     (o que o agendador chama; respeita o horário)
#   powershell -ExecutionPolicy Bypass -File pcp_sync.ps1 agora     (roda já, fora do horário também)
#   powershell -ExecutionPolicy Bypass -File pcp_sync.ps1 testar    (confere acessos, não grava nada no app)
#   powershell -ExecutionPolicy Bypass -File pcp_sync.ps1 status    (último envio)
#   powershell -ExecutionPolicy Bypass -File pcp_sync.ps1 descobrir [código]  (mapa do banco do Plannix, só nomes)
#   powershell -ExecutionPolicy Bypass -File pcp_sync.ps1 comparar  (banco x última exportação all_data da pasta)
#
# Autodoc: lê só as pastas das obras ATIVAS no app e envia SÓ quando algo mudou.
# Plannix (modo pasta): envia as exportações all_data / a_realizar salvas na pasta vigiada, só as novas ou alteradas.
param([string]$Comando = "rodar", [string]$Valor = "")

$ErrorActionPreference = "Stop"
$AQUI = Split-Path -Parent $MyInvocation.MyCommand.Path
$INI = Join-Path $AQUI "pcp_sync.ini"
$LOG = Join-Path $AQUI "pcp_sync.log"
$ESTADO = Join-Path $AQUI ".estado.json"
$TRAVA = Join-Path $AQUI ".rodando"
$SESSAO = Join-Path $AQUI ".autodoc_sessao.json"
$BATIMENTO_H = 6

$POOL = "us-east-1_wzg5eXXLU"
$CLIENTE_APP = "2qepdfbpe43utn1mkvbakdu78e"
$CLIENTE_CIVIL = "66047f7eb7e6b6d2570499ab"
$PASTA_RAIZ = "6918bc4ea5122aea6f0f2fc3"
$BFF = "https://projetos-ng-bff.autodoc.com.br/v1/"
$LINK_PASTA = "https://plataforma.autodoc.com.br/projetos/directories/folder/"

try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
Add-Type -AssemblyName System.Numerics
$UTF8 = New-Object System.Text.UTF8Encoding($false)

# ------------------------------------------------------------ base
function Log([string]$msg) {
    $linha = (Get-Date).ToString("dd/MM/yyyy HH:mm:ss") + "  " + $msg
    Write-Host $linha
    try {
        if ((Test-Path $LOG) -and (Get-Item $LOG).Length -gt 1MB) { Move-Item $LOG ($LOG + ".1") -Force }
        [IO.File]::AppendAllText($LOG, $linha + "`r`n", $UTF8)
    } catch {}
}

function Ler-Ini {
    if (-not (Test-Path $INI)) { throw "Falta pcp_sync.ini. Rode o instalar.bat." }
    $cfg = @{}; $sec = ""
    foreach ($l in [IO.File]::ReadAllLines($INI, $UTF8)) {
        $t = $l.Trim()
        if ($t -eq "" -or $t.StartsWith(";") -or $t.StartsWith("#")) { continue }
        if ($t -match '^\[(.+)\]$') { $sec = $Matches[1].ToLower(); $cfg[$sec] = @{}; continue }
        $i = $t.IndexOf("=")
        if ($i -gt 0 -and $sec) { $cfg[$sec][$t.Substring(0, $i).Trim().ToLower()] = $t.Substring($i + 1).Trim() }
    }
    return $cfg
}

function V($cfg, $sec, $chave, $padrao = "") {
    if ($cfg.ContainsKey($sec) -and $cfg[$sec].ContainsKey($chave) -and $cfg[$sec][$chave] -ne "") { return $cfg[$sec][$chave] }
    return $padrao
}

function Para-Hashtable($o) {
    if ($null -eq $o) { return $null }
    if ($o -is [System.Management.Automation.PSCustomObject]) {
        $h = @{}; foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = Para-Hashtable $p.Value }; return $h
    }
    if ($o -is [System.Collections.IEnumerable] -and -not ($o -is [string])) { return @($o | ForEach-Object { Para-Hashtable $_ }) }
    return $o
}

function Ler-Estado {
    try { if (Test-Path $ESTADO) { return Para-Hashtable ([IO.File]::ReadAllText($ESTADO, $UTF8) | ConvertFrom-Json) } } catch {}
    return @{}
}

function Gravar-Estado($e) { [IO.File]::WriteAllText($ESTADO, ($e | ConvertTo-Json -Depth 6), $UTF8) }

function Sha256Hex([byte[]]$b) {
    $s = [Security.Cryptography.SHA256]::Create()
    return (-join ($s.ComputeHash($b) | ForEach-Object { $_.ToString("x2") }))
}

function Gzip([byte[]]$b) {
    $ms = New-Object IO.MemoryStream
    $gz = New-Object IO.Compression.GZipStream($ms, [IO.Compression.CompressionMode]::Compress)
    $gz.Write($b, 0, $b.Length); $gz.Close()
    return $ms.ToArray()
}

function Http([string]$metodo, [string]$url, $cab, [byte[]]$corpo, [int]$timeout = 120) {
    $req = [Net.HttpWebRequest]::Create($url)
    $req.Method = $metodo; $req.Timeout = $timeout * 1000; $req.ReadWriteTimeout = $timeout * 1000
    $req.UserAgent = "pcp-sync"
    foreach ($k in $cab.Keys) {
        if ($k -eq "Content-Type") { $req.ContentType = $cab[$k] }
        elseif ($k -eq "Accept") { $req.Accept = $cab[$k] }
        else { $req.Headers.Add($k, $cab[$k]) }
    }
    if ($corpo) { $req.ContentLength = $corpo.Length; $s = $req.GetRequestStream(); $s.Write($corpo, 0, $corpo.Length); $s.Close() }
    try { $resp = $req.GetResponse() }
    catch [Net.WebException] {
        $r = $_.Exception.Response
        if ($r) { $txt = (New-Object IO.StreamReader($r.GetResponseStream(), $UTF8)).ReadToEnd(); throw ("{0} em {1}: {2}" -f [int]$r.StatusCode, ($url -split '\?')[0], $txt.Substring(0, [Math]::Min(300, $txt.Length))) }
        throw ("sem conexão com {0}: {1}" -f ([Uri]$url).Host, $_.Exception.Message)
    }
    $txt = (New-Object IO.StreamReader($resp.GetResponseStream(), $UTF8)).ReadToEnd(); $resp.Close()
    if ($txt) { return ($txt | ConvertFrom-Json) }
    return $null
}

function App($cfg, [string]$caminho, [byte[]]$corpo = $null, [string]$tipo = "application/json") {
    $url = (V $cfg "app" "url").TrimEnd("/") + $caminho
    $cab = @{ "Authorization" = "Bearer " + (V $cfg "app" "token"); "Accept" = "application/json" }
    if ($corpo) {
        $cab["Content-Type"] = $tipo; $cab["Content-Encoding"] = "gzip"
        try { return Http "POST" $url $cab (Gzip $corpo) 300 } catch { throw ("app: " + $_.Exception.Message) }
    }
    try { return Http "GET" $url $cab $null 60 } catch { throw ("app: " + $_.Exception.Message) }
}

# ------------------------------------------------------------ SRP (AWS Cognito, igual ao site do Autodoc)
$N_HEX = "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD" +
         "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED" +
         "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F" +
         "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B" +
         "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510" +
         "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7" +
         "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C" +
         "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
$G_HEX = "2"

function HexParaBig([string]$h) { return [Numerics.BigInteger]::Parse("0" + $h, [Globalization.NumberStyles]::AllowHexSpecifier) }
function BigParaHex([Numerics.BigInteger]$v) { $h = $v.ToString("x").TrimStart("0"); if ($h -eq "") { $h = "0" }; return $h }
function Pad([string]$h) {
    if ($h.Length % 2 -eq 1) { return "0" + $h }
    if ("89abcdefABCDEF".Contains($h.Substring(0, 1))) { return "00" + $h }
    return $h
}
function HexParaBytes([string]$h) {
    $b = New-Object byte[] ($h.Length / 2)
    for ($i = 0; $i -lt $b.Length; $i++) { $b[$i] = [Convert]::ToByte($h.Substring($i * 2, 2), 16) }
    return ,$b
}
function HexHash([string]$h) { return Sha256Hex (HexParaBytes $h) }
function Mod([Numerics.BigInteger]$v, [Numerics.BigInteger]$n) { $r = [Numerics.BigInteger]::Remainder($v, $n); if ($r.Sign -lt 0) { $r = $r + $n }; return $r }
function Hmac([byte[]]$chave, [byte[]]$msg) { $h = New-Object Security.Cryptography.HMACSHA256(,$chave); return ,$h.ComputeHash($msg) }

$N = HexParaBig $N_HEX
$G = HexParaBig $G_HEX
$K = HexParaBig (HexHash ("00" + $N_HEX + "0" + $G_HEX))

function Srp-A([Numerics.BigInteger]$a) { return [Numerics.BigInteger]::ModPow($G, $a, $N) }

function Srp-Novo-a {
    $b = New-Object byte[] 129
    (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($b)
    $b[128] = 0   # little-endian: último byte 0 = positivo
    return Mod (New-Object Numerics.BigInteger(,$b)) $N
}

function Srp-Resposta([Numerics.BigInteger]$a, $p, [string]$senha, [string]$ts) {
    # atenção: no PowerShell $a e $A são a mesma variável; por isso gA e gB
    $gA = Srp-A $a
    $userId = $p.USER_ID_FOR_SRP
    $gB = HexParaBig $p.SRP_B
    $u = HexParaBig (HexHash ((Pad (BigParaHex $gA)) + (Pad (BigParaHex $gB))))
    $pool = $POOL.Split("_")[1]
    $up = Sha256Hex ($UTF8.GetBytes($pool + $userId + ":" + $senha))
    $x = HexParaBig (HexHash ((Pad $p.SALT) + $up))
    $base = Mod ($gB - $K * [Numerics.BigInteger]::ModPow($G, $x, $N)) $N
    $s = [Numerics.BigInteger]::ModPow($base, $a + $u * $x, $N)
    $prk = Hmac (HexParaBytes (Pad (BigParaHex $u))) (HexParaBytes (Pad (BigParaHex $s)))
    $info = $UTF8.GetBytes("Caldera Derived Key") + [byte[]]@(1)
    $chave = (Hmac $prk $info)[0..15]
    $msg = $UTF8.GetBytes($pool) + $UTF8.GetBytes($userId) + [Convert]::FromBase64String($p.SECRET_BLOCK) + $UTF8.GetBytes($ts)
    $ass = [Convert]::ToBase64String((Hmac ([byte[]]$chave) ([byte[]]$msg)))
    return @{ TIMESTAMP = $ts; USERNAME = $userId; PASSWORD_CLAIM_SECRET_BLOCK = $p.SECRET_BLOCK; PASSWORD_CLAIM_SIGNATURE = $ass }
}

function Carimbo {
    $d = [DateTime]::UtcNow; $ci = [Globalization.CultureInfo]::InvariantCulture
    return $d.ToString("ddd MMM ", $ci) + $d.Day + $d.ToString(" HH:mm:ss 'UTC' yyyy", $ci)
}

function Cognito([string]$acao, $corpo) {
    $cab = @{ "Content-Type" = "application/x-amz-json-1.1"; "X-Amz-Target" = "AWSCognitoIdentityProviderService." + $acao }
    return Http "POST" "https://cognito-idp.us-east-1.amazonaws.com/" $cab ($UTF8.GetBytes(($corpo | ConvertTo-Json -Depth 5 -Compress))) 60
}

function Autodoc-Entrar($cfg) {
    if (Test-Path $SESSAO) {
        try {
            $s = [IO.File]::ReadAllText($SESSAO, $UTF8) | ConvertFrom-Json
            $r = Cognito "InitiateAuth" @{ AuthFlow = "REFRESH_TOKEN_AUTH"; ClientId = $CLIENTE_APP; AuthParameters = @{ REFRESH_TOKEN = $s.refresh } }
            return $r.AuthenticationResult
        } catch {}
    }
    $email = V $cfg "autodoc" "email"; $senha = V $cfg "autodoc" "senha"
    $a = Srp-Novo-a
    $d = Cognito "InitiateAuth" @{ AuthFlow = "USER_SRP_AUTH"; ClientId = $CLIENTE_APP; AuthParameters = @{ USERNAME = $email; SRP_A = (BigParaHex (Srp-A $a)) } }
    if ($d.ChallengeName -ne "PASSWORD_VERIFIER") { throw ("Autodoc pediu um passo de login inesperado: " + $d.ChallengeName) }
    $resp = Srp-Resposta $a $d.ChallengeParameters $senha (Carimbo)
    $r = Cognito "RespondToAuthChallenge" @{ ChallengeName = "PASSWORD_VERIFIER"; ClientId = $CLIENTE_APP; ChallengeResponses = $resp }
    if (-not $r.AuthenticationResult) { throw ("Login do Autodoc exige um passo extra (" + $r.ChallengeName + "). Entre uma vez no site com esse usuário e resolva.") }
    if ($r.AuthenticationResult.RefreshToken) { [IO.File]::WriteAllText($SESSAO, (@{ refresh = $r.AuthenticationResult.RefreshToken } | ConvertTo-Json), $UTF8) }
    return $r.AuthenticationResult
}

function Autodoc-Get($tok, [string]$caminho) {
    $cab = @{ "Authorization" = "Bearer " + $tok.IdToken; "X-Customer-Id" = $CLIENTE_CIVIL; "X-Product-value" = "projetos"; "Accept" = "application/json" }
    for ($t = 0; $t -lt 3; $t++) {
        try { return Http "GET" ($BFF + $caminho) $cab $null 60 }
        catch { if ($t -eq 2 -or $_.Exception.Message -match '^(401|403|404)') { throw }; Start-Sleep -Seconds (3 * ($t + 1)) }
    }
}

function Autodoc-Ler($cfg, $siglas, $tok) {
    $st = @{}; foreach ($x in (Autodoc-Get $tok "status").data) { $st[[string]$x.id] = $x.name }
    $raiz = Autodoc-Get $tok ("folders/" + $PASTA_RAIZ + "/subitems/light")
    $fila = New-Object System.Collections.Queue
    foreach ($s in $raiz.subFolders) { if ($siglas -contains ([string]$s.name).Substring(0, [Math]::Min(4, ([string]$s.name).Length))) { $fila.Enqueue(@($s.id, $s.name)) } }
    $linhas = New-Object System.Collections.ArrayList; $pastas = 0
    while ($fila.Count -gt 0) {
        $f = $fila.Dequeue()
        $j = Autodoc-Get $tok ("folders/" + $f[0] + "/subitems/light"); $pastas++
        foreach ($s in $j.subFolders) { $fila.Enqueue(@($s.id, $f[1])) }
        foreach ($d in $j.documents) {
            $nome = [string]$d.name; $cam = [string]$d.path
            $data = [string]$d.status_updated_at; if (-not $data) { $data = [string]$d.updated_at }
            $ext = ""; if ($nome.Contains(".")) { $ext = $nome.Substring($nome.LastIndexOf(".") + 1) }
            $pasta = ""; if ($cam.Contains("/")) { $pasta = $cam.Substring(0, $cam.LastIndexOf("/") + 1) }
            $status = $st[[string]$d.status]; if (-not $status) { $status = [string]$d.status }
            $obs = "Não"; if (([string]$d.obsolete).ToLower() -eq "true") { $obs = "Sim" }
            [void]$linhas.Add([ordered]@{ "Código" = $nome; "Extensão" = $ext; "Revisão" = [string]$d.revision; "Título" = [string]$d.description
                "Data" = $data.Substring(0, [Math]::Min(16, $data.Length)); "Status" = $status; "Obsoleto" = $obs; "Pasta" = $pasta
                "Link da Pasta" = $LINK_PASTA + [string]$d.folder_id; "Projeto" = [string]$f[1]; "Observações" = [string]$d.observations
                "_url" = [string]$d.fileUrl; "_hash" = [string]$d.hash })
        }
        Start-Sleep -Milliseconds 100
    }
    Log ("Autodoc: {0} pastas, {1} arquivos ({2} obras ativas)" -f $pastas, $linhas.Count, $siglas.Count)
    return ,$linhas
}

function Rodada-Autodoc($cfg, $e, [bool]$testar = $false) {
    if (-not (V $cfg "autodoc" "email")) { Log "Autodoc: desligado (sem e-mail no ini)."; return }
    $siglas = @((App $cfg "/api/autodoc/obras").siglas)
    $tok = Autodoc-Entrar $cfg
    $linhas = Autodoc-Ler $cfg $siglas $tok
    if ($testar) {
        $bl = @($linhas | Where-Object { $_.Status -like "Bloqueado*" -and $_.Obsoleto -eq "Não" }).Count
        Log ("Autodoc OK. Desenhos bloqueados agora: {0}. Nada enviado (teste)." -f $bl); return
    }
    $ess = ($linhas | ForEach-Object { $_.Projeto + "|" + $_."Código" + "|" + $_."Revisão" + "|" + $_.Status + "|" + $_.Obsoleto + "|" + $_._hash } | Sort-Object) -join "`n"
    $h = Sha256Hex ($UTF8.GetBytes($ess))
    $ult = $e["autodoc"]; if (-not $ult) { $ult = @{} }
    $agora = [DateTimeOffset]::Now.ToUnixTimeSeconds()
    if ($ult["hash"] -eq $h) {
        if ($agora - [long]$ult["contato"] -gt $BATIMENTO_H * 3600) { [void](App $cfg "/api/autodoc/linhas" ($UTF8.GetBytes('{"linhas":[]}'))); $ult["contato"] = $agora }
        Log "Autodoc: sem mudança, nada enviado."
    } else {
        $corpo = $UTF8.GetBytes((@{ linhas = $linhas } | ConvertTo-Json -Depth 4 -Compress))
        $r = App $cfg "/api/autodoc/linhas" $corpo
        Log ("Autodoc: enviado. Novos {0}, atualizados {1}, bloqueadas: {2}" -f $r.novos, $r.atualizados, (($r.bloqueadas | Select-Object -First 20) -join ", "))
        $ult["hash"] = $h; $ult["contato"] = $agora; $ult["enviado"] = (Get-Date).ToString("yyyy-MM-ddTHH:mm")
    }
    $e["autodoc"] = $ult
}

# ------------------------------------------------------------ Plannix (modo pasta)
function Rodada-Plannix($cfg, $e, [bool]$testar = $false) {
    $modo = (V $cfg "plannix" "modo" "desligado").ToLower()
    if ($modo -eq "sql") {
        $r = Plannix-Sql-Ler $cfg
        $falta = @($OBRIGATORIAS | Where-Object { $r.cols -notcontains $_ })
        if ($testar) { Log ("Plannix (banco) OK: {0} linhas, {1} colunas." -f $r.linhas, $r.cols.Count); if ($falta.Count) { Log ("Faltam colunas: " + ($falta -join ", ")) }; return }
        if ($falta.Count) { throw ("a consulta não traz: " + ($falta -join ", ")) }
        $h = Sha256Hex $r.corpo
        $ult = $e["plannix"]; if (-not $ult) { $ult = @{} }
        $agora = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        if ($ult["hash"] -eq $h -and ($agora - [long]$ult["contato"]) -lt $BATIMENTO_H * 3600) { Log "Plannix (banco): sem mudança, nada enviado." }
        else {
            $x = App $cfg "/api/plannix" $r.corpo "text/tab-separated-values; charset=latin-1"
            if ($x.sem_mudanca) { Log "Plannix (banco): o app já tinha esses dados." } else { Log ("Plannix (banco): enviado, {0} linhas, {1} peças novas." -f $x.resumo.linhas, $x.resumo.novas) }
            $ult["hash"] = $h; $ult["contato"] = $agora; $ult["enviado"] = (Get-Date).ToString("yyyy-MM-ddTHH:mm")
        }
        $e["plannix"] = $ult
        return
    }
    if ($modo -ne "pasta") { Log ("Plannix: desligado (modo '{0}')." -f $modo); return }
    $pastas = Pastas-Downloads $cfg
    $pasta = $pastas -join " | "
    $padroes = (V $cfg "plannix" "arquivos" "all_data*.xls, *a_realizar*.xls").Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $limite = (Get-Date).AddDays(-3)
    $arqs = @()
    foreach ($f in (@(foreach ($pp in $pastas) { Get-ChildItem -LiteralPath $pp -File -ErrorAction SilentlyContinue }) | Sort-Object LastWriteTime)) {
        if ($f.LastWriteTime -le $limite) { continue }
        if ($f.Name -match '(?i)(all.?data|a.?realizar)' -and $f.Extension -match '(?i)^\.(xls|txt|csv|tsv)$') { $arqs += $f; continue }
        foreach ($pd in $padroes) { if ($f.Name -like $pd) { $arqs += $f; break } }
    }
    if ($testar) { Log ("Plannix (pasta) OK: {0} · {1} exportação(ões) recentes: {2}" -f $pasta, $arqs.Count, (($arqs | Select-Object -Last 5 | ForEach-Object { $_.Name }) -join ", ")); return }
    $ult = $e["plannix"]; if (-not $ult) { $ult = @{} }
    $enviados = $ult["arquivos"]; if (-not $enviados) { $enviados = @{} }
    $novos = 0
    foreach ($a in $arqs) {
        $b = [IO.File]::ReadAllBytes($a.FullName); $h = Sha256Hex $b
        if ($enviados[$a.Name] -eq $h) { continue }
        $r = App $cfg "/api/plannix" $b "text/tab-separated-values; charset=latin-1"
        if ($r.sem_mudanca) { Log ("Plannix ({0}): o app já tinha esses dados." -f $a.Name) }
        else { Log ("Plannix ({0}): enviado, {1} linhas, {2} peças novas." -f $a.Name, $r.resumo.linhas, $r.resumo.novas) }
        $enviados[$a.Name] = $h; $novos++
        $ult["enviado"] = (Get-Date).ToString("yyyy-MM-ddTHH:mm")
    }
    foreach ($n in @($enviados.Keys)) { if (-not @($arqs | Where-Object { $_.Name -eq $n }).Count) { $enviados.Remove($n) } }
    if ($novos -eq 0) { Log "Plannix (pasta): nenhuma exportação nova." }
    $ult["arquivos"] = $enviados; $e["plannix"] = $ult
}

# ------------------------------------------------------------ Plannix (modo sql: banco PrecastControl, só leitura)
$OBRIGATORIAS = @("Peça", "Código", "Nome Obra", "Produto")
$PISTAS = @("fabric", "concret", "peca", "peça", "obra", "protens", "armac", "teoric", "traco", "traço", "forma", "volume", "secao", "seção", "codigo", "código")

function Sql-Conectar($cfg) {
    $srv = V $cfg "plannix" "servidor"; $banco = V $cfg "plannix" "banco"
    if (-not $srv -or -not $banco) { throw "Preencha servidor e banco na seção [plannix] do pcp_sync.ini." }
    $cs = "Server=$srv;Database=$banco;Connect Timeout=20;Application Name=PCP-Sync;ApplicationIntent=ReadOnly;"
    if (V $cfg "plannix" "usuario") { $cs += "User Id=" + (V $cfg "plannix" "usuario") + ";Password=" + (V $cfg "plannix" "senha") + ";" }
    else { $cs += "Integrated Security=True;" }
    $cx = New-Object System.Data.SqlClient.SqlConnection($cs)
    $cx.Open()
    return $cx
}

function Sql-Tabela($cx, [string]$sql, [int]$timeout = 120) {
    $cmd = $cx.CreateCommand(); $cmd.CommandText = $sql; $cmd.CommandTimeout = $timeout
    $da = New-Object System.Data.SqlClient.SqlDataAdapter($cmd)
    $ds = New-Object System.Data.DataSet
    [void]$da.Fill($ds)
    foreach ($t in $ds.Tables) { if ($t.Columns.Count -gt 0) { return ,$t } }   # primeira tabela com colunas
    throw "A consulta não devolveu nenhuma tabela."
}

function Celula($v) {
    if ($null -eq $v -or $v -is [DBNull]) { return "" }
    if ($v -is [DateTime]) { if ($v.Hour -or $v.Minute -or $v.Second) { return $v.ToString("dd/MM/yyyy HH:mm:ss") }; return $v.ToString("dd/MM/yyyy") }
    if ($v -is [decimal] -or $v -is [double] -or $v -is [single]) { return ([double]$v).ToString("0.######", [Globalization.CultureInfo]::GetCultureInfo("pt-BR")) }
    return ([string]$v).Replace("`t", " ").Replace("`r", " ").Replace("`n", " ")
}

function Plannix-Sql-Ler($cfg) {
    $arq = Join-Path $AQUI "consulta.sql"
    if (-not (Test-Path $arq)) { throw "Falta consulta.sql (rode descobrir.bat e me mande o plannix_esquema.txt)." }
    $sql = [IO.File]::ReadAllText($arq, $UTF8)
    if ($sql.Contains("{SIGLAS}")) {
        $sg = @((App $cfg "/api/autodoc/obras").siglas | ForEach-Object { "'" + ([string]$_).Replace("'", "") + "'" })
        if (-not $sg.Count) { throw "o app não tem obra ativa" }
        $sql = $sql.Replace("{SIGLAS}", ($sg -join ","))
    }
    if ($sql -notmatch '(?i)\bselect\b') { throw "consulta.sql ainda vazia (rode descobrir.bat e me mande o plannix_esquema.txt)." }
    $cx = Sql-Conectar $cfg
    try { $t = Sql-Tabela $cx $sql ([int](V $cfg "plannix" "timeout_consulta" "180")) } finally { $cx.Close() }
    $cols = @($t.Columns | ForEach-Object { $_.ColumnName })
    $sb = New-Object Text.StringBuilder
    [void]$sb.Append(($cols -join "`t")).Append("`n")
    foreach ($r in $t.Rows) { [void]$sb.Append((@($r.ItemArray | ForEach-Object { Celula $_ }) -join "`t")).Append("`n") }
    return @{ cols = $cols; linhas = $t.Rows.Count; corpo = [Text.Encoding]::GetEncoding(28591).GetBytes($sb.ToString()) }
}

function Ler-Tsv([byte[]]$b) {
    $txt = [Text.Encoding]::GetEncoding(28591).GetString($b)
    $ls = $txt -split "`r?`n" | Where-Object { $_ -ne "" }
    $cab = $ls[0].Split("`t") | ForEach-Object { $_.Trim() }
    $idx = @{}
    $ic = [array]::IndexOf($cab, "Código")
    foreach ($l in $ls[1..($ls.Count - 1)]) { $c = $l.Split("`t"); if ($c.Count -gt $ic -and $c[$ic].Trim()) { $idx[$c[$ic].Trim()] = $c } }
    return @{ cab = $cab; idx = $idx }
}

function Norm([string]$v) {
    $v = $v.Trim()
    if ($v -eq "-" -or $v -eq "N/A" -or $v -eq "nan") { return "" }
    if ($v -match '^(\d{2}/\d{2}/\d{4})( (\d{2}:\d{2}))?') { if ($Matches[3] -and $Matches[3] -ne "00:00") { return $Matches[1] + " " + $Matches[3] }; return $Matches[1] }
    $n = 0.0
    $t = $v; if ($t.Contains(",")) { $t = $t.Replace(".", "").Replace(",", ".") }
    if ([double]::TryParse($t, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$n)) {
        if ($n -eq 0) { return "" }   # vazio e zero são a mesma coisa para o app
        return [Math]::Round($n, 2, [MidpointRounding]::AwayFromZero).ToString([Globalization.CultureInfo]::InvariantCulture)
    }
    return $v.ToUpper()
}

function Pastas-Downloads($cfg) {
    # a pasta do ini e a pasta Downloads de verdade (pode estar no OneDrive ou em outro disco)
    $ps = New-Object Collections.ArrayList
    [void]$ps.Add([Environment]::ExpandEnvironmentVariables((V $cfg "plannix" "pasta" "%USERPROFILE%\Downloads")))
    try { [void]$ps.Add((New-Object -ComObject Shell.Application).NameSpace("shell:Downloads").Self.Path) } catch {}
    try { $k = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -ErrorAction Stop
          [void]$ps.Add([Environment]::ExpandEnvironmentVariables($k."{374DE290-123F-4565-9164-39C4925E467B}")) } catch {}
    foreach ($o in @($env:OneDrive, $env:OneDriveCommercial)) { if ($o) { [void]$ps.Add((Join-Path $o "Downloads")) } }
    if ($env:USERPROFILE) { [void]$ps.Add((Join-Path $env:USERPROFILE "Desktop")) }
    return @($ps | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)
}

function Achar-Exportacao($cfg, [string]$caminho) {
    if ($caminho -and (Test-Path -LiteralPath $caminho)) { return Get-Item -LiteralPath $caminho }
    $achados = @()
    foreach ($p in (Pastas-Downloads $cfg)) {
        $achados += @(Get-ChildItem -LiteralPath $p -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)all.?data' -and $_.Extension -match '(?i)^\.(xls|txt|csv|tsv)$' })
    }
    return $achados | Sort-Object LastWriteTime | Select-Object -Last 1
}

function Comparar($cfg, [string]$caminho) {
    $exp = Achar-Exportacao $cfg $caminho
    if (-not $exp) {
        $onde = Pastas-Downloads $cfg
        $rec = @(foreach ($p in $onde) { Get-ChildItem -LiteralPath $p -File -ErrorAction SilentlyContinue }) | Sort-Object LastWriteTime | Select-Object -Last 6
        Log ("Procurei em: " + ($onde -join " | "))
        Log ("Arquivos mais recentes: " + (($rec | ForEach-Object { $_.Name }) -join " | "))
        throw "Não achei a exportação 'all data'. Arraste o arquivo exportado em cima do comparar.bat."
    }
    Log ("Exportação: " + $exp.FullName)
    Log ("Lendo o banco do Plannix...")
    $r = Plannix-Sql-Ler $cfg
    $A = Ler-Tsv $r.corpo; $B = Ler-Tsv ([IO.File]::ReadAllBytes($exp.FullName))
    $out = New-Object Collections.ArrayList
    [void]$out.Add("Comparação banco x exportação " + $exp.Name + " (" + $exp.LastWriteTime.ToString("dd/MM HH:mm") + ")")
    [void]$out.Add(("Banco: {0} peças | Exportação: {1} peças" -f $A.idx.Count, $B.idx.Count))
    $soA = @($A.idx.Keys | Where-Object { -not $B.idx.ContainsKey($_) }); $soB = @($B.idx.Keys | Where-Object { -not $A.idx.ContainsKey($_) })
    $iA = @{}; for ($i = 0; $i -lt $A.cab.Count; $i++) { $iA[$A.cab[$i]] = $i }
    $iB = @{}; for ($i = 0; $i -lt $B.cab.Count; $i++) { $iB[$B.cab[$i]] = $i }
    [void]$out.Add(("Só no banco: {0} (ex.: {1})" -f $soA.Count, (($soA | Select-Object -First 8 | ForEach-Object { $_ + " " + $A.idx[$_][$iA["Peça"]] + " " + $A.idx[$_][$iA["Nome Obra"]] + " fab=" + $A.idx[$_][$iA["Data Fabricação"]] + " st=" + $A.idx[$_][$iA["Status Projeto"]] }) -join " | ")))
    [void]$out.Add(("Só na exportação: {0} (ex.: {1})" -f $soB.Count, (($soB | Select-Object -First 8 | ForEach-Object { $_ + " " + $B.idx[$_][$iB["Peça"]] + " " + $B.idx[$_][$iB["Nome Obra"]] }) -join " | ")))
    $comuns = @($A.idx.Keys | Where-Object { $B.idx.ContainsKey($_) })
    [void]$out.Add("Em comum: " + $comuns.Count); [void]$out.Add("")
    [void]$out.Add("Colunas só na exportação: " + ((@($B.cab | Where-Object { $_ -and -not $iA.ContainsKey($_) })) -join " | "))
    [void]$out.Add(""); [void]$out.Add("== COLUNA: % igual nas peças em comum (exemplos de diferença: código banco / exportação)")
    foreach ($c in $A.cab) {
        if (-not $c) { continue }
        if (-not $iB.ContainsKey($c)) { [void]$out.Add(("{0,-42} (não existe na exportação)" -f $c)); continue }
        $ok = 0; $ex = New-Object Collections.ArrayList
        foreach ($k in $comuns) {
            $va = Norm ([string]$A.idx[$k][$iA[$c]]); $vb = Norm ([string]$B.idx[$k][$iB[$c]])
            if ($va -eq $vb) { $ok++ } elseif ($ex.Count -lt 4) { [void]$ex.Add($k + ": [" + $A.idx[$k][$iA[$c]] + "] / [" + $B.idx[$k][$iB[$c]] + "]") }
        }
        $pct = 0; if ($comuns.Count) { $pct = [Math]::Round(100.0 * $ok / $comuns.Count, 1) }
        [void]$out.Add(("{0,-42} {1,6}%  {2}" -f $c, $pct, ($ex -join " ; ")))
    }
    $arq = Join-Path $AQUI "plannix_comparacao.txt"
    [IO.File]::WriteAllLines($arq, [string[]]$out, $UTF8)
    Log ("Comparação salva em plannix_comparacao.txt. Mande esse arquivo.")
}

function Descobrir($cfg, [string]$procurar) {
    $cx = Sql-Conectar $cfg
    $out = New-Object Collections.ArrayList
    try {
        $v = Sql-Tabela $cx "SELECT @@VERSION AS v, DB_NAME() AS b"
        [void]$out.Add("Banco: " + $v.Rows[0].b); [void]$out.Add("Versão: " + ([string]$v.Rows[0].v).Split("`n")[0]); [void]$out.Add("")
        $tab = Sql-Tabela $cx "SELECT s.name AS esquema, t.name AS tabela, SUM(p.rows) AS linhas FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) GROUP BY s.name, t.name ORDER BY SUM(p.rows) DESC"
        [void]$out.Add("== TABELAS (" + $tab.Rows.Count + ") : linhas aproximadas")
        foreach ($r in $tab.Rows) { [void]$out.Add(("{0,10}  {1}.{2}" -f [long]$r.linhas, $r.esquema, $r.tabela)) }
        $col = Sql-Tabela $cx "SELECT TABLE_SCHEMA AS s, TABLE_NAME AS t, COLUMN_NAME AS c, DATA_TYPE AS tipo FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        $por = [ordered]@{}
        foreach ($r in $col.Rows) { $k = $r.s + "." + $r.t; if (-not $por.Contains($k)) { $por[$k] = New-Object Collections.ArrayList }; [void]$por[$k].Add(@($r.c, $r.tipo)) }
        [void]$out.Add(""); [void]$out.Add("== COLUNAS QUE PARECEM DE PEÇA / OBRA / FABRICAÇÃO")
        foreach ($k in $por.Keys) {
            $ach = @($por[$k] | Where-Object { $c = ([string]$_[0]).ToLower(); @($PISTAS | Where-Object { $c.Contains($_) }).Count -gt 0 } | ForEach-Object { $_[0] + " (" + $_[1] + ")" })
            if ($ach.Count) { [void]$out.Add($k + ": " + ($ach -join ", ")) }
        }
        $mods = Sql-Tabela $cx "SELECT s.name AS s, o.name AS n, o.type_desc AS tipo, m.definition AS d FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id"
        $rel = @($mods.Rows | Where-Object { $d = ([string]$_.d).ToLower(); $d -and @($PISTAS | Where-Object { $d.Contains($_) }).Count -ge 3 })
        [void]$out.Add(""); [void]$out.Add("== VIEWS E PROCEDURES COM CARA DE RELATÓRIO DE PRODUÇÃO (" + $rel.Count + " de " + $mods.Rows.Count + ")")
        foreach ($m in $rel) { [void]$out.Add(""); [void]$out.Add("--- " + $m.tipo + " " + $m.s + "." + $m.n); [void]$out.Add(([string]$m.d).Trim()) }
        if ($procurar) {
            [void]$out.Add(""); [void]$out.Add("== ONDE APARECE O VALOR '" + $procurar + "' (só colunas de código/peça)")
            $num = $procurar -match '^\d+$'
            foreach ($r in $tab.Rows) {
                if ([long]$r.linhas -eq 0) { continue }
                $k = $r.esquema + "." + $r.tabela
                foreach ($c in $por[$k]) {
                    $nome = ([string]$c[0]).ToLower()
                    if ($nome -notmatch 'cod|pe[cç]a|ident|etiq|num|marca|nome') { continue }
                    $txt = @("varchar", "nvarchar", "char", "nchar") -contains $c[1]
                    $int = @("int", "bigint", "numeric", "decimal") -contains $c[1]
                    if (-not $txt -and -not ($num -and $int)) { continue }
                    $val = "N'" + $procurar.Replace("'", "''") + "'"; if (-not $txt) { $val = $procurar }
                    try {
                        $q = Sql-Tabela $cx ("SELECT TOP 1 1 AS x FROM [" + $r.esquema + "].[" + $r.tabela + "] WITH (NOLOCK) WHERE [" + $c[0] + "] = " + $val) 30
                        if ($q.Rows.Count) { [void]$out.Add($k + "." + $c[0]) }
                    } catch {}
                }
            }
        }
        [void]$out.Add(""); [void]$out.Add("== TODAS AS COLUNAS")
        foreach ($k in $por.Keys) { [void]$out.Add($k + ": " + ((@($por[$k] | ForEach-Object { $_[0] + " (" + $_[1] + ")" })) -join ", ")) }
    } finally { $cx.Close() }
    $arq = Join-Path $AQUI "plannix_esquema.txt"
    [IO.File]::WriteAllLines($arq, [string[]]$out, $UTF8)
    Log ("Mapa do banco salvo em plannix_esquema.txt ({0} tabelas, {1} consultas candidatas). Só nomes de tabelas e colunas, nenhum dado de peça. Mande esse arquivo." -f $tab.Rows.Count, $rel.Count)
}

# ------------------------------------------------------------ comandos
function No-Horario($cfg) {
    $h = (Get-Date).Hour
    if ((V $cfg "geral" "dias" "seg-sab") -eq "seg-sab" -and (Get-Date).DayOfWeek -eq "Sunday") { return $false }
    return ($h -ge [int](V $cfg "geral" "hora_inicio" "6") -and $h -le [int](V $cfg "geral" "hora_fim" "20"))
}

function Rodar($cfg, [bool]$forcar) {
    if (-not $forcar -and -not (No-Horario $cfg)) { return 0 }
    if ((Test-Path $TRAVA) -and ((Get-Date) - (Get-Item -Force $TRAVA).LastWriteTime).TotalMinutes -lt 50) { Log "Outra rodada ainda em andamento: esta foi pulada."; return 0 }
    [IO.File]::WriteAllText($TRAVA, [string]$PID)
    $e = Ler-Estado; $erro = 0
    try {
        foreach ($p in @(@("Autodoc", ${function:Rodada-Autodoc}), @("Plannix", ${function:Rodada-Plannix}))) {
            try { & $p[1] $cfg $e $false } catch { Log ($p[0] + ": ERRO " + $_.Exception.Message); $erro = 1 }
        }
        Gravar-Estado $e
    } finally { Remove-Item -Force $TRAVA -ErrorAction SilentlyContinue }
    return $erro
}

if ($Comando -eq "srp-teste") { return }   # usado só nos testes das funções de login

try {
    $cfg = Ler-Ini
    switch ($Comando) {
        "rodar" { exit (Rodar $cfg $false) }
        "agora" { exit (Rodar $cfg $true) }
        "testar" {
            Log ("App: " + (V $cfg "app" "url"))
            try { $s = App $cfg "/api/autodoc/obras"; Log ("App OK. Obras ativas: " + (@($s.siglas) -join ", ")) } catch { Log ("App: ERRO " + $_.Exception.Message); exit 1 }
            foreach ($p in @(@("Autodoc", ${function:Rodada-Autodoc}), @("Plannix", ${function:Rodada-Plannix}))) {
                try { & $p[1] $cfg @{} $true } catch { Log ($p[0] + ": ERRO " + $_.Exception.Message) }
            }
        }
        "status" { if (Test-Path $ESTADO) { Get-Content $ESTADO } else { "Nada enviado ainda." } }
        "descobrir" { Descobrir $cfg $Valor }
        "comparar" { Comparar $cfg $Valor }
        default { Write-Host "Comandos: rodar | agora | testar | status" }
    }
} catch { Log ("ERRO: " + $_.Exception.Message); exit 1 }
