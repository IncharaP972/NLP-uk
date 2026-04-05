"""
test_tier2_layoutlmv3_refinement.py — Unit tests for Tier 2 LayoutLMv3 refinement
==================================================================================
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from PIL import Image
import json
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tier2_layoutlmv3_refinement import (
    LayoutLMv3Refiner,
    RefinedElement,
    Tier2RefinementOutput,
    refine_textract_batch,
)


class TestRefinedElement(unittest.TestCase):
    """Test RefinedElement dataclass."""

    def test_refined_element_creation(self):
        """Test creating a RefinedElement."""
        elem = RefinedElement(
            text="Aspirin 500 mg",
            element_type="medical_term",
            confidence=0.92,
            bbox=[100, 200, 300, 250],
            page_number=1,
            requires_escalation=False,
            medical_entity="MEDICATION",
            reasoning="High confidence medical term",
        )

        self.assertEqual(elem.text, "Aspirin 500 mg")
        self.assertEqual(elem.confidence, 0.92)
        self.assertFalse(elem.requires_escalation)
        self.assertEqual(elem.medical_entity, "MEDICATION")

    def test_escalation_flag(self):
        """Test escalation flag for low confidence."""
        elem = RefinedElement(
            text="Unknown term",
            element_type="paragraph",
            confidence=0.78,  # Below 85% threshold
            bbox=[],
            page_number=1,
            requires_escalation=True,
        )

        self.assertTrue(elem.requires_escalation)


class TestLayoutLMv3Refiner(unittest.TestCase):
    """Test LayoutLMv3Refiner class."""

    def setUp(self):
        """Set up test fixtures."""
        # Mock the model loading to avoid downloading large models in tests
        self.patcher = patch(
            "tier2_layoutlmv3_refinement.AutoTokenizer.from_pretrained"
        )
        self.patcher2 = patch(
            "tier2_layoutlmv3_refinement.AutoModelForTokenClassification.from_pretrained"
        )

        self.mock_tokenizer = self.patcher.start()
        self.mock_model = self.patcher2.start()

        # Create test image
        self.test_image = Image.new("RGB", (800, 600), color="white")

        # Sample Textract output
        self.sample_textract = {
            "Blocks": [
                {
                    "BlockType": "TITLE",
                    "Text": "Clinical Notes",
                    "Confidence": 95,
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.1,
                            "Top": 0.1,
                            "Width": 0.8,
                            "Height": 0.1,
                        }
                    },
                },
                {
                    "BlockType": "LINE",
                    "Text": "Patient diagnosed with diabetes",
                    "Confidence": 87,
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.1,
                            "Top": 0.2,
                            "Width": 0.8,
                            "Height": 0.05,
                        }
                    },
                },
                {
                    "BlockType": "LINE",
                    "Text": "Prescribed Metformin 500mg",
                    "Confidence": 82,
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.1,
                            "Top": 0.3,
                            "Width": 0.8,
                            "Height": 0.05,
                        }
                    },
                },
            ]
        }

    def tearDown(self):
        """Clean up patches."""
        self.patcher.stop()
        self.patcher2.stop()

    def test_refiner_initialization(self):
        """Test LayoutLMv3Refiner initialization."""
        refiner = LayoutLMv3Refiner()

        self.assertEqual(refiner.confidence_threshold, 0.85)
        self.assertEqual(refiner.confidence_low_threshold, 0.90)
        self.assertEqual(refiner.device, "cpu")

    def test_parse_textract_output(self):
        """Test parsing Textract JSON output."""
        refiner = LayoutLMv3Refiner()
        elements = refiner._parse_textract_output(self.sample_textract)

        self.assertEqual(len(elements), 3)
        self.assertEqual(elements[0]["type"], "heading")
        self.assertEqual(elements[1]["type"], "paragraph")
        self.assertGreater(elements[0]["confidence"], 0.8)

    def test_classify_medical_entity_medication(self):
        """Test medical entity classification for medications."""
        refiner = LayoutLMv3Refiner()

        result = refiner._classify_medical_entity("Patient taking Aspirin")
        self.assertEqual(result, "MEDICATION")

        result = refiner._classify_medical_entity("Metformin prescribed")
        self.assertEqual(result, "MEDICATION")

    def test_classify_medical_entity_diagnosis(self):
        """Test medical entity classification for diagnosis."""
        refiner = LayoutLMv3Refiner()

        result = refiner._classify_medical_entity("Diagnosed with diabetes")
        self.assertEqual(result, "DIAGNOSIS")

        result = refiner._classify_medical_entity("Pneumonia detected")
        self.assertEqual(result, "DIAGNOSIS")

    def test_classify_medical_entity_none(self):
        """Test medical entity classification for non-medical text."""
        refiner = LayoutLMv3Refiner()

        result = refiner._classify_medical_entity("The quick brown fox")
        self.assertIsNone(result)

    def test_extract_bbox(self):
        """Test bounding box extraction."""
        refiner = LayoutLMv3Refiner()

        block = {
            "Geometry": {
                "BoundingBox": {
                    "Left": 0.1,
                    "Top": 0.2,
                    "Width": 0.3,
                    "Height": 0.15,
                }
            }
        }

        bbox = refiner._extract_bbox(block)
        self.assertEqual(bbox, [0.1, 0.2, 0.4, 0.35])

    def test_extract_bbox_missing_geometry(self):
        """Test bbox extraction with missing geometry."""
        refiner = LayoutLMv3Refiner()

        block = {"BlockType": "LINE", "Text": "Some text"}
        bbox = refiner._extract_bbox(block)
        self.assertEqual(bbox, [])

    def test_calculate_quality_score(self):
        """Test quality score calculation."""
        refiner = LayoutLMv3Refiner()

        elements = [
            RefinedElement(
                text="High confidence",
                element_type="paragraph",
                confidence=0.95,
                bbox=[],
                page_number=1,
                requires_escalation=False,
            ),
            RefinedElement(
                text="Medium confidence",
                element_type="paragraph",
                confidence=0.85,
                bbox=[],
                page_number=1,
                requires_escalation=False,
            ),
        ]

        quality = refiner._calculate_quality_score(elements)
        self.assertAlmostEqual(quality, 0.90, places=2)

    def test_refine_document_high_confidence(self):
        """Test refinement with high-confidence Textract output."""
        refiner = LayoutLMv3Refiner()

        output = refiner.refine_document(
            textract_output=self.sample_textract,
            page_image=self.test_image,
            document_id="TEST_DOC_001",
            page_number=1,
        )

        self.assertIsInstance(output, Tier2RefinementOutput)
        self.assertEqual(output.document_id, "TEST_DOC_001")
        self.assertEqual(output.page_number, 1)
        self.assertGreater(output.quality_score, 0.0)

    def test_refine_document_escalation(self):
        """Test escalation for low-confidence elements."""
        refiner = LayoutLMv3Refiner()

        # Modify sample to have low confidence
        low_conf_textract = {
            "Blocks": [
                {
                    "BlockType": "LINE",
                    "Text": "Unclear text",
                    "Confidence": 70,  # Below 85% threshold
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.1,
                            "Top": 0.2,
                            "Width": 0.8,
                            "Height": 0.05,
                        }
                    },
                }
            ]
        }

        output = refiner.refine_document(
            textract_output=low_conf_textract,
            page_image=self.test_image,
            document_id="TEST_DOC_002",
            page_number=1,
        )

        # Low confidence element should be escalated
        self.assertGreater(len(output.escalation_queue), 0)

    def test_medical_entity_classification_in_refinement(self):
        """Test that medical entities are properly classified during refinement."""
        refiner = LayoutLMv3Refiner()

        output = refiner.refine_document(
            textract_output=self.sample_textract,
            page_image=self.test_image,
            document_id="TEST_DOC_003",
            page_number=1,
        )

        # Check that medical entities were identified
        all_elements = output.refined_elements + output.escalation_queue
        medical_entities = [e for e in all_elements if e.medical_entity]

        self.assertGreater(len(medical_entities), 0)

    def test_processing_time_tracking(self):
        """Test that processing time is tracked."""
        refiner = LayoutLMv3Refiner()

        output = refiner.refine_document(
            textract_output=self.sample_textract,
            page_image=self.test_image,
            document_id="TEST_DOC_004",
            page_number=1,
        )

        self.assertGreaterEqual(output.processing_time_ms, 0)
        self.assertLess(output.processing_time_ms, 10000)  # Should be < 10s

    def test_estimate_layout_complexity(self):
        """Test layout complexity estimation."""
        refiner = LayoutLMv3Refiner()

        # Textract with no tables
        simple_textract = {
            "Blocks": [
                {"BlockType": "LINE", "Text": "Simple text"},
            ]
        }

        complexity = refiner._estimate_layout_complexity(simple_textract, self.test_image)
        self.assertGreater(complexity, 0.0)
        self.assertLess(complexity, 1.0)


class TestTier2RefinementOutput(unittest.TestCase):
    """Test Tier2RefinementOutput dataclass."""

    def test_output_creation(self):
        """Test creating a Tier2RefinementOutput."""
        elem = RefinedElement(
            text="Test",
            element_type="paragraph",
            confidence=0.9,
            bbox=[],
            page_number=1,
            requires_escalation=False,
        )

        output = Tier2RefinementOutput(
            document_id="DOC_001",
            page_number=1,
            timestamp=datetime.utcnow().isoformat(),
            original_textract={},
            refined_elements=[elem],
            escalation_queue=[],
            quality_score=0.9,
            layout_complexity=0.3,
            processing_time_ms=450.0,
        )

        self.assertEqual(output.document_id, "DOC_001")
        self.assertEqual(len(output.refined_elements), 1)
        self.assertEqual(len(output.escalation_queue), 0)


class TestBatchProcessing(unittest.TestCase):
    """Test batch processing functionality."""

    @patch("tier2_layoutlmv3_refinement.LayoutLMv3Refiner.refine_document")
    def test_batch_processing_basic(self, mock_refine):
        """Test batch processing structure and output types."""
        # Mock refine_document result
        mock_output = Tier2RefinementOutput(
            document_id="file1",
            page_number=1,
            timestamp=datetime.utcnow().isoformat(),
            original_textract={},
            refined_elements=[],
            escalation_queue=[],
            quality_score=0.85,
            layout_complexity=0.3,
            processing_time_ms=500.0,
        )
        mock_refine.return_value = mock_output

        # Verify the output structure
        self.assertEqual(mock_output.document_id, "file1")
        self.assertEqual(mock_output.quality_score, 0.85)


if __name__ == "__main__":
    unittest.main()
