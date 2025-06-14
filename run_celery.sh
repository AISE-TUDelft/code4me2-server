#!/bin/bash

export PYTHONPATH=$PYTHONPATH:$(pwd)/src

if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

echo "MODEL_CACHE_DIR: $MODEL_CACHE_DIR"

PRELOAD_MODELS=true PYTHONPATH=$(pwd)/src celery -A celery_app.celery_app worker --pool=solo --loglevel=info -E -Q llm,db
