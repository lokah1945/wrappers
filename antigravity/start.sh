#!/bin/bash
# Start the Antigravity API Wrapper server on port 9101

# Path to the wrapper project
PROJECT_DIR="/root/wrapper/antigravity"

# Check if uvicorn is installed
if ! python3 -c "import uvicorn" 2>/dev/null; then
    echo "Error: uvicorn is not installed in the Python environment."
    exit 1
fi

echo "Starting Antigravity API Wrapper on http://localhost:9101..."
exec python3 -m uvicorn src.main:app --host 0.0.0.0 --port 9101
