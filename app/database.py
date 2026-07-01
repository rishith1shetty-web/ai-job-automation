from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrations = [
        "ALTER TYPE applicationstatus ADD VALUE IF NOT EXISTS 'NEEDS_REVIEW'",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS technologies JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS domain_expertise JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS detected_role VARCHAR(255)",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS role_suggestions JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS generated_search_keywords JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS generated_search_query TEXT",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS role_confidence DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS dry_run BOOLEAN DEFAULT TRUE NOT NULL",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS current_step VARCHAR(255)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS evidence_path VARCHAR(1024)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS resume_filename_used VARCHAR(255)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS resume_upload_status VARCHAR(100)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS resume_upload_evidence_path VARCHAR(1024)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS search_batch_id VARCHAR(36)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS search_timestamp TIMESTAMP WITHOUT TIME ZONE",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS complexity_score DOUBLE PRECISION DEFAULT 50 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS priority_score DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS complexity_level VARCHAR(30)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS estimated_steps INTEGER DEFAULT 1 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS screening_question_count INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS required_fields INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS employment_history_required BOOLEAN DEFAULT FALSE NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS freshness_score DOUBLE PRECISION DEFAULT 50 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS search_query_used TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS role_detected VARCHAR(255)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS expanded_role VARCHAR(255)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS priority_reason TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS match_reason TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS job_description_metadata JSONB DEFAULT '{}'::jsonb NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS matched_skills JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS missing_skills JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS semantic_match_score DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS overall_match_summary TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS matched_skills_summary TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS missing_skills_summary TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS strength_summary TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS weakness_summary TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS recommendation_summary TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS generated_cover_letter TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS resume_improvement_suggestions JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS technical_match_score DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS industry_match_score DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS confidence_score VARCHAR(50)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS reasoning_type VARCHAR(100)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS reasoning_summary TEXT",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS confidence_score VARCHAR(50)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS evidence_sources JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS address VARCHAR(512)",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS current_job_title VARCHAR(255)",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS professional_headline VARCHAR(512)",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS current_company VARCHAR(255)",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS previous_companies JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS certifications JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS professional_links JSONB DEFAULT '{}'::jsonb NOT NULL",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS field_sources JSONB DEFAULT '{}'::jsonb NOT NULL",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS missing_required_after_apply JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE job_opportunities DROP CONSTRAINT IF EXISTS uq_job_url_resume",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_job_url_resume_batch'
            ) THEN
                ALTER TABLE job_opportunities
                ADD CONSTRAINT uq_job_url_resume_batch
                UNIQUE (job_url, resume_id, search_batch_id);
            END IF;
        END $$;
        """,
        "CREATE INDEX IF NOT EXISTS ix_job_opportunities_search_batch_id ON job_opportunities (search_batch_id)",
        "CREATE INDEX IF NOT EXISTS ix_job_opportunities_search_timestamp ON job_opportunities (search_timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_job_opportunities_priority_score ON job_opportunities (priority_score)",
        "CREATE INDEX IF NOT EXISTS ix_candidate_profiles_is_default ON candidate_profiles (is_default)",
        """
        CREATE TABLE IF NOT EXISTS screening_automation_settings (
            id SERIAL PRIMARY KEY,
            mode VARCHAR(30) DEFAULT 'safe' NOT NULL,
            notice_period_default VARCHAR(100),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS screening_knowledge_answers (
            id SERIAL PRIMARY KEY,
            candidate_profile_id INTEGER REFERENCES candidate_profiles(id),
            resume_id INTEGER REFERENCES resume_profiles(id),
            question_text TEXT NOT NULL,
            normalized_question VARCHAR(512) NOT NULL,
            category VARCHAR(100) DEFAULT 'general' NOT NULL,
            answer TEXT NOT NULL,
            source VARCHAR(100) DEFAULT 'Manual' NOT NULL,
            confidence VARCHAR(50) DEFAULT 'High' NOT NULL,
            currency VARCHAR(10),
            evidence JSONB DEFAULT '{}'::jsonb NOT NULL,
            use_count INTEGER DEFAULT 0 NOT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            CONSTRAINT uq_screening_answer_candidate_question
                UNIQUE (candidate_profile_id, normalized_question)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_screening_knowledge_answers_candidate_profile_id ON screening_knowledge_answers (candidate_profile_id)",
        "CREATE INDEX IF NOT EXISTS ix_screening_knowledge_answers_resume_id ON screening_knowledge_answers (resume_id)",
        "CREATE INDEX IF NOT EXISTS ix_screening_knowledge_answers_normalized_question ON screening_knowledge_answers (normalized_question)",
        "CREATE INDEX IF NOT EXISTS ix_screening_knowledge_answers_category ON screening_knowledge_answers (category)",
        """
        CREATE TABLE IF NOT EXISTS form_field_learning (
            id SERIAL PRIMARY KEY,
            original_label TEXT NOT NULL,
            normalized_field VARCHAR(255) NOT NULL,
            field_category VARCHAR(100) NOT NULL,
            field_type VARCHAR(100),
            company VARCHAR(255),
            role VARCHAR(255),
            times_seen INTEGER DEFAULT 0 NOT NULL,
            times_successful INTEGER DEFAULT 0 NOT NULL,
            times_failed INTEGER DEFAULT 0 NOT NULL,
            times_used INTEGER DEFAULT 0 NOT NULL,
            confidence VARCHAR(50) DEFAULT 'Low' NOT NULL,
            first_seen TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            last_seen TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            validation_examples JSONB DEFAULT '[]'::jsonb NOT NULL,
            reasoning_used JSONB DEFAULT '[]'::jsonb NOT NULL,
            last_successful_value_source VARCHAR(255),
            suggested_value_source VARCHAR(255),
            reasoning_summary TEXT,
            CONSTRAINT uq_form_field_learning_mapping
                UNIQUE (original_label, normalized_field, company, role)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_form_field_learning_normalized_field ON form_field_learning (normalized_field)",
        "CREATE INDEX IF NOT EXISTS ix_form_field_learning_field_category ON form_field_learning (field_category)",
        "CREATE INDEX IF NOT EXISTS ix_form_field_learning_company ON form_field_learning (company)",
        "CREATE INDEX IF NOT EXISTS ix_form_field_learning_role ON form_field_learning (role)",
        """
        CREATE TABLE IF NOT EXISTS generalized_form_field_metadata (
            id SERIAL PRIMARY KEY,
            portal VARCHAR(100) DEFAULT 'linkedin' NOT NULL,
            original_field_label TEXT NOT NULL,
            normalized_field_name VARCHAR(255) NOT NULL,
            field_category VARCHAR(100) DEFAULT 'general' NOT NULL,
            detected_control_type VARCHAR(100) NOT NULL,
            recommended_fill_strategy VARCHAR(100) NOT NULL,
            confidence VARCHAR(50) DEFAULT 'Low' NOT NULL,
            times_seen INTEGER DEFAULT 0 NOT NULL,
            times_successful INTEGER DEFAULT 0 NOT NULL,
            times_failed INTEGER DEFAULT 0 NOT NULL,
            last_validation_message TEXT,
            evidence_used JSONB DEFAULT '{}'::jsonb NOT NULL,
            last_used_at TIMESTAMP WITHOUT TIME ZONE,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            CONSTRAINT uq_generalized_form_field_metadata
                UNIQUE (portal, original_field_label, normalized_field_name)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_generalized_form_metadata_portal ON generalized_form_field_metadata (portal)",
        "CREATE INDEX IF NOT EXISTS ix_generalized_form_metadata_normalized ON generalized_form_field_metadata (normalized_field_name)",
        "CREATE INDEX IF NOT EXISTS ix_generalized_form_metadata_category ON generalized_form_field_metadata (field_category)",
        "CREATE INDEX IF NOT EXISTS ix_generalized_form_metadata_control ON generalized_form_field_metadata (detected_control_type)",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_job_opportunities_search_batch'
            ) THEN
                ALTER TABLE job_opportunities
                ADD CONSTRAINT fk_job_opportunities_search_batch
                FOREIGN KEY (search_batch_id) REFERENCES job_search_batches(id);
            END IF;
        END $$;
        """,
    ]
    with engine.begin() as connection:
        for statement in migrations:
            connection.execute(text(statement))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
