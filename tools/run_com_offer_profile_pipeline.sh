#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ii_models/Users/hizhenkov/mro_kb_platform

echo "[$(date -Is)] build-com-offer-profiles started"
MRO_KB_LLM_ENABLED=1 python3 -m apps.api.server build-com-offer-profiles

echo "[$(date -Is)] reindex-com-offer-profile-vectors started"
MRO_KB_LLM_ENABLED=0 python3 -m apps.api.server reindex-com-offer-profile-vectors

echo "[$(date -Is)] profile pipeline complete"
