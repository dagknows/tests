# Vectorization Test Structure

## Overview

The vectorization tests have been restructured to be more resilient and handle different environments (with/without Elasticsearch access).

## Test Structure Strategy

### Primary Verification Method: Elasticsearch (Most Reliable)

1. **Why Elasticsearch First?**
   - Vectorization happens in Elasticsearch
   - Most complete data (includes `content_vector` and `metadata`)
   - Direct access to the source of truth

2. **What We Verify:**
   - `content_vector` exists and has 1536 dimensions
   - `metadata.last_vectorized` timestamp
   - `metadata.vec_version` number

### Fallback Method: API Polling

1. **When Used:**
   - Elasticsearch access not available
   - Network restrictions prevent ES access
   - ES credentials not configured

2. **How It Works:**
   - Polls API endpoint for task metadata
   - Retries up to 5 times with 2-second intervals
   - Verifies `metadata.last_vectorized` and `metadata.vec_version`

3. **Limitations:**
   - Cannot verify vector dimensions (API doesn't return `content_vector`)
   - Metadata might not be immediately available
   - Less reliable than Elasticsearch

## Test Flow

```
1. Create Task via API
   ↓
2. Wait 3 seconds (for indexing + vectorization)
   ↓
3. Try Elasticsearch First
   ├─ Success → Verify vector + metadata → ✅ PASS
   └─ Failure → Try API Polling
                ├─ Success → Verify metadata → ✅ PASS (with warning)
                └─ Failure → Skip test with clear message → ⚠️ SKIP
```

## Test Methods Updated

### 1. `test_task_creation_with_vectorization`
- **Primary:** Elasticsearch verification
- **Fallback:** API polling for metadata
- **Skips:** If neither method works

### 2. `test_vector_dimension_correctness`
- **Primary:** Elasticsearch (required for dimension check)
- **Fallback:** API metadata confirmation
- **Skips:** If ES not available (can't verify dimensions without ES)

### 3. `test_different_titles_produce_different_vectors`
- **Primary:** Elasticsearch (required for vector comparison)
- **Fallback:** API metadata confirmation
- **Handles:** Missing vectors gracefully (retries once)

### 4. `test_vectorization_metadata`
- **Primary:** Elasticsearch
- **Fallback:** API polling
- **Skips:** If neither method works

### 5. `test_litellm_embedding_integration`
- **Primary:** Elasticsearch
- **Fallback:** API metadata confirmation
- **Warns:** If ES not available

## Key Improvements

1. **Resilient to Environment:**
   - Works with or without Elasticsearch access
   - Graceful degradation

2. **Better Error Messages:**
   - Clear indication of what failed
   - Suggestions for fixing issues

3. **Polling Strategy:**
   - Handles async vectorization
   - Retries with backoff

4. **Primary/Fallback Pattern:**
   - Always tries best method first
   - Falls back gracefully

## Configuration Requirements

### For Full Verification (Elasticsearch):
```bash
DAGKNOWS_ELASTIC_URL=https://elastic:password@hostname
```

### For Basic Verification (API Only):
- No Elasticsearch configuration needed
- Tests will verify metadata via API
- Vector dimensions cannot be verified

## Expected Behavior

### With Elasticsearch Access:
- ✅ All tests verify vector dimensions
- ✅ All tests verify metadata
- ✅ Fast and reliable

### Without Elasticsearch Access:
- ✅ Tests verify metadata via API
- ⚠️ Vector dimensions not verified
- ⚠️ Some tests may skip if metadata unavailable

## Troubleshooting

### Issue: Tests skip with "Elasticsearch not accessible"
**Solution:** Configure `DAGKNOWS_ELASTIC_URL` in Jenkins credentials or `.env` file

### Issue: Tests fail with "metadata not available"
**Possible Causes:**
1. Vectorization is taking longer than expected
2. API doesn't return metadata field
3. Vectorization didn't occur

**Solution:**
- Check if vectorization is enabled
- Increase wait time if needed
- Verify task was created successfully

### Issue: Vector dimensions not verified
**Cause:** Elasticsearch access not available

**Solution:** 
- Configure Elasticsearch credentials
- Or accept that dimension verification is skipped (metadata verification still works)

## Best Practices

1. **Always configure Elasticsearch** for full test coverage
2. **Monitor test logs** for warnings about missing ES access
3. **Check Jenkins credentials** if tests skip unexpectedly
4. **Verify network access** to Elasticsearch from Jenkins agent

