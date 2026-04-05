"""
test_tier3_dynamodb_integration.py — Integration tests for Tier 3 + DynamoDB
============================================================================
Verifies that:
1. Tier 3 audit logs can be written to DynamoDB
2. Schema matches table expectations
3. Persistence is non-blocking (doesn't fail OCR)
4. Batch writes work correctly
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dynamodb_integration import (
    write_audit_to_dynamodb,
    write_audit_batch_to_dynamodb,
    get_audit_history,
)


class TestDynamoDBIntegration(unittest.TestCase):
    """Test Tier 3 ↔ DynamoDB audit persistence."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_table = Mock()
        self.document_id = "test_doc_12345"
        self.user_id = "test_user"

        # Sample audit entry from tier3_processor.audit_logging()
        self.sample_audit_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "status": "ACCEPTED",
            "reason_code": "ACCEPTED",
            "original_text": "Aspirin 500mg",
            "corrected_text": "Aspirin 500 mg",
            "ocr_confidence": 0.72,
            "llm_confidence": 0.95,
            "deviation_score": 0.05,
            "token_similarity": 0.99,
            "levenshtein_distance": 2,
            "reasoning": "Space added for clinical clarity",
            "bbox": [100, 200, 300, 250],
            "page_number": 1,
            "model_id": "claude-3-5-sonnet-bedrock",
        }

    def test_write_single_audit_entry_success(self):
        """Test successful write of a single audit entry."""
        self.mock_table.put_item.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = write_audit_to_dynamodb(
            self.sample_audit_entry,
            self.document_id,
            self.mock_table,
            self.user_id,
        )

        self.assertTrue(result)
        self.mock_table.put_item.assert_called_once()

        # Verify the item structure
        call_args = self.mock_table.put_item.call_args
        item = call_args.kwargs["Item"]
        
        self.assertIn("log_id", item)
        self.assertEqual(item["document_id"], self.document_id)
        self.assertEqual(item["status"], "ACCEPTED")
        self.assertEqual(item["user_id"], self.user_id)
        self.assertEqual(item["original_text"], "Aspirin 500mg")
        self.assertEqual(item["corrected_text"], "Aspirin 500 mg")

    def test_write_audit_entry_db_failure_non_blocking(self):
        """Test that DB failures don't crash the system."""
        self.mock_table.put_item.side_effect = Exception("DynamoDB connection failed")

        result = write_audit_to_dynamodb(
            self.sample_audit_entry,
            self.document_id,
            self.mock_table,
            self.user_id,
        )

        self.assertFalse(result)
        # OCR pipeline continues despite DB error

    def test_write_audit_entry_empty_entry(self):
        """Test handling of empty audit entries."""
        result = write_audit_to_dynamodb(
            None,
            self.document_id,
            self.mock_table,
            self.user_id,
        )

        self.assertFalse(result)
        self.mock_table.put_item.assert_not_called()

    def test_batch_write_mixed_success_failure(self):
        """Test batch writes with some successes and failures."""
        # First call succeeds, second fails, third succeeds
        self.mock_table.put_item.side_effect = [
            None,  # Success
            Exception("DB error"),  # Failure
            None,  # Success
        ]

        entries = [
            self.sample_audit_entry,
            {**self.sample_audit_entry, "status": "REVIEW_REQUIRED"},
            {**self.sample_audit_entry, "status": "NO_CHANGE"},
        ]

        result = write_audit_batch_to_dynamodb(
            entries,
            self.document_id,
            self.mock_table,
            self.user_id,
        )

        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["failed_entries"]), 1)

    def test_batch_write_all_success(self):
        """Test successful batch write of multiple entries."""
        self.mock_table.put_item.return_value = None

        entries = [
            self.sample_audit_entry,
            {**self.sample_audit_entry, "status": "REVIEW_REQUIRED"},
            {**self.sample_audit_entry, "status": "NO_CHANGE"},
        ]

        result = write_audit_batch_to_dynamodb(
            entries,
            self.document_id,
            self.mock_table,
            self.user_id,
        )

        self.assertEqual(result["succeeded"], 3)
        self.assertEqual(result["failed"], 0)
        self.mock_table.put_item.assert_called()

    def test_audit_status_to_action_mapping(self):
        """Test that audit status is mapped to action field correctly."""
        self.mock_table.put_item.return_value = None

        for status in ["SUCCESS", "REVIEW_REQUIRED", "ACCEPTED", "HALLUCINATED"]:
            entry = {**self.sample_audit_entry, "status": status}
            write_audit_to_dynamodb(entry, self.document_id, self.mock_table)

            call_args = self.mock_table.put_item.call_args
            item = call_args.kwargs["Item"]
            
            # Verify action field is populated
            self.assertIn("action", item)
            self.assertNotEqual(item["action"], "OCR_CORRECTION_UNKNOWN")

    def test_get_audit_history(self):
        """Test retrieving audit history for a document."""
        mock_response = {
            "Items": [
                {
                    "log_id": "audit_001",
                    "timestamp": "2025-01-10T12:00:00",
                    "status": "ACCEPTED",
                },
                {
                    "log_id": "audit_002",
                    "timestamp": "2025-01-10T12:05:00",
                    "status": "REVIEW_REQUIRED",
                },
            ]
        }
        self.mock_table.query.return_value = mock_response

        result = get_audit_history(self.mock_table, self.document_id)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["log_id"], "audit_001")
        self.mock_table.query.assert_called_once()

    def test_get_audit_history_failure(self):
        """Test handling of query failures."""
        self.mock_table.query.side_effect = Exception("Query failed")

        result = get_audit_history(self.mock_table, self.document_id)

        self.assertIsNone(result)

    def test_audit_fields_preservation(self):
        """Test that all audit fields are preserved in DynamoDB item."""
        self.mock_table.put_item.return_value = None

        write_audit_to_dynamodb(
            self.sample_audit_entry,
            self.document_id,
            self.mock_table,
        )

        call_args = self.mock_table.put_item.call_args
        item = call_args.kwargs["Item"]

        # Verify all important fields are present
        expected_fields = [
            "log_id",
            "document_id",
            "timestamp",
            "status",
            "reason_code",
            "original_text",
            "corrected_text",
            "ocr_confidence",
            "llm_confidence",
            "deviation_score",
            "token_similarity",
            "levenshtein_distance",
            "reasoning",
            "bbox",
            "page_number",
        ]

        for field in expected_fields:
            self.assertIn(field, item)


if __name__ == "__main__":
    unittest.main()
