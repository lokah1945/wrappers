#!/bin/bash
# Cron job to refresh model catalog daily

cd /root/wrapper/model_fetcher
export PATH="/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="/root/wrapper/model_fetcher:/root/wrapper/model_fetcher/src:$PYTHONPATH"

# Load environment variables if .env exists
if [ -f /root/wrapper/model_fetcher/.env ]; then
    export $(cat /root/wrapper/model_fetcher/.env | grep -v '^#' | xargs)
fi

python3 refresh_catalog.py >> /root/wrapper/model_fetcher/refresh.log 2>&1
