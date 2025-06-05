#!/bin/bash

export PYTHONPATH=$PYTHONPATH:$(pwd)/src
PRELOAD_MODELS=true celery -A celery_app.celery_app worker --pool=solo --loglevel=info -E -Q llm,db
