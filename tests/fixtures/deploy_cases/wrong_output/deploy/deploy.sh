#!/usr/bin/env bash
printf '%s' "$DEPLOY_HOST" > deployed-host.txt
printf 'deployed to %s\n' "$DEPLOY_HOST"
