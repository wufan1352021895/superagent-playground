#Requires -Version 5.1
<#
.SYNOPSIS
    中控台跨机控制：通过 WinRM(优先 Tailscale IP) 对吴凡机(Sewing tools)做
    只读状态查询 / 列出可控脚本 / 触发脚本。只读查询绝不改远程；run 为显式触发。

.DESCRIPTION
    - 凭据复用 wufan.clixml (DPAPI)，与同步脚本同源。
    - 远程命令全程 ASCII + 通配符枚举，避开中文路径过 WinRM 的编码坑。
    - 安全护栏：
        * run 前检测远程是否已有 step/wrapper 在跑（browser-act 单例，禁止并发）。
        * 只允许触发远程桌面/12步运行器里真实存在的 .bat。
        * step03(2-3_14天) 不主动触发（人工确认类），需 -ForceStep03 才放行。
    - 触发用 Start-Process 分离进程，立即返回；脚本在远程后台跑，状态由
      Temu_12steps_status.json / 飞书通知体现，中控台经同步拉回展示。

.PARAMETER Action
    status | list | run
.PARAMETER Step
    run 时：步骤号(如 5)或 bat 文件名(如 5_*.bat / 全名)。
.PARAMETER TargetIp
    默认 Tailscale IP；失败可传局域网 192.168.0.109。
.PARAMETER ForceStep03
    显式允许触发 step03（默认禁止）。
.PARAMETER Json
    只输出一行 JSON 结果（供中控台后端调用）。
#>
[CmdletBinding()]
param(
    [ValidateSet('status','list','run','runbatch','tasksoff','taskson','pccodestatus','pccoderun','returnsrun','returnsstatus','returnsopen','violationopen','receivedrun','receivedstatus','receivedapply','pendsweep','pendsweepstatus','skcretstatus','skcretrun','skctrackstatus','skctrackrun','pccodeguard','login-check','login-refresh','todayfiles','readfile','killpy','listproc','notify','listdir','readcsv','readimage','baprobe','bacall')]
    [string]$Action = 'status',
    [string]$Step = '',
    [string]$Batch = '',
    [string]$TargetIp = '',
    [switch]$ForceStep03,
    # PC码批量换物流（pccoderun）：-Pcs 逗号分隔，-Day today|tomorrow，-ForceRun 跳过往 3 天防重复确认
    [string]$Pcs = '',
    # SKC 下架+退货申请（skcretrun）：-Skcs 逗号分隔纯数字，-DelistOnly 只下架不退货
    [string]$Skcs = '',
    [switch]$DelistOnly,
    [ValidateSet('today','tomorrow')]
    [string]$Day = 'tomorrow',
    [switch]$ForceRun,
    # 自动/调度触发时由中控台传入：远程任务进程内打 TEMU_12STEPS_RUNNING=1，
    # 业务脚本据此跳过标签机实物打印（手动面板按钮/吴凡桌面双击不传 -> 照打）
    [switch]$QuietLabel,
    # py 入口（bat→py 迁移 2026-10-12）：-PyScript '04. 查违规.py' 时触发桌面 py 而非 bat。
    # 运行模式 -PyMode trial|run（默认 trial：产物 *_py、不发飞书、不打印、不覆盖 bat 产物）。
    # python 解释器：吴凡用户级 3.12.10（C:\Users\吴凡\AppData\...\Python312），脚本内
    # 用 $env:USERPROFILE 拼接避开中文字面量过 WinRM 的编码坑。
    [string]$PyScript = '',
    [ValidateSet('trial','run')]
    [string]$PyMode = 'trial',
    # todayfiles/readfile 专用：桌面相对路径（如 'txt\在售中货号.txt'、'Temu12步运行器\Temu_12steps_status.json'）
    [string]$RelPath = '',
    # killpy 专用：命令行匹配模式（如 '销量近7日0'）。空=只列不杀（dry-run）。
    [string]$PyPattern = '',
    # listproc 专用：进程名/命令行匹配模式（如 '凌风|lingfeng'），只读列出 pid/age/cmd
    [string]$ProcPattern = '',
    # notify 专用：代调吴凡机 pitfalls\notify-cockpit.ps1（官方 status.json 写入通道+飞书）。
    # 背景：bat→py 迁移后定时全走 py 路由，py 不写 Temu_12steps_status.json；手动补跑若误走
    # bat 老路还会留下 fail 污点。本 action 按真实业务结果补账/修正（StepId=纯数字）。
    [string]$StepId = '',
    [string]$Name = '',
    [ValidateSet('ok','fail')]
    [string]$Status = 'ok',
    [int]$DurationSec = 0,
    [int]$ExitCode = 0,
    # status 动作专用：自动识别并清理“僵尸 busy”（browser-act 崩溃后空转的 cmd/wrapper）
    [switch]$StaleClear,
    # pc码 17:00 时间护栏开关（pccodeguard）：
    #   -GuardSet 'on'|'off' 改开关（留空=不改，只读返回当前状态）
    #   -GuardHour 0-23 改截止小时（传 -1 = 不改）
    [ValidateSet('','on','off')]
    [string]$GuardSet = '',
    [int]$GuardHour = -1,
    [int]$StaleMinAgeMin = 50,
    # 僵尸 busy 硬寿命（分钟）：无浏览器/node 后代的业务 wrapper 一旦超过该年龄，
    # 即使 CPU 在涨（满核空转型僵尸，如 pc_run 死循环）也判僵死结束。
    # 150min 高于任何真实远程步（看门狗上限 70min），且真在跑的单必有浏览器/node 后代不被命中。
    [int]$StaleHardAgeMin = 150,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
# 输出强制 UTF-8（无 BOM）：PS1 经 Python subprocess 管道读取，默认本地码组会让中文 bat 名乱码
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
try { $OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$Port      = 5985
# Tailscale 优先(跨网络稳定)，局域网兜底；显式传 -TargetIp 则只用该地址
if ($TargetIp) { $Targets = @($TargetIp) } else { $Targets = @('100.84.203.94','192.168.0.109') }
$credPath  = Join-Path $env:USERPROFILE '.openclaw\workspace\credentials\wufan.clixml'

function Emit($obj) {
    if ($Json) { ($obj | ConvertTo-Json -Compress -Depth 6) } else { $obj | Format-List }
}

function Fail($why) {
    $online = if ($why -match 'trustedhosts|session ') { $true } else { $false }
    Emit @{ ok = $false; online = $online; action = $Action; reason = $why }
    return
}

# ---- 凭据 ----
if (-not (Test-Path $credPath)) { Fail 'no-credential-file'; return }
try { $cred = Import-Clixml -Path $credPath } catch { Fail "cred-read: $($_.Exception.Message)"; return }

# ---- 探活 + 建会话（逐目标：Tailscale 优先，失败试局域网）----
$so = New-PSSessionOption -OperationTimeout 120000 -OpenTimeout 15000
$sess = $null
$TargetIp = $null
$errs = @()
$authErr = $null   # TCP 可达但会话失败（TrustedHosts/认证）——优先级最高
foreach ($t in $Targets) {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $reach = $false
    try {
        $iar = $tcp.BeginConnect($t, $Port, $null, $null)
        $reach = $iar.AsyncWaitHandle.WaitOne(4000,$false) -and $tcp.Connected
    } finally { $tcp.Close() }
    if (-not $reach) { $errs += "unreachable $t"; continue }
    try {
        $sess = New-PSSession -ComputerName $t -Port $Port -Credential $cred -SessionOption $so
        $TargetIp = $t
        break
    } catch {
        $msg = $_.Exception.Message
        $errs += "session $t : $msg"
        if (-not $authErr) {
            if ($msg -match 'TrustedHosts|trusted|信任|身份验证|authentication') {
                $authErr = "trustedhosts-$t"
            } else {
                $authErr = "session $t : $msg"
            }
        }
        $sess = $null; continue
    }
}
if (-not $sess) { Fail ($(if ($authErr) { $authErr } else { $errs -join ' | ' })); return }

try {
    switch ($Action) {

        'status' {
            # 僵尸 busy 自愈：-StaleClear 时，远程对“业务忙进程”做两次 CPU 采样，
            # 仅当某进程 年龄>$StaleMinAgeMin 分钟 且 两次采样 CPU 零增长 且 无浏览器/pw(node) 后(子)代
            # 才判为僵死并结束（browser-act 崩溃后 cmd/wrapper 空转的典型形态），随后重算 busy。
            # 绝不动 chrome/node/系统进程，避免杀掉真正在跑的单。
            $r = Invoke-Command -Session $sess -ArgumentList $StaleClear,$StaleMinAgeMin,$StaleHardAgeMin -ScriptBlock {
                param($doClear,[int]$minAgeMin,[int]$hardAgeMin)
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                function Get-BizProcs {
                    Get-CimInstance Win32_Process | Where-Object {
                        # busy 节流治理 2026-10-13：原 python 判据为 'wrapper|step|Temu12|all12|digest'，
                        # 而 bat->py 后业务命令形如 python.exe "...\01. 备货.py" --run（中文名不含上述关键词）
                        # -> status 看不见正在跑的 py 步，wait_idle 误判空闲，下一步随即被 run 闸门
                        # 以 remote-busy 秒拒（今日 51 次）。现与 run 闸门对齐为 '\.py'；
                        # cmd 判据同步放宽到 \.(bat|py)，让 cmd 包装进程一启动即被计入（关闭启动盲窗）。
                        ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.(bat|py)') -or
                        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                        ($_.Name -eq 'python.exe' -and $_.CommandLine -match '\.py')
                    }
                }
                # 判断某 pid 的后(子)代里是否有浏览器/playwright(node) 活动；有则绝不判僵死
                function Has-BrowserDescendant($rootPid,$allProcs){
                    $byParent=@{}; foreach($p in $allProcs){ if(-not $byParent.ContainsKey([int]$p.ParentProcessId)){ $byParent[[int]$p.ParentProcessId]=@() }; $byParent[[int]$p.ParentProcessId]+=$p }
                    $st=New-Object System.Collections.Stack; $st.Push([int]$rootPid); $seen=@{} 
                    while($st.Count){ $cur=$st.Pop(); if($seen.ContainsKey($cur)){continue}; $seen[$cur]=$true
                        if($byParent.ContainsKey($cur)){ foreach($c in $byParent[$cur]){
                            if($c.Name -match 'chrome|msedge|firefox|browser_act|playwright|node.exe'){ return $true }
                            $st.Push([int]$c.ProcessId)
                        } }
                    }
                    return $false
                }
                $staleKilled=@()
                if($doClear){
                    $all0 = Get-CimInstance Win32_Process
                    $now = Get-Date
                    $allBiz = @(Get-BizProcs)
                    # 仅保留“无浏览器/playwright(node) 后代”的候选；真在跑的单必有此类后代，绝不命中
                    $orphan = @($allBiz | Where-Object { -not (Has-BrowserDescendant $_.ProcessId $all0) })

                    # 分支A（硬寿命）：孤儿业务进程超过 hardAgeMin —— 不管 CPU 是否在涨都结束。
                    # 覆盖 browser-act 崩溃后 cmd/wrapper 满核空转（CPU 一直涨，旧规则漏杀）。
                    $hard = @($orphan | Where-Object { $_.CreationDate -lt $now.AddMinutes(-$hardAgeMin) })
                    foreach($p in $hard){
                        $short = $p.CommandLine; if($short -and $short.Length -gt 60){ $short=$short.Substring(0,60) }
                        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $staleKilled += ("{0}:{1}:hard" -f $p.ProcessId,$p.Name) } catch {}
                    }
                    $hardIds = @{}; foreach($p in $hard){ $hardIds[$p.ProcessId]=$true }

                    # 分支B（零增长）：年龄>minAgeMin 的孤儿，两次采样 CPU 零增长 -> 结束（原规则）
                    $cand = @($orphan | Where-Object { -not $hardIds.ContainsKey($_.ProcessId) -and $_.CreationDate -lt $now.AddMinutes(-$minAgeMin) })
                    if($cand.Count){
                        $s1=@{}; foreach($p in $cand){ $gp=Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue; if($gp){ $s1[$p.ProcessId]=$gp.CPU } }
                        Start-Sleep -Seconds 12
                        foreach($p in $cand){
                            $gp=Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
                            if(-not $gp){ continue }
                            $old = $s1[$p.ProcessId]
                            if($null -ne $old -and ($gp.CPU - $old) -le 0.001){
                                $short = $p.CommandLine; if($short -and $short.Length -gt 60){ $short=$short.Substring(0,60) }
                                try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $staleKilled += ("{0}:{1}:idle" -f $p.ProcessId,$p.Name) } catch {}
                            }
                        }
                    }
                }
                # 是否有 step/wrapper 在跑（browser-act 互斥）。
                $busyProcs = @(Get-BizProcs)
                $statusFile = Get-ChildItem $desktop -Recurse -Depth 3 -Filter 'Temu_12steps_status.json' |
                              Where-Object { $_.FullName -notmatch 'node_modules|stage|_backup|独立部署' } |
                              Sort-Object LastWriteTime -Descending | Select-Object -First 1
                $statusMtime = if ($statusFile) { $statusFile.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
                $statusSteps = $null
                if ($statusFile) {
                    try {
                        $raw = Get-Content -LiteralPath $statusFile.FullName -Raw -Encoding utf8
                        if ($raw -match '^\xEF\xBB\xBF') { $raw = $raw.Substring(1) }
                        $sd = $raw | ConvertFrom-Json
                        $statusSteps = @($sd.results | ForEach-Object {
                            [pscustomobject]@{ step=$_.step; name=$_.name; status=$_.status; exitCode=$_.exitCode; ts=$_.ts; time=$_.time }
                        })
                    } catch { $statusSteps = [string]$_ }
                }
                # 2026-10-28：合规标签计划任务最近一次运行（夜间定时跑，本机 runs_history 无记录，
                # 这是面板第 55 步唯一的真实证据来源）。只读，不触发任何任务。
                $hegui = [pscustomobject]@{ task = 'TEMU_HeguiBiaoqian'; state = 'missing'; lastRunTime = $null; lastResult = $null; nextRunTime = $null }
                try {
                    $ht = Get-ScheduledTask -TaskName 'TEMU_HeguiBiaoqian' -ErrorAction Stop
                    $hi = Get-ScheduledTaskInfo -TaskName 'TEMU_HeguiBiaoqian' -ErrorAction Stop
                    $lrt = $null
                    if ($hi.LastRunTime -and $hi.LastRunTime.Year -ge 2000) { $lrt = $hi.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
                    $nrt = $null
                    if ($hi.NextRunTime -and $hi.NextRunTime.Year -ge 2000) { $nrt = $hi.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
                    $hegui = [pscustomobject]@{ task = 'TEMU_HeguiBiaoqian'; state = "$($ht.State)"; lastRunTime = $lrt; lastResult = $hi.LastTaskResult; nextRunTime = $nrt }
                } catch { }
                [pscustomobject]@{
                    computer   = $env:COMPUTERNAME
                    user       = $env:USERNAME
                    busy       = [bool]$busyProcs.Count
                    busy_count = $busyProcs.Count
                    status_mtime = $statusMtime
                    stale_killed = ($staleKilled -join ',')
                    status_steps = $statusSteps
                    hegui      = $hegui
                }
            }
            Emit @{ ok=$true; online=$true; action='status'; target=$TargetIp;
                    computer=$r.computer; user=$r.user; busy=$r.busy; busy_count=$r.busy_count;
                    status_file_mtime=$r.status_mtime; stale_killed=$r.stale_killed;
                    status_steps=$r.status_steps; hegui=$r.hegui }
        }

        'killpy' {
            # 精准杀僵尸业务进程：按命令行模式匹配 busy 进程并结束（StaleClear 只杀无浏览器后代
            # 的孤儿；卡在登录/业务中段的僵尸带浏览器后代，需本动作按名精准杀）。
            # $PyPattern 为空 = dry-run 只列不杀。杀完重算 busy，返回 before/after。
            $r = Invoke-Command -Session $sess -ArgumentList $PyPattern -ScriptBlock {
                param($pattern)
                $ErrorActionPreference='SilentlyContinue'
                function Get-BizProcs {
                    Get-CimInstance Win32_Process | Where-Object {
                        ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.(bat|py)') -or
                        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                        ($_.Name -eq 'python.exe' -and $_.CommandLine -match '\.py')
                    }
                }
                $before = @(Get-BizProcs)
                $listed = @($before | ForEach-Object {
                    $cl = $_.CommandLine; if($cl -and $cl.Length -gt 110){ $cl=$cl.Substring(0,110) }
                    [pscustomobject]@{ pid=$_.ProcessId; name=$_.Name;
                        age_min=[int]((Get-Date) - $_.CreationDate).TotalMinutes; cmd=$cl }
                })
                $killed=@()
                if($pattern){
                    $match = @($before | Where-Object { $_.CommandLine -like "*$pattern*" })
                    foreach($p in $match){
                        $cl = $p.CommandLine; if($cl -and $cl.Length -gt 110){ $cl=$cl.Substring(0,110) }
                        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                              $killed += ("{0}:{1}:{2}" -f $p.ProcessId,$p.Name,$cl) } catch {}
                    }
                }
                Start-Sleep -Seconds 3
                $after = @(Get-BizProcs)
                [pscustomobject]@{
                    pattern    = $pattern
                    before_cnt = $before.Count
                    after_cnt  = $after.Count
                    procs      = $listed
                    killed     = $killed
                }
            }
            Emit @{ ok=$true; online=$true; action='killpy'; target=$TargetIp;
                    pattern=$r.pattern; before_cnt=$r.before_cnt; after_cnt=$r.after_cnt;
                    procs=$r.procs; killed=$r.killed }
        }

        'listproc' {
            # 只读：按 ProcPattern 匹配列出进程（pid/age/cmd），用于诊断凌风客户端等非业务进程
            $r = Invoke-Command -Session $sess -ArgumentList $ProcPattern -ScriptBlock {
                param($pattern)
                $ErrorActionPreference='SilentlyContinue'
                $all = Get-CimInstance Win32_Process
                $m = @($all | Where-Object { $_.CommandLine -like "*$pattern*" -or $_.Name -like "*$pattern*" })
                @($m | ForEach-Object {
                    $cl = $_.CommandLine; if($cl -and $cl.Length -gt 130){ $cl=$cl.Substring(0,130) }
                    [pscustomobject]@{ pid=$_.ProcessId; name=$_.Name;
                        age_min=[int]((Get-Date) - $_.CreationDate).TotalMinutes; cmd=$cl }
                })
            }
            Emit @{ ok=$true; online=$true; action='listproc'; target=$TargetIp;
                    pattern=$ProcPattern; count=@($r).Count; procs=$r }
        }

        'notify' {
            # 代调吴凡机 pitfalls\notify-cockpit.ps1（官方 status.json 写入通道，附推飞书）。
            # 背景：bat→py 迁移后定时全走 py 路由，py 不写 Temu_12steps_status.json；手动补跑
            # 若误走 bat 老路还会留下 fail 污点。本 action 按真实业务结果补账/修正。
            if (-not $StepId -or -not $Name) { Fail 'notify 需要 -StepId 和 -Name'; return }
            $aStepId = [string]$StepId; $aName = [string]$Name
            $aStatus = [string]$Status; $aDur = [int]$DurationSec; $aEc = [int]$ExitCode
            $r = Invoke-Command -Session $sess -ArgumentList @($aStepId,$aName,$aStatus,$aDur,$aEc) -ScriptBlock {
                param($stepId,$name,$status,$dur,$ec)
                $ErrorActionPreference='Continue'
                $nc = Join-Path $env:USERPROFILE 'pitfalls\notify-cockpit.ps1'
                if (-not (Test-Path -LiteralPath $nc)) {
                    return [pscustomobject]@{ ok=$false; reason='notify-cockpit-not-found' }
                }
                $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $nc `
                    -StepId $stepId -Name $name -Status $status -DurationSec $dur -ExitCode $ec 2>&1 | Out-String
                return [pscustomobject]@{ ok=$true; stepId=$stepId; status=$status; tail=$out }
            }
            Emit @{ ok=$true; online=$true; action='notify'; target=$TargetIp; detail=$r }
        }

        'login-check' {
            # 只读诊断：确认吴凡机专用 browser-act 浏览器 chrome_local_114366298678362348
            # 的 Temu 登录态是否过期（参考本机 temu-sewing-auto-login 持久登录机制）。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $out = [ordered]@{}
                $ba = Get-Command browser-act -ErrorAction SilentlyContinue
                if ($ba) {
                    $out.browser_act = $ba.Source
                    $out.browser_list = (& browser-act browser list 2>&1 | Out-String)
                } else { $out.browser_list = 'browser-act-not-found' }
                $out.target_browser = 'chrome_local_114366298678362348'
                # 查 TEMU_BROWSER_ID 注入的浏览器是否仍注册
                $found = $false
                if ($ba) {
                    $list = & browser-act browser list 2>&1 | Out-String
                    if ($list -match 'chrome_local_114366298678362348') { $found = $true }
                }
                $out.target_browser_present = $found
                return [pscustomobject]$out
            }
            Emit @{ ok=$true; online=$true; action='login-check'; target=$TargetIp; detail=$r }
        }

        'todayfiles' {
            # 只读诊断：列出吴凡机桌面今天(00:00起)修改过的文件（深度2，最多60条），
            # 用于验证不写 12steps status.json 的独立业务步产物
            # （查在售数据/待快递揽收/品质分差下架退货/在售货号 等）。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                $today = (Get-Date).Date
                $files = @(Get-ChildItem $desktop -File -Recurse -Depth 2 |
                    Where-Object { $_.LastWriteTime -ge $today -and $_.Extension -ne '.tmp' } |
                    Sort-Object LastWriteTime -Descending |
                    Select-Object -First 80 |
                    ForEach-Object { [pscustomobject]@{ name=$_.Name; rel=$_.FullName.Substring($desktop.Length+1); dir=$_.Directory.Name; mtime=$_.LastWriteTime.ToString('HH:mm:ss'); kb=[int]($_.Length/1KB) } })
                return $files
            }
            Emit @{ ok=$true; online=$true; action='todayfiles'; target=$TargetIp; count=@($r).Count; files=$r }
        }

        'readfile' {
            # 只读诊断：读吴凡机桌面小文本文件（<=64KB）内容，验证独立产物/失败报告。
            if (-not $RelPath) { Fail 'readfile 需要 -RelPath 桌面相对路径'; return }
            $aRel = [string]$RelPath
            $r = Invoke-Command -Session $sess -ArgumentList $aRel -ScriptBlock {
                param($rel)
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                $p = Join-Path $desktop $rel
                if (-not (Test-Path -LiteralPath $p)) { return [pscustomobject]@{ exists=$false; path=$rel } }
                $fi = Get-Item -LiteralPath $p
                if ($fi.Length -gt 65536) {
                    return [pscustomobject]@{ exists=$true; too_big=$true; kb=[int]($fi.Length/1KB); mtime=$fi.LastWriteTime.ToString('HH:mm:ss') }
                }
                $txt = Get-Content -LiteralPath $p -Raw -Encoding UTF8
                return [pscustomobject]@{ exists=$true; too_big=$false; mtime=$fi.LastWriteTime.ToString('HH:mm:ss'); kb=[int]($fi.Length/1KB); content=$txt }
            }
            Emit @{ ok=$true; online=$true; action='readfile'; target=$TargetIp; file=$r }
        }

        # 2026-10-19 新增（给「查在售数据（带图）·吴凡机店铺」提供数据源）。
        # 三个都是只读，绝不改远程任何东西。
        'listdir' {
            # 列吴凡机桌面某目录下的文件：名字/大小/修改时间。
            # 用途：找「查在售数据」目录下日期最大的那份 CSV，不用逐个日期去猜。
            if (-not $RelPath) { Fail 'listdir 需要 -RelPath 桌面相对路径'; return }
            $aRel = [string]$RelPath
            $r = Invoke-Command -Session $sess -ArgumentList $aRel -ScriptBlock {
                param($rel)
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                $p = Join-Path $desktop $rel
                if (-not (Test-Path -LiteralPath $p -PathType Container)) {
                    return [pscustomobject]@{ exists=$false; path=$rel }
                }
                $items = @(Get-ChildItem -LiteralPath $p -File -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending |
                    Select-Object -First 200 |
                    ForEach-Object { [pscustomobject]@{
                        name   = $_.Name
                        kb     = [int]($_.Length/1KB)
                        mtime  = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                    } })
                return [pscustomobject]@{ exists=$true; path=$rel; count=$items.Count; items=$items }
            }
            Emit @{ ok=$true; online=$true; action='listdir'; target=$TargetIp; dir=$r }
        }

        'baprobe' {
            # 只读探测吴凡机是否具备「本机跑浏览器拉平台数据」的条件：
            #   1) browser-act.exe 在不在（用户级 .local\bin 标准路径）
            #   2) Chrome 调试端口 9222 通不通（通=有带 --remote-debugging-port 的 Chrome 在跑）
            #   3) Temu 卖家后台是否已登录（本机能不能 self-contained 查吴凡机店铺的唯一前提）
            # 只返回 存在与否 / 端口连通性，不返回任何 Cookie、URL、页面内容。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $baPaths = @(
                    (Join-Path $env:USERPROFILE '.local\bin\browser-act.exe'),
                    (Join-Path $env:LOCALAPPDATA  'browser-act\browser-act.exe'),
                    (Join-Path $env:USERPROFILE  'AppData\Local\bin\browser-act.exe')
                )
                $found = $null
                foreach ($bp in $baPaths) { if (Test-Path -LiteralPath $bp) { $found = $bp; break } }
                $baProc = @(Get-Process -Name 'browser-act' -ErrorAction SilentlyContinue).Count
                $chromeArgp = 0
                $chromeAll  = @(Get-Process -Name 'chrome' -ErrorAction SilentlyContinue)
                $chromeArgp = $chromeAll.Count
                $port9222 = $false
                try {
                    $t = New-Object System.Net.Sockets.TcpClient
                    $t.ReceiveTimeout = 1500; $t.SendTimeout = 1500
                    $ar = $t.BeginConnect('127.0.0.1', 9222, $null, $null)
                    if ($ar.AsyncWaitHandle.WaitOne(1500, $false) -and $t.Connected) { $port9222 = $true }
                    $t.Close()
                } catch {}
                $py = $null
                foreach ($pp in @((Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python312\python.exe'),
                                  (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python311\python.exe'))) {
                    if (Test-Path -LiteralPath $pp) { $py = $pp; break }
                }
                return [pscustomobject]@{
                    ba_found    = [bool]$found
                    ba_path_out = [bool]$found
                    ba_proc     = $baProc
                    chrome_proc = $chromeArgp
                    port9222    = $port9222
                    python      = [bool]$py
                    user        = $env:USERNAME
                }
            }
            Emit @{ ok=$true; online=$true; action='baprobe'; target=$TargetIp; probe=$r }
        }

        'bacall' {
            # 在吴凡机上本地调用它自己的 browser-act.exe 执行**只读**命令，把 stdout 文本带回来。
            # 用途：验证「吴凡机能不能自己连自己的 Temu 卖家后台」——这是吴凡机店铺能否有平台数据的前提。
            # 只接受 -Step 里的白名单子命令（sessions / tabs / href / probe），不接收任意 JS：
            #   sessions -> browser-act sessions list
            #   tabs     -> browser-act tabs list --session <name>
            #   href     -> 当前页 eval location.href/document.title（固定 JS，无外部输入）
            #   probe    -> 固定只读 JS：识别当前页是不是 Temu 卖家后台 + 有没有「销售管理/查询」按钮
            # 编码坑：browser-act 在中文 locale 下输出含中文会挂（本机踩过 230305），
            # 所以这里强制 UTF8 并只看 ASCII 部分。
            $subCmd = if ($Step) { [string]$Step } else { 'sessions' }
            $sess2  = if ($Name) { [string]$Name } else { 'default' }
            $r = Invoke-Command -Session $sess -ArgumentList $subCmd, $sess2 -ScriptBlock {
                param($sub, $sessName)
                $ErrorActionPreference='SilentlyContinue'
                $exe = Join-Path $env:USERPROFILE '.local\bin\browser-act.exe'
                if (-not (Test-Path -LiteralPath $exe)) {
                    return [pscustomobject]@{ ok=$false; reason='browser-act.exe not found' }
                }
                $prev = $env:PYTHONUTF8
                # JS 一律写成文件用 --stdin 喂进去：命令行传 JS 时引号会被吃掉（已验证）。
                # 且必须纯 ASCII —— 中文 JS 在中文 locale 下会让 browser-act 报 230305（本机踩过），
                # 中文一律写成 \uXXXX 转义序列（这里是字面反斜杠，不是真中文字符）。
                $jsFile = Join-Path $env:TEMP ("ba_js_" + [guid]::NewGuid().ToString('N') + '.js')
                $argsStr = ''
                $feedStdin = $false
                switch ($sub) {
                    'sessions' { $argsStr = '--format text session list' }
                    'tabs'     { $argsStr = '--format text --session ' + $sessName + ' tab list' }
                    'href' {
                        [IO.File]::WriteAllText($jsFile, '(function(){return JSON.stringify({href:location.href,title:document.title});})()')
                        $argsStr = '--format text --session ' + $sessName + ' eval --stdin'; $feedStdin = $true
                    }
                    'probe' {
                        # \u9500\u552e\u7ba1\u7406 = 销售管理 ; \u67e5\u8be2 = 查询
                        $js = '(function(){var v=function(e){return !!(e&&(e.offsetWidth||e.offsetHeight||e.getClientRects().length));};var bs=[].slice.call(document.querySelectorAll("button")).filter(v).map(function(b){return (b.textContent||"").trim();});var j=bs.join("|");return JSON.stringify({href:location.href,title:document.title,nBtn:bs.length,hasSales:j.indexOf("\u9500\u552e\u7ba1\u7406")>=0,hasQuery:j.indexOf("\u67e5\u8be2")>=0,menu:(document.body.innerText||"").slice(0,120),sample:bs.slice(0,20)});})()'
                        [IO.File]::WriteAllText($jsFile, $js)
                        $argsStr = '--format text --session ' + $sessName + ' eval --stdin'; $feedStdin = $true
                    }
                    default    { return [pscustomobject]@{ ok=$false; reason='unsupported sub: ' + $sub } }
                }
                try {
                    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
                    $pinfo.FileName = $exe
                    $pinfo.WorkingDirectory = Split-Path $exe
                    $pinfo.UseShellExecute = $false
                    $pinfo.RedirectStandardOutput = $true
                    $pinfo.RedirectStandardError = $true
                    $pinfo.RedirectStandardInput = $true
                    $pinfo.StandardOutputEncoding = [Text.Encoding]::UTF8
                    $pinfo.StandardErrorEncoding  = [Text.Encoding]::UTF8
                    $pinfo.CreateNoWindow = $true
                    $pinfo.Arguments = $argsStr          # PS 5.1 没有 ArgumentList，用 Arguments 字符串
                    $env:PYTHONUTF8 = '1'
                    $p = New-Object System.Diagnostics.Process
                    $p.StartInfo = $pinfo
                    [void]$p.Start()
                    if ($feedStdin -and (Test-Path -LiteralPath $jsFile)) {
                        $p.StandardInput.Write([IO.File]::ReadAllText($jsFile))
                    }
                    $p.StandardInput.Close()
                    $so = $p.StandardOutput.ReadToEnd()
                    $se = $p.StandardError.ReadToEnd()
                    $p.WaitForExit(60000)
                    $code = $p.ExitCode
                    if (-not $p.HasExited) { $p.Kill(); $code = -2 }
                } catch { $code = -1; $so = ''; $se = 'EXC: ' + $_.Exception.Message }
                $env:PYTHONUTF8 = $prev
                if (Test-Path -LiteralPath $jsFile) { Remove-Item -LiteralPath $jsFile -Force }
                # WinRM 序列化 / JSON 编码安全：截断并剔除控制字符
                $clean = ($so + $se) -replace '[\x00-\x08\x0B\x0C\x0E-\x1F]', ' '
                if ($clean.Length -gt 1500) { $clean = $clean.Substring(0,1500) }
                return [pscustomobject]@{
                    ok      = ($code -eq 0)
                    code    = $code
                    sub     = $sub
                    session = $sessName
                    output  = $clean
                }
            }
            Emit @{ ok=$true; online=$true; action='bacall'; target=$TargetIp; bacall=$r }
        }

        'readcsv' {
            # 读吴凡机桌面文本文件全文（CSV/清单，无 64KB 限制，上限 4MB）。
            # readfile 卡 64KB 是防误读大日志；CSV 会超，所以单独开一路。
            if (-not $RelPath) { Fail 'readcsv 需要 -RelPath 桌面相对路径'; return }
            $aRel = [string]$RelPath
            $aMax = 4096
            $r = Invoke-Command -Session $sess -ArgumentList $aRel,$aMax -ScriptBlock {
                param($rel,$maxkb)
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                $p = Join-Path $desktop $rel
                if (-not (Test-Path -LiteralPath $p)) { return [pscustomobject]@{ exists=$false; path=$rel } }
                $fi = Get-Item -LiteralPath $p
                if ($fi.Length -gt ($maxkb*1024)) {
                    return [pscustomobject]@{ exists=$true; too_big=$true; kb=[int]($fi.Length/1KB) }
                }
                # -Raw 一次性读，保留行尾；encoding UTF8 兼容 CSV 里的中文
                $txt = Get-Content -LiteralPath $p -Raw -Encoding UTF8
                return [pscustomobject]@{
                    exists=$true; too_big=$false
                    mtime=$fi.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                    kb=[int]($fi.Length/1KB); content=$txt
                }
            }
            Emit @{ ok=$true; online=$true; action='readcsv'; target=$TargetIp; file=$r }
        }

        'readimage' {
            # 取吴凡机白底图的 base64。二进制没法过 readfile，只能这么传。
            # 中控台收到后落本机缓存，避免同一张图反复跨机拉。
            if (-not $RelPath) { Fail 'readimage 需要 -RelPath 桌面相对路径'; return }
            $aRel = [string]$RelPath
            $r = Invoke-Command -Session $sess -ArgumentList $aRel -ScriptBlock {
                param($rel)
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                $base = Join-Path $desktop $rel          # 传目录时自动补扩展名
                $hit = $null
                if (Test-Path -LiteralPath $base -PathType Leaf) {
                    $hit = Get-Item -LiteralPath $base
                } else {
                    foreach ($ext in @('.jpg','.jpeg','.png','.webp')) {
                        $cand = $base + $ext
                        if (Test-Path -LiteralPath $cand -PathType Leaf) { $hit = Get-Item -LiteralPath $cand; break }
                    }
                }
                if (-not $hit) { return [pscustomobject]@{ exists=$false; path=$rel } }
                if ($hit.Length -gt 6MB) {
                    return [pscustomobject]@{ exists=$true; too_big=$true; kb=[int]($hit.Length/1KB) }
                }
                $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($hit.FullName))
                return [pscustomobject]@{
                    exists=$true; too_big=$false
                    name=$hit.Name
                    mtime=$hit.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                    kb=[int]($hit.Length/1KB)
                    b64=$b64
                }
            }
            Emit @{ ok=$true; online=$true; action='readimage'; target=$TargetIp; img=$r }
        }

        'list' {
            # 列出吴凡机桌面【根目录】全部可控 bat，分两组：
            #   steps  = 数字开头的 12 步入口（'01. 备货.bat' / '05-草稿箱填写.bat'）
            #   others = 其余中文命名独立业务 bat（品质分差下架退货/在售货号/待快递揽收/跑temu-digest 等）
            # 只列桌面根目录：子目录里的 run.bat/版本管理.bat/诊断.bat 是内部脚本，不从中控台触发。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'
                $all = @(Get-ChildItem $desktop -Filter '*.bat' | Sort-Object Name)
                $steps  = @($all | Where-Object { $_.Name -match '^\d{1,2}[.\-\s]' } | ForEach-Object { [string]$_.Name })
                $others = @($all | Where-Object { $_.Name -notmatch '^\d{1,2}[.\-\s]' } | ForEach-Object { [string]$_.Name })
                [pscustomobject]@{ steps = $steps; others = $others }
            }
            $steps  = @($r.steps  | ForEach-Object { [string]$_ })
            $others = @($r.others | ForEach-Object { [string]$_ })
            Emit @{ ok=$true; online=$true; action='list'; target=$TargetIp;
                    bats = $steps; steps = $steps; others = $others;
                    count = ($steps.Count + $others.Count) }
        }

        'run' {
            # py 入口：-PyScript 优先（不需要 -Step）
            if (-not $Step -and -not $PyScript) { Fail 'run 需要 -Step 步骤号/bat名 或 -PyScript py文件名'; return }
            # step03 护栏（2-3_14天，人工确认类）
            $isStep03 = ($Step -eq '3' -or $Step -match '^0?3[.\-_\s]')
            if ($isStep03 -and -not $ForceStep03) {
                Emit @{ ok=$false; online=$true; action='run'; reason='step03-guard';
                        message='step03(2-3_14天) 属人工确认类，中控台不主动触发。确需远程触发请加 -ForceStep03。' }
                return
            }

            $aStep  = [string]$Step
            $aQuiet = [bool]$QuietLabel
            $aPy    = [string]$PyScript
            $aPyMode = [string]$PyMode
            $res = Invoke-Command -Session $sess -ArgumentList @($aStep, $aQuiet, $aPy, $aPyMode) -ScriptBlock {
                param($step,$quietLabel,$pyScript,$pyMode)
                $ErrorActionPreference='Stop'
                $desktop = Join-Path $env:USERPROFILE 'Desktop'

                # 1) 互斥护栏：已有任务在跑则拒绝（browser-act 单例）。
                #    cmd 跑任意 .bat（含中文命名业务 bat）/ powershell 跑 wrapper / python 跑业务 .py 都算忙。
                #    2026-10-12 py 入口：python.exe 命令行含 '.py' 即业务脚本（browser-act 单例互斥，宁严勿漏）。
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    # busy 节流治理 2026-10-13：cmd 判据放宽到 \.(bat|py)，与 status 的 Get-BizProcs 对齐
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.(bat|py)') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'pitfall-wrapper|wrapper|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'python.exe' -and $_.CommandLine -match '\.py')
                }
                if ($busy) {
                    return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count; bat=$null }
                }

                # 1.5) py 入口分支（-PyScript）：触发桌面 .py 而非 .bat。
                #      同样走交互计划任务（Session 1），python 用 USERPROFILE 拼接。
                #      【编码铁律 2026-10-12】$PyScript 经 WinRM 传来已可能乱码 → 不用字符串字面量，
                #      按 $PyScript 里的 ASCII 序号/正则（如 ^04[.\-\s]）在远程枚举 *.py 匹配 FileInfo，
                #      用对象本身拼命令行，中文名不经过脚本字面量。
                if ($PyScript) {
                    $pypick = $null
                    if ($PyScript -match '^0?(\d{1,2})([.\-\s]|$)') {
                        $pn = [int]$Matches[1]
                        $pypick = Get-ChildItem $desktop -Filter '*.py' |
                                  Where-Object { $_.Name -match ("^0*{0}[.\-\s]" -f $pn) } |
                                  Sort-Object Name | Select-Object -First 1
                    }
                    if (-not $pypick) {
                        # 中文名链外步：ASCII 别名映射（char 码位构建，不经中文字面量）。
                        # 查在售数据 / 待快递揽收 / 销量近7日0 / 品质分差下架退货
                        $alias = @{
                            'zaishou'  = (-join @([char]0x67E5,[char]0x5728,[char]0x552E,[char]0x6570,[char]0x636E)) + '.py'
                            'kuaidi'   = (-join @([char]0x5F85,[char]0x5FEB,[char]0x9012,[char]0x63FD,[char]0x6536)) + '.py'
                            'sales7'   = (-join @([char]0x9500,[char]0x91CF,[char]0x8FD1)) + '7' + (-join @([char]0x65E5)) + '0.py'
                            'quality'  = (-join @([char]0x54C1,[char]0x8D28,[char]0x5206,[char]0x5DEE,[char]0x4E0B,[char]0x67B6,[char]0x9000,[char]0x8D27)) + '.py'
                        }
                        if ($alias.ContainsKey($PyScript)) {
                            $want = Join-Path $desktop $alias[$PyScript]
                            if (Test-Path -LiteralPath $want) { $pypick = Get-Item -LiteralPath $want }
                        }
                    }
                    if (-not $pypick) {
                        # 兜底：ASCII 子串匹配（PyScript 传 ASCII 关键词如 '04' 或文件名 ASCII 部分）
                        $pat = ($PyScript -replace '\.py$','').Trim()
                        if ($pat -match '^[\x21-\x7e]+$') {
                            $pypick = Get-ChildItem $desktop -Filter '*.py' |
                                      Where-Object { $_.BaseName -match [regex]::Escape($pat) } |
                                      Sort-Object Name | Select-Object -First 1
                        }
                    }
                    if (-not $pypick) {
                        return [pscustomobject]@{ launched=$false; reason='py-not-found'; step=$PyScript; bat=$null }
                    }
                    $pyExe = Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python312\python.exe'
                    if (-not (Test-Path $pyExe)) {
                        $g = Get-Command python.exe -ErrorAction SilentlyContinue
                        if ($g) { $pyExe = $g.Source } else {
                            return [pscustomobject]@{ launched=$false; reason='python-not-found'; bat=$null }
                        }
                    }
                    $modeFlag = if ($PyMode -eq 'run') { '--run' } else { '--trial' }
                    # 多机迁移 2026-10-12：py 入口必须注入吴凡机浏览器 ID（tpc 读 env，
                    # 吴凡正主=temu-weiqi-daily chrome_local_114366298678362348；
                    # 试点 15:14 漏注入侥幸走对，正式切换前补上确定性）。
                    # 同场加注：吴凡机店铺名 Sewing tools（bat 版各业务实证店名，py 版读 TEMU_STORE_NAME）。
                    $envPrefix = 'set "TEMU_BROWSER_ID=chrome_local_114366298678362348"&& set "TEMU_STORE_NAME=Sewing tools"&& '
                    $taskName = 'TemuRmt_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
                    try {
                        $quietPrefix = $envPrefix
                        if ($quietLabel) { $quietPrefix += 'set "TEMU_12STEPS_RUNNING=1"&& set "TEMU_NO_LABEL_PRINT=1"&& ' }
                        # 日志重定向：python 秒退时 traceback 落在桌面 _py_run_last.log（覆盖式，永远最新一次）
                        $pyLog = Join-Path $desktop '_py_run_last.log'
                        $arg = '/c {0}cd /d "{1}" && "{2}" "{3}" {4} > "{5}" 2>&1 < nul' -f $quietPrefix,$desktop,$pyExe,$pypick.FullName,$modeFlag,$pyLog
                        $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg
                        $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                        Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                        Start-Sleep -Seconds 2
                        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                    } catch {
                        return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message); bat=$pypick.Name }
                    }
                    return [pscustomobject]@{ launched=$true; reason='ok'; bat=$pypick.Name; dir=$desktop; via='scheduled-task-interactive-py'; mode=$PyMode }
                }

                # 2) 解析 bat：候选集 = 桌面根目录【全部】bat（含中文命名独立业务脚本）。
                #    纯数字按开头序号匹配（12 步）；否则按文件名精确/前缀匹配（中文名业务 bat）。
                $cand = @(Get-ChildItem $desktop -Filter '*.bat')
                $pick = $null
                if ($step -match '^\d+$') {
                    $n = [int]$step
                    $pick = $cand | Where-Object { $_.Name -match ("^0*{0}[.\-\s]" -f $n) } |
                            Sort-Object Name | Select-Object -First 1
                } else {
                    $pat = ($step -replace '\.bat$','').Trim()
                    $pick = $cand | Where-Object { $_.BaseName.Trim() -eq $pat -or $_.Name -eq ($pat+'.bat') } | Select-Object -First 1
                    if (-not $pick) {
                        $pick = $cand | Where-Object { $_.BaseName.Trim() -like ($pat + '*') } | Sort-Object Name | Select-Object -First 1
                    }
                }
                if (-not $pick) {
                    return [pscustomobject]@{ launched=$false; reason='bat-not-found'; step=$step; bat=$null }
                }

                # 3) 交互启动：WinRM 直接 Start-Process 会落在 Session 0（看不到桌面、碰不到 Chrome）。
                #    用计划任务以“交互用户(Interactive)”身份启动 → 进程落在吴凡桌面 Session 1，
                #    等同双击，browser-act 能驱到本机 Chrome。任务启动后即注销定义，进程继续跑。
                $taskName = 'TemuRmt_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
                try {
                    # stdin 接 NUL：桌面 bat 结尾的 pause 在非交互计划任务里会永久等待按键、
                    # cmd 永不退出 -> busy 恒 1 -> 后续步骤全超时。< nul 让 pause 立即读到 EOF 返回；
                    # 用户手动双击不受影响（双击时 stdin 是控制台，pause 仍暂停）。
                    # QuietLabel（中控台定时/补跑传入）：在 cmd 会话内 set 静默标记，子进程继承，
                    # 业务脚本跳过标签机实物打印；仅本进程树生效，不写注册表。手动按钮/吴凡桌面双击不带 -> 照打。
                    $quietPrefix = ''
                    if ($quietLabel) { $quietPrefix = 'set "TEMU_12STEPS_RUNNING=1"&& set "TEMU_NO_LABEL_PRINT=1"&& ' }
                    $arg = '/c {0}cd /d "{1}" && call "{2}" < nul' -f $quietPrefix,$pick.DirectoryName, $pick.FullName
                    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message); bat=$pick.Name }
                }
                return [pscustomobject]@{ launched=$true; reason='ok'; bat=$pick.Name; dir=$pick.DirectoryName; via='scheduled-task-interactive' }
            }

            if (-not $res.launched) {
                Emit @{ ok=$false; online=$true; action='run'; reason=$res.reason;
                        busy_count=$res.busy_count; step=$Step; bat=$res.bat }
            } else {
                Emit @{ ok=$true; online=$true; action='run'; target=$TargetIp;
                        launched=$true; bat=$res.bat; dir=$res.dir; step=$Step;
                        message=('已在吴凡机后台触发: ' + $res.bat) }
            }
        }

        'runbatch' {
            # 远程拉起吴凡机现成的整批脚本：all12 = run-all12-daily.ps1（12步整批、非交互、内部串行）；
            # digest = run-digest-daily.ps1（汇总/自愈）。fire-and-forget：远程自己跑、写日志、飞书通知。
            if (-not $Batch) { Fail 'runbatch 需要 -Batch all12|digest'; return }
            $aBatch = [string]$Batch
            $aQuiet = [bool]$QuietLabel
            $res = Invoke-Command -Session $sess -ArgumentList @($aBatch, $aQuiet) -ScriptBlock {
                param($batch,$quietLabel)
                $ErrorActionPreference='Stop'
                # 互斥护栏：已有批次/步骤在跑则拒绝（browser-act 单例）。
                #    cmd 跑任意 .bat（含中文命名业务 bat）/ powershell 跑 wrapper 都算忙。
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    # busy 节流治理 2026-10-13：与 status/run 两处对齐——cmd 放宽到 \.(bat|py)，
                    # 并补上原本缺失的 python.exe \.py 判据（否则整批闸门看不见 py 业务进程）。
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.(bat|py)') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'pitfall-wrapper|wrapper|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'python.exe' -and $_.CommandLine -match '\.py')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                # 解析整批脚本（WinRM 会话用户即吴凡，USERPROFILE 指向中文目录，避开中文字面量）
                $fname = if ($batch -eq 'digest') { 'run-digest-daily.ps1' } else { 'run-all12-daily.ps1' }
                $script = Join-Path (Join-Path $env:USERPROFILE 'pitfalls') $fname
                if (-not (Test-Path $script)) {
                    $hit = Get-ChildItem (Join-Path $env:USERPROFILE 'pitfalls') -Filter $fname -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($hit) { $script = $hit.FullName } else { return [pscustomobject]@{ launched=$false; reason='script-not-found'; batch=$batch; script=$fname } }
                }
                # 交互式计划任务启动 → 进程落在吴凡桌面会话，browser-act 能驱 Chrome；启动后即注销定义
                $taskName = 'TemuRmtBatch_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
                try {
                    # QuietLabel：进程级注入静默标记（cmd 会话内 set，powershell 子进程继承），不写注册表
                    if ($quietLabel) {
                        $psArg = '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $script
                        $arg = '/c set "TEMU_12STEPS_RUNNING=1"&& set "TEMU_NO_LABEL_PRINT=1"&& powershell.exe ' + $psArg
                        $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg
                    } else {
                        $arg = '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $script
                        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
                    }
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message); batch=$batch }
                }
                return [pscustomobject]@{ launched=$true; reason='ok'; batch=$batch; script=$fname; via='scheduled-task-interactive' }
            }
            if (-not $res.launched) {
                Emit @{ ok=$false; online=$true; action='runbatch'; reason=$res.reason;
                        busy_count=$res.busy_count; batch=$Batch; script=$res.script }
            } else {
                Emit @{ ok=$true; online=$true; action='runbatch'; target=$TargetIp;
                        launched=$true; batch=$Batch; script=$res.script;
                        message=('已在吴凡机后台触发整批: ' + $res.script) }
            }
        }

        { $Action -eq 'tasksoff' -or $Action -eq 'taskson' } {
            # 禁用(tasksoff)/启用(taskson)吴凡机上的 Temu 每日计划任务（调度收归中控台后关掉，避免双跑）。
            # 任务名在远程真实枚举，不写死：匹配 Temu 相关的每日整批/摘要任务。
            # 注意：tasksoff/taskson 共用此块，$want 按 action 取真假（此前 tasksoff 是空块导致关不掉）。
            $want = ($Action -eq 'taskson')
            $r = Invoke-Command -Session $sess -ArgumentList $want -ScriptBlock {
                param($enable)
                $ErrorActionPreference='SilentlyContinue'
                # 真实任务名按关键字枚举：TemuAll2 / TemuDigest / TemuAll12_Daily0400 / TemuDigest_Daily0500 等
                $tasks = Get-ScheduledTask | Where-Object {
                    $_.TaskName -match 'Temu' -and $_.TaskName -match 'All|Digest|12|Daily'
                }
                $out = @()
                foreach ($t in $tasks) {
                    $n = $t.TaskName
                    $before = [string]$t.State
                    if ($enable) { Enable-ScheduledTask -TaskName $n | Out-Null }
                    else { Disable-ScheduledTask -TaskName $n | Out-Null }
                    $after = [string](Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue).State
                    $out += [pscustomobject]@{ name=$n; before=$before; after=$after }
                }
                if (-not $tasks) { $out += [pscustomobject]@{ name='(none-found)'; before='missing'; after='missing' } }
                return $out
            }
            Emit @{ ok=$true; online=$true; action=$Action; target=$TargetIp; enabled=$want;
                    tasks = @($r | ForEach-Object { [pscustomobject]@{ name=$_.name; before=$_.before; after=$_.after } });
                    message = ($(if ($want) { '已启用' } else { '已禁用' }) + ' 吴凡机 Temu 每日计划任务') }
        }

        # ============ PC码批量换物流（吴凡机 Sewing 店，桌面 pc码 文件夹，预约取货18:00）============
        'pccodestatus' {
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                # 中文文件名全部用代码点拼，远程块里不出现中文字面量
                $batchName = [string]([char]0x6279+[char]0x91cf+[char]0x6d41+[char]0x7a0b+'.ps1')
                $pc = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory |
                      Where-Object { $_.Name -match '^pc' } | Select-Object -First 1
                $ret = [ordered]@{ exists=$false; running=$false; newest_csv=''; newest_csv_text='';
                                    updated=''; log_tail=''; done=@{}; busy_count=0 }
                if (-not $pc) { return [pscustomobject]$ret }
                $batchPs1 = Join-Path $pc.FullName $batchName
                $ret.exists = Test-Path $batchPs1
                # 运行检测：命令行含“批量流程.ps1”，排除检测命令自身
                $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
                    $_.CommandLine -and ($_.CommandLine -like ('*' + $batchName + '*')) -and
                    ($_.CommandLine -notlike '*Get-CimInstance*')
                }
                $ret.running = [bool]$procs
                $ret.busy_count = @($procs).Count
                $logDir = Join-Path $pc.FullName 'logs'
                if (Test-Path $logDir) {
                    $all = Get-ChildItem $logDir -File
                    $csvs = @($all | Where-Object { $_.Name -like 'batch-*.csv' })
                    $webs = @($all | Where-Object { $_.Name -like 'web_pccode_*.log' })
                    $blogs = @($all | Where-Object { $_.Name -like 'batch-*.log' })
                    if ($csvs.Count) {
                        $newest = $csvs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
                        $ret.newest_csv = $newest.Name
                        $ret.updated = $newest.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                        $ret.newest_csv_text = [System.IO.File]::ReadAllText($newest.FullName, [System.Text.Encoding]::UTF8)
                    }
                    # 日志优先 web 后台启动日志，否则最新 batch log，取尾部 4000 字符
                    $cand = $null
                    if ($webs.Count) { $cand = $webs | Sort-Object LastWriteTime -Descending | Select-Object -First 1 }
                    elseif ($blogs.Count) { $cand = $blogs | Sort-Object LastWriteTime -Descending | Select-Object -First 1 }
                    if ($cand) {
                        $txt = [System.IO.File]::ReadAllText($cand.FullName, [System.Text.Encoding]::UTF8)
                        if ($txt.Length -gt 4000) { $txt = $txt.Substring($txt.Length - 4000) }
                        $ret.log_tail = $txt
                        if (-not $ret.updated) { $ret.updated = $cand.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') }
                    }
                    # 近 3 天成功记录（用于防重复）；CSV 列序固定：序号/PC号/备货单/取货时间/耗时秒/结果
                    $cut = (Get-Date).AddDays(-3)
                    $done = @{}
                    foreach ($cf in ($csvs | Where-Object { $_.LastWriteTime -ge $cut } | Sort-Object LastWriteTime -Descending)) {
                        try {
                            foreach ($row in (Import-Csv $cf.FullName)) {
                                $vals = @($row.PSObject.Properties.Value)
                                $pcv = [string]$vals[1]; $resv = [string]$vals[5]
                                if ($pcv -and $resv -eq ([string]([char]0x6210+[char]0x529f)) -and -not $done.ContainsKey($pcv)) {
                                    $done[$pcv] = [pscustomobject]@{ wb=[string]$vals[2]; tm=[string]$vals[3];
                                        when=$cf.LastWriteTime.ToString('MM-dd HH:mm') }
                                }
                            }
                        } catch {}
                    }
                    $ret.done = $done
                }
                return [pscustomobject]$ret
            }
            Emit @{ ok=$true; online=$true; action='pccodestatus'; target=$TargetIp;
                    exists=$r.exists; running=$r.running; busy_count=$r.busy_count;
                    newest_csv=$r.newest_csv; newest_csv_text=$r.newest_csv_text;
                    updated_at=$r.updated; log_tail=$r.log_tail; done=$r.done }
        }

        'pccodeguard' {
            # 读写吴凡机 pc码\config.ps1 里的 $TimeGuard / $TimeGuardHour（17:00 时间护栏开关）
            $r = Invoke-Command -Session $sess -ScriptBlock {
                param($Set, $Hour)
                $ErrorActionPreference='SilentlyContinue'
                # 中文目录名用代码点拼：pc + 码
                $pcDir = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory |
                         Where-Object { $_.Name -match '^pc' } | Select-Object -First 1
                $ret = [ordered]@{ exists=$false; file=''; on=$true; hour=17;
                                   changed=$false; backup=''; err='' }
                if (-not $pcDir) { $ret.err='no-pc-dir'; return [pscustomobject]$ret }
                $cfg = Join-Path $pcDir.FullName 'config.ps1'
                $ret.file = $cfg
                $ret.exists = Test-Path -LiteralPath $cfg
                if (-not $ret.exists) { $ret.err='no-config'; return [pscustomobject]$ret }

                $t = [System.IO.File]::ReadAllText($cfg, [System.Text.Encoding]::UTF8)
                if ($t -match '(?m)^\s*\$TimeGuard\s*=\s*\$(true|false)') {
                    $ret.on = ($Matches[1] -eq 'true')
                }
                if ($t -match '(?m)^\s*\$TimeGuardHour\s*=\s*(\d+)') { $ret.hour = [int]$Matches[1] }

                if ($Set -eq '' -and $Hour -lt 0) { return [pscustomobject]$ret }   # 只读

                $bak = $cfg + '.bak_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_guard'
                try { Copy-Item -LiteralPath $cfg -Destination $bak -Force; $ret.backup = $bak } catch {}

                $val = if ($Set -eq 'off') { '$false' }
                       elseif ($Set -eq 'on') { '$true' }
                       else { if ($ret.on) { '$true' } else { '$false' } }

                if ($t -match '(?m)^\s*\$TimeGuard\s*=\s*\$(true|false)') {
                    $t = [regex]::Replace($t, '(?m)^([ \t]*)\$TimeGuard\s*=\s*\$(true|false)', ('$1$TimeGuard     = ' + $val))
                } else {
                    $t = $t.TrimEnd() + "`r`n`r`n# --- 17:00 时间护栏开关（中控台远程写入）---`r`n`$TimeGuard     = $val`r`n`$TimeGuardHour = $($ret.hour)`r`n"
                }

                if ($Hour -ge 0 -and $Hour -le 23) {
                    $h = [int]$Hour
                    if ($t -match '(?m)^\s*\$TimeGuardHour\s*=\s*(\d+)') {
                        $t = [regex]::Replace($t, '(?m)^([ \t]*)\$TimeGuardHour\s*=\s*(\d+)', ('$1$TimeGuardHour = ' + $h))
                    } else {
                        $t = $t.TrimEnd() + "`r`n`$TimeGuardHour = $h`r`n"
                    }
                    $ret.hour = $h
                }

                [System.IO.File]::WriteAllText($cfg, $t, (New-Object System.Text.UTF8Encoding($true)))
                $ret.on = ($val -eq '$true')
                $ret.changed = $true
                return [pscustomobject]$ret
            } -ArgumentList $GuardSet, $GuardHour

            Emit @{ ok=$true; online=$true; action='pccodeguard'; target=$TargetIp;
                    exists=[bool]$r.exists; file=$r.file; on=[bool]$r.on; hour=[int]$r.hour;
                    changed=[bool]$r.changed; backup=$r.backup; err=$r.err }
        }

        'pccoderun' {
            # PC 号本地白名单校验（防命令注入），去重保序
            $pcList = @()
            $seen = @{}
            foreach ($tok in ($Pcs -split ',')) {
                $v = ($tok -replace '\s', '')
                if ($v -and ($v -match '^[A-Za-z0-9_-]{4,40}$') -and -not $seen.ContainsKey($v)) {
                    $seen[$v] = $true; $pcList += $v
                }
            }
            if (-not $pcList) { Emit @{ ok=$false; online=$true; action='pccoderun'; reason='no-pcs' }; return }
            # === gates17 gate5 gray (2026-11-06): batch needs human first-piece token; single pc = first piece allowed ===
            $g17TokDir = Join-Path $PSScriptRoot 'gates17\data\tokens'
            $g17Today = (Get-Date).ToString('yyyy-MM-dd')
            $g17Tok = Join-Path $g17TokDir ('pccoderun_' + $g17Today + '.token')
            if ($pcList.Count -gt 1 -and -not (Test-Path -LiteralPath $g17Tok)) {
                Emit @{ ok=$false; online=$true; action='pccoderun'; reason='gates17-gray-blocked'; batch=$pcList.Count; token=$g17Tok;
                        message='gates17 gate5: batch pccoderun without human-confirm token. Run 1 pc (first piece), verify it on the platform, then confirm via: python gates17\gray.py confirm --op pccoderun --by <name>' }
                return
            }
            if ($pcList.Count -eq 1) {
                try {
                    New-Item -ItemType Directory -Force -Path $g17TokDir | Out-Null
                    $g17Cl = @{ ts=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); op='pccoderun'; day=$g17Today; first_item=$pcList[0]; note='first-piece checklist: human verifies this one on platform, then confirm token to unlock batch' }
                    [IO.File]::WriteAllText((Join-Path $g17TokDir ('checklist_pccoderun_' + $g17Today + '.json')), ($g17Cl | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($true)))
                } catch {}
            }
            # === end gates17 gate5 ===
            # 17:00 护栏（快递下班，预约取货18:00安排不了；远程脚本自身也会 exit 3，这里提前挡）
            # 已接进「时间护栏开关」：读 吴凡机 pc码\config.ps1 的 $TimeGuard/$TimeGuardHour（与 pccodeguard 同一套解析）。
            # 开关=关 或 环境变量 TEMU_FORCE_AFTER_17=1 时放行，不再硬编码拦截。
            $guardBlocked = $false
            if ($env:TEMU_FORCE_AFTER_17 -ne '1') {
                $guardOn = $true; $guardHour = 17
                try {
                    $pcDirG = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory -ErrorAction SilentlyContinue |
                              Where-Object { $_.Name -match '^pc' } | Select-Object -First 1
                    if ($pcDirG) {
                        $cfgG = Join-Path $pcDirG.FullName 'config.ps1'
                        if (Test-Path -LiteralPath $cfgG) {
                            $txtG = [System.IO.File]::ReadAllText($cfgG, [System.Text.Encoding]::UTF8)
                            if ($txtG -match '(?m)^\s*\$TimeGuard\s*=\s*\$(true|false)') { $guardOn = ($Matches[1] -eq 'true') }
                            if ($txtG -match '(?m)^\s*\$TimeGuardHour\s*=\s*(\d+)') { $guardHour = [int]$Matches[1] }
                        }
                    }
                } catch {}
                if ($guardOn -and (Get-Date).Hour -ge $guardHour) { $guardBlocked = $true }
            }
            if ($guardBlocked) {
                Emit @{ ok=$false; online=$true; action='pccoderun'; reason='after-1700';
                        message='已过 17:00，快递下班，今天不能预约 18:00 取货，请明天 17:00 前再跑。' }
                return
            }
            # 关键坑：WinRM 传递 [string[]] 数组会被截断成第一个元素（实测 3 个只剩 1 个），
            # 所以列表一律传逗号串、在远程块内 split。
            # 坑（2026-10-05）：-ArgumentList 内联写 [string]$Day / [bool]$X 会被当成字符串
            # 传下去，值变成 "[string]tomorrow"，导致远程脚本 ValidateSet 直接拒绝。
            # 必须先算成变量、再用 @() 包裹传入。
            $aPc    = [string]($pcList -join ',')
            $aDay   = [string]$Day
            $aForce = [bool]$ForceRun
            $res = Invoke-Command -Session $sess -ArgumentList @($aPc, $aDay, $aForce) -ScriptBlock {
                param($pcCsv, $day, $forceRun)
                $ErrorActionPreference='Stop'
                $pcs = @($pcCsv -split ',' | Where-Object { $_ })
                $batchName = [string]([char]0x6279+[char]0x91cf+[char]0x6d41+[char]0x7a0b+'.ps1')
                $okWord    = [string]([char]0x6210+[char]0x529f)
                $pc = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory |
                      Where-Object { $_.Name -match '^pc' } | Select-Object -First 1
                if (-not $pc) { return [pscustomobject]@{ launched=$false; reason='pc-dir-not-found' } }
                $batchPs1 = Join-Path $pc.FullName $batchName
                if (-not (Test-Path $batchPs1)) { return [pscustomobject]@{ launched=$false; reason='script-not-found' } }
                # 互斥护栏：任何 browser-act 业务（.bat / wrapper / 批次）或已有批量流程在跑，都拒绝
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.bat') -or
                    ($_.Name -eq 'powershell.exe' -and ($_.CommandLine -match 'pitfall-wrapper|wrapper|rerun-step|Temu12|all12|digest' -or
                        $_.CommandLine -like ('*' + $batchName + '*')))
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                $logDir = Join-Path $pc.FullName 'logs'
                if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
                # 近 3 天已成功防重复（未带 forceRun 时只回报，不启动）
                if (-not $forceRun) {
                    $cut = (Get-Date).AddDays(-3)
                    $done = @{}
                    foreach ($cf in (Get-ChildItem $logDir -File -Filter 'batch-*.csv' | Where-Object { $_.LastWriteTime -ge $cut } | Sort-Object LastWriteTime -Descending)) {
                        try {
                            foreach ($row in (Import-Csv $cf.FullName)) {
                                $vals = @($row.PSObject.Properties.Value)
                                $pcv = [string]$vals[1]; $resv = [string]$vals[5]
                                if ($pcv -and $resv -eq $okWord -and -not $done.ContainsKey($pcv)) {
                                    $done[$pcv] = [pscustomobject]@{ wb=[string]$vals[2]; tm=[string]$vals[3]; when=$cf.LastWriteTime.ToString('MM-dd HH:mm') }
                                }
                            }
                        } catch {}
                    }
                    $hit = @{}
                    foreach ($v in $pcs) { if ($done.ContainsKey($v)) { $hit[$v] = $done[$v] } }
                    if ($hit.Count) { return [pscustomobject]@{ launched=$false; reason='need-confirm'; done=$hit } }
                }
                # 交互式计划任务启动：进程落吴凡桌面 Session 1，browser-act 能驱本机 Chrome
                $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
                $webLog = Join-Path $logDir ('web_pccode_' + [int][double]::Parse((Get-Date -UFormat %s)) + '.log')
                $pcArgs = (($pcs | ForEach-Object { '"' + $_ + '"' }) -join ' ')
                $arg = '/c cd /d "{0}" && powershell -NoProfile -ExecutionPolicy Bypass -File "{1}" -PcNumbers {2} -PickupDay {3} -AutoConfirm > "{4}" 2>&1 < nul' -f `
                       $pc.FullName, $batchPs1, $pcArgs, $day, $webLog
                $taskName = 'TemuRmtPc_' + $stamp
                try {
                    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                return [pscustomobject]@{ launched=$true; reason='ok'; count=$pcs.Count; day=$day; log=$webLog; via='scheduled-task-interactive'; arg=$arg }
            }
            if ($res.launched) {
                $dayCn = if ($Day -eq 'today') { '今天' } else { '明天' }
                Emit @{ ok=$true; online=$true; action='pccoderun'; target=$TargetIp; launched=$true;
                        count=$res.count; day=$Day; log=$res.log; arg=$res.arg;
                        message=('已在吴凡机后台启动 PC 码批量换物流：' + $res.count + ' 个包裹，取货 ' + $dayCn + ' 18:00，全自动提交') }
            } elseif ($res.reason -eq 'need-confirm') {
                Emit @{ ok=$false; online=$true; action='pccoderun'; needConfirm=$true; done=$res.done }
            } else {
                Emit @{ ok=$false; online=$true; action='pccoderun'; reason=$res.reason; busy_count=$res.busy_count }
            }
        }

        'skcretstatus' {
            # 只读：在吴凡机探 单SKC下架退货.ps1 是否在 / 是否在跑 / 上次 last_result.json + 日志尾
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                # 中文名用码点拼，远程块里不出现中文字面量（避免过 WinRM 编码坑）
                $skcName = [string]([char]0x5355) + 'SKC' + [string]([char]0x4e0b+[char]0x67b6+[char]0x9000+[char]0x8d27) + '.ps1'
                $pc = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory |
                      Where-Object { $_.Name -match '^pc' } | Select-Object -First 1
                $ret = [ordered]@{ exists=$false; running=$false; busy_count=0; updated='';
                                    log_tail=''; result_text='' }
                if (-not $pc) { return [pscustomobject]$ret }
                $skcPs1 = Join-Path $pc.FullName $skcName
                $ret.exists = Test-Path $skcPs1
                # 运行检测：命令行含该脚本名，排除检测命令自身
                $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
                    $_.CommandLine -and ($_.CommandLine -like ('*' + $skcName + '*')) -and
                    ($_.CommandLine -notlike '*Get-CimInstance*')
                }
                $ret.running = [bool]$procs
                $ret.busy_count = @($procs).Count
                # 结果目录：优先脚本同机约定的桌面\Temu中控台\skc_return_logs
                $outDir = Join-Path $env:USERPROFILE ('Desktop\' + [string]([char]0x0054+[char]0x0065+[char]0x006d+[char]0x0075+[char]0x4e2d+[char]0x63a7+[char]0x53f0) + '\skc_return_logs')
                if (-not (Test-Path $outDir)) {
                    $alt = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory -ErrorAction SilentlyContinue |
                           Where-Object { Test-Path (Join-Path $_.FullName 'skc_return_logs') } | Select-Object -First 1
                    if ($alt) { $outDir = Join-Path $alt.FullName 'skc_return_logs' }
                }
                if (Test-Path $outDir) {
                    $jp = Join-Path $outDir 'last_result.json'
                    if (Test-Path $jp) {
                        $ret.result_text = [System.IO.File]::ReadAllText($jp, [System.Text.Encoding]::UTF8)
                        $ret.updated = (Get-Item $jp).LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                    }
                    $logs = @(Get-ChildItem $outDir -File -Filter 'web_*.log' -ErrorAction SilentlyContinue |
                              Sort-Object LastWriteTime -Descending)
                    if ($logs.Count) {
                        $txt = [System.IO.File]::ReadAllText($logs[0].FullName, [System.Text.Encoding]::UTF8)
                        if ($txt.Length -gt 4000) { $txt = $txt.Substring($txt.Length - 4000) }
                        $ret.log_tail = $txt
                        if (-not $ret.updated) { $ret.updated = $logs[0].LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') }
                    }
                }
                return [pscustomobject]$ret
            }
            Emit @{ ok=$true; online=$true; action='skcretstatus'; target=$TargetIp;
                    exists=$r.exists; running=$r.running; busy_count=$r.busy_count;
                    updated_at=$r.updated; log_tail=$r.log_tail; result_text=$r.result_text }
        }

        'skcretrun' {
            # 在吴凡机后台启动 单SKC下架退货.ps1（交互式计划任务落 Session 1，browser-act 能驱本机 Chrome）
            $skcList = @()
            $seenS = @{}
            foreach ($tok in ($Skcs -split ',')) {
                $v = ($tok -replace '\s', '')
                if ($v -and ($v -match '^\d{6,20}$') -and -not $seenS.ContainsKey($v)) {
                    $seenS[$v] = $true; $skcList += $v
                }
            }
            if (-not $skcList) { Emit @{ ok=$false; online=$true; action='skcretrun'; reason='no-skcs' }; return }
            # === gates17 gate5 gray (2026-11-06): batch needs human first-piece token; single skc = first piece allowed ===
            $g17TokDirS = Join-Path $PSScriptRoot 'gates17\data\tokens'
            $g17TodayS = (Get-Date).ToString('yyyy-MM-dd')
            $g17TokS = Join-Path $g17TokDirS ('skcretrun_' + $g17TodayS + '.token')
            if ($skcList.Count -gt 1 -and -not (Test-Path -LiteralPath $g17TokS)) {
                Emit @{ ok=$false; online=$true; action='skcretrun'; reason='gates17-gray-blocked'; batch=$skcList.Count; token=$g17TokS;
                        message='gates17 gate5: batch skcretrun without human-confirm token. Run 1 skc (first piece), verify it on the platform, then confirm via: python gates17\gray.py confirm --op skcretrun --by <name>' }
                return
            }
            if ($skcList.Count -eq 1) {
                try {
                    New-Item -ItemType Directory -Force -Path $g17TokDirS | Out-Null
                    $g17ClS = @{ ts=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); op='skcretrun'; day=$g17TodayS; first_item=$skcList[0]; delist_only=[bool]$DelistOnly; note='first-piece checklist: human verifies this one on platform, then confirm token to unlock batch' }
                    [IO.File]::WriteAllText((Join-Path $g17TokDirS ('checklist_skcretrun_' + $g17TodayS + '.json')), ($g17ClS | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($true)))
                } catch {}
            }
            # === end gates17 gate5 ===
            # 关键坑：WinRM 传递 [string[]] 数组会被截断成第一个元素（实测 3 个只剩 1 个），
            # 所以这里传逗号串、在远程块内 split。
            $aSkc   = [string]($skcList -join ',')
            $aDel   = [bool]$DelistOnly
            $res = Invoke-Command -Session $sess -ArgumentList @($aSkc, $aDel) -ScriptBlock {
                param($skcCsv, $delistOnly)
                $ErrorActionPreference='Stop'
                $skcs = @($skcCsv -split ',' | Where-Object { $_ })
                $skcName = [string]([char]0x5355) + 'SKC' + [string]([char]0x4e0b+[char]0x67b6+[char]0x9000+[char]0x8d27) + '.ps1'
                $pc = Get-ChildItem (Join-Path $env:USERPROFILE 'Desktop') -Directory |
                      Where-Object { $_.Name -match '^pc' } | Select-Object -First 1
                if (-not $pc) { return [pscustomobject]@{ launched=$false; reason='pc-dir-not-found' } }
                $skcPs1 = Join-Path $pc.FullName $skcName
                if (-not (Test-Path $skcPs1)) { return [pscustomobject]@{ launched=$false; reason='script-not-found' } }
                # 已在跑同一批：直接拒绝，避免两个 browser-act 抢同一 Chrome
                $mine = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
                    $_.CommandLine -and ($_.CommandLine -like ('*' + $skcName + '*')) -and
                    ($_.CommandLine -notlike '*Get-CimInstance*')
                }
                if ($mine) { return [pscustomobject]@{ launched=$false; reason='already-running'; busy_count=@($mine).Count } }
                # 互斥护栏：任何 browser-act 业务在跑都拒绝抢浏览器
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.bat') -or
                    ($_.Name -eq 'powershell.exe' -and ($_.CommandLine -match 'pitfall-wrapper|wrapper|rerun-step|Temu12|all12|digest')) -or
                    ($_.Name -eq 'browser-act.exe')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                # 结果/日志目录（与脚本约定一致；不存在则建）
                $outDir = Join-Path $env:USERPROFILE ('Desktop\' + [string]([char]0x0054+[char]0x0065+[char]0x006d+[char]0x0075+[char]0x4e2d+[char]0x63a7+[char]0x53f0) + '\skc_return_logs')
                if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
                $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
                $webLog = Join-Path $outDir ('web_' + $stamp + '.log')
                $skcArgs = (($skcs | ForEach-Object { '"' + $_ + '"' }) -join ',')
                if ($delistOnly) { $skcArgs += ' -DelistOnly' }
                $arg = '/c cd /d "{0}" && powershell -NoProfile -ExecutionPolicy Bypass -File "{1}" -Skcs {2} -NoPause > "{3}" 2>&1 < nul' -f `
                       $pc.FullName, $skcPs1, $skcArgs, $webLog
                $taskName = 'TemuRmtSkcRet_' + $stamp
                try {
                    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                return [pscustomobject]@{ launched=$true; reason='ok'; count=$skcs.Count; delist_only=$delistOnly;
                                          log=$webLog; via='scheduled-task-interactive' }
            }
            if ($res.launched) {
                $mode = if ($DelistOnly) { '只下架' } else { '下架+退货申请' }
                Emit @{ ok=$true; online=$true; action='skcretrun'; target=$TargetIp; launched=$true;
                        count=$res.count; delist_only=$res.delist_only; log=$res.log;
                        message=('已在吴凡机后台启动 SKC ' + $mode + '：' + $res.count + ' 个 SKC，约 1~3 分钟') }
            } else {
                Emit @{ ok=$false; online=$true; action='skcretrun'; reason=$res.reason; busy_count=$res.busy_count }
            }
        }

        'skctrackstatus' {
            # 只读：在吴凡机探 wf_skctrack.ps1 是否在跑 + 上次 results.json + 日志尾
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $dir = Join-Path $env:USERPROFILE 'pitfalls\wf_skctrack'
                $ps1 = Join-Path $dir 'wf_skctrack.ps1'
                $out = Join-Path $dir 'results.json'
                $log = Join-Path $dir 'wf_skctrack.log'
                $ret = [ordered]@{ exists=$false; running=$false; busy_count=0; updated='';
                                    log_tail=''; results_text='' }
                $ret.exists = Test-Path $ps1
                # 运行检测：命令行含 wf_skctrack，排除检测命令自身
                $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
                    $_.CommandLine -and ($_.CommandLine -like '*wf_skctrack*') -and
                    ($_.CommandLine -notlike '*Get-CimInstance*')
                }
                $ret.running = [bool]$procs
                $ret.busy_count = @($procs).Count
                if (Test-Path $out) {
                    $ret.results_text = [System.IO.File]::ReadAllText($out, [System.Text.Encoding]::UTF8)
                    $ret.updated = (Get-Item $out).LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                }
                if (Test-Path $log) {
                    $txt = [System.IO.File]::ReadAllText($log, [System.Text.Encoding]::UTF8)
                    if ($txt.Length -gt 4000) { $txt = $txt.Substring($txt.Length - 4000) }
                    $ret.log_tail = $txt
                    if (-not $ret.updated) { $ret.updated = (Get-Item $log).LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') }
                }
                return [pscustomobject]$ret
            }
            Emit @{ ok=$true; online=$true; action='skctrackstatus'; target=$TargetIp;
                    exists=$r.exists; running=$r.running; busy_count=$r.busy_count;
                    updated_at=$r.updated; log_tail=$r.log_tail; results_text=$r.results_text }
        }

        'skctrackrun' {
            # 在吴凡机后台跑 SKC 批量在途跟踪（交互式计划任务落 Session 1，browser-act 能驱本机已登录 Chrome）。
            # 只读扫描发货单，不回写店铺。
            $skcList = @()
            $seenT = @{}
            foreach ($tok in ($Skcs -split ',')) {
                $v = ($tok -replace '\s', '')
                if ($v -and ($v -match '^\d{6,20}$') -and -not $seenT.ContainsKey($v)) {
                    $seenT[$v] = $true; $skcList += $v
                }
            }
            if (-not $skcList) { Emit @{ ok=$false; online=$true; action='skctrackrun'; reason='no-skcs' }; return }
            # 关键坑：WinRM 传递 [string[]] 数组会被截断成第一个元素（实测 3 个只剩 1 个），
            # 所以这里传逗号串、在远程块内 split。已在本机 PowerShell 直调验证过这两种写法的差异。
            $aSkc   = [string]($skcList -join ',')
            $res = Invoke-Command -Session $sess -ArgumentList @($aSkc) -ScriptBlock {
                param($skcCsv)
                $ErrorActionPreference='Stop'
                $skcs = @($skcCsv -split ',' | Where-Object { $_ -match '^\d{6,20}$' })
                $dir = Join-Path $env:USERPROFILE 'pitfalls\wf_skctrack'
                $ps1 = Join-Path $dir 'wf_skctrack.ps1'
                $js  = Join-Path $dir 'skc_track.js'
                $out = Join-Path $dir 'results.json'
                foreach ($need in @($ps1, $js)) {
                    if (-not (Test-Path $need)) { return [pscustomobject]@{ launched=$false; reason='script-not-found' } }
                }
                # 已在跑同一批：拒绝，避免两个 browser-act 抢同一 Chrome
                $mine = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
                    $_.CommandLine -and ($_.CommandLine -like '*wf_skctrack*') -and
                    ($_.CommandLine -notlike '*Get-CimInstance*')
                }
                if ($mine) { return [pscustomobject]@{ launched=$false; reason='already-running'; busy_count=@($mine).Count } }
                # 互斥护栏：任何 browser-act 业务在跑都拒绝抢浏览器
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'run-returns-export|run-received-export|\.bat') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'wf_pending_query|export-received|export-returns|pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'browser-act.exe')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                # 清掉上次的 results.json，避免中控台把旧结果当本次结果读走
                if (Test-Path $out) { Remove-Item $out -Force }
                $skcArg = ($skcs -join ',')
                $arg = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -TrackJs "{1}" -OutFile "{2}" -Skcs "{3}"' -f $ps1, $js, $out, $skcArg
                $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
                $taskName = 'TemuRmtSkcTrack_' + $stamp
                try {
                    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                return [pscustomobject]@{ launched=$true; reason='ok'; count=$skcs.Count;
                                          via='scheduled-task-interactive' }
            }
            if ($res.launched) {
                Emit @{ ok=$true; online=$true; action='skctrackrun'; target=$TargetIp; launched=$true;
                        count=$res.count;
                        message=('已在吴凡机（Sewing）后台开始跟踪 ' + $res.count + ' 个 SKC，全量翻页约 1~5 分钟') }
            } else {
                Emit @{ ok=$false; online=$true; action='skctrackrun'; reason=$res.reason; busy_count=$res.busy_count }
            }
        }

        'returnsrun' {
            # 在吴凡机触发 Sewing 退货导出计划任务（交互用户 Session 1，等同到点自跑）。带互斥护栏。
            $res = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='Stop'
                $tn = 'TEMU_ReturnsExport_Sewing'
                $task = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
                if (-not $task) { return [pscustomobject]@{ launched=$false; reason='task-not-found' } }
                $info = Get-ScheduledTaskInfo -TaskName $tn
                if ($task.State -eq 'Running') { return [pscustomobject]@{ launched=$false; reason='already-running'; busy_count=1 } }
                # browser-act 单例互斥：导出/其他步在跑时不重复拉浏览器
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'run-returns-export') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'export-returns|pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'browser-act.exe')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                try {
                    Start-ScheduledTask -TaskName $tn -ErrorAction Stop
                    Start-Sleep -Seconds 2
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                $st = (Get-ScheduledTask -TaskName $tn).State
                return [pscustomobject]@{ launched=$true; reason='ok'; state=[string]$st }
            }
            if (-not $res.launched) {
                Emit @{ ok=$false; online=$true; action='returnsrun'; reason=$res.reason; busy_count=$res.busy_count }
            } else {
                Emit @{ ok=$true; online=$true; action='returnsrun'; target=$TargetIp; launched=$true; state=$res.state;
                        message='已在吴凡机后台开始导出 Sewing 退货包裹，约 1~3 分钟' }
            }
        }

        'returnsopen' {
            # 在吴凡机把同款 Chrome 带到退货面单页（只开+登录预检，不导出）。交互用户 Session 1。
            $res = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='Stop'
                # browser-act 忙闲：其他步/导出在跑时不抢浏览器
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'run-returns-export|\.bat') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'export-returns|pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'browser-act.exe')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                # 定位 export-returns.ps1（吴凡机 workspace scripts）
                $scriptPath = Join-Path $env:USERPROFILE '.openclaw\workspace\scripts\export-returns.ps1'
                if (-not (Test-Path $scriptPath)) { return [pscustomobject]@{ launched=$false; reason='script-not-found' } }
                $taskName = 'TemuRmtOpen_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
                try {
                    $arg = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -OpenOnly' -f $scriptPath
                    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                return [pscustomobject]@{ launched=$true; reason='ok' }
            }
            if (-not $res.launched) {
                Emit @{ ok=$false; online=$true; action='returnsopen'; reason=$res.reason; busy_count=$res.busy_count }
            } else {
                Emit @{ ok=$true; online=$true; action='returnsopen'; target=$TargetIp; launched=$true;
                        message='已在吴凡机打开 Sewing 退货面单页（登录过期会自动先登录）' }
            }
        }

        'violationopen' {
            # 在吴凡机把同款 Chrome 带到违规处理页（只开+登录预检，不筛查/不改状态）。交互用户 Session 1。
            $res = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='Stop'
                # browser-act 忙闲：其他步/导出在跑时不抢浏览器
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.bat') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'export-returns|open-violation-page|pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'browser-act.exe')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                # 定位 open-violation-page.ps1（吴凡机 workspace scripts）
                $scriptPath = Join-Path $env:USERPROFILE '.openclaw\workspace\scripts\open-violation-page.ps1'
                if (-not (Test-Path $scriptPath)) { return [pscustomobject]@{ launched=$false; reason='script-not-found' } }
                $taskName = 'TemuRmtViolOpen_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
                try {
                    $arg = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath
                    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
                    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN,$env:USERNAME) -LogonType Interactive -RunLevel Limited
                    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force -ErrorAction Stop | Out-Null
                    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    Start-Sleep -Seconds 2
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                return [pscustomobject]@{ launched=$true; reason='ok' }
            }
            if (-not $res.launched) {
                Emit @{ ok=$false; online=$true; action='violationopen'; reason=$res.reason; busy_count=$res.busy_count }
            } else {
                Emit @{ ok=$true; online=$true; action='violationopen'; target=$TargetIp; launched=$true;
                        message='已在吴凡机打开违规处理页（登录过期会自动先登录）' }
            }
        }

        'returnsstatus' {
            # 读吴凡机 Sewing 退货任务：计划任务状态/上次结果/下次运行 + 最新调度日志尾部。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $tn = 'TEMU_ReturnsExport_Sewing'
                $ret = [ordered]@{ exists=$false; running=$false; state=''; last_run=''; last_result=$null;
                                    next_run=''; updated=''; log_tail=''; appended=''; success=$false }
                $task = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
                if (-not $task) { return [pscustomobject]$ret }
                $ret.exists = $true
                $info = Get-ScheduledTaskInfo -TaskName $tn
                $ret.state = [string]$task.State
                $ret.running = ($task.State -eq 'Running')
                $ret.last_run = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
                $ret.last_result = $info.LastTaskResult
                $ret.next_run = if ($info.NextRunTime) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
                $logDir = Join-Path $env:USERPROFILE 'pitfalls\returns_sched_logs'
                if (Test-Path $logDir) {
                    $cand = Get-ChildItem $logDir -Filter 'sched_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
                    if ($cand) {
                        $ret.updated = $cand.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                        $txt = [System.IO.File]::ReadAllText($cand.FullName, [System.Text.Encoding]::UTF8)
                        if ($txt.Length -gt 2500) { $txt = $txt.Substring($txt.Length - 2500) }
                        $ret.log_tail = $txt
                        $ret.success = ($txt -match '\[INFO\] success')
                        $m = [regex]::Match($txt, 'Appended:\s*\d+[^\r\n]*')
                        if ($m.Success) { $ret.appended = $m.Value.Trim() }
                    }
                }
                return [pscustomobject]$ret
            }
            Emit @{ ok=$true; online=$true; action='returnsstatus'; target=$TargetIp;
                    exists=$r.exists; running=$r.running; state=$r.state; last_run=$r.last_run;
                    last_result=$r.last_result; next_run=$r.next_run; updated_at=$r.updated;
                    appended=$r.appended; success=$r.success; log_tail=$r.log_tail }
        }

        'receivedrun' {
            # 在吴凡机触发 Sewing 已收货发货单导出计划任务（交互用户 Session，等同到点自跑）。带互斥护栏。
            $res = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='Stop'
                $tn = 'TEMU_ReceivedExport_Sewing'
                $task = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
                if (-not $task) { return [pscustomobject]@{ launched=$false; reason='task-not-found' } }
                if ($task.State -eq 'Running') { return [pscustomobject]@{ launched=$false; reason='already-running'; busy_count=1 } }
                # browser-act 单例互斥：任何导出/业务步在跑时不重复拉浏览器
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'run-returns-export|run-received-export') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'export-received|export-returns|pitfall-wrapper|wrapper-step|rerun-step|Temu12|all12|digest') -or
                    ($_.Name -eq 'browser-act.exe')
                }
                if ($busy) { return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                try {
                    Start-ScheduledTask -TaskName $tn -ErrorAction Stop
                    Start-Sleep -Seconds 2
                } catch {
                    return [pscustomobject]@{ launched=$false; reason=('task-launch: ' + $_.Exception.Message) }
                }
                $st = (Get-ScheduledTask -TaskName $tn).State
                return [pscustomobject]@{ launched=$true; reason='ok'; state=[string]$st }
            }
            if (-not $res.launched) {
                Emit @{ ok=$false; online=$true; action='receivedrun'; reason=$res.reason; busy_count=$res.busy_count }
            } else {
                Emit @{ ok=$true; online=$true; action='receivedrun'; target=$TargetIp; launched=$true; state=$res.state;
                        message='已在吴凡机后台开始导出 Sewing 已收货发货单，首次回看 120 天按月分块，可能 30~40 分钟' }
            }
        }

        'receivedstatus' {
            # 读吴凡机 Sewing 已收货任务状态 + state.json + 最新调度日志尾部。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $tn = 'TEMU_ReceivedExport_Sewing'
                $ret = [ordered]@{ exists=$false; running=$false; state=''; last_run=''; last_result=$null;
                                    next_run=''; updated=''; log_tail=''; success=$false; state_json=''; archive_exists=$false }
                $task = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
                if ($task) {
                    $ret.exists = $true
                    $info = Get-ScheduledTaskInfo -TaskName $tn
                    $ret.state = [string]$task.State
                    $ret.running = ($task.State -eq 'Running')
                    $ret.last_run = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
                    $ret.last_result = $info.LastTaskResult
                    $ret.next_run = if ($info.NextRunTime) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
                }
                $statePath = Join-Path $env:USERPROFILE 'pitfalls\received-shipping\state.json'
                if (Test-Path $statePath) { $ret.state_json = [System.IO.File]::ReadAllText($statePath,[System.Text.Encoding]::UTF8) }
                $archDir = Join-Path $env:USERPROFILE (Join-Path 'Desktop' ([string](-join (@(0x5df2,0x6536,0x8d27,0x53d1,0x8d27,0x5355,0x5f52,0x6863) | ForEach-Object { [char]$_ }))))
                $ret.archive_exists = Test-Path $archDir
                $logDir = Join-Path $env:USERPROFILE 'pitfalls\received_sched_logs'
                if (Test-Path $logDir) {
                    $cand = Get-ChildItem $logDir -Filter 'sched_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
                    if ($cand) {
                        $ret.updated = $cand.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                        $txt = [System.IO.File]::ReadAllText($cand.FullName, [System.Text.Encoding]::UTF8)
                        if ($txt.Length -gt 2500) { $txt = $txt.Substring($txt.Length - 2500) }
                        $ret.log_tail = $txt
                        $ret.success = ($txt -match 'SUCCESS:')
                    }
                }
                return [pscustomobject]$ret
            }
            Emit @{ ok=$true; online=$true; action='receivedstatus'; target=$TargetIp;
                    exists=$r.exists; running=$r.running; state=$r.state; last_run=$r.last_run;
                    last_result=$r.last_result; next_run=$r.next_run; updated_at=$r.updated;
                    success=$r.success; log_tail=$r.log_tail; state_json=$r.state_json; archive_exists=$r.archive_exists }
        }

        'receivedapply' {
            # 在吴凡机本地对最新 Sewing 已收货归档跑黑名单重过滤（xlsx 在远程，必须数据所在机执行）。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='Continue'
                $pyExe = $null
                foreach($c in @('C:\Program Files\Python312\python.exe','C:\Program Files\Python311\python.exe','C:\Program Files\Python310\python.exe')){
                    if(Test-Path $c){ $pyExe=$c; break }
                }
                if(-not $pyExe){ $g=Get-Command python.exe -ErrorAction SilentlyContinue; if($g){ $pyExe=$g.Source } }
                $tool = Join-Path $env:USERPROFILE '.openclaw\workspace\scripts\recv_apply_blacklist.py'
                if(-not $pyExe -or -not (Test-Path $tool)){
                    return [pscustomobject]@{ ok=$false; line=''; reason=$(if(-not $pyExe){'no-python'}else{'apply-tool-missing'}) }
                }
                $line = (& $pyExe $tool 2>&1 | Out-String)
                return [pscustomobject]@{ ok=$true; line=$line.Trim() }
            }
            if(-not $r.ok){
                Emit @{ ok=$false; online=$true; action='receivedapply'; reason=$r.reason }
            } else {
                $parsed=$null
                try { $parsed = $r.line | ConvertFrom-Json } catch {}
                Emit @{ ok=$true; online=$true; action='receivedapply'; target=$TargetIp; raw=$r.line; result=$parsed }
            }
        }

        'pendsweep' {
            # 在吴凡机桌面会话(Session 1)启动"在途超期批量查件"；-AutoAppend 让确认已收货的
            # 件经安全补录脚本自动补进收货归档（去重/备份/23列校验都在该脚本内）。-Pcs 逗号分隔。
            $pcs = "$Pcs"
            $res = Invoke-Command -Session $sess -ScriptBlock {
                param($pcs)
                $ErrorActionPreference='Stop'
                $wu = [char]0x5434 + [char]0x51e1
                $dir = "C:\Users\$wu\pitfalls\wf_pending_query"
                $ps1 = "$dir\wf_pending_query.ps1"
                $js  = "$dir\recv_pkg_detail.js"
                $out = "$dir\results.json"
                foreach($need in @($ps1,$js)){ if(-not (Test-Path $need)){ return [pscustomobject]@{ launched=$false; reason='script-missing' } } }
                $busy = Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'browser-act.exe') -or
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'wf_pending_query|export-received|export-returns|wrapper-step|rerun-step|digest')
                }
                if($busy){ return [pscustomobject]@{ launched=$false; reason='remote-busy'; busy_count=@($busy).Count } }
                if(Test-Path $out){ Remove-Item $out -Force }
                $arg = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -PkgJs "{1}" -OutFile "{2}" -Pcs "{3}" -AutoAppend' -f $ps1,$js,$out,$pcs
                $tn = 'TemuPendSweep_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
                $a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
                $principal = New-ScheduledTaskPrincipal -UserId ("DESKTOP-07I8A8M\{0}" -f $wu) -LogonType Interactive -RunLevel Limited
                Register-ScheduledTask -TaskName $tn -Action $a -Principal $principal -Force | Out-Null
                Start-ScheduledTask -TaskName $tn
                Start-Sleep -Seconds 2
                Unregister-ScheduledTask -TaskName $tn -Confirm:$false
                return [pscustomobject]@{ launched=$true; reason='ok'; task=$tn }
            } -ArgumentList $pcs
            if(-not $res.launched){
                Emit @{ ok=$false; online=$true; action='pendsweep'; reason=$res.reason; busy_count=$res.busy_count }
            } else {
                Emit @{ ok=$true; online=$true; action='pendsweep'; target=$TargetIp; launched=$true; task=$res.task;
                        message='已在吴凡机开始超期在途批量查件，约 2~3 分钟' }
            }
        }

        'pendsweepstatus' {
            # 读吴凡机超期查件进度：进程是否在跑 + results.json 当前内容。
            $r = Invoke-Command -Session $sess -ScriptBlock {
                $ErrorActionPreference='SilentlyContinue'
                $wu = [char]0x5434 + [char]0x51e1
                $out = "C:\Users\$wu\pitfalls\wf_pending_query\results.json"
                $n = @(Get-CimInstance Win32_Process | Where-Object {
                    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'wf_pending_query') -or
                    ($_.Name -eq 'browser-act.exe')
                }).Count
                $rj = $null
                if(Test-Path $out){ $rj = [System.IO.File]::ReadAllText($out,[System.Text.Encoding]::UTF8) }
                return [pscustomobject]@{ running=($n -gt 0); busy_count=$n; results_json=$rj }
            }
            Emit @{ ok=$true; online=$true; action='pendsweepstatus'; target=$TargetIp;
                    running=$r.running; busy_count=$r.busy_count; results_json=$r.results_json }
        }
    }
}
catch {
    Fail "remote: $($_.Exception.Message)"
}
finally {
    if ($sess) { Remove-PSSession $sess -ErrorAction SilentlyContinue }
}
