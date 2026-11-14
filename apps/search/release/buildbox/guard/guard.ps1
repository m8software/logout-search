# logout-gpu-guard (2026-11-11): the owner's own use of buildbox's graphics card comes first.
# Owner: "if im running youtube videos or playing video games it should automatically scale to keep my personal use
# uninterrupted", then "cant it just use whats left?" for video. So:
#   video or ordinary desktop use   our jobs keep running (video decoding has its own engine and needs little memory)
#   a game or other heavy 3D use    our jobs are stopped at once so the card's memory is free, and stay stopped until
#                                   the heavy use has been gone for QUIET_S seconds
# "Ours" on this machine: vmwp.exe (the WSL virtual machine, where the scoring model runs), llama-server.exe started
# with the Qwen3-14B model, and python.exe started from C:\logout-*. Everything else on the 3D engine is the owner's.
# A game = one program that is not ours and not the desktop compositor holding GAME_PCT or more of the 3D engine for
# three samples in a row (six seconds). Stopping our work: the current batch's `yield` file (buildbox-run.sh reads it before
# every chunk), then our llama-server.exe by process id, then the scoring process inside WSL by process id, each only
# after its command line is checked. Nothing is ever stopped by name pattern alone.
# State for other tools: C:\logout-stored\guard\state (free | game <since>), log: guard.log. Run by the scheduled task
# logout-gpu-guard at logon. GUARD_MODE=log in guard.conf makes it report only.
$ErrorActionPreference = 'SilentlyContinue'
$D = 'C:\logout-stored\guard'; $N = 'C:\logout-stored\nightly'
New-Item -ItemType Directory -Force $D | Out-Null
$GAME_PCT = 15; $VIDEO_PCT = 2; $QUIET_S = 600; $MODE = 'act'
if (Test-Path "$D\guard.conf") { Get-Content "$D\guard.conf" | ForEach-Object { if ($_ -match '^\s*(\w+)\s*=\s*(\S+)') { Set-Variable -Name $Matches[1] -Value $Matches[2] } } }
$GAME_PCT = [double]$GAME_PCT; $VIDEO_PCT = [double]$VIDEO_PCT; $QUIET_S = [int]$QUIET_S
function Log($m) { Add-Content "$D\guard.log" ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
function OurPids {
    $p = @()
    $p += (Get-Process -Name vmwp).Id
    $p += (Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | Where-Object { $_.CommandLine -match 'Qwen3-14B' }).ProcessId
    $p += (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'C:\\logout-' -or $_.ExecutablePath -match 'C:\\logout-' }).ProcessId
    return @($p | Where-Object { $_ })
}
function StopOurs {
    $cur = (Get-Content "$N\current" -TotalCount 1)
    if ($cur -and (Test-Path "$N\batches\$cur")) { Set-Content "$N\batches\$cur\yield" 'guard' }
    Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | Where-Object { $_.CommandLine -match 'Qwen3-14B' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Log "stopped llama-server $($_.ProcessId)" }
    # inside WSL: stop-wsl.sh checks each process's command line and folder, then stops it by process id
    $out = wsl -d logout-test -u root -e bash /mnt/c/logout-stored/guard/stop-wsl.sh
    if ($out) { Log ("wsl: " + ($out -join ' ')) }
}
$state = 'free'; $hits = 0; $lastGame = [datetime]::MinValue; $tick = 0
Set-Content "$D\state" 'free'; Log "guard started (mode $MODE, game at $GAME_PCT% 3D, quiet $QUIET_S s)"
while ($true) {
    $ours = OurPids
    $by = @{}; $vid = 0.0
    foreach ($c in (Get-Counter '\GPU Engine(*)\Utilization Percentage').CounterSamples) {
        if ($c.InstanceName -match '^pid_(\d+)_.*engtype_(\w+)$') {
            $p = [int]$Matches[1]; $t = $Matches[2]
            if ($ours -contains $p) { continue }
            if ($t -eq '3D') { $by[$p] = [double]$by[$p] + $c.CookedValue }
            elseif ($t -like 'VideoDecode*') { $vid += $c.CookedValue }
        }
    }
    $top = $by.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1
    $name = ''; if ($top) { $name = (Get-Process -Id $top.Key).ProcessName }
    # 2026-11-12, owner: "lr is not appropriately ramping down when I am trying to watch tv". Video playing counts as
    # the owner using the machine, exactly like a game: everything of ours stops, including the answer-builder
    # container in WSL. (The day before he had asked whether it could "just use what's left" during video; in practice
    # it did not leave enough.)
    $game = ($top -and $top.Value -ge $GAME_PCT -and $name -ne 'dwm') -or ($vid -ge $VIDEO_PCT)
    if ($game) { $hits++; $lastGame = Get-Date } else { $hits = 0 }
    if ($state -eq 'free' -and $hits -ge 3) {
        $state = 'game'; Set-Content "$D\state" ("game " + (Get-Date -Format s))
        Log ("owner is using the card ($name {0:N0}% 3D, video {1:N0}%): stepping aside" -f $(if ($top) { $top.Value } else { 0 }), $vid)
        if ($MODE -eq 'act') { StopOurs }
    } elseif ($state -eq 'game') {
        if ($MODE -eq 'act' -and $hits -ge 1 -and ($tick % 15) -eq 0) { StopOurs }      # anything of ours that started meanwhile
        if (((Get-Date) - $lastGame).TotalSeconds -ge $QUIET_S) { $state = 'free'; Set-Content "$D\state" 'free'; Log 'quiet again: free' }
    }
    if (($tick % 30) -eq 0) {
        $m = (nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader)
        Log ("sample state=$state top3d=$name {0:N0}% video={1:N0}% gpu=$m ours=$($ours -join ',')" -f $(if ($top) { $top.Value } else { 0 }), $vid)
    }
    $tick++; Start-Sleep -Seconds 2
}
