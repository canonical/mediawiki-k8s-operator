#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

MODEL_UUID=$1
APP_NAME=$2
TIMEOUT=$3

LOG="/tmp/wait-for-active.$$.log"

if [ -z "$MODEL_UUID" ] || [ -z "$APP_NAME" ] || [ -z "$TIMEOUT" ]; then
	echo "Usage: $0 <model_uuid|model_name> <app_name> <timeout, e.g. 5m>"
	echo "[$(date)] missing arguments" >> $LOG
	exit 1
fi

if ! juju show-model "$MODEL_UUID" &> /dev/null; then
	echo '{"status": "model_not_found"}'
	echo "[$(date)] model not found: $MODEL_UUID" >> $LOG
	exit
fi

if ! juju show-application "$APP_NAME" --model "$MODEL_UUID" &> /dev/null; then
	echo '{"status": "app_not_found"}'
	echo "[$(date)] app not found: $APP_NAME" >> $LOG
	exit
fi

echo "[$(date)] waiting for $APP_NAME in $MODEL_UUID to be active" >> $LOG

juju wait-for application "$APP_NAME" --timeout="$TIMEOUT" --model "$MODEL_UUID" &>> $LOG
STATUS=$(juju status "$APP_NAME" --model "$MODEL_UUID" --format=json | jq -r '.applications | to_entries[0].value["application-status"].current')

echo '{"status": "'"$STATUS"'"}'
