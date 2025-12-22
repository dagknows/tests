# Environment Setup Guide for E2E Tests

## Quick Setup (Recommended)

```bash
cd tests/e2e_tests

# 1. Activate virtual environment
source venv/bin/activate

# 2. Run the setup script (sets PYTHONPATH)
bash setup_environment.sh

# 3. Run tests
pytest api_tests/test_user_role_assignment_api.py -v
```

## Detailed Setup

### Step 1: Activate Virtual Environment

```bash
cd tests/e2e_tests
source venv/bin/activate
```

### Step 2: Set Up Environment

**Option A: Use the setup script (Recommended)**
```bash
bash setup_environment.sh
```

**Option B: Source the environment script**
```bash
source setup_env.sh
```

**Option C: Set PYTHONPATH manually**
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### Step 3: Verify Setup

```bash
# Check PYTHONPATH is set
echo $PYTHONPATH

# Should show: ...:/path/to/tests/e2e_tests

# Test import
python -c "from fixtures.api_client import create_api_client; print('✓ Import successful')"
```

### Step 4: Run Tests

```bash
# Run specific test
pytest api_tests/test_user_role_assignment_api.py -v

# Or use the run script
./run_user_role_assignment_test.sh
```

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'fixtures'"

**Solution 1: Set PYTHONPATH**
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
pytest api_tests/test_user_role_assignment_api.py -v
```

**Solution 2: Use pytest with pythonpath**
```bash
# pytest.ini already has pythonpath = . configured
# But you can also run:
PYTHONPATH=. pytest api_tests/test_user_role_assignment_api.py -v
```

**Solution 3: Run from the test directory**
```bash
cd tests/e2e_tests
python -m pytest api_tests/test_user_role_assignment_api.py -v
```

### Issue: "Bad substitution" error

**Cause:** Script is being run with `sh` instead of `bash`

**Solution:**
```bash
# Use bash explicitly
bash setup.sh
bash setup_env.sh

# OR source it (which uses current shell)
source setup_env.sh
```

### Issue: PYTHONPATH not persisting

**Solution:** Add to your shell profile
```bash
# Add to ~/.bashrc or ~/.zshrc
echo 'export PYTHONPATH="${PYTHONPATH}:/home/ubuntu/tests/e2e_tests"' >> ~/.bashrc
source ~/.bashrc
```

## Docker Setup (Optional)

If you want to run tests in a Docker container:

```bash
# Build the Docker image (if needed)
cd tests/e2e_tests/ci
docker build -f Dockerfile.e2e-agent -t e2e-test-agent .

# Run tests in container
docker run -it --rm \
  -v $(pwd):/workspace \
  -w /workspace \
  -e PYTHONPATH=/workspace \
  e2e-test-agent \
  pytest api_tests/test_user_role_assignment_api.py -v
```

## Verification Checklist

- [ ] Virtual environment activated (`which python` shows venv path)
- [ ] PYTHONPATH includes test directory (`echo $PYTHONPATH`)
- [ ] `.env` file exists with credentials
- [ ] Can import fixtures: `python -c "from fixtures.api_client import create_api_client"`
- [ ] pytest can find tests: `pytest --collect-only api_tests/`

## Quick Test

```bash
# One-liner to test everything
cd tests/e2e_tests && \
source venv/bin/activate && \
export PYTHONPATH="${PYTHONPATH}:$(pwd)" && \
python -c "from fixtures.api_client import create_api_client; print('✓ Setup OK')" && \
pytest api_tests/test_user_role_assignment_api.py::TestUserRoleAssignmentAPIE2E::test_assign_role_to_user_for_workspace_via_api -v
```

