#!/usr/bin/env python3
"""
Tests CRUD operations, embedding generation, and similarity search.
"""

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Import database modules
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import database.crud as crud
import Queries
from database import db_schemas
from database.db import Base
from database.embedding_service import (
    EmbeddingService,
)

# Mock sentence-transformers if not available
try:
    import sentence_transformers

    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False

    # Create mock classes
    class MockSentenceTransformer:
        def __init__(self, model_name):
            self.model_name = model_name

        def get_sentence_embedding_dimension(self):
            return 384

        def encode(self, texts, convert_to_tensor=False):
            import numpy as np

            if isinstance(texts, str):
                texts = [texts]
            # Return mock embeddings - different for different texts
            embeddings = []
            for i, text in enumerate(texts):
                # Create deterministic "embedding" based on text content
                base_embedding = [0.1] * 384
                text_hash = hash(text) % 1000
                for j in range(min(len(text), 50)):
                    base_embedding[j % 384] = (text_hash + j) / 1000.0
                embeddings.append(base_embedding)

            return (
                np.array(embeddings) if len(embeddings) > 1 else np.array(embeddings[0])
            )

    # Patch the import
    import sys

    sys.modules["sentence_transformers"] = MagicMock()
    sys.modules["sentence_transformers"].SentenceTransformer = MockSentenceTransformer

# Now import the modules we need to test
import sys
from pathlib import Path

# Add project root to path
current_dir = Path(__file__).parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))


# Test database configuration
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@pytest.fixture(scope="function")
def db_session():
    """Creates a fresh database session for each test function."""
    engine = create_engine(TEST_DB_URL)

    # Create all tables
    Base.metadata.create_all(engine)

    # Enable pgvector extension
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        except Exception as e:
            print(f"Note: Could not create vector extension: {e}")

    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()

    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def setup_reference_data(db_session):
    """Set up reference data needed for tests."""
    # Add basic reference data if tables exist and are empty
    try:
        # This might fail if tables don't exist, which is fine
        if (
            hasattr(db_schemas, "Config")
            and db_session.query(db_schemas.Config).count() == 0
        ):
            config = db_schemas.Config(config_data='{"test": true}')
            db_session.add(config)
            db_session.commit()
    except Exception:
        # If reference tables don't exist, that's fine for documentation tests
        pass


class TestEmbeddingService:
    """Test the embedding service functionality."""

    def test_embedding_service_initialization(self):
        """Test that embedding service can be initialized."""
        service = EmbeddingService()
        assert service is not None
        assert service.get_embedding_dimension() == 384

    def test_encode_single_text(self):
        """Test encoding a single text."""
        service = EmbeddingService()

        text = "def hello_world():\n    print('Hello, World!')"
        embedding = service.encode_text(text)

        assert isinstance(embedding, list)
        assert len(embedding) == 384
        assert all(isinstance(x, float) for x in embedding)

    def test_encode_empty_text(self):
        """Test encoding empty text."""
        service = EmbeddingService()

        embedding = service.encode_text("")
        assert isinstance(embedding, list)
        assert len(embedding) == 384
        assert all(x == 0.0 for x in embedding)

    def test_encode_batch(self):
        """Test encoding multiple texts."""
        service = EmbeddingService()

        texts = [
            "def add(a, b): return a + b",
            "def multiply(x, y): return x * y",
            "class Calculator: pass",
        ]

        embeddings = service.encode_batch(texts)

        assert isinstance(embeddings, list)
        assert len(embeddings) == 3
        assert all(len(emb) == 384 for emb in embeddings)

    def test_compute_similarity(self):
        """Test similarity computation."""
        service = EmbeddingService()

        text1 = "def add(a, b): return a + b"
        text2 = "def add(x, y): return x + y"  # Very similar
        text3 = "class Database: pass"  # Different

        emb1 = service.encode_text(text1)
        emb2 = service.encode_text(text2)
        emb3 = service.encode_text(text3)

        # Similar texts should have higher similarity
        sim_12 = service.compute_similarity(emb1, emb2)
        sim_13 = service.compute_similarity(emb1, emb3)

        assert 0.0 <= sim_12 <= 1.0
        assert 0.0 <= sim_13 <= 1.0
        assert sim_12 > sim_13  # More similar texts should have higher score

    def test_preprocess_text(self):
        """Test text preprocessing."""
        service = EmbeddingService()

        messy_text = "def   function():\n    print('hello')   \n\n    return True   "
        processed = service._preprocess_text(messy_text)

        # Should clean up excessive whitespace but preserve structure
        assert "def function():" in processed
        assert "print('hello')" in processed
        assert "return True" in processed


class TestDocumentationCRUD:
    """Test CRUD operations for documentation."""

    def test_create_documentation(self, db_session, setup_reference_data):
        """Test creating documentation entry."""
        doc_data = Queries.CreateDocumentation(
            content="def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)",
            language="python",
        )

        created_doc = crud.create_documentation(db_session, doc_data)

        assert created_doc is not None
        assert created_doc.documentation_id is not None
        assert created_doc.content == doc_data.content
        assert created_doc.language == doc_data.language
        assert created_doc.embedding is not None
        assert len(created_doc.embedding) == 384
        assert created_doc.created_at is not None

    def test_get_documentation_by_id(self, db_session, setup_reference_data):
        """Test retrieving documentation by ID."""
        # Create documentation first
        doc_data = Queries.CreateDocumentation(
            content="function bubbleSort(arr) { /* implementation */ }",
            language="javascript",
        )
        created_doc = crud.create_documentation(db_session, doc_data)

        # Retrieve it
        retrieved_doc = crud.get_documentation_by_id(
            db_session, created_doc.documentation_id
        )

        assert retrieved_doc is not None
        assert retrieved_doc.documentation_id == created_doc.documentation_id
        assert retrieved_doc.content == created_doc.content
        assert retrieved_doc.language == created_doc.language

    def test_get_documentation_nonexistent(self, db_session):
        """Test retrieving non-existent documentation."""
        doc = crud.get_documentation_by_id(db_session, 99999)
        assert doc is None

    def test_get_all_documentation(self, db_session, setup_reference_data):
        """Test getting all documentation."""
        # Create multiple documentation entries
        docs_data = [
            Queries.CreateDocumentation(
                content="Python code example", language="python"
            ),
            Queries.CreateDocumentation(
                content="JavaScript code example", language="javascript"
            ),
            Queries.CreateDocumentation(
                content="Another Python example", language="python"
            ),
        ]

        created_docs = []
        for doc_data in docs_data:
            created_doc = crud.create_documentation(db_session, doc_data)
            created_docs.append(created_doc)

        # Get all documentation
        all_docs = crud.get_all_documentation(db_session)
        assert len(all_docs) == 3

        # Get only Python documentation
        python_docs = crud.get_all_documentation(db_session, language="python")
        assert len(python_docs) == 2

        # Get with limit
        limited_docs = crud.get_all_documentation(db_session, limit=2)
        assert len(limited_docs) == 2

    def test_update_documentation(self, db_session, setup_reference_data):
        """Test updating documentation."""
        # Create documentation
        doc_data = Queries.CreateDocumentation(
            content="Original content", language="python"
        )
        created_doc = crud.create_documentation(db_session, doc_data)
        original_embedding = created_doc.embedding.copy()

        # Update it
        update_data = Queries.UpdateDocumentation(
            content="Updated content with new information", language="python"
        )

        updated_doc = crud.update_documentation(
            db_session, created_doc.documentation_id, update_data
        )

        assert updated_doc is not None
        assert updated_doc.content == "Updated content with new information"
        assert updated_doc.language == "python"
        # Embedding should be regenerated when content changes
        assert not np.array_equal(updated_doc.embedding, original_embedding)

    def test_update_documentation_nonexistent(self, db_session):
        """Test updating non-existent documentation."""
        update_data = Queries.UpdateDocumentation(content="New content")
        result = crud.update_documentation(db_session, 99999, update_data)
        assert result is None

    def test_delete_documentation(self, db_session, setup_reference_data):
        """Test deleting documentation."""
        # Create documentation
        doc_data = Queries.CreateDocumentation(
            content="Content to delete", language="python"
        )
        created_doc = crud.create_documentation(db_session, doc_data)
        doc_id = created_doc.documentation_id

        # Delete it
        success = crud.delete_documentation(db_session, doc_id)
        assert success is True

        # Verify it's gone
        deleted_doc = crud.get_documentation_by_id(db_session, doc_id)
        assert deleted_doc is None

    def test_delete_documentation_nonexistent(self, db_session):
        """Test deleting non-existent documentation."""
        success = crud.delete_documentation(db_session, 99999)
        assert success is False


class TestSimilaritySearch:
    """Test similarity search functionality."""

    def test_search_similar_documentation(self, db_session, setup_reference_data):
        """Test searching for similar documentation."""
        # Create sample documentation
        docs_data = [
            Queries.CreateDocumentation(
                content="def add(a, b):\n    '''Add two numbers'''\n    return a + b",
                language="python",
            ),
            Queries.CreateDocumentation(
                content="def subtract(x, y):\n    '''Subtract y from x'''\n    return x - y",
                language="python",
            ),
            Queries.CreateDocumentation(
                content="function multiply(a, b) {\n    // Multiply two numbers\n    return a * b;\n}",
                language="javascript",
            ),
            Queries.CreateDocumentation(
                content="class Database:\n    '''Database connection class'''\n    def connect(self): pass",
                language="python",
            ),
        ]

        for doc_data in docs_data:
            crud.create_documentation(db_session, doc_data)

        # Search for addition-related documentation
        search_query = Queries.SearchDocumentation(
            query_text="def add_numbers(x, y): return x + y",
            limit=10,
            similarity_threshold=0.1,
        )

        results = crud.search_similar_documentation(db_session, search_query)

        assert len(results) > 0
        # Results should be tuples of (documentation, similarity_score)
        for doc, score in results:
            assert isinstance(doc, db_schemas.Documentation)
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

        # Results should be ordered by similarity (highest first)
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_with_language_filter(self, db_session, setup_reference_data):
        """Test search with language filtering."""
        # Create documentation in different languages
        python_doc = crud.create_documentation(
            db_session,
            Queries.CreateDocumentation(
                content="def python_function(): pass", language="python"
            ),
        )

        js_doc = crud.create_documentation(
            db_session,
            Queries.CreateDocumentation(
                content="function jsFunction() {}", language="javascript"
            ),
        )

        # Search only for Python documentation
        search_query = Queries.SearchDocumentation(
            query_text="python function definition",
            language="python",
            similarity_threshold=0.0,
        )

        results = crud.search_similar_documentation(db_session, search_query)

        # Should only return Python documentation
        for doc, score in results:
            assert doc.language == "python"

    def test_search_with_threshold(self, db_session, setup_reference_data):
        """Test search with similarity threshold."""
        # Create documentation
        doc_data = Queries.CreateDocumentation(
            content="Very specific function that does something unique",
            language="python",
        )
        crud.create_documentation(db_session, doc_data)

        # Search with high threshold
        search_query = Queries.SearchDocumentation(
            query_text="Completely different content about databases",
            similarity_threshold=0.9,  # Very high threshold
            limit=10,
        )

        results = crud.search_similar_documentation(db_session, search_query)

        # Should return fewer or no results due to high threshold
        for doc, score in results:
            assert score >= 0.9

    def test_search_empty_database(self, db_session):
        """Test search when no documentation exists."""
        search_query = Queries.SearchDocumentation(
            query_text="Any search query", limit=10
        )

        results = crud.search_similar_documentation(db_session, search_query)
        assert len(results) == 0


class TestUtilityFunctions:
    """Test utility functions for documentation management."""

    def test_regenerate_embeddings(self, db_session, setup_reference_data):
        """Test regenerating embeddings for existing documentation."""
        # Create documentation
        docs_data = [
            Queries.CreateDocumentation(content="Content 1", language="python"),
            Queries.CreateDocumentation(content="Content 2", language="javascript"),
            Queries.CreateDocumentation(content="Content 3", language="python"),
        ]

        for doc_data in docs_data:
            crud.create_documentation(db_session, doc_data)

        # Regenerate all embeddings
        count = crud.regenerate_embeddings(db_session)
        assert count == 3

        # Regenerate only Python embeddings
        python_count = crud.regenerate_embeddings(db_session, language="python")
        assert python_count == 2

    def test_get_documentation_stats(self, db_session, setup_reference_data):
        """Test getting documentation statistics."""
        # Initially should be empty
        stats = crud.get_documentation_stats(db_session)
        assert stats["total_documents"] == 0
        assert stats["documents_with_embeddings"] == 0
        assert stats["embedding_coverage"] == 0
        assert stats["languages"] == {}

        # Create documentation
        docs_data = [
            Queries.CreateDocumentation(content="Python doc 1", language="python"),
            Queries.CreateDocumentation(content="Python doc 2", language="python"),
            Queries.CreateDocumentation(content="JS doc 1", language="javascript"),
        ]

        for doc_data in docs_data:
            crud.create_documentation(db_session, doc_data)

        # Check stats again
        stats = crud.get_documentation_stats(db_session)
        assert stats["total_documents"] == 3
        assert stats["documents_with_embeddings"] == 3
        assert stats["embedding_coverage"] == 1.0
        assert stats["languages"]["python"] == 2
        assert stats["languages"]["javascript"] == 1


class TestDocumentationValidation:
    """Test validation of documentation data."""

    def test_create_documentation_validation(self):
        """Test validation rules for creating documentation."""
        # Test empty content
        with pytest.raises(Exception):  # ValidationError from Pydantic
            Queries.CreateDocumentation(content="", language="python")

        # Test empty language
        with pytest.raises(Exception):
            Queries.CreateDocumentation(content="Valid content", language="")

        # Test too long language
        with pytest.raises(Exception):
            Queries.CreateDocumentation(
                content="Valid content", language="x" * 51  # Over 50 character limit
            )

    def test_search_documentation_validation(self):
        """Test validation rules for search queries."""
        # Test empty query text
        with pytest.raises(Exception):
            Queries.SearchDocumentation(query_text="")

        # Test invalid limit
        with pytest.raises(Exception):
            Queries.SearchDocumentation(query_text="test", limit=0)

        with pytest.raises(Exception):
            Queries.SearchDocumentation(query_text="test", limit=101)

        # Test invalid similarity threshold
        with pytest.raises(Exception):
            Queries.SearchDocumentation(query_text="test", similarity_threshold=-0.1)

        with pytest.raises(Exception):
            Queries.SearchDocumentation(query_text="test", similarity_threshold=1.1)


class TestIntegrationScenarios:
    """Test real-world integration scenarios."""

    def test_complete_documentation_workflow(self, db_session, setup_reference_data):
        """Test a complete workflow from creation to search."""
        # 1. Create a knowledge base of documentation
        knowledge_base = [
            {
                "content": '''
def binary_search(arr, target):
    """
    Perform binary search on a sorted array.

    Args:
        arr: Sorted list of elements
        target: Element to search for

    Returns:
        Index of target if found, -1 otherwise
    """
    left, right = 0, len(arr) - 1

    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1

    return -1
                '''.strip(),
                "language": "python",
            },
            {
                "content": '''
def quicksort(arr):
    """
    Sort an array using quicksort algorithm.

    Args:
        arr: List of comparable elements

    Returns:
        Sorted list
    """
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)
                '''.strip(),
                "language": "python",
            },
            {
                "content": """
function mergeSort(arr) {
    if (arr.length <= 1) {
        return arr;
    }

    const mid = Math.floor(arr.length / 2);
    const left = mergeSort(arr.slice(0, mid));
    const right = mergeSort(arr.slice(mid));

    return merge(left, right);
}

function merge(left, right) {
    let result = [];
    let i = 0, j = 0;

    while (i < left.length && j < right.length) {
        if (left[i] <= right[j]) {
            result.push(left[i]);
            i++;
        } else {
            result.push(right[j]);
            j++;
        }
    }

    return result.concat(left.slice(i)).concat(right.slice(j));
}
                """.strip(),
                "language": "javascript",
            },
            {
                "content": '''
class DatabaseConnection:
    """
    A simple database connection class.
    Handles connection pooling and basic operations.
    """

    def __init__(self, host, port, database, username, password):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.connection = None

    def connect(self):
        """Establish database connection."""
        # Implementation would go here
        pass

    def disconnect(self):
        """Close database connection."""
        # Implementation would go here
        pass

    def execute_query(self, query, params=None):
        """Execute a database query."""
        # Implementation would go here
        pass
                '''.strip(),
                "language": "python",
            },
        ]

        # 2. Add all documentation to the database
        created_docs = []
        for doc_info in knowledge_base:
            doc_data = Queries.CreateDocumentation(**doc_info)
            created_doc = crud.create_documentation(db_session, doc_data)
            created_docs.append(created_doc)

        assert len(created_docs) == 4

        # 3. Test various search scenarios

        # Search for sorting algorithms
        search_results = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="I need to sort an array of numbers efficiently",
                similarity_threshold=0.1,
                limit=5,
            ),
        )

        assert len(search_results) > 0
        # Should find sorting-related documentation
        sorting_docs = [
            doc
            for doc, score in search_results
            if any(
                keyword in doc.content.lower()
                for keyword in ["sort", "quicksort", "mergesort"]
            )
        ]
        assert len(sorting_docs) > 0

        # Search for database-related content
        db_search_results = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="How to connect to a database and run queries",
                similarity_threshold=0.1,
                limit=5,
            ),
        )

        # Should find database-related documentation
        db_docs = [
            doc for doc, score in db_search_results if "database" in doc.content.lower()
        ]
        assert len(db_docs) > 0

        # Search with language filter
        python_search_results = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="searching and sorting algorithms",
                language="python",
                similarity_threshold=0.1,
                limit=10,
            ),
        )

        # Should only return Python documentation
        for doc, score in python_search_results:
            assert doc.language == "python"

        # 4. Test statistics
        stats = crud.get_documentation_stats(db_session)
        assert stats["total_documents"] == 4
        assert stats["documents_with_embeddings"] == 4
        assert stats["embedding_coverage"] == 1.0
        assert stats["languages"]["python"] == 3
        assert stats["languages"]["javascript"] == 1

        # 5. Test update functionality
        # Update one of the documents
        update_data = Queries.UpdateDocumentation(
            content="Updated binary search with improved documentation and error handling"
        )

        updated_doc = crud.update_documentation(
            db_session, created_docs[0].documentation_id, update_data
        )

        assert updated_doc.content == update_data.content
        # Embedding should be updated
        assert updated_doc.embedding is not None

        # 6. Test that updated document can still be found in search
        updated_search_results = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="binary search algorithm", similarity_threshold=0.1, limit=5
            ),
        )

        # Should still find the updated document
        found_updated = any(
            doc.documentation_id == updated_doc.documentation_id
            for doc, score in updated_search_results
        )
        assert found_updated

    def test_code_snippet_similarity_search(self, db_session, setup_reference_data):
        """Test similarity search with actual code snippets."""
        # Create documentation with common coding patterns
        patterns = [
            {
                "content": "for i in range(len(array)):\n    print(array[i])",
                "language": "python",
            },
            {"content": "for item in items:\n    process(item)", "language": "python"},
            {"content": "if __name__ == '__main__':\n    main()", "language": "python"},
            {
                "content": "try:\n    risky_operation()\nexcept Exception as e:\n    handle_error(e)",
                "language": "python",
            },
            {
                "content": "for (let i = 0; i < array.length; i++) {\n    console.log(array[i]);\n}",
                "language": "javascript",
            },
        ]

        for pattern in patterns:
            doc_data = Queries.CreateDocumentation(**pattern)
            crud.create_documentation(db_session, doc_data)

        # Test search with similar code snippet
        search_results = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="for i in range(10):\n    do_something(i)",
                similarity_threshold=0.1,
                limit=5,
            ),
        )

        assert len(search_results) > 0

        # The most similar should be loop-related patterns
        top_result = search_results[0]
        assert "for" in top_result[0].content

        # Test search for exception handling
        exception_search = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="try:\n    something()\nexcept:\n    pass",
                similarity_threshold=0.1,
                limit=5,
            ),
        )

        # Should find exception handling pattern
        exception_docs = [
            doc
            for doc, score in exception_search
            if "try" in doc.content and "except" in doc.content
        ]
        assert len(exception_docs) > 0


class TestErrorHandling:
    """Test error handling and edge cases."""

    def test_embedding_generation_failure(self, db_session, setup_reference_data):
        """Test behavior when embedding generation fails."""
        # Mock the embedding service to fail
        with patch("database.crud.encode_text") as mock_encode:
            mock_encode.side_effect = Exception("Embedding service unavailable")

            # Should still create documentation, just without embedding
            doc_data = Queries.CreateDocumentation(
                content="Test content when embedding fails", language="python"
            )

            created_doc = crud.create_documentation(db_session, doc_data)

            assert created_doc is not None
            assert created_doc.content == doc_data.content
            assert created_doc.embedding is None  # Should be None due to failure

    def test_search_with_no_embeddings(self, db_session, setup_reference_data):
        """Test search when documents have no embeddings."""
        # Create documentation without embeddings (mock embedding failure)
        with patch("database.crud.encode_text") as mock_encode:
            mock_encode.side_effect = Exception("No embeddings")

            doc_data = Queries.CreateDocumentation(
                content="Content without embedding", language="python"
            )
            crud.create_documentation(db_session, doc_data)

        # Search should return empty results
        search_results = crud.search_similar_documentation(
            db_session,
            Queries.SearchDocumentation(
                query_text="Any search query", similarity_threshold=0.1
            ),
        )

        assert len(search_results) == 0

    def test_malformed_embeddings(self, db_session, setup_reference_data):
        """Test handling of malformed embeddings in database."""
        # This test is more complex and would require direct database manipulation
        # For now, we'll test the embedding service's robustness
        service = EmbeddingService()

        # Test with various edge case inputs
        edge_cases = [
            "",  # Empty string
            " ",  # Whitespace only
            "\n\n\n",  # Newlines only
            "a" * 10000,  # Very long string
            "🚀🌟💻",  # Unicode/emoji
            "def\tfunction():\n\t\tpass",  # Mixed whitespace
        ]

        for txt in edge_cases:
            embedding = service.encode_text(txt)
            assert isinstance(embedding, list)
            assert len(embedding) == 384
            assert all(isinstance(x, float) for x in embedding)


if __name__ == "__main__":
    """Run tests directly."""
    import pytest

    # Run the tests
    pytest.main([__file__, "-v", "--tb=short", "-s"])  # Don't capture output
