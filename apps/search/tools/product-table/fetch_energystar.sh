#!/usr/bin/env bash
# ENERGY STAR Model Index (data.energystar.gov dataset 8wj2-sec8), paged CSV, one connection, 2 MB/s cap, resumable by page.
set -euo pipefail
out="${1:?usage: fetch_energystar.sh OUTDIR}"
mkdir -p "$out"
ua="Mozilla/5.0 (X11; Linux x86_64) data-fetch/1.0"
sel='pd_id,product_category,product_type,brand_name,model_name,model_number,additional_model_information,energy_star_partner'
n=0
while :; do
  f="$out/es-$(printf %04d "$n").csv"
  if [ ! -s "$f" ]; then
    curl -sf -m 600 --compressed --limit-rate 2m -A "$ua" -G "https://data.energystar.gov/resource/8wj2-sec8.csv" \
      --data-urlencode "\$select=$sel" --data-urlencode '$order=:id' --data-urlencode '$limit=100000' \
      --data-urlencode "\$offset=$((n * 100000))" -o "$f.part" && mv "$f.part" "$f"
    sleep 3
  fi
  lines=$(wc -l < "$f")
  [ "$lines" -lt 100000 ] && break
  n=$((n + 1))
done
echo "pages: $((n + 1))"
