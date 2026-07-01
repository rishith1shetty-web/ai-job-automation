import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import delete, select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.models import FormFieldLearning, GeneralizedFormFieldMetadata
from app.services.generalized_form_engine import GeneralizedFieldDetection, GeneralizedFieldInput, GeneralizedFormEngine


class GeneralizedFormEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.db = SessionLocal()
        self.db.execute(delete(GeneralizedFormFieldMetadata))
        self.db.execute(delete(FormFieldLearning))
        self.db.commit()
        self.settings = Settings(openai_api_key=None)
        self.engine = GeneralizedFormEngine(self.settings)

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.execute(delete(GeneralizedFormFieldMetadata))
        self.db.execute(delete(FormFieldLearning))
        self.db.commit()
        self.db.close()

    def test_textbox_detection(self) -> None:
        detection = self.engine.detect_field(GeneralizedFieldInput(label="First name", input_type="text", tag_name="input"))

        self.assertEqual(detection.detected_control_type, "textbox")
        self.assertEqual(detection.recommended_fill_strategy, "type_value")

    def test_textarea_detection(self) -> None:
        detection = self.engine.detect_field(GeneralizedFieldInput(label="Cover letter", tag_name="textarea"))

        self.assertEqual(detection.detected_control_type, "textarea")
        self.assertEqual(detection.recommended_fill_strategy, "type_longer_answer")

    def test_dropdown_detection(self) -> None:
        detection = self.engine.detect_field(
            GeneralizedFieldInput(label="Phone country code", tag_name="select", options=["India (+91)", "United States (+1)"])
        )

        self.assertEqual(detection.detected_control_type, "dropdown")
        self.assertEqual(detection.recommended_fill_strategy, "select_matching_option")

    def test_searchable_dropdown_detection(self) -> None:
        detection = self.engine.detect_field(
            GeneralizedFieldInput(
                label="Location",
                tag_name="select",
                role="combobox",
                dom_attributes={"class": "search typeahead"},
                options=["Mumbai", "Pune"],
            )
        )

        self.assertEqual(detection.detected_control_type, "searchable_dropdown")

    def test_autocomplete_detection(self) -> None:
        detection = self.engine.detect_field(
            GeneralizedFieldInput(label="School", role="combobox", dom_attributes={"aria-autocomplete": "list"})
        )

        self.assertEqual(detection.detected_control_type, "autocomplete")

    def test_checkbox_detection(self) -> None:
        detection = self.engine.detect_field(GeneralizedFieldInput(label="Currently work here", input_type="checkbox"))

        self.assertEqual(detection.detected_control_type, "checkbox")

    def test_radio_detection(self) -> None:
        detection = self.engine.detect_field(GeneralizedFieldInput(label="Authorized to work", input_type="radio"))

        self.assertEqual(detection.detected_control_type, "radio")

    def test_date_month_year_numeric_file_and_multiselect_detection(self) -> None:
        cases = [
            (GeneralizedFieldInput(label="Start date", input_type="date"), "date_picker"),
            (GeneralizedFieldInput(label="Start Month", tag_name="select"), "month_picker"),
            (GeneralizedFieldInput(label="Start Year", input_type="number"), "year_picker"),
            (GeneralizedFieldInput(label="How many years of SQL experience?", input_type="number"), "numeric_input"),
            (GeneralizedFieldInput(label="Upload Resume", input_type="file"), "file_upload"),
            (GeneralizedFieldInput(label="Skills", tag_name="select", multiple=True), "multi_select"),
        ]

        for field, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.engine.detect_field(field).detected_control_type, expected)

    def test_field_normalization_examples(self) -> None:
        examples = {
            "Current CTC": "Salary",
            "Right to Work": "Work Authorization",
            "Month of From": "Employment Start Month",
        }

        for label, expected in examples.items():
            with self.subTest(label=label):
                self.assertEqual(self.engine.detect_field(GeneralizedFieldInput(label=label)).normalized_field_name, expected)

    def test_ai_form_learning_reuse(self) -> None:
        learned = FormFieldLearning(
            original_label="Current Compensation",
            normalized_field="Salary",
            field_category="Salary",
            field_type="text",
            confidence="High",
            times_seen=3,
            times_successful=3,
        )
        detection = self.engine.detect_field(GeneralizedFieldInput(label="Current Compensation"), learned=learned)

        self.assertEqual(detection.source, "AI Form Learning Reuse")
        self.assertEqual(detection.normalized_field_name, "Salary")
        self.assertEqual(detection.evidence_used["learning_reuse"]["times_successful"], 3)

    def test_portal_adapter_interface_supports_linkedin(self) -> None:
        detection = self.engine.detect_field(
            GeneralizedFieldInput(
                label="Location (city)",
                portal="linkedin",
                dom_attributes={"class": "jobs-easy-apply-form-section__grouping"},
            )
        )

        self.assertEqual(detection.field_category, "Contact")
        self.assertEqual(detection.evidence_used["dom_attribute_keys"], ["class"])

    def test_metadata_is_stored_for_dashboard(self) -> None:
        field = GeneralizedFieldInput(label="Current CTC", portal="linkedin", input_type="number")
        detection = self.engine.detect_field(field)
        record = self.engine.record_metadata(self.db, field, detection, success=True)
        self.db.commit()

        self.assertEqual(record.portal, "linkedin")
        self.assertEqual(record.normalized_field_name, "Salary")
        self.assertEqual(record.detected_control_type, "numeric_input")
        self.assertEqual(record.times_successful, 1)

    def test_duplicate_metadata_before_commit_reuses_pending_record(self) -> None:
        field = GeneralizedFieldInput(label="required field 6", portal="linkedin", input_type="text")
        detection = GeneralizedFieldDetection(
            detected_control_type="textbox",
            normalized_field_name="Cover Letter",
            field_category="Cover Letter",
            confidence="High",
            recommended_fill_strategy="type_value",
            evidence_used={},
        )

        first = self.engine.record_metadata(self.db, field, detection, success=True)
        second = self.engine.record_metadata(self.db, field, detection, success=False)
        self.db.commit()

        records = list(self.db.scalars(select(GeneralizedFormFieldMetadata)).all())
        self.assertIs(first, second)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].times_seen, 2)
        self.assertEqual(records[0].times_successful, 1)
        self.assertEqual(records[0].times_failed, 1)

    def test_debug_files_show_unified_pipeline_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(openai_api_key=None, generalized_form_debug_dir=Path(temporary))
            engine = GeneralizedFormEngine(settings)
            fields = [GeneralizedFieldInput(label="Current CTC", input_type="number")]
            detections = engine.detect_fields(fields)
            debug_dir = engine.create_debug_dir()
            engine.write_debug(debug_dir, fields, detections)

            self.assertTrue((debug_dir / "detected_fields.json").exists())
            pipeline = json.loads((debug_dir / "pipeline_integration.json").read_text(encoding="utf-8"))
            self.assertEqual(pipeline["engine_role"], "control_detection_only")
            self.assertEqual(pipeline["value_source"], "Unified decision pipeline")


if __name__ == "__main__":
    unittest.main()
