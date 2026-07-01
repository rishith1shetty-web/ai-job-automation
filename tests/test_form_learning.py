import unittest

from sqlalchemy import delete, select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import _save_form_learning_observations
from app.models import ApplicationStatus, FormFieldLearning, GeneralizedFormFieldMetadata, JobOpportunity
from app.services.form_learning import FieldLearningInput, FormLearningEngine
from app.services.linkedin import AutomationResult


class FormLearningEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.db = SessionLocal()
        self.db.execute(delete(GeneralizedFormFieldMetadata))
        self.db.execute(delete(FormFieldLearning))
        self.db.commit()
        self.engine = FormLearningEngine(Settings(openai_api_key=None))

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.execute(delete(GeneralizedFormFieldMetadata))
        self.db.execute(delete(FormFieldLearning))
        self.db.commit()
        self.db.close()

    def test_salary_equivalent_labels_normalize_to_salary(self) -> None:
        labels = ["Current Annual Salary", "Current Compensation", "Current CTC"]
        normalized = [self.engine.evaluate_field(FieldLearningInput(label=label)).normalized_field for label in labels]

        self.assertEqual(normalized, ["Salary", "Salary", "Salary"])

    def test_work_authorization_equivalent_labels(self) -> None:
        labels = ["Visa Sponsorship", "Right to Work", "Authorized to Work"]
        normalized = [self.engine.evaluate_field(FieldLearningInput(label=label)).normalized_field for label in labels]

        self.assertEqual(normalized, ["Work Authorization", "Work Authorization", "Work Authorization"])

    def test_skill_years_normalization(self) -> None:
        decision = self.engine.evaluate_field(FieldLearningInput(label="SQL Experience (Years)"))

        self.assertEqual(decision.normalized_field, "SQL Years")
        self.assertEqual(decision.field_category, "Skill Years")
        self.assertEqual(decision.suggested_value_source, "Skill Experience Estimator")

    def test_successful_mapping_increases_confidence(self) -> None:
        field = FieldLearningInput(label="Mobile phone number", company="A", role="Analyst", input_type="text")
        for _ in range(2):
            self.engine.record_observation(self.db, field, success=True, value_source="Candidate Profile")
        self.db.commit()

        record = self.db.scalar(select(FormFieldLearning).where(FormFieldLearning.normalized_field == "Mobile Phone"))
        self.assertEqual(record.times_seen, 2)
        self.assertEqual(record.times_successful, 2)
        self.assertEqual(record.confidence, "High")

    def test_validation_failure_records_suggested_fix_and_decreases_confidence(self) -> None:
        record = self.engine.record_observation(
            self.db,
            FieldLearningInput(label="Month of From", company="A", role="Analyst", input_type="select"),
            success=False,
            validation_message="This field is required.",
        )
        self.db.commit()

        self.assertEqual(record.normalized_field, "Start Month")
        self.assertEqual(record.confidence, "Very Low")
        self.assertIn("suggested_fix", record.validation_examples[0])

    def test_cross_company_learning_groups_equivalent_fields(self) -> None:
        self.engine.record_observation(
            self.db,
            FieldLearningInput(label="Current Compensation", company="A", role="Analyst"),
            success=True,
            value_source="Candidate Knowledge Base",
        )
        self.engine.record_observation(
            self.db,
            FieldLearningInput(label="Current CTC", company="B", role="Analyst"),
            success=True,
            value_source="Candidate Knowledge Base",
        )
        self.db.commit()

        records = list(self.db.scalars(select(FormFieldLearning).where(FormFieldLearning.normalized_field == "Salary")).all())
        self.assertEqual(len(records), 2)
        self.assertEqual({record.company for record in records}, {"A", "B"})

    def test_learn_from_application_result_records_contact_screening_and_employment(self) -> None:
        job = JobOpportunity(
            id=501,
            resume_id=1,
            title="Data Analyst",
            company="ExampleCo",
            location="India",
            job_url="https://example.com",
            description="SQL and dashboard role.",
        )
        result = AutomationResult(
            status=ApplicationStatus.SKIPPED,
            contact_fields_detected=[
                {
                    "field_label": "Mobile phone number",
                    "contact_key": "mobile_phone",
                    "field_type": "text",
                    "required": True,
                    "existing_value_present": False,
                }
            ],
            contact_fields_filled=[
                {
                    "field_label": "Mobile phone number",
                    "contact_key": "mobile_phone",
                    "value_source": "Candidate Profile",
                }
            ],
            screening_answers=[
                {
                    "question_text": "SQL Experience (Years)",
                    "field_type": "text",
                    "filled_successfully": True,
                    "source": "Skill Experience Estimator",
                    "confidence": "Medium",
                }
            ],
            log=[
                {
                    "event": "employment_history_autofill",
                    "detail": '[{"field_label":"Company","field_key":"company_name","field_type":"text","success":true,"source":"LinkedIn Prefill"}]',
                }
            ],
        )

        learned = self.engine.learn_from_application_result(self.db, job, result)
        self.db.commit()

        normalized = {item.normalized_field for item in learned}
        self.assertIn("Mobile Phone", normalized)
        self.assertIn("SQL Years", normalized)
        self.assertIn("Company", normalized)

    def test_debug_files_are_written(self) -> None:
        record = self.engine.record_observation(
            self.db,
            FieldLearningInput(label="Current Package", company="A", role="Analyst"),
            success=True,
            value_source="Candidate Knowledge Base",
        )
        debug_dir = self.engine.create_debug_dir()
        self.engine.write_debug(debug_dir, [record])

        self.assertTrue((debug_dir / "learned_fields.json").exists())
        self.assertTrue((debug_dir / "field_normalization.json").exists())
        self.assertTrue((debug_dir / "confidence_updates.json").exists())

    def test_application_learning_populates_generalized_form_metadata(self) -> None:
        job = JobOpportunity(
            id=502,
            resume_id=1,
            title="Data Analyst",
            company="ExampleCo",
            location="India",
            job_url="https://example.com/502",
            description="SQL and dashboard role.",
        )
        result = AutomationResult(
            status=ApplicationStatus.SKIPPED,
            contact_fields_detected=[
                {
                    "field_label": "Mobile phone number",
                    "contact_key": "mobile_phone",
                    "field_type": "text",
                    "required": True,
                    "existing_value_present": False,
                }
            ],
            contact_fields_filled=[
                {
                    "field_label": "Mobile phone number",
                    "contact_key": "mobile_phone",
                    "value_source": "Candidate Profile",
                }
            ],
        )

        _save_form_learning_observations(self.db, job, result)
        self.db.commit()

        metadata = list(self.db.scalars(select(GeneralizedFormFieldMetadata)).all())
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].portal, "linkedin")
        self.assertEqual(metadata[0].normalized_field_name, "Mobile Phone")
        self.assertEqual(metadata[0].recommended_fill_strategy, "type_value")


if __name__ == "__main__":
    unittest.main()
