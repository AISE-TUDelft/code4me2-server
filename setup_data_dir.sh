#!/bin/bash

# Load environment variables from .env file
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# Create data directory if it doesn't exist
DATA_DIR=${DATA_DIR:-./data}
mkdir -p $DATA_DIR

# Create subdirectories for each service
mkdir -p $DATA_DIR/postgres
mkdir -p $DATA_DIR/postgres_test
mkdir -p $DATA_DIR/pgadmin
mkdir -p $DATA_DIR/redis
mkdir -p $DATA_DIR/website

echo "Data directory structure created at $DATA_DIR"
echo "You can now start the Docker environment with 'docker-compose up'"
