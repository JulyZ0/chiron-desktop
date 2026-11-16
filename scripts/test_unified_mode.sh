#!/bin/bash
# Test unified database and Redis management mode

echo "=========================================="
echo "Testing Unified DB/Redis Management"
echo "=========================================="

# Set environment variables
export USE_UNIFIED_DB_CLIENT=true
export USE_UNIFIED_REDIS_CLIENT=true
export INTERNAL_TOKEN="test-token-for-development"

echo ""
echo "1. Starting Go Gateway..."
cd X:/project/Chiron
./chiron.exe &
GATEWAY_PID=$!
sleep 3

echo ""
echo "2. Testing Go API endpoints..."
curl -s -H "X-Internal-Token: $INTERNAL_TOKEN" http://localhost:8080/v1/internal/db/health | jq .
curl -s -H "X-Internal-Token: $INTERNAL_TOKEN" http://localhost:8080/v1/internal/redis/health | jq .

echo ""
echo "3. Running Python tests..."
cd X:/project/Chiron/python-engine
python -m pytest tests/test_unified_db.py -v

echo ""
echo "4. Cleanup..."
kill $GATEWAY_PID 2>/dev/null

echo ""
echo "✅ Test completed!"
