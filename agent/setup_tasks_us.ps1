# 미국 주식 (실제 돈 · KIS) 작업 스케줄러 등록. 한 번만 실행. 지우려면: .\setup_tasks_us.ps1 -Remove
# 미국 장은 한국 시간 밤 (23:30~06:00, 서머타임 22:30~05:00) 이고 서머타임마다 1시간씩 움직임
# → 시각을 고정해 걸지 않고, 매일 21:00~08:00 15분마다 run_us.bat tick 만 부름.
#   tick 이 뉴욕 시간 · NYSE 휴장일을 보고 준비 · 판단 · 마감 · 회고 · 조정 · 보고서를 고르고, 감시 · 잔고 루프가 죽어 있으면 다시 띄움
# 국내 대회 작업(Hynix298-*)은 건드리지 않음
param([switch]$Remove)

$agent = $PSScriptRoot
$live  = Join-Path (Split-Path $agent) "live"
$tasks = @(
  @{ Name = "SigmaUS-tick";     Time = "21:00"; Cmd = "`"$agent\run_us.bat`" tick"; Every = 15; For = "11:00" },
  @{ Name = "SigmaUS-monitor";  Time = "21:15"; Cmd = "`"$agent\run_us.bat`" monitor" },
  @{ Name = "SigmaUS-snapshot"; Time = "21:15"; Cmd = "`"$live\run_snapshot_us.bat`"" }
)
foreach ($t in $tasks) {
  schtasks /Delete /TN $t.Name /F 2>$null | Out-Null
  if (-not $Remove) {
    $rep = if ($t.Every) { @("/RI", $t.Every, "/DU", $t.For) } else { @() }
    schtasks /Create /TN $t.Name /TR $t.Cmd /SC DAILY /ST $t.Time @rep /F | Out-Null
    # 감시 · 잔고 루프는 장 끝날 때까지 돌아야 해서 시간 제한 12시간. 이미 돌고 있으면 새로 안 띄움
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
         -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
    Set-ScheduledTask -TaskName $t.Name -Settings $s | Out-Null
    Write-Output ("등록: {0}  매일 {1}{2}" -f $t.Name, $t.Time, $(if ($t.Every) { " 부터 $($t.Every)분마다 $($t.For)시간" } else { "" }))
  }
}
if ($Remove) { Write-Output "미국 작업을 모두 삭제했습니다." }
