#!/usr/bin/with-contenv bash
set -e

# HA add-ons giver options i /data/options.json
export ADDON_OPTIONS="/data/options.json"

python /app/app.py
