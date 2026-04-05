"""
dynamodb_integration.py — Tier 3 ↔ DynamoDB Audit Persistence
==============================================================
Handles writing Tier 3 audit logs to DynamoDB for compliance + traceability.

This module provides the bridge between Tier 3 OCR corrections and persistent
audit storage. All corrections flow through here for immutable recording.

Usage
-----
    from dynamodb_integration import write_audit_to_dynamodb

    result = process_low_confidence_regions(...)
    
    # Persist audit trail
    for audit_entry in result['audit_log']:
        write_audit_to_dynamodb(
            audit_entry,
            document_id='doc_12345',
            dynamodb_resource=boto3_resource
        )
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write_audit_to_dynamodb(
    audit_entry: dict[str, Any],
    document_id: str,
    dynamodb_table: Any,
    user_id: str = "SYSTEM",
) -> bool:
    """
    Write a Tier 3 audit log entry to DynamoDB.

    Args:
        audit_entry: Audit log dict from tier3_processor.audit_logging()
                     Contains: timestamp, status, reason_code, original_text,
                               corrected_text, deviation_score, etc.
        document_id: Document identifier for cross-referencing with
                     ClinicalDocs_Documents table.
        dynamodb_table: Boto3 DynamoDB resource table for ClinicalDocs_AuditLogs.
        user_id: User who triggered correction (default: "SYSTEM" for pipeline).

    Returns:
        bool: True if write succeeded, False if failed.

    Side Effects:
        - Logs warnings/errors but does NOT raise exceptions
        - Audit log is not persisted on failure (logged only)
        - Non-blocking: Tier 3 corrections proceed even if DB write fails
    """
    if not audit_entry:
        logger.warning("Skipping audit write: empty audit_entry")
        return False

    try:
        # Generate globally unique audit event ID
        audit_id = str(uuid.uuid4())

        # Build DynamoDB item from audit entry
        # Note: DynamoDB is schemaless for non-key fields, so we can store
        # the entire audit_entry as-is, plus add required key attributes
        item = {
            "log_id": audit_id,  # PK: globally unique event ID
            "document_id": document_id,  # FK: cross-reference to Documents
            "timestamp": audit_entry.get("timestamp", ""),  # Sort key for GSI
            "user_id": user_id,  # For user activity tracking (GSI)
            "action": _map_audit_status_to_action(
                audit_entry.get("status", "UNKNOWN")
            ),
            # Full audit data (schemaless storage)
            "original_text": audit_entry.get("original_text", ""),
            "corrected_text": audit_entry.get("corrected_text"),
            "ocr_confidence": audit_entry.get("ocr_confidence"),
            "llm_confidence": audit_entry.get("llm_confidence"),
            "deviation_score": audit_entry.get("deviation_score"),
            "token_similarity": audit_entry.get("token_similarity"),
            "levenshtein_distance": audit_entry.get("levenshtein_distance"),
            "status": audit_entry.get("status", "UNKNOWN"),
            "reason_code": audit_entry.get("reason_code", ""),
            "reasoning": audit_entry.get("reasoning", ""),
            "bbox": audit_entry.get("bbox", []),
            "page_number": audit_entry.get("page_number", 0),
            "model_id": audit_entry.get("model_id", ""),
        }

        # Write to DynamoDB
        dynamodb_table.put_item(Item=item)

        logger.info(
            "Audit log persisted: doc=%s status=%s reason=%s",
            document_id,
            audit_entry.get("status"),
            audit_entry.get("reason_code"),
        )
        return True

    except Exception as e:
        # Log error but don't block Tier 3 processing
        logger.error(
            "Failed to persist audit log for document %s: %s",
            document_id,
            str(e),
            exc_info=True,
        )
        return False


def write_audit_batch_to_dynamodb(
    audit_entries: list[dict[str, Any]],
    document_id: str,
    dynamodb_table: Any,
    user_id: str = "SYSTEM",
) -> dict[str, Any]:
    """
    Write multiple audit entries to DynamoDB in batch.

    Args:
        audit_entries: List of audit dicts from tier3_processor
        document_id: Document identifier
        dynamodb_table: Boto3 DynamoDB table resource
        user_id: User who triggered corrections

    Returns:
        dict with keys:
            - succeeded: count of successful writes
            - failed: count of failed writes
            - failed_entries: list of entries that failed
    """
    results = {"succeeded": 0, "failed": 0, "failed_entries": []}

    for entry in audit_entries:
        if write_audit_to_dynamodb(
            entry, document_id, dynamodb_table, user_id
        ):
            results["succeeded"] += 1
        else:
            results["failed"] += 1
            results["failed_entries"].append(entry)

    logger.info(
        "Batch audit write complete: %d succeeded, %d failed",
        results["succeeded"],
        results["failed"],
    )
    return results


def get_audit_history(
    dynamodb_table: Any,
    document_id: str,
) -> Optional[list[dict[str, Any]]]:
    """
    Retrieve full audit history for a document.

    Args:
        dynamodb_table: Boto3 DynamoDB table resource (ClinicalDocs_AuditLogs)
        document_id: Document to retrieve history for

    Returns:
        List of audit entries sorted by timestamp, or None on error.
    """
    try:
        response = dynamodb_table.query(
            IndexName="document_id-timestamp-index",
            KeyConditionExpression="document_id = :doc_id",
            ExpressionAttributeValues={":doc_id": document_id},
            ScanIndexForward=True,  # Sort ascending (oldest first)
        )
        return response.get("Items", [])
    except Exception as e:
        logger.error("Failed to retrieve audit history for %s: %s", document_id, e)
        return None


def _map_audit_status_to_action(status: str) -> str:
    """
    Map Tier 3 status to human-readable action string.

    Used for audit trail clarity in compliance reviews.
    """
    mapping = {
        "SUCCESS": "OCR_CORRECTION_COMPLETED",
        "REVIEW_REQUIRED": "OCR_CORRECTION_FLAGGED_FOR_REVIEW",
        "ACCEPTED": "OCR_CORRECTION_ACCEPTED",
        "HALLUCINATED": "OCR_CORRECTION_REJECTED_HALLUCINATION",
        "NO_CHANGE": "OCR_TEXT_UNCHANGED",
        "SKIPPED": "OCR_REGION_SKIPPED",
    }
    return mapping.get(status, "OCR_CORRECTION_UNKNOWN")
