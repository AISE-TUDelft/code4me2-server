#!/bin/bash

# Navigate to the root if needed
cd "$(dirname "$0")"

# Set PYTHONPATH
export PYTHONPATH=$PYTHONPATH:$(pwd)/src
echo "PYTHONPATH is $PYTHONPATH"

# Run Celery worker
PYTHONPATH=$(pwd)/src PRELOAD_MODELS=false celery -A celery_app.celery_app worker --pool=solo --loglevel=info -E -Q llm,db
#PRELOAD_MODELS=false celery -A celery_app.celery_app worker --pool=solo --loglevel=info -E -Q llm,db

# Wait for user input before exiting
read -p "Press Enter to exit..."
