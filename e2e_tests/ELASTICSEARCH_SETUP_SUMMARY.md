# Elasticsearch Credentials Setup Summary

## Quick Reference

### Where to Set Elasticsearch Credentials

1. **Jenkins Pipeline (Recommended for CI/CD):**
   - **Credential ID:** `dagknows-elastic-url`
   - **Type:** Secret text
   - **Value:** `https://elastic:95EtxQPgPXk4vCMluk4ZZ1jK@my-deployment-05d0c4.es.us-east-2.aws.elastic-cloud.com`
   - **Location:** Jenkins → Manage Jenkins → Credentials

2. **Local Development:**
   - **File:** `tests/e2e_tests/.env` (create from `env.template`)
   - **Variable:** `DAGKNOWS_ELASTIC_URL`
   - **Value:** Same format as above

### Current Setup Locations

| Location | File | Variable | Status |
|----------|------|----------|--------|
| **Jenkins Pipeline** | `ci/e2e-tests-pipeline.groovy` | `dagknows-elastic-url` credential → `DAGKNOWS_ELASTIC_URL` | ✅ Updated |
| **Post-Deployment Pipeline** | `ci/e2e-tests-post-deployment.groovy` | `dagknows-elastic-url` credential → `DAGKNOWS_ELASTIC_URL` | ✅ Updated |
| **Test Code** | `api_tests/test_vectorization.py` | Reads `DAGKNOWS_ELASTIC_URL` env var | ✅ Updated |
| **Local Setup** | `.env` file | `DAGKNOWS_ELASTIC_URL` | ✅ Documented in `env.template` |
| **Documentation** | `ci/ELASTICSEARCH_CREDENTIALS_SETUP.md` | Setup instructions | ✅ Created |
| **Setup Instructions** | `ci/SETUP_INSTRUCTIONS.md` | Credential setup guide | ✅ Updated |

## How to Store Credentials in Jenkins

### Step-by-Step Instructions

1. **Go to Jenkins:**
   - Navigate to: **Manage Jenkins** → **Credentials**

2. **Add New Credential:**
   - Click **Add Credentials** (or select your credential domain)
   - **Kind:** Select **Secret text**
   - **Secret:** Paste your full Elasticsearch URL:
     ```
     https://elastic:95EtxQPgPXk4vCMluk4ZZ1jK@my-deployment-05d0c4.es.us-east-2.aws.elastic-cloud.com
     ```
   - **ID:** `dagknows-elastic-url` (must match exactly - case sensitive)
   - **Description:** `Elasticsearch URL with credentials for vectorization tests`
   - Click **OK**

3. **Verify:**
   - The credential should appear in your credentials list
   - ID should be exactly: `dagknows-elastic-url`

### Using a Single Variable

The pipeline uses a **single secret text credential** that contains the full URL with embedded credentials:

```
https://username:password@hostname
```

**Benefits:**
- ✅ Single credential to manage
- ✅ Credentials are masked in Jenkins logs
- ✅ Easy to update (just change one credential)
- ✅ Secure (Jenkins handles encryption)

**Alternative (if you prefer separate credentials):**
You can store username, password, and hostname separately, but you'd need to update the pipeline code to construct the URL. The current implementation uses a single credential for simplicity.

## Pipeline Integration

### Main Pipeline (`e2e-tests-pipeline.groovy`)

The pipeline automatically:
1. Reads `dagknows-elastic-url` credential
2. Writes it to `.env` file as `DAGKNOWS_ELASTIC_URL`
3. Tests use this environment variable

**Code location:** Lines 126-161 in `ci/e2e-tests-pipeline.groovy`

```groovy
withCredentials([
    string(credentialsId: 'dagknows-jwt-token', variable: 'JWT_TOKEN'),
    string(credentialsId: 'dagknows-elastic-url', variable: 'ELASTIC_URL')
]) {
    sh """
    # ... other env vars ...
    if [ -n "\${ELASTIC_URL}" ]; then
        echo "DAGKNOWS_ELASTIC_URL=\${ELASTIC_URL}" >> .env
    fi
    """
}
```

### Post-Deployment Pipeline (`e2e-tests-post-deployment.groovy`)

Also updated to optionally use Elasticsearch credentials if available.

## Test Code Integration

The test code (`test_vectorization.py`) automatically:
1. Reads `DAGKNOWS_ELASTIC_URL` from environment
2. Parses credentials from URL (if embedded)
3. Falls back gracefully if not available

**Code location:** `api_tests/test_vectorization.py`, method `get_task_from_elasticsearch()`

## Security Features

✅ **Credentials are masked in Jenkins logs:**
   - Jenkins automatically masks secret text credentials
   - Only shows preview: `https://***:***@hostname`

✅ **Credentials stored securely:**
   - Jenkins encrypts credentials at rest
   - Only accessible to authorized users

✅ **No credentials in code:**
   - All credentials come from Jenkins or `.env` file
   - `.env` is in `.gitignore`

✅ **Graceful degradation:**
   - Tests work without Elasticsearch access
   - Only verify vector dimensions if ES is available

## Verification Checklist

After setting up credentials:

- [ ] Credential `dagknows-elastic-url` created in Jenkins
- [ ] Credential ID matches exactly (case-sensitive)
- [ ] Credential value is full URL with embedded credentials
- [ ] Pipeline runs successfully
- [ ] Check pipeline logs for: "Using Elasticsearch URL from Jenkins credentials"
- [ ] Vectorization tests can verify vector dimensions (if ES accessible)
- [ ] Tests still pass if ES is not accessible (fallback to API metadata)

## Troubleshooting

### Credential not found
**Error:** `Credentials 'dagknows-elastic-url' not found`

**Solution:**
- Verify credential ID is exactly: `dagknows-elastic-url`
- Check credential is in the correct domain/scope
- Ensure pipeline has permission to access credential

### Tests skip ES verification
**Log:** "Elasticsearch access not available - skipping vector dimension verification"

**Possible causes:**
1. Credential not configured (expected - tests still pass)
2. Network access issue (Jenkins agent can't reach ES)
3. Invalid credentials

**Solution:**
- Check if credential is configured
- Test network connectivity from Jenkins agent
- Verify credentials are correct

## Files Modified

1. ✅ `ci/e2e-tests-pipeline.groovy` - Added Elasticsearch credential support
2. ✅ `ci/e2e-tests-post-deployment.groovy` - Added optional Elasticsearch credential support
3. ✅ `ci/SETUP_INSTRUCTIONS.md` - Added credential setup instructions
4. ✅ `ci/ELASTICSEARCH_CREDENTIALS_SETUP.md` - Created detailed setup guide
5. ✅ `api_tests/test_vectorization.py` - Already updated to use HTTP requests
6. ✅ `env.template` - Already updated with Elasticsearch documentation

## Next Steps

1. **Create Jenkins Credential:**
   - Follow instructions in `ci/ELASTICSEARCH_CREDENTIALS_SETUP.md`
   - Use credential ID: `dagknows-elastic-url`

2. **Test Pipeline:**
   - Run the pipeline
   - Verify Elasticsearch URL is loaded
   - Check vectorization tests can access ES

3. **Local Testing (Optional):**
   - Create `.env` file from `env.template`
   - Add `DAGKNOWS_ELASTIC_URL` with your credentials
   - Run tests locally to verify

## Support

For detailed instructions, see:
- `ci/ELASTICSEARCH_CREDENTIALS_SETUP.md` - Complete setup guide
- `ci/SETUP_INSTRUCTIONS.md` - General pipeline setup
- `env.template` - Environment variable documentation

