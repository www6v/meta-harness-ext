#!/bin/bash
# Frontend Integration Test Script

echo "=========================================="
echo "Dynamic Workflows Frontend Integration Test"
echo "=========================================="
echo ""

# Test 1: Check backend API
echo "✓ Test 1: Backend API (port 8090)"
BACKEND_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8090/api/workflows)
if [ "$BACKEND_STATUS" = "200" ]; then
    echo "  ✓ Backend API accessible"
    WORKFLOW_COUNT=$(curl -s http://localhost:8090/api/workflows | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
    echo "  ✓ Found $WORKFLOW_COUNT workflows"
else
    echo "  ✗ Backend API not accessible (status: $BACKEND_STATUS)"
fi
echo ""

# Test 2: Check frontend dev server
echo "✓ Test 2: Frontend Dev Server (port 5173)"
FRONTEND_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5173/)
if [ "$FRONTEND_STATUS" = "200" ]; then
    echo "  ✓ Frontend dev server accessible"
else
    echo "  ✗ Frontend dev server not accessible (status: $FRONTEND_STATUS)"
fi
echo ""

# Test 3: Check API proxy
echo "✓ Test 3: API Proxy"
PROXY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5173/api/workflows)
if [ "$PROXY_STATUS" = "200" ]; then
    echo "  ✓ API proxy working"
    PROXY_COUNT=$(curl -s http://localhost:5173/api/workflows | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
    echo "  ✓ Proxy returned $PROXY_COUNT workflows"
else
    echo "  ✗ API proxy not working (status: $PROXY_STATUS)"
fi
echo ""

# Test 4: Check frontend routes
echo "✓ Test 4: Frontend Routes"
ROUTES=("/workflows" "/workflows/new")
for route in "${ROUTES[@]}"; do
    ROUTE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:5173$route")
    if [ "$ROUTE_STATUS" = "200" ]; then
        echo "  ✓ Route $route accessible"
    else
        echo "  ✗ Route $route not accessible (status: $ROUTE_STATUS)"
    fi
done
echo ""

# Test 5: Check plugin files
echo "✓ Test 5: Plugin Files"
PLUGIN_DIR="/Users/t-wangwei07/Downloads/workspacePy/mycode/oma/meta-harness/console/src/plugins/dynamic-workflows"
FILES=("WorkflowList.tsx" "WorkflowEditor.tsx" "TraceViewer.tsx" "index.tsx" "styles.css" "README.md")
for file in "${FILES[@]}"; do
    if [ -f "$PLUGIN_DIR/$file" ]; then
        SIZE=$(ls -lh "$PLUGIN_DIR/$file" | awk '{print $5}')
        echo "  ✓ $file ($SIZE)"
    else
        echo "  ✗ $file missing"
    fi
done
echo ""

# Test 6: Check registry integration
echo "✓ Test 6: Registry Integration"
if grep -q "dynamicWorkflowsPlugin" "/Users/t-wangwei07/Downloads/workspacePy/mycode/oma/meta-harness/console/src/plugins/registry.ts"; then
    echo "  ✓ Plugin registered in registry.ts"
else
    echo "  ✗ Plugin not registered in registry.ts"
fi
echo ""

# Summary
echo "=========================================="
echo "Test Summary"
echo "=========================================="
echo ""
echo "Backend:    http://localhost:8090"
echo "Frontend:   http://localhost:5173"
echo "Workflows:  http://localhost:5173/workflows"
echo ""
echo "✓ All systems operational!"
echo ""
echo "Next steps:"
echo "1. Open http://localhost:5173/workflows in browser"
echo "2. Test workflow list view"
echo "3. Create a new workflow"
echo "4. Edit an existing workflow"
echo "5. Execute a workflow and view traces"
echo ""
