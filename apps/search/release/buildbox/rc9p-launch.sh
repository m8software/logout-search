#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# usage: rc9-launch.sh N   -> unit rc9-stepN, log /data/release/rc9-stepN.log
U=rc9p-step$1; systemctl reset-failed $U 2>/dev/null || true
systemd-run --unit $U --collect --property=Nice=10 --property=StandardOutput=append:/data/release/$U.log --property=StandardError=append:/data/release/$U.log bash /mnt/c/logout-wsltest/$U.sh
