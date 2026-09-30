#!/usr/bin/env bash
set -e
pip install -r requirements.txt
export TZ=America/Chicago
python app.py
