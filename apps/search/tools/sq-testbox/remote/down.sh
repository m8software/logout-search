#!/usr/bin/env bash
# down.sh NAME PORT — remove the engine, wait until it is gone, its guard has ended and the port is closed.
set -uo pipefail
R=$(cd "$(dirname "$0")" && pwd); cd "$R" || exit 1
name=$1 port=$2
docker logs --tail 300 "$name" > "engine.$name.log" 2>&1 || true
docker rm -f "$name" >/dev/null 2>&1 || true
t=0; while docker container inspect "$name" >/dev/null 2>&1; do (( t >= 30 )) && { echo "testbox down: $name still exists" >&2; exit 1; }; sleep 1; t=$((t + 1)); done
if [[ -f "guard.$name.pid" ]]; then
    g=$(cat "guard.$name.pid")
    if ps -o args= -p "$g" 2>/dev/null | grep -q "guard.sh $name\$"; then kill "$g" 2>/dev/null; fi
    rm -f "guard.$name.pid"
fi
t=0; while ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; do (( t >= 30 )) && { echo "testbox down: port $port still open" >&2; exit 1; }; sleep 1; t=$((t + 1)); done
echo "down"
