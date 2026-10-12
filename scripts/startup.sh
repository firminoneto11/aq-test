#!/bin/bash
set -e

python3 scripts/extraction.py;

source .env;

exec python3 manage.py prod
