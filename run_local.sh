#!/bin/bash

# Set the PYTHONPATH to include the src directory
export PYTHONPATH=$PYTHONPATH:$(pwd)/src

# Run the FastAPI server
python -m src.backend.main
