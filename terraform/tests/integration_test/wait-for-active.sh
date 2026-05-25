#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

MODEL_UUID=$1
APP_NAME=$2
TIMEOUT=$3

LOG="/tmp/wait-for-active.$$.log"

if [ -z "$MODEL_UUID" ] || [ -z "$APP_NAME" ] || [ -z "$TIMEOUT" ]; then
	echo "Usage: $0 <model_uuid|model_name> <app_name> <timeout_seconds>"
	echo "[$(date)] missing arguments" >>$LOG
	exit 1
fi

if ! juju show-model "$MODEL_UUID" &>/dev/null; then
	echo '{"status": "model_not_found"}'
	echo "[$(date)] model not found: $MODEL_UUID" >>$LOG
	exit
fi

if ! juju show-application "$APP_NAME" --model "$MODEL_UUID" &>/dev/null; then
	echo '{"status": "app_not_found"}'
	echo "[$(date)] app not found: $APP_NAME" >>$LOG
	exit
fi

SETTLE_CHECKS=3
SETTLE_INTERVAL=10
MAX_WAIT=$TIMEOUT

echo "[$(date)] waiting for $APP_NAME in $MODEL_UUID to be active" >>$LOG

consecutive=0
elapsed=0
while [ "$consecutive" -lt "$SETTLE_CHECKS" ] && [ "$elapsed" -lt "$MAX_WAIT" ]; do
	STATUS=$(juju status "$APP_NAME" --model "$MODEL_UUID" --format=json | jq -r '.applications | to_entries[0].value["application-status"].current')
	echo "[$(date)] consecutive=$consecutive/$SETTLE_CHECKS elapsed=${elapsed}s status=$STATUS" >>$LOG
	if [ "$STATUS" = "active" ]; then
		consecutive=$((consecutive + 1))
	else
		consecutive=0
	fi
	if [ "$consecutive" -lt "$SETTLE_CHECKS" ]; then
		sleep $SETTLE_INTERVAL
		elapsed=$((elapsed + SETTLE_INTERVAL))
	fi
done

echo '{"status": "'"$STATUS"'"}'
