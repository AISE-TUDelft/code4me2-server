import uuid
import json
from unittest.mock import patch, MagicMock
import pytest


# Class to test (without importing real dependencies)
class CompletionController:
    def request_completion(self, completion_request, app):
        # Get database session
        db_session = app.get_db_session()

        # Logic to create completions (simplified)
        query_id = uuid.uuid4()
        completions = []

        for model_id in completion_request["model_ids"]:
            completions.append({
                "model_id": model_id,
                "model_name": f"model-{model_id}",
                "completion": "def example_function():\n    pass",
                "confidence": 0.85
            })

        return {
            "message": "Completions generated successfully",
            "data": {
                "query_id": query_id,
                "completions": completions
            }
        }

    def get_completions(self, query_id, app):
        # Get database session
        db_session = app.get_db_session()

        # Check if query exists (would query DB in real implementation)
        if query_id:
            return {
                "message": "Completions retrieved successfully",
                "data": {
                    "query_id": query_id,
                    "completions": [
                        {
                            "model_id": 1,
                            "model_name": "model-1",
                            "completion": "def example_function():\n    pass",
                            "confidence": 0.85
                        }
                    ]
                }
            }
        else:
            return {"message": "Query not found"}, 404

    def submit_feedback(self, feedback, app):
        # Get database session
        db_session = app.get_db_session()

        # Check if generation exists (would query DB in real implementation)
        if feedback["query_id"] and feedback["model_id"]:
            return {
                "message": "Feedback recorded successfully",
                "data": {
                    "query_id": feedback["query_id"],
                    "model_id": feedback["model_id"]
                }
            }
        else:
            return {"message": "Generation record not found"}, 404


# Tests
class TestCompletionController:
    def setup_method(self):
        # Create controller
        self.controller = CompletionController()

        # Mock App
        self.mock_app = MagicMock()
        self.mock_app.get_db_session.return_value = MagicMock()

    def test_request_completion(self):
        # Test data
        request_data = {
            "user_id": str(uuid.uuid4()),
            "prefix": "def calculate_sum(a, b):",
            "suffix": "",
            "language_id": 67,
            "trigger_type_id": 1,
            "version_id": 1,
            "model_ids": [1, 2],
            "time_since_last_completion": 5000,
            "typing_speed": 120,
            "document_char_length": 500,
            "relative_document_position": 0.5
        }

        # Request completion
        response = self.controller.request_completion(request_data, self.mock_app)

        # Assertions
        assert response["message"] == "Completions generated successfully"
        assert "query_id" in response["data"]
        assert len(response["data"]["completions"]) == 2
        assert response["data"]["completions"][0]["model_id"] == 1
        assert response["data"]["completions"][1]["model_id"] == 2

        # Return query_id for next tests
        return response["data"]["query_id"]

    def test_get_completions(self):
        # Generate a query_id
        query_id = self.test_request_completion()

        # Get completions
        response = self.controller.get_completions(query_id, self.mock_app)

        # Assertions
        assert response["message"] == "Completions retrieved successfully"
        assert response["data"]["query_id"] == query_id
        assert len(response["data"]["completions"]) > 0

    def test_submit_feedback(self):
        # Generate a query_id
        query_id = self.test_request_completion()

        # Feedback data
        feedback_data = {
            "query_id": query_id,
            "model_id": 1,
            "was_accepted": True,
            "ground_truth": "def calculate_sum(a, b):\n    return a + b"
        }

        # Submit feedback
        response = self.controller.submit_feedback(feedback_data, self.mock_app)

        # Assertions
        assert response["message"] == "Feedback recorded successfully"
        assert response["data"]["query_id"] == query_id
        assert response["data"]["model_id"] == 1