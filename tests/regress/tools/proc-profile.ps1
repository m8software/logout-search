# Per-process CPU and disk of the logout-search processes on Windows, every 2 s, for -Seconds (default 300): read-only.
#   powershell -File proc-profile.ps1 -Seconds 300 -Out profile.csv
# Columns: t_s, pid, process, cpu_pct (of one core), read_MBps, write_MBps, working_set_MB. Uses the process's own counters,
# changes nothing and starts nothing.
param([int]$Seconds = 300, [string]$Out = "proc-profile.csv", [string]$Match = "logout-search*")
$ErrorActionPreference = "SilentlyContinue"
$prev = @{}
$t0 = Get-Date
"t_s,pid,process,cpu_pct,read_MBps,write_MBps,ws_MB" | Out-File -Encoding ascii $Out
while (((Get-Date) - $t0).TotalSeconds -lt $Seconds) {
    Start-Sleep 2
    $now = ((Get-Date) - $t0).TotalSeconds
    foreach ($p in Get-Process -Name $Match) {
        $io = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)"
        $r = [double]$io.ReadTransferCount; $w = [double]$io.WriteTransferCount; $cpu = $p.TotalProcessorTime.TotalSeconds
        if ($prev.ContainsKey($p.Id)) {
            $o = $prev[$p.Id]; $dt = $now - $o.t
            $line = "{0},{1},{2},{3},{4},{5},{6}" -f [int]$now, $p.Id, $p.Name, [int](100 * ($cpu - $o.cpu) / $dt), [math]::Round(($r - $o.r) / 1MB / $dt, 1), [math]::Round(($w - $o.w) / 1MB / $dt, 1), [int]($p.WorkingSet64 / 1MB)
            $line | Out-File -Append -Encoding ascii $Out
        }
        $prev[$p.Id] = @{ t = $now; cpu = $cpu; r = $r; w = $w }
    }
}
