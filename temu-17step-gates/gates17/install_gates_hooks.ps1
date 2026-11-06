# install_gates_hooks.ps1 - inject gates17 judge hook into the 17 local step wrappers
# idempotent; backs up each wrapper as .bak-gates17-<ts>
$ErrorActionPreference = 'Stop'
$Pit = 'C:\Users\Administrator\pitfalls'
$Gates = 'C:\Users\Administrator\Desktop\Temu中控台\gates17'
$Py = 'C:\Program Files\Python311\python.exe'
$Ts = Get-Date -Format 'yyyyMMdd_HHmmss'

$pairs = @(
  @('pitfall-wrapper-step01.ps1', '1'),
  @('pitfall-wrapper-step02.ps1', '2'),
  @('pitfall-wrapper-step03.ps1', '3'),
  @('pitfall-wrapper-step04.ps1', '4'),
  @('pitfall-wrapper-step05.ps1', '5'),
  @('pitfall-wrapper-step06.ps1', '6'),
  @('pitfall-wrapper-step07.ps1', '7'),
  @('pitfall-wrapper-step08.ps1', '8'),
  @('pitfall-wrapper-step09.ps1', '9'),
  @('pitfall-wrapper-step10.ps1', '10'),
  @('pitfall-wrapper-step11.ps1', '11'),
  @('pitfall-wrapper-step12.ps1', '12'),
  @('pitfall-wrapper-pinzhi.ps1', '14'),
  @('pitfall-wrapper-kuaidi.ps1', '15'),
  @('pitfall-wrapper-zaishou.ps1', '29'),
  @('pitfall-wrapper-chazaishou.ps1', '34'),
  @('pitfall-wrapper-sales7.ps1', '42')
)

$MarkStart = '# === gates17 hook 20261106 ==='
$MarkEnd = '# === end gates17 hook ==='

$hookTemplate = @'
# === gates17 hook 20261106 ===
try {
  $gatesArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File','__GATES__\gates_hook.py','--store','local','--step','__STEP__','--name','__WRAP__','--exitcode',"$ec")
  if ($RunDir) { $gatesArgs += @('--rundir', $RunDir) }
  & '__PY__' @gatesArgs | Out-Host
  if ($LASTEXITCODE -eq 3) {
    Write-PitfallLog '[gates17] judge verdict=failed; false-success blocked, exit code rewritten to 1' 'ERROR'
    $ec = 1
    $global:LASTEXITCODE = 1
  }
} catch {
  Write-PitfallLog ('[gates17] hook error (not blocking business): ' + $_.Exception.Message) 'ERROR'
}
# === end gates17 hook ===
'@

foreach ($p in $pairs) {
  $k = $p[0]; $step = $p[1]
  $f = Join-Path $Pit $k
  if (-not (Test-Path -LiteralPath $f)) { Write-Output "SKIP(missing): $k"; continue }
  $raw = Get-Content -LiteralPath $f -Raw -Encoding UTF8
  if ($raw.Contains($MarkStart)) { Write-Output "SKIP(already): $k"; continue }
  Copy-Item -LiteralPath $f -Destination ($f + '.bak-gates17-' + $Ts) -Force
  $hook = $hookTemplate.Replace('__GATES__', $Gates).Replace('__STEP__', $step).Replace('__WRAP__', $k).Replace('__PY__', $Py)
  $idx = $raw.LastIndexOf('exit $ec')
  if ($idx -lt 0) { Write-Output "SKIP(no-exit): $k"; continue }
  $new = $raw.Insert($idx, $hook + "`r`n")
  [IO.File]::WriteAllText($f, $new, (New-Object System.Text.UTF8Encoding($true)))
  Write-Output "OK: $k -> step $step (backup .bak-gates17-$Ts)"
}
Write-Output 'done.'
