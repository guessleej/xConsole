# xConsole 單機安裝（Windows 10／11、Windows Server 2019 以上）：主控台＋節點代理裝在這一台。
# 在解開的安裝包資料夾裡，按右鍵開「Windows PowerShell（系統管理員）」執行：
#
#   powershell -ExecutionPolicy Bypass -File install\install.ps1              安裝或更新（資料與設定都保留）
#   powershell -ExecutionPolicy Bypass -File install\install.ps1 -Uninstall   移除開機自啟（資料保留）
#   ... install.ps1 -PasswordFile <檔>   自動化安裝：檔案第一行帳號、第二行密碼（用完請刪）
#
# 大量部署（只裝節點代理、加入既有的主控台；Intune、SCCM、映像派送都走這條），用系統管理員或 SYSTEM 執行：
#   powershell -ExecutionPolicy Bypass -File install\install.ps1 -Console <主控台:埠> -Fingerprint <指紋> -JoinCodeFile <回應檔> [-DataDrive D:]
#   加入碼只收回應檔（權限只給 SYSTEM 與 Administrators，讀完就刪）或標準輸入（-JoinCodeFile -），不收指令列與環境變數。
#   裝成 SYSTEM 開機自啟，沒有人登入也會跑；-DataDrive 把程式、資料與模型放到那顆磁碟的 xConsole 資料夾。
#   指紋與指令在主控台「加一台機器 → 大量部署」抄；主控台換成正式憑證後可以不給指紋。
#
# 系統管理員身分：裝成開機自動啟動（SYSTEM 帳號，不必有人登入），資料放 C:\ProgramData\xConsole，
#                 權限只給 SYSTEM 與系統管理員；並在防火牆開 9400（網頁）、9401（代理）、9402（工作站連回）、8090～8289（模型 API）。
# 一般身分：裝在自己的家目錄，登入後才啟動，防火牆要另外請管理員開。
param([switch]$Uninstall, [string]$PasswordFile, [string]$Console, [string]$Fingerprint, [string]$JoinCodeFile,
      [string]$DataDrive, [string]$JoinCode)
# 不用 Stop：Windows PowerShell 5.1 會把原生程式（python、pip）寫到 stderr 的任何一行當成致命錯誤直接中斷。
# 原生程式看 $LASTEXITCODE，cmdlet 個別加 -ErrorAction Stop
$ErrorActionPreference = 'Continue'
$SRC = Split-Path -Parent $PSScriptRoot
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$base = if ($admin) { Join-Path $env:ProgramData 'xConsole' } else { $env:USERPROFILE }
$dir = Join-Path $base 'xconsole'
$st = Join-Path $base '.xconsole'
function Say($t) { Write-Host "`n== $t ==" }
function Die($t) { Write-Host "`n安裝停止：$t" -ForegroundColor Red; exit 1 }
function FindPy {
  $probe = "import sys;print(sys.executable if sys.version_info>=(3,9) else '')"
  # py 啟動器優先；python 可能只是 Microsoft Store 的捷徑（執行會開商店），問不出版本就當沒有
  try { $v = & py -3 -c $probe 2>$null; if ($v -and (Test-Path $v.Trim())) { return $v.Trim() } } catch {}
  try { $v = & python -c $probe 2>$null; if ($v -and (Test-Path $v.Trim())) { return $v.Trim() } } catch {}
  $null
}
function WaitAgent($st) {
  $tok = Join-Path $st 'token'; $crt = Join-Path $st 'agent-cert.pem'
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep 1
    if ((Test-Path $tok) -and (Test-Path $crt)) {
      # 只接受代理自己那一張憑證，不是關掉驗證
      $script:xcMine = [Convert]::ToBase64String((New-Object Security.Cryptography.X509Certificates.X509Certificate2 $crt).RawData)
      [Net.ServicePointManager]::ServerCertificateValidationCallback = { param($s, $c) [Convert]::ToBase64String($c.GetRawCertData()) -eq $script:xcMine }
      try { if ((New-Object Net.WebClient).DownloadString('https://127.0.0.1:9401/healthz') -match 'xconsole-agent') { return $true } } catch {}
    }
  }
  $false
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 放在指令列會留在行程清單與派送紀錄，看得到的人在有效期間內就能拿去加入別的機器
if ($PSBoundParameters.ContainsKey('JoinCode')) { Die '加入碼不能放在指令列。請改成 -JoinCodeFile <回應檔>（權限只給 SYSTEM 與 Administrators，讀完會刪掉），或 -JoinCodeFile - 從標準輸入給' }
if (-not $Console -and ($Fingerprint -or $JoinCodeFile -or $DataDrive)) { Die '-Fingerprint、-JoinCodeFile、-DataDrive 要搭配 -Console（大量部署）使用' }

if ($Console) {
  # ---- 大量部署：只裝節點代理，SYSTEM 開機自啟，裝好直接加入 -Console 那台主控台
  if (-not $admin) { Die '大量部署要用系統管理員身分（或 SYSTEM，例如 Intune）執行：要裝成開機自啟' }
  if (-not (Test-Path (Join-Path $SRC 'install\join.py'))) { Die '請在解開的 xConsole 安裝包資料夾裡執行（找不到 install\join.py）' }
  if (-not $JoinCodeFile) { Die '缺加入碼：請給 -JoinCodeFile <回應檔>，或 -JoinCodeFile - 從標準輸入讀' }
  if ($JoinCodeFile -eq '-') { $code = [Console]::In.ReadLine() }
  else {
    if (-not (Test-Path -LiteralPath $JoinCodeFile -PathType Leaf)) { Die "找不到回應檔 $JoinCodeFile" }
    $okSid = @('S-1-5-18', 'S-1-5-32-544')
    $bad = @((Get-Acl -LiteralPath $JoinCodeFile).Access | Where-Object { $_.AccessControlType -eq 'Allow' } | ForEach-Object {
      $r = $_; try { $r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $r.IdentityReference.Value } } |
      Where-Object { $okSid -notcontains $_ } | Select-Object -Unique)
    $code = Get-Content -LiteralPath $JoinCodeFile -TotalCount 1
    Remove-Item -LiteralPath $JoinCodeFile -Force
    if ($bad.Count) {
      Die ("回應檔的權限要只給 SYSTEM 與 Administrators（現在還給了 " + ($bad -join '、') + "）。別人讀得到的加入碼可能已外流，" +
           "已刪掉這個檔，請在主控台作廢它、重新產生；準備回應檔：icacls <檔> /inheritance:r /grant:r *S-1-5-18:F *S-1-5-32-544:F")
    }
  }
  if (-not $code) { Die '加入碼是空的' }
  if ($DataDrive) {
    if ($DataDrive -notmatch '^[A-Za-z]:\\?$') { Die "-DataDrive 要是磁碟代號，例：D:" }
    $drv = $DataDrive.Substring(0, 2) + '\'
    if (-not (Test-Path $drv)) { Die "找不到磁碟 $drv" }
    $base = Join-Path $drv 'xConsole'
  }
  $dir = Join-Path $base 'xconsole'; $st = Join-Path $base '.xconsole'

  Say '1/5 檢查這台'
  $py = FindPy
  if (-not $py) { Die '這台沒有 Python 3.9 以上。請先派送 Python（例：winget install -e --id Python.Python.3.12 --scope machine），再跑一次' }
  Stop-ScheduledTask -TaskName 'xConsole Agent' -ErrorAction SilentlyContinue

  Say "2/5 複製代理到 $dir"
  New-Item -ItemType Directory -Force -Path $st, $dir | Out-Null
  # 權杖與私鑰放在這裡：只留給 SYSTEM 與系統管理員（ProgramData 與其他磁碟的根目錄預設一般使用者讀得到）
  icacls $base /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
  Copy-Item -Force (Join-Path $SRC 'agent\xconsole_agent.py') (Join-Path $dir 'xconsole_agent.py')
  Copy-Item -Force (Join-Path $SRC 'install\join.py') (Join-Path $dir 'join.py')

  Say '3/5 準備 Python 環境（cryptography）'
  $vpy = Join-Path $dir '.venv\Scripts\python.exe'; $vpyw = Join-Path $dir '.venv\Scripts\pythonw.exe'
  if (-not (Test-Path $vpy)) { & $py -m venv (Join-Path $dir '.venv'); if ($LASTEXITCODE) { Die '建不了 Python 虛擬環境' } }
  & $vpy -m pip install -q --disable-pip-version-check 'cryptography>=42'
  if ($LASTEXITCODE) { Die '裝不了 cryptography；這台要能連 pypi.org，或請 IT 先放行' }

  Say '4/5 開機自啟（SYSTEM）並啟動'
  $agent = Join-Path $dir 'xconsole_agent.py'
  Unregister-ScheduledTask -TaskName 'xConsole Agent' -Confirm:$false -ErrorAction SilentlyContinue
  $act = New-ScheduledTaskAction -Execute $vpyw -Argument ('"' + $agent + '" --home "' + $base + '"') -WorkingDirectory $dir
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  $pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  Register-ScheduledTask -TaskName 'xConsole Agent' -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $pri -Settings $set -ErrorAction Stop | Out-Null
  foreach ($r in @(@('xConsole 節點代理', '9401'), @('xConsole 模型 API', '8090-8289'))) {
    Get-NetFirewallRule -DisplayName $r[0] -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $r[0] -Direction Inbound -Protocol TCP -LocalPort $r[1] -Action Allow -Profile Domain,Private | Out-Null
  }
  Start-ScheduledTask -TaskName 'xConsole Agent'
  if (-not (WaitAgent $st)) { Die "節點代理沒有起來，請看 $st\agent.log，或在「工作排程器」查看 xConsole Agent 的最後執行結果" }

  Say "5/5 加入主控台 $Console"
  # Windows PowerShell 5.1 傳空字串給原生程式會整個漏掉，沒給指紋就不帶這個參數
  $ja = @((Join-Path $dir 'join.py'), '--console', $Console, '--home', $base)
  if ($Fingerprint) { $ja += @('--fingerprint', $Fingerprint) }
  # 加入碼經由管線給 join.py 的標準輸入，不出現在行程的命令列
  $code | & $vpy @ja
  if ($LASTEXITCODE) { Die '加入主控台失敗（原因見上一行）。代理已經裝好在跑，在主控台重新產生加入碼後，用同一個指令再跑一次即可' }
  Write-Host '完成：這台會一直開機自動啟動，沒有人登入也會跑。回到主控台就看得到它。'
  exit 0
}

if ($Uninstall) {
  foreach ($t in 'xConsole', 'xConsole Agent', 'xConsole Desktop') {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
  }
  Get-NetFirewallRule -DisplayName 'xConsole*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Write-Host "已移除開機自啟與防火牆規則。資料保留在 $st，要整個刪掉請自行刪除 $base 底下的 xconsole 與 .xconsole。"
  Write-Host '注意：代理帶起來的模型服務（llama-server.exe）會繼續跑到重新開機，或先在網頁上停掉。'
  exit 0
}

Say '1/6 檢查這台'
if (-not (Test-Path (Join-Path $SRC 'console\xconsole_server.py'))) { Die '請在解開的 xConsole 安裝包資料夾裡執行（找不到 console\）' }
if (-not $admin) { Write-Host '提醒：不是用系統管理員身分執行，會裝成「登入後才啟動」，防火牆也不會開。' -ForegroundColor Yellow }
$py = FindPy
if (-not $py) {
  $ans = Read-Host '這台沒有 Python 3.9 以上。要用 winget 安裝 Python 3.12 嗎？(Y/n)'
  if ($ans -match '^(n|N)') { Die '請先安裝 Python 3（https://www.python.org），再執行一次' }
  $scope = if ($admin) { 'machine' } else { 'user' }   # 一般身分裝不了全機版，改裝到自己的帳號底下
  winget install -e --id Python.Python.3.12 --scope $scope --accept-package-agreements --accept-source-agreements
  $py = (Get-ChildItem 'C:\Program Files\Python3*\python.exe', "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1).FullName
  if (-not $py) { Die 'Python 裝好了但找不到，請重新開一個 PowerShell 視窗再執行一次' }
}

Say "2/6 複製程式到 $dir"
New-Item -ItemType Directory -Force -Path $st, (Join-Path $dir 'static'), (Join-Path $dir 'agent') | Out-Null
if ($admin) {
  # ProgramData 預設一般使用者讀得到：權杖、私鑰、帳密雜湊都在這裡，只留給 SYSTEM 與系統管理員
  icacls $base /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
}
foreach ($f in 'xconsole_server.py', 'tls.py', 'agentlink.py', 'linkcert.py', 'groups.py', 'joincodes.py', 'store.py', 'report.py', 'recommend.py', 'extract.py', 'chats.py', 'computer.py', 'updater.py', 'catalog_default.json', 'sd_catalog_default.json') {
  Copy-Item -Force (Join-Path $SRC "console\$f") (Join-Path $dir $f)
}
Copy-Item -Force (Join-Path $SRC 'console\static\*') (Join-Path $dir 'static')
Copy-Item -Force (Join-Path $SRC 'agent\xconsole_agent.py') (Join-Path $dir 'agent\xconsole_agent.py')
Copy-Item -Force (Join-Path $SRC 'agent\xconsole_agent.py') (Join-Path $dir 'xconsole_agent.py')
Copy-Item -Force (Join-Path $SRC 'requirements.txt') (Join-Path $dir 'requirements.txt')
Copy-Item -Force (Join-Path $SRC 'VERSION') (Join-Path $dir 'VERSION')

Say '3/6 準備 Python 環境（主控台與代理都需要 cryptography）'
$vpy = Join-Path $dir '.venv\Scripts\python.exe'
$vpyw = Join-Path $dir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $vpy)) { & $py -m venv (Join-Path $dir '.venv'); if ($LASTEXITCODE) { Die '建不了 Python 虛擬環境' } }
& $vpy -m pip install -q --disable-pip-version-check -r (Join-Path $dir 'requirements.txt')
if ($LASTEXITCODE) { Die '裝不了相依套件；這台要能連 pypi.org，或請 IT 先放行' }

Say '4/6 管理者帳號'
$env:XCONSOLE_HOME = $base
$has = & $vpy -c "import json,os
try:
    c=json.load(open(os.path.join(os.environ['XCONSOLE_HOME'],'.xconsole','fleet.json'),encoding='utf-8')); print('yes' if c.get('auth',{}).get('hash') else '')
except Exception:
    print('')"
if ($has -eq 'yes') { Write-Host '已經設過帳號，保留原本的（要改請到網頁「設定 → 帳號」）' }
else {
  Push-Location $dir
  if ($PasswordFile) {
    # 密碼走檔案不走參數：參數會出現在工作管理員的命令列欄位
    Get-Content -LiteralPath $PasswordFile | & $vpy xconsole_server.py --set-credentials
  } else {
    Write-Host '設定主控台的登入帳號與密碼（密碼至少 8 碼，不會顯示在畫面上）'
    & $vpy xconsole_server.py --set-credentials
  }
  $rc = $LASTEXITCODE; Pop-Location
  if ($rc) { Die '帳號沒設好' }
}

Say '5/6 建立開機自啟並啟動'
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
if ($admin) {
  $trg = New-ScheduledTaskTrigger -AtStartup
  $pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  foreach ($r in @(@('xConsole 網頁', '9400'), @('xConsole 節點代理', '9401'), @('xConsole 工作站連回', '9402'), @('xConsole 模型 API', '8090-8289'))) {
    Get-NetFirewallRule -DisplayName $r[0] -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $r[0] -Direction Inbound -Protocol TCP -LocalPort $r[1] -Action Allow -Profile Domain,Private | Out-Null
  }
} else {
  $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
}
function Task($name, $script, $wd) {
  Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
  $act = New-ScheduledTaskAction -Execute $vpyw -Argument ('"' + $script + '" --home "' + $base + '"') -WorkingDirectory $wd
  Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg -Principal $pri -Settings $set -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $name
}
Task 'xConsole Agent' (Join-Path $dir 'xconsole_agent.py') $dir
if (-not (WaitAgent $st)) { Die "節點代理沒有起來，請看 $st\agent.log，或在「工作排程器」查看 xConsole Agent 的最後執行結果" }
& $vpy (Join-Path $SRC 'install\setup_local.py') --home $base
Task 'xConsole' (Join-Path $dir 'xconsole_server.py') $dir
if ($admin) {
  # 桌面小幫手（操作電腦用）：代理以 SYSTEM 跑在看不到桌面的工作階段，改由這個在使用者登入後、以使用者身分啟動的
  # 小程式代為截圖與操作。只聽 127.0.0.1、權杖放在使用者自己的 %LOCALAPPDATA%\xConsole；程式資料夾使用者讀不到，所以另外複製一份
  $hd = Join-Path $env:LOCALAPPDATA 'xConsole\desktop'
  New-Item -ItemType Directory -Force -Path $hd | Out-Null
  Copy-Item -Force (Join-Path $SRC 'agent\xconsole_agent.py') (Join-Path $hd 'xconsole_agent.py')
  Set-Content -LiteralPath (Join-Path $st 'desktop-user.txt') -Value $env:USERNAME -Encoding ASCII
  $pyw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
  if (-not (Test-Path $pyw)) { $pyw = $py }
  $dn = 'xConsole Desktop'
  Stop-ScheduledTask -TaskName $dn -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $dn -Confirm:$false -ErrorAction SilentlyContinue
  $dact = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + (Join-Path $hd 'xconsole_agent.py') + '" --desktop-helper') -WorkingDirectory $hd
  $dtrg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $dpri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $dn -Action $dact -Trigger $dtrg -Principal $dpri -Settings $set -ErrorAction SilentlyContinue | Out-Null
  Start-ScheduledTask -TaskName $dn -ErrorAction SilentlyContinue
}
# 存活檢查打同埠的 http://：回 308（轉到 https）就代表主控台活著
$up = $false
for ($i = 0; $i -lt 40 -and -not $up; $i++) {
  Start-Sleep 1
  try {
    $rq = [Net.HttpWebRequest]::Create('http://127.0.0.1:9400/healthz'); $rq.AllowAutoRedirect = $false; $rq.Timeout = 3000
    $rs = $rq.GetResponse(); $up = [int]$rs.StatusCode -eq 308; $rs.Close()
  } catch [Net.WebException] { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 308) { $up = $true } } catch {}
}
if (-not $up) { Die "主控台沒有起來，請看 $st\console.log" }

Say '6/6 完成'
try { $u = New-Object Net.Sockets.UdpClient; $u.Connect('1.1.1.1', 80); $ip = $u.Client.LocalEndPoint.Address.ToString(); $u.Close() } catch { $ip = '127.0.0.1' }
Write-Host "開瀏覽器到：https://${ip}:9400"
Write-Host '第一次會看到憑證警告（系統先用暫用自簽），登入後到「設定 → HTTPS 與憑證」換成正式憑證。'
Write-Host '接著照總覽頁的三步：確認記憶體架構 → 一鍵安裝推理引擎（會依顯示卡下載官方預編譯版）→ 從推薦模型挑一顆下載。'
