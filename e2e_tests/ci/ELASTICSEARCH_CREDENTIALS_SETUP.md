# Elasticsearch Credentials Setup for E2E Tests

## Overview

The E2E tests can optionally verify vectorization by directly querying Elasticsearch to check `content_vector` dimensions. This requires Elasticsearch credentials to be configured.

## Where Elasticsearch Credentials Are Used

1. **Test Code:** `tests/e2e_tests/api_tests/test_vectorization.py`
   - Method: `get_task_from_elasticsearch()`
   - Reads from: `DAGKNOWS_ELASTIC_URL` environment variable

2. **Jenkins Pipeline:** `tests/e2e_tests/ci/e2e-tests-pipeline.groovy`
   - Stage: "Configure Test Environment"
   - Reads from: Jenkins credential `dagknows-elastic-url`
   - Writes to: `.env` file as `DAGKNOWS_ELASTIC_URL`

3. **Local Testing:** `.env` file (created from `env.template`)
   - Set `DAGKNOWS_ELASTIC_URL` in your local `.env` file

## Setting Up Elasticsearch Credentials in Jenkins

### Step 1: Create Jenkins Credential

1. Go to **Manage Jenkins** → **Credentials**
2. Click **Add Credentials** (or select your credential domain)
3. Configure:
   - **Kind:** Secret text
   - **Secret:** Your full Elasticsearch URL with embedded credentials
   - **ID:** `dagknows-elastic-url` (must match exactly)
   - **Description:** `Elasticsearch URL with credentials for vectorization tests`

### Step 2: Credential Format

Store the full URL with embedded credentials in this format:

```
https://elastic:95EtxQPgPXk4vCMluk4ZZ1jK@my-deployment-05d0c4.es.us-east-2.aws.elastic-cloud.com
```

**Format:** `https://username:password@hostname`

### Step 3: Using Environment Variables (Alternative)

If your dev environment uses environment variables, you can construct the URL:

```bash
ELASTIC_USER=elastic
ELASTIC_PASSWORD=95EtxQPgPXk4vCMluk4ZZ1jK
ELASTIC_HOST=my-deployment-05d0c4.es.us-east-2.aws.elastic-cloud.com
DAGKNOWS_ELASTIC_URL=https://${ELASTIC_USER}:${ELASTIC_PASSWORD}@${ELASTIC_HOST}
```

Then store the final `DAGKNOWS_ELASTIC_URL` value in Jenkins.

## Security Best Practices

1. **Jenkins Credentials:**
   - ✅ Store as "Secret text" credential type
   - ✅ Use descriptive ID: `dagknows-elastic-url`
   - ✅ Credentials are automatically masked in Jenkins logs
   - ✅ Only accessible to users with appropriate permissions

2. **Local Development:**
   - ✅ Add `.env` to `.gitignore` (should already be there)
   - ✅ Never commit credentials to git
   - ✅ Use `env.template` as a reference (without real credentials)

3. **Pipeline Security:**
   - ✅ Credentials are injected via `withCredentials` block
   - ✅ Credentials are masked in console output
   - ✅ Only written to `.env` file during test execution
   - ✅ `.env` file is not archived as artifact

## How It Works

### In Jenkins Pipeline

```groovy
withCredentials([
    string(credentialsId: 'dagknows-elastic-url', variable: 'ELASTIC_URL')
]) {
    sh """
    if [ -n "\${ELASTIC_URL}" ]; then
        echo "DAGKNOWS_ELASTIC_URL=\${ELASTIC_URL}" >> .env
    fi
    """
}
```

### In Test Code

```python
# Reads from environment variable
es_url = os.getenv("DAGKNOWS_ELASTIC_URL")

# Parses credentials from URL
parsed_url = urlparse(es_url)
es_username = parsed_url.username
es_password = parsed_url.password

# Uses HTTPBasicAuth for requests
auth = HTTPBasicAuth(es_username, es_password)
```

## Verification

After setting up credentials:

1. **Check Jenkins Pipeline:**
   - Run the pipeline
   - Check "Configure Test Environment" stage logs
   - Should see: "Using Elasticsearch URL from Jenkins credentials (preview: https://***:***@...)"
   - Should NOT see actual credentials in logs

2. **Check Test Execution:**
   - Vectorization tests should be able to fetch tasks from Elasticsearch
   - If credentials are missing, tests will still pass but skip ES verification

## Troubleshooting

### Issue: "Elasticsearch URL not set" warning

**Cause:** Credential `dagknows-elastic-url` not configured in Jenkins

**Solution:**
- Create the credential as described above
- Ensure ID matches exactly: `dagknows-elastic-url`
- Re-run the pipeline

### Issue: "Failed to fetch task from Elasticsearch via HTTP"

**Possible Causes:**
1. **Network access:** Jenkins agent cannot reach Elasticsearch host
2. **Invalid credentials:** Username/password incorrect
3. **Wrong hostname:** Elasticsearch hostname changed

**Solution:**
1. Verify network connectivity from Jenkins agent
2. Test credentials manually:
   ```bash
   curl -u elastic:password https://your-es-host/_cluster/health
   ```
3. Update credential in Jenkins if hostname/credentials changed

### Issue: Tests pass but vector dimensions not verified

**Cause:** Elasticsearch access not available, tests fall back to API metadata verification

**Solution:**
- This is expected behavior - tests are designed to work without ES access
- To enable ES verification, configure `dagknows-elastic-url` credential
- Ensure Jenkins agent can reach Elasticsearch host

## Alternative: Separate Credentials

If you prefer to store username and password separately:

1. Create two credentials:
   - `dagknows-elastic-username` (Secret text)
   - `dagknows-elastic-password` (Secret text)

2. Update pipeline to construct URL:
   ```groovy
   withCredentials([
       string(credentialsId: 'dagknows-elastic-username', variable: 'ES_USER'),
       string(credentialsId: 'dagknows-elastic-password', variable: 'ES_PASS'),
       string(credentialsId: 'dagknows-elastic-host', variable: 'ES_HOST')
   ]) {
       sh """
       echo "DAGKNOWS_ELASTIC_URL=https://\${ES_USER}:\${ES_PASS}@\${ES_HOST}" >> .env
       """
   }
   ```

**Note:** The current implementation uses a single credential for simplicity and security (fewer credentials to manage).

## Summary

- **Credential ID:** `dagknows-elastic-url`
- **Type:** Secret text
- **Value:** `https://username:password@hostname`
- **Optional:** Yes (tests work without it, but skip ES verification)
- **Security:** Masked in Jenkins logs, stored securely

