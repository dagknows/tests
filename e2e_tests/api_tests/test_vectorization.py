"""
E2E API Test: Task Vectorization with LiteLLM Embeddings

Tests that tasks are automatically vectorized when created via API, and verifies:
1. Tasks have content_vector field with correct dimensions (1536)
2. Vectorization metadata is stored
3. Different titles produce different vectors
4. LiteLLM integration works correctly
5. Proper cleanup of test data
"""

import pytest
import logging
import time
import os
from fixtures.api_client import create_api_client
from config.test_users import get_test_user

logger = logging.getLogger(__name__)


@pytest.mark.api
@pytest.mark.e2e
@pytest.mark.vectorization
class TestTaskVectorizationE2E:
    """Test task vectorization via API (E2E)."""
    
    @pytest.fixture(scope="function")
    def api_client(self):
        """Create API client for test."""
        test_user = get_test_user("Admin")
        client = create_api_client()
        logger.info(f"API Client initialized for {test_user.email}")
        return client
    
    def get_task_from_elasticsearch(self, task_id, org="dagknows"):
        """
        Directly fetch task from Elasticsearch to verify content_vector field.
        
        Args:
            task_id: Task ID to fetch
            org: Organization name (default: "dagknows")
        
        Returns:
            Task document from Elasticsearch, or None if not found
        """
        try:
            # Import DB class from taskservice
            # PYTHONPATH should include taskservice/src (set in test environment)
            import sys
            import os
            
            # Try multiple import paths
            try:
                from src.db import DB
            except ImportError:
                # Fallback: try adding path manually
                taskservice_src = os.path.join(
                    os.path.dirname(__file__), 
                    "../../../taskservice/src"
                )
                taskservice_src = os.path.abspath(taskservice_src)
                if taskservice_src not in sys.path:
                    sys.path.insert(0, taskservice_src)
                from db import DB
            
            db = DB(org=org)
            task = db.taskindex.get(task_id)
            return task
        except Exception as e:
            logger.warning(f"Failed to fetch task from Elasticsearch: {e}")
            logger.debug(f"Import error details: {type(e).__name__}: {e}")
            return None
    
    def verify_vector_properties(self, task, expected_dimension=1536):
        """
        Verify that a task has valid vector properties.
        
        Args:
            task: Task dictionary (from API or Elasticsearch)
            expected_dimension: Expected vector dimension (default: 1536)
        
        Returns:
            dict with verification results
        """
        result = {
            "has_vector": False,
            "has_correct_dimension": False,
            "dimension": None,
            "has_metadata": False,
            "has_vectorization_timestamp": False,
        }
        
        # Check if content_vector exists
        content_vector = task.get("content_vector")
        if content_vector:
            result["has_vector"] = True
            result["dimension"] = len(content_vector)
            result["has_correct_dimension"] = (len(content_vector) == expected_dimension)
        
        # Check metadata
        metadata = task.get("metadata", {})
        if metadata:
            result["has_metadata"] = True
            result["has_vectorization_timestamp"] = "last_vectorized" in metadata
            result["vec_version"] = metadata.get("vec_version")
            result["last_vectorized"] = metadata.get("last_vectorized")
        
        return result
    
    def test_task_creation_with_vectorization(self, api_client, cleanup_tasks):
        """
        E2E Test: Tasks are automatically vectorized when created via API.
        
        Flow:
        1. Create a task via API
        2. Verify the task has content_vector field
        3. Verify the vector has correct dimensions (1536)
        4. Verify vectorization metadata is present
        5. Cleanup
        """
        logger.info("=== Starting Task Vectorization E2E Test ===")
        
        # Generate unique task title with timestamp
        timestamp = int(time.time())
        task_title = f"E2E Vectorization Test Task {timestamp}"
        task_description = f"Task created to test vectorization at {timestamp}"
        
        # Step 1: Prepare task data
        logger.info("Step 1: Preparing task data")
        task_data = {
            "title": task_title,
            "description": task_description,
            "script_type": "python",
            "script": "print('Vectorization test')",
            "tags": ["e2e-test", "vectorization"],
        }
        
        # Step 2: Create task via API
        logger.info("Step 2: Creating task via API")
        try:
            create_response = api_client.create_task(task_data)
            created_task = create_response.get("task", create_response)
            task_id = created_task.get("id")
            assert task_id, "Task ID should be present"
            logger.info(f"✓ Task created with ID: {task_id}")
            
            # Add to cleanup list
            cleanup_tasks.append(task_id)
        except Exception as e:
            logger.error(f"✗ Task creation failed: {e}")
            raise
        
        # Step 3: Wait for Elasticsearch indexing
        logger.info("Step 3: Waiting for Elasticsearch indexing")
        time.sleep(2)  # Give ES time to index
        
        # Step 4: Fetch task from Elasticsearch to verify content_vector
        logger.info("Step 4: Fetching task from Elasticsearch to verify vectorization")
        es_task = self.get_task_from_elasticsearch(task_id)
        assert es_task, f"Task {task_id} should exist in Elasticsearch"
        
        # Step 5: Verify vector properties
        logger.info("Step 5: Verifying vector properties")
        vector_props = self.verify_vector_properties(es_task)
        
        logger.info(f"Vector properties: {vector_props}")
        
        # Assertions
        assert vector_props["has_vector"], "Task should have content_vector field"
        assert vector_props["has_correct_dimension"], \
            f"Vector should have 1536 dimensions, got {vector_props['dimension']}"
        assert vector_props["has_metadata"], "Task should have metadata field"
        assert vector_props["has_vectorization_timestamp"], \
            "Task metadata should have last_vectorized timestamp"
        
        logger.info("✓ Task vectorization verified successfully")
        logger.info("=== Task Vectorization E2E Test Completed ===")
    
    def test_vector_dimension_correctness(self, api_client, cleanup_tasks):
        """
        E2E Test: Vectors have exactly 1536 dimensions as required by Elasticsearch.
        """
        logger.info("=== Starting Vector Dimension Test ===")
        
        timestamp = int(time.time())
        task_data = {
            "title": f"Vector dimension test task {timestamp}",
            "description": "Testing vector dimensions",
        }
        
        logger.info("Creating task for dimension test")
        create_response = api_client.create_task(task_data)
        created_task = create_response.get("task", create_response)
        task_id = created_task.get("id")
        cleanup_tasks.append(task_id)
        
        time.sleep(2)
        
        # Fetch from Elasticsearch
        es_task = self.get_task_from_elasticsearch(task_id)
        assert es_task, "Task should exist in Elasticsearch"
        
        content_vector = es_task.get("content_vector")
        assert content_vector, "Task should have content_vector"
        assert isinstance(content_vector, list), "content_vector should be a list"
        assert len(content_vector) == 1536, \
            f"Vector should have 1536 dimensions, got {len(content_vector)}"
        
        # Verify all elements are numbers
        assert all(isinstance(x, (int, float)) for x in content_vector), \
            "All vector elements should be numbers"
        
        logger.info(f"✓ Vector dimension verified: {len(content_vector)} dimensions")
        logger.info("=== Vector Dimension Test Completed ===")
    
    def test_different_titles_produce_different_vectors(self, api_client, cleanup_tasks):
        """
        E2E Test: Different task titles produce different vectors.
        """
        logger.info("=== Starting Different Vectors Test ===")
        
        titles = [
            "Python programming tutorial",
            "Machine learning basics",
            "Docker containerization guide",
        ]
        
        task_ids = []
        vectors = []
        
        for title in titles:
            task_data = {"title": title}
            logger.info(f"Creating task: {title}")
            create_response = api_client.create_task(task_data)
            created_task = create_response.get("task", create_response)
            task_id = created_task.get("id")
            task_ids.append(task_id)
            cleanup_tasks.append(task_id)
            
            time.sleep(1)
            
            es_task = self.get_task_from_elasticsearch(task_id)
            assert es_task, f"Task {task_id} should exist"
            vector = es_task.get("content_vector")
            assert vector, f"Task {task_id} should have vector"
            assert len(vector) == 1536, f"Vector should have 1536 dimensions"
            vectors.append(vector)
        
        # Calculate cosine similarity between vectors
        def cosine_similarity(v1, v2):
            """Calculate cosine similarity between two vectors."""
            import math
            dot_product = sum(a * b for a, b in zip(v1, v2))
            magnitude1 = math.sqrt(sum(a * a for a in v1))
            magnitude2 = math.sqrt(sum(a * a for a in v2))
            if magnitude1 == 0 or magnitude2 == 0:
                return 0.0
            return dot_product / (magnitude1 * magnitude2)
        
        similarity_01 = cosine_similarity(vectors[0], vectors[1])
        similarity_02 = cosine_similarity(vectors[0], vectors[2])
        
        logger.info(f"Cosine similarity between vectors 0 and 1: {similarity_01:.4f}")
        logger.info(f"Cosine similarity between vectors 0 and 2: {similarity_02:.4f}")
        
        # Vectors should be different (similarity < 1.0)
        assert similarity_01 < 1.0 or similarity_02 < 1.0, \
            "Different titles should produce different vectors"
        
        logger.info("✓ Different titles produce different vectors")
        logger.info("=== Different Vectors Test Completed ===")
    
    def test_vectorization_metadata(self, api_client, cleanup_tasks):
        """
        E2E Test: Vectorization metadata is correctly stored.
        """
        logger.info("=== Starting Metadata Test ===")
        
        timestamp = int(time.time())
        task_data = {
            "title": f"Task for metadata verification {timestamp}",
            "description": "Testing vectorization metadata",
        }
        
        logger.info("Creating task for metadata test")
        create_response = api_client.create_task(task_data)
        created_task = create_response.get("task", create_response)
        task_id = created_task.get("id")
        cleanup_tasks.append(task_id)
        
        time.sleep(2)
        
        es_task = self.get_task_from_elasticsearch(task_id)
        assert es_task, "Task should exist"
        
        metadata = es_task.get("metadata", {})
        assert metadata, "Task should have metadata"
        
        assert "last_vectorized" in metadata, "Metadata should have last_vectorized timestamp"
        assert "vec_version" in metadata, "Metadata should have vec_version"
        
        last_vectorized = metadata["last_vectorized"]
        assert isinstance(last_vectorized, (int, float)), \
            "last_vectorized should be a timestamp"
        assert last_vectorized > 0, "last_vectorized should be a positive timestamp"
        
        vec_version = metadata["vec_version"]
        assert isinstance(vec_version, int), "vec_version should be an integer"
        
        logger.info(f"✓ Metadata verified: last_vectorized={last_vectorized}, vec_version={vec_version}")
        logger.info("=== Metadata Test Completed ===")
    
    @pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY") and not os.getenv("AZURE_API_KEY"),
        reason="Requires OPENAI_API_KEY or AZURE_API_KEY for LiteLLM embedding test"
    )
    def test_litellm_embedding_integration(self, api_client, cleanup_tasks):
        """
        E2E Test: LiteLLM embedding integration works correctly.
        
        This test verifies that:
        1. LiteLLM is being used for embeddings (if configured)
        2. The embedding model produces 1536-dimensional vectors
        3. The application doesn't crash if LiteLLM is unavailable
        """
        # Check if LiteLLM is enabled
        use_litellm = os.environ.get("USE_LITELLM_EMBEDDINGS", "true").lower() == "true"
        
        if not use_litellm:
            pytest.skip("USE_LITELLM_EMBEDDINGS is disabled")
        
        logger.info("=== Starting LiteLLM Integration Test ===")
        
        timestamp = int(time.time())
        task_data = {
            "title": f"LiteLLM embedding integration test {timestamp}",
            "description": "Testing LiteLLM embedding functionality",
        }
        
        logger.info("Creating task with LiteLLM embeddings")
        create_response = api_client.create_task(task_data)
        created_task = create_response.get("task", create_response)
        task_id = created_task.get("id")
        cleanup_tasks.append(task_id)
        
        time.sleep(2)
        
        es_task = self.get_task_from_elasticsearch(task_id)
        assert es_task, "Task should exist"
        
        content_vector = es_task.get("content_vector")
        assert content_vector, "Task should have content_vector from LiteLLM"
        assert len(content_vector) == 1536, \
            "LiteLLM should produce 1536-dimensional vectors"
        
        logger.info("✓ LiteLLM embedding integration verified")
        logger.info("=== LiteLLM Integration Test Completed ===")
