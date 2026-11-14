#!/usr/bin/env bash
# Memory time series of one container, every 30 s, as CSV. Run ON testbox: ssh testbox 'bash -s ee-c out.csv' < memmon.sh
# Columns: time, docker-stats usage, python server pid Rss/Anonymous/Pss_File (smaps_rollup, kB), cgroup memory.current,
# memory.stat anon/file/shmem (bytes), memory.events oom/oom_kill counts. Stops when the container stops.
set -euo pipefail
c="${1:?container}"; out="${2:?csv}"
echo "time,docker_usage,pid,rss_kb,anon_kb,pss_file_kb,cg_current,cg_anon,cg_file,cg_shmem,oom,oom_kill" > "$out"
while [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ]; do
    id=$(docker inspect -f '{{.Id}}' "$c"); pid=$(docker inspect -f '{{.State.Pid}}' "$c")
    cg=/sys/fs/cgroup/system.slice/docker-$id.scope
    [ -d "$cg" ] || cg=$(dirname "$(sed -n 's/^0:://p' /proc/$pid/cgroup | sed 's#^#/sys/fs/cgroup#')")/$(basename "$(sed -n 's/^0:://p' /proc/$pid/cgroup)")
    sm=/proc/$pid/smaps_rollup
    rss=$(awk '/^Rss:/{print $2}' "$sm" 2>/dev/null || echo ""); anon=$(awk '/^Anonymous:/{print $2}' "$sm" 2>/dev/null || echo "")
    rf=$(awk '/^Pss_File:/{print $2}' "$sm" 2>/dev/null || echo "")
    cur=$(cat "$cg/memory.current" 2>/dev/null || echo "")
    sa=$(awk '$1=="anon"{print $2}' "$cg/memory.stat" 2>/dev/null || echo ""); sf=$(awk '$1=="file"{print $2}' "$cg/memory.stat" 2>/dev/null || echo "")
    ss=$(awk '$1=="shmem"{print $2}' "$cg/memory.stat" 2>/dev/null || echo "")
    oo=$(awk '$1=="oom"{print $2}' "$cg/memory.events" 2>/dev/null || echo ""); ok=$(awk '$1=="oom_kill"{print $2}' "$cg/memory.events" 2>/dev/null || echo "")
    du=$(docker stats --no-stream --format '{{.MemUsage}}' "$c" | tr -d ' ')
    echo "$(date +%T),$du,$pid,$rss,$anon,$rf,$cur,$sa,$sf,$ss,$oo,$ok" >> "$out"
    sleep 30
done
