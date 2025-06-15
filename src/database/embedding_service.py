"""
Embedding utilities for documentation similarity search.
Supports offline models using sentence-transformers.
"""

import logging
import os
from typing import List, Optional

import numpy as np

try:
    from sentence_transformers import SentenceTransformer

    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Service for generating embeddings for documentation similarity search."""

    def __init__(self, model_name: Optional[str] = None):
        """
        Initialize the embedding service.

        Args:
            model_name: Name of the sentence-transformer model to use.
                       Defaults to 'all-MiniLM-L6-v2' which is good for general text.
                       For code-specific tasks, consider 'microsoft/codebert-base' or
                       'sentence-transformers/all-mpnet-base-v2'.
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "sentence-transformers is required for embedding functionality. "
                "Install with: pip install sentence-transformers"
            )

        # Default to a small, fast model that works well offline
        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        self.model = None
        self._load_model()

    def _load_model(self):
        """Load the sentence transformer model."""
        try:
            logger.info(f"Loading embedding model: {self.model_name}")
            self.model = SentenceTransformer(self.model_name)
            logger.info(
                f"Model loaded successfully. Embedding dimension: {self.get_embedding_dimension()}"
            )
        except Exception as e:
            logger.error(f"Failed to load model {self.model_name}: {e}")
            # Fallback to a smaller model if the requested one fails
            if self.model_name != "all-MiniLM-L6-v2":
                logger.info("Falling back to all-MiniLM-L6-v2")
                self.model_name = "all-MiniLM-L6-v2"
                self.model = SentenceTransformer(self.model_name)
            else:
                raise

    def get_embedding_dimension(self) -> int:
        """Get the dimension of embeddings produced by this model."""
        if self.model is None:
            return 384  # Default for all-MiniLM-L6-v2
        return self.model.get_sentence_embedding_dimension()

    def encode_text(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to encode

        Returns:
            List of floats representing the embedding
        """
        if self.model is None:
            raise RuntimeError("Model not loaded")

        if not text or not text.strip():
            # Return zero vector for empty text
            return [0.0] * self.get_embedding_dimension()

        # Preprocess text for better code understanding
        processed_text = self._preprocess_text(text)

        embedding = self.model.encode(processed_text, convert_to_tensor=False)

        # Ensure it's a list of floats
        if isinstance(embedding, np.ndarray):
            embedding = embedding.tolist()

        return embedding

    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts efficiently.

        Args:
            texts: List of texts to encode

        Returns:
            List of embeddings (each embedding is a list of floats)
        """
        if self.model is None:
            raise RuntimeError("Model not loaded")

        if not texts:
            return []

        # Preprocess all texts
        processed_texts = [self._preprocess_text(text) for text in texts]

        embeddings = self.model.encode(processed_texts, convert_to_tensor=False)

        # Convert to list of lists
        if isinstance(embeddings, np.ndarray):
            embeddings = embeddings.tolist()

        return embeddings

    def _preprocess_text(self, text: str) -> str:
        """
        Preprocess text to improve embedding quality for code.

        Args:
            text: Raw text

        Returns:
            Preprocessed text
        """
        if not text:
            return ""

        # Basic preprocessing - you can enhance this based on your needs
        # Remove excessive whitespace but preserve some structure
        lines = text.split("\n")
        cleaned_lines = []

        for line in lines:
            # Keep indentation for code structure but normalize excessive spaces
            stripped = line.rstrip()
            if stripped:
                # Replace multiple spaces with single space, but keep leading spaces
                leading_spaces = len(line) - len(line.lstrip())
                content = " ".join(stripped.split())
                cleaned_lines.append(" " * leading_spaces + content)
            else:
                cleaned_lines.append("")

        return "\n".join(cleaned_lines)

    def compute_similarity(
        self, embedding1: List[float], embedding2: List[float]
    ) -> float:
        """
        Compute cosine similarity between two embeddings.

        Args:
            embedding1: First embedding
            embedding2: Second embedding

        Returns:
            Similarity score between 0 and 1
        """
        if len(embedding1) != len(embedding2):
            raise ValueError("Embeddings must have the same dimension")

        # Convert to numpy arrays for efficient computation
        vec1 = np.array(embedding1)
        vec2 = np.array(embedding2)

        # Compute cosine similarity
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        similarity = dot_product / (norm1 * norm2)

        # Ensure the result is between 0 and 1
        return max(0.0, min(1.0, float(similarity)))


# Global embedding service instance
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Get the global embedding service instance."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


def encode_text(text: str) -> List[float]:
    """Convenience function to encode a single text."""
    return get_embedding_service().encode_text(text)


def encode_batch(texts: List[str]) -> List[List[float]]:
    """Convenience function to encode multiple texts."""
    return get_embedding_service().encode_batch(texts)


def compute_similarity(embedding1: List[float], embedding2: List[float]) -> float:
    """Convenience function to compute similarity between embeddings."""
    return get_embedding_service().compute_similarity(embedding1, embedding2)
