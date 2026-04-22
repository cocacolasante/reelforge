#!/usr/bin/env bash
set -euo pipefail
exec arq apps.worker.main.WorkerSettings "$@"
