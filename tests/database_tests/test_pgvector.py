# test_pgvector_fixed.py
# !/usr/bin/env python3

import os
import uuid
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
from database.embedding_service import EmbeddingService

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

# Test database configuration
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@pytest.fixture(scope="function", autouse=True)
def isolate_from_external_mocks():
    """Completely isolate these tests from any external mocking."""
    # Clear all active patches before starting each test
    patch.stopall()

    yield

    # Clean up after test - clear any patches created during this test
    patch.stopall()


@pytest.fixture(scope="function")
def db_session():
    """Creates a fresh database session for each test function."""
    # Create test database engine with a unique connection
    engine = create_engine(
        TEST_DB_URL,
        echo=False,
        pool_pre_ping=True,  # Verify connections before use
        pool_recycle=300,  # Recycle connections every 5 minutes
        connect_args={"application_name": f"test_pgvector_{uuid.uuid4().hex[:8]}"},
    )

    # Drop and recreate all tables for clean state
    Base.metadata.drop_all(engine)
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
        # Clean up after test
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(scope="function")
def setup_reference_data(db_session):
    """Set up minimal reference data needed for tests."""
    # Only add data if tables exist and are empty
    try:
        # Check if Config table exists and add minimal data
        if hasattr(db_schemas, "Config"):
            if db_session.query(db_schemas.Config).count() == 0:
                config = db_schemas.Config(config_data='{"test": true}')
                db_session.add(config)
                db_session.commit()
    except Exception:
        # If reference tables don't exist, that's fine for documentation tests
        db_session.rollback()


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

        text1 = "def add_numbers(a, b): return a + b"
        text2 = "def sum_values(x, y): return x + y"
        text3 = "class Database: pass"

        emb1 = service.encode_text(text1)
        emb2 = service.encode_text(text2)
        emb3 = service.encode_text(text3)

        # Similar functions should have higher similarity than unrelated code
        similarity_12 = service.compute_similarity(emb1, emb2)
        similarity_13 = service.compute_similarity(emb1, emb3)

        assert 0 <= similarity_12 <= 1
        assert 0 <= similarity_13 <= 1
        # Functions should be more similar to each other than to class
        assert similarity_12 > similarity_13


class TestDocumentationCRUD:
    """Test CRUD operations for Documentation model."""

    def test_create_documentation(self, db_session, setup_reference_data):
        """Test creating documentation entry."""
        # Create a completely isolated patch just for this test
        with patch.object(crud, "encode_text", return_value=[0.1] * 384) as mock_encode:
            doc_data = Queries.CreateDocumentation(
                content="def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)",
                language="python",
            )

            created_doc = crud.create_documentation(db_session, doc_data)

            # Verify the document was created correctly
            assert created_doc is not None
            assert (
                created_doc.documentation_id is not None
            )  # Should have auto-generated ID
            assert isinstance(created_doc.documentation_id, int)
            assert created_doc.documentation_id > 0  # Should be positive
            assert created_doc.content == doc_data.content
            assert created_doc.language == doc_data.language
            assert created_doc.embedding is not None
            assert len(created_doc.embedding) == 384
            assert created_doc.created_at is not None

            # Verify it was persisted to database
            retrieved_doc = crud.get_documentation_by_id(
                db_session, created_doc.documentation_id
            )
            assert retrieved_doc is not None
            assert retrieved_doc.content == doc_data.content

    def test_get_documentation_by_id(self, db_session, setup_reference_data):
        """Test retrieving documentation by ID."""
        # Create isolated patch
        with patch.object(crud, "encode_text", return_value=[0.1] * 384):
            # Create documentation first
            doc_data = Queries.CreateDocumentation(
                content="function bubbleSort(arr) { /* implementation */ }",
                language="javascript",
            )
            created_doc = crud.create_documentation(db_session, doc_data)

            # Test retrieval
            retrieved_doc = crud.get_documentation_by_id(
                db_session, created_doc.documentation_id
            )
            assert retrieved_doc is not None
            assert retrieved_doc.documentation_id == created_doc.documentation_id
            assert retrieved_doc.content == doc_data.content
            assert retrieved_doc.language == doc_data.language

    def test_get_documentation_nonexistent(self, db_session):
        """Test retrieving non-existent documentation."""
        doc = crud.get_documentation_by_id(db_session, 99999)
        assert doc is None

    def test_get_all_documentation(self, db_session, setup_reference_data):
        """Test getting all documentation."""
        with patch.object(crud, "encode_text", return_value=[0.1] * 384):
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

            # Test getting all documentation
            all_docs = crud.get_all_documentation(db_session)
            assert len(all_docs) == 3

            # Test filtering by language
            python_docs = crud.get_all_documentation(db_session, language="python")
            assert len(python_docs) == 2
            assert all(doc.language == "python" for doc in python_docs)

            # Test limit
            limited_docs = crud.get_all_documentation(db_session, limit=2)
            assert len(limited_docs) == 2

    def test_update_documentation(self, db_session, setup_reference_data):
        """Test updating documentation."""
        with patch.object(crud, "encode_text") as mock_encode:
            # First call for creation
            mock_encode.return_value = [0.1] * 384

            # Create documentation
            doc_data = Queries.CreateDocumentation(
                content="Original content", language="python"
            )
            created_doc = crud.create_documentation(db_session, doc_data)

            # Second call for update with different embedding
            mock_encode.return_value = [0.2] * 384

            # Update documentation
            update_data = Queries.UpdateDocumentation(
                content="Updated content", language="javascript"
            )

            updated_doc = crud.update_documentation(
                db_session, created_doc.documentation_id, update_data
            )

            assert updated_doc is not None
            assert updated_doc.content == "Updated content"
            assert updated_doc.language == "javascript"
            assert updated_doc.documentation_id == created_doc.documentation_id

            # Verify embedding was updated
            assert np.allclose(updated_doc.embedding, [0.2] * 384)

    def test_update_documentation_nonexistent(self, db_session):
        """Test updating non-existent documentation."""
        update_data = Queries.UpdateDocumentation(content="New content")
        result = crud.update_documentation(db_session, 99999, update_data)
        assert result is None

    def test_delete_documentation(self, db_session, setup_reference_data):
        """Test deleting documentation."""
        with patch.object(crud, "encode_text", return_value=[0.1] * 384):
            # Create documentation
            doc_data = Queries.CreateDocumentation(
                content="Content to delete", language="python"
            )
            created_doc = crud.create_documentation(db_session, doc_data)

            # Delete documentation
            success = crud.delete_documentation(
                db_session, created_doc.documentation_id
            )
            assert success is True

            # Verify it's deleted
            deleted_doc = crud.get_documentation_by_id(
                db_session, created_doc.documentation_id
            )
            assert deleted_doc is None

    def test_delete_documentation_nonexistent(self, db_session):
        """Test deleting non-existent documentation."""
        success = crud.delete_documentation(db_session, 99999)
        assert success is False

    def test_search_similar_documentation(self, db_session, setup_reference_data):
        """Test searching for similar documentation."""
        with patch.object(crud, "encode_text") as mock_encode:
            # Mock different embeddings for different content
            def side_effect(text):
                if "python" in text.lower():
                    return [0.9] + [0.1] * 383  # Python content
                elif "javascript" in text.lower():
                    return [0.1] + [0.9] + [0.1] * 382  # JavaScript content
                else:
                    return [0.5] * 384  # Default

            mock_encode.side_effect = side_effect

            # Create documentation with different content
            python_doc = crud.create_documentation(
                db_session,
                Queries.CreateDocumentation(
                    content="Python function definition", language="python"
                ),
            )

            js_doc = crud.create_documentation(
                db_session,
                Queries.CreateDocumentation(
                    content="JavaScript function definition", language="javascript"
                ),
            )

            # Search for Python-related content
            search_query = Queries.SearchDocumentation(
                query_text="python function",
                similarity_threshold=0.7,
                limit=10,
            )

            results = crud.search_similar_documentation(db_session, search_query)

            # Should find results
            assert len(results) > 0
            # Each result should be a tuple of (doc, similarity_score)
            for doc, score in results:
                assert isinstance(doc, db_schemas.Documentation)
                assert isinstance(score, float)
                assert 0 <= score <= 1
                assert score >= 0.7  # Should meet threshold

    def test_embedding_generation_failure(self, db_session, setup_reference_data):
        """Test behavior when embedding generation fails."""
        # Mock the embedding service to fail
        with patch.object(
            crud, "encode_text", side_effect=Exception("Embedding service unavailable")
        ):
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
        with patch.object(crud, "encode_text") as mock_encode:
            # Create documentation without embeddings (mock embedding failure)
            mock_encode.side_effect = Exception("No embeddings")

            doc_data = Queries.CreateDocumentation(
                content="Content without embedding", language="python"
            )
            crud.create_documentation(db_session, doc_data)

            # Search should return empty results when query embedding succeeds but docs have no embeddings
            mock_encode.side_effect = None  # Reset side effect
            mock_encode.return_value = [0.1] * 384  # Query embedding succeeds

            search_results = crud.search_similar_documentation(
                db_session,
                Queries.SearchDocumentation(
                    query_text="Any search query", similarity_threshold=0.1
                ),
            )

            assert len(search_results) == 0

    def test_search_with_language_filter(self, db_session, setup_reference_data):
        """Test search with language filtering."""
        with patch.object(crud, "encode_text", return_value=[0.1] * 384):
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

    def test_search_empty_database(self, db_session):
        """Test search when no documentation exists."""
        search_query = Queries.SearchDocumentation(
            query_text="Any search query", limit=10
        )

        results = crud.search_similar_documentation(db_session, search_query)
        assert len(results) == 0


class TestUtilityFunctions:
    """Test utility functions for documentation management."""

    def test_documentation_stats(self, db_session, setup_reference_data):
        """Test getting documentation statistics."""
        with patch.object(crud, "encode_text", return_value=[0.1] * 384):
            # Create some test documentation
            docs_data = [
                Queries.CreateDocumentation(content="Python code", language="python"),
                Queries.CreateDocumentation(
                    content="JavaScript code", language="javascript"
                ),
                Queries.CreateDocumentation(content="More Python", language="python"),
            ]

            for doc_data in docs_data:
                crud.create_documentation(db_session, doc_data)

            # Test stats function if it exists
            if hasattr(crud, "get_documentation_stats"):
                stats = crud.get_documentation_stats(db_session)
                assert stats["total_documents"] == 3
                assert stats["languages"]["python"] == 2
                assert stats["languages"]["javascript"] == 1


if __name__ == "__main__":
    """Run tests directly."""
    import pytest

    # Run the tests
    pytest.main([__file__, "-v", "--tb=short", "-s"])
