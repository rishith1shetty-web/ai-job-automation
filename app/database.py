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
        "ALTER TYPE applicationstatus ADD VALUE IF NOT EXISTS 'DRY_RUN_READY'",
        "ALTER TYPE applicationstatus ADD VALUE IF NOT EXISTS 'UNSUPPORTED_FORM'",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS technologies JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE resume_profiles ADD COLUMN IF NOT EXISTS portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL",
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
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'completed' NOT NULL",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS manual_keywords TEXT",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS requested_limit INTEGER DEFAULT 50 NOT NULL",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS current_search_variation TEXT",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS jobs_collected INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS easy_apply_count INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS duplicates_removed INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS latest_status_message TEXT",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS debug_path VARCHAR(1024)",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITHOUT TIME ZONE",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP WITHOUT TIME ZONE",
        "ALTER TABLE job_search_batches ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS complexity_score DOUBLE PRECISION DEFAULT 50 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS priority_score DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS complexity_level VARCHAR(30)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS estimated_steps INTEGER DEFAULT 1 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS screening_question_count INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS required_fields INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS employment_history_required BOOLEAN DEFAULT FALSE NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS application_friction_score DOUBLE PRECISION DEFAULT 50 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS completion_likelihood_score DOUBLE PRECISION DEFAULT 50 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS unsupported_field_count INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS external_form_risk DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS manual_review_risk DOUBLE PRECISION DEFAULT 0 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS form_provider VARCHAR(100)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS friction_reasons JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS recommended_apply_priority VARCHAR(30)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS apply_priority_reason TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS freshness_score DOUBLE PRECISION DEFAULT 50 NOT NULL",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS search_query_used TEXT",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS role_detected VARCHAR(255)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS expanded_role VARCHAR(255)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS search_role VARCHAR(255)",
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS role_priority VARCHAR(50)",
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
        "ALTER TABLE job_opportunities ADD COLUMN IF NOT EXISTS portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL",
        "ALTER TABLE apply_top_batches ADD COLUMN IF NOT EXISTS portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS reasoning_type VARCHAR(100)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS reasoning_summary TEXT",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS confidence_score VARCHAR(50)",
        "ALTER TABLE application_records ADD COLUMN IF NOT EXISTS evidence_sources JSONB DEFAULT '[]'::jsonb NOT NULL",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS address VARCHAR(512)",
        "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL",
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
        "CREATE INDEX IF NOT EXISTS ix_job_search_batches_status ON job_search_batches (status)",
        "CREATE INDEX IF NOT EXISTS ix_job_search_batches_portal ON job_search_batches (portal)",
        "CREATE INDEX IF NOT EXISTS ix_job_opportunities_portal ON job_opportunities (portal)",
        "CREATE INDEX IF NOT EXISTS ix_application_records_portal ON application_records (portal)",
        "CREATE INDEX IF NOT EXISTS ix_apply_top_batches_portal ON apply_top_batches (portal)",
        "CREATE INDEX IF NOT EXISTS ix_resume_profiles_portal ON resume_profiles (portal)",
        "CREATE INDEX IF NOT EXISTS ix_candidate_profiles_portal ON candidate_profiles (portal)",
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
        CREATE TABLE IF NOT EXISTS apply_top_batches (
            id VARCHAR(36) PRIMARY KEY,
            resume_id INTEGER REFERENCES resume_profiles(id),
            search_batch_id VARCHAR(36),
            status VARCHAR(50) DEFAULT 'queued' NOT NULL,
            dry_run BOOLEAN DEFAULT TRUE NOT NULL,
            screening_mode VARCHAR(30),
            application_limit INTEGER DEFAULT 10 NOT NULL,
            total_count INTEGER DEFAULT 0 NOT NULL,
            current_index INTEGER DEFAULT 0 NOT NULL,
            current_job_id INTEGER REFERENCES job_opportunities(id),
            current_title VARCHAR(255),
            current_company VARCHAR(255),
            completed_count INTEGER DEFAULT 0 NOT NULL,
            skipped_count INTEGER DEFAULT 0 NOT NULL,
            failed_count INTEGER DEFAULT 0 NOT NULL,
            needs_review_count INTEGER DEFAULT 0 NOT NULL,
            timed_out_count INTEGER DEFAULT 0 NOT NULL,
            latest_status_message TEXT,
            error_message TEXT,
            debug_path VARCHAR(1024),
            job_results JSONB DEFAULT '[]'::jsonb NOT NULL,
            started_at TIMESTAMP WITHOUT TIME ZONE,
            finished_at TIMESTAMP WITHOUT TIME ZONE,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_apply_top_batches_resume_id ON apply_top_batches (resume_id)",
        "ALTER TABLE apply_top_batches ADD COLUMN IF NOT EXISTS application_limit INTEGER DEFAULT 10 NOT NULL",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'apply_top_batches' AND column_name = 'limit'
            ) THEN
                ALTER TABLE apply_top_batches ALTER COLUMN "limit" DROP NOT NULL;
                ALTER TABLE apply_top_batches ALTER COLUMN "limit" SET DEFAULT 10;
            END IF;
        END $$;
        """,
        "CREATE INDEX IF NOT EXISTS ix_apply_top_batches_search_batch_id ON apply_top_batches (search_batch_id)",
        "CREATE INDEX IF NOT EXISTS ix_apply_top_batches_status ON apply_top_batches (status)",
        """
        CREATE TABLE IF NOT EXISTS portal_runtime_state (
            id INTEGER PRIMARY KEY,
            active_portal VARCHAR(50) DEFAULT 'linkedin' NOT NULL,
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
        )
        """,
        """
        INSERT INTO portal_runtime_state (id, active_portal, updated_at)
        VALUES (1, 'linkedin', now())
        ON CONFLICT (id) DO NOTHING
        """,
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
