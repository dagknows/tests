#!/bin/bash
# E2E API Test Runner: User Role Assignment
# This script runs the API-based E2E test for assigning roles to users

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure PYTHONPATH is set
export PYTHONPATH="${PYTHONPATH}:${SCRIPT_DIR}"

# Create __init__.py files if needed
touch __init__.py config/__init__.py fixtures/__init__.py pages/__init__.py \
      api_tests/__init__.py ui_tests/__init__.py utils/__init__.py 2>/dev/null || true

# Set environment variables (use .env if available, otherwise defaults)
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# Default values
LOCAL=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --local)
            LOCAL=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--local]"
            exit 1
            ;;
    esac
done

# Set environment variables based on mode
if [ "$LOCAL" = true ]; then
    export DAGKNOWS_URL="${DAGKNOWS_URL:-http://localhost:3000}"
    export DAGKNOWS_PROXY="${DAGKNOWS_PROXY:-?proxy=yashlocal}"
    export JWT_TOKEN="${JWT_TOKEN:-your_local_jwt_token_here}"
    echo "Running: Local Docker Mode"
else
    export DAGKNOWS_URL="${DAGKNOWS_URL:-https://dev.dagknows.com}"
    export DAGKNOWS_PROXY="${DAGKNOWS_PROXY:-?proxy=dev1}"
    export JWT_TOKEN="${JWT_TOKEN:-your_dev_jwt_token_here}"
    echo "Running: dev.dagknows.com Mode"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  User Role Assignment API E2E Test Runner"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "PYTHONPATH: ${PYTHONPATH}"
echo "Base URL: ${DAGKNOWS_URL}"
echo ""
echo "Starting test..."
echo "Command: pytest api_tests/test_user_role_assignment_api.py::TestUserRoleAssignmentAPIE2E::test_assign_role_to_user_for_workspace_via_api -v"
echo ""

pytest api_tests/test_user_role_assignment_api.py::TestUserRoleAssignmentAPIE2E::test_assign_role_to_user_for_workspace_via_api -v

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Test completed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

