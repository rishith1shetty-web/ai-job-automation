from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/linkedin_auto_apply"
    openai_api_key: str | None = None
    openai_match_model: str = "gpt-4o-mini"
    linkedin_storage_state: Path = Path("storage/linkedin_state.json")
    dice_storage_state: Path = Path("storage/dice_state.json")
    linkedin_headless: bool = False
    linkedin_slow_mo_ms: int = 50
    app_upload_dir: Path = Path("storage/uploads")
    max_applications_per_run: int = 50
    find_matches_default_limit: int = 100
    find_matches_max_results: int = 100
    auto_apply_dry_run: bool = True
    live_test_output_dir: Path = Path("outputs/live_test")
    easy_apply_debug_dir: Path = Path("outputs/easy_apply_debug")
    contact_debug_dir: Path = Path("outputs/contact_debug")
    profile_autofill_debug_dir: Path = Path("outputs/profile_autofill_debug")
    form_navigation_debug_dir: Path = Path("outputs/form_navigation_debug")
    screening_debug_dir: Path = Path("outputs/screening_debug")
    resume_upload_debug_dir: Path = Path("outputs/resume_upload_debug")
    workflow_debug_dir: Path = Path("outputs/workflow_debug")
    employment_history_debug_dir: Path = Path("outputs/employment_history_debug")
    find_matches_debug_dir: Path = Path("outputs/find_matches_debug")
    find_matches_batch_debug_dir: Path = Path("outputs/find_matches_batch_debug")
    dice_debug_dir: Path = Path("outputs/dice_debug")
    portal_debug_dir: Path = Path("outputs/portal_debug")
    performance_debug_dir: Path = Path("outputs/performance_debug")
    reasoning_debug_dir: Path = Path("outputs/reasoning_debug")
    resume_intelligence_debug_dir: Path = Path("outputs/resume_intelligence_debug")
    form_learning_debug_dir: Path = Path("outputs/form_learning_debug")
    generalized_form_debug_dir: Path = Path("outputs/generalized_form_debug")
    apply_top_debug_dir: Path = Path("outputs/apply_top_debug")
    radio_writer_debug_dir: Path = Path("outputs/radio_writer_debug")
    api_base_url: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.app_upload_dir.mkdir(parents=True, exist_ok=True)
    settings.linkedin_storage_state.parent.mkdir(parents=True, exist_ok=True)
    settings.dice_storage_state.parent.mkdir(parents=True, exist_ok=True)
    settings.live_test_output_dir.mkdir(parents=True, exist_ok=True)
    (settings.live_test_output_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    settings.easy_apply_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.contact_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.profile_autofill_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.form_navigation_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.screening_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.resume_upload_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.workflow_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.employment_history_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.find_matches_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.find_matches_batch_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.dice_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.portal_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.performance_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.reasoning_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.resume_intelligence_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.form_learning_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.generalized_form_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.apply_top_debug_dir.mkdir(parents=True, exist_ok=True)
    settings.radio_writer_debug_dir.mkdir(parents=True, exist_ok=True)
    return settings
