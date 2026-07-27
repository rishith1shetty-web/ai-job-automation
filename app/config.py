from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import runtime_paths


def _from_paths(attribute: str):
    """A lazy default that reads one resolved runtime location.

    Deliberately a factory, not a value: the layout must be resolved when Settings is
    INSTANTIATED, not when this module is imported, so an environment override
    (``AI_JOB_AUTOMATION_DATA_DIR``) set by a launcher or a test is always honoured. Every
    field keeps its original name, so the matching environment variable
    (e.g. ``APP_UPLOAD_DIR``) still overrides it exactly as before."""

    def factory() -> Path:
        return getattr(runtime_paths(), attribute)

    return factory


def _debug_dir(name: str):
    """A lazy default for one named diagnostics directory under ``outputs``."""

    def factory() -> Path:
        return runtime_paths().debug_dir(name)

    return factory


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:5555@localhost:5432/linkedin_auto_apply"
    openai_api_key: str | None = None
    openai_match_model: str = "gpt-4o-mini"
    # Portal session state. Filenames and JSON format are unchanged -- these are written and
    # read by Playwright's storage_state and must stay byte-compatible. They live under the
    # MACHINE-LOCAL application-data location (never roaming), so each Windows account on each
    # computer keeps its own LinkedIn/Dice login; a session can never follow the user to
    # another machine.
    linkedin_storage_state: Path = Field(default_factory=_from_paths("linkedin_state_file"))
    dice_storage_state: Path = Field(default_factory=_from_paths("dice_state_file"))
    # Dice-only fallback for the wizard's required Work Authorization dropdown, used
    # ONLY when neither the candidate profile nor a saved screening answer supplies
    # one. It is configuration (env-overridable), never a value the automation
    # invents about the candidate's legal status.
    dice_default_work_authorization: str | None = "US Citizen"
    linkedin_headless: bool = False
    linkedin_slow_mo_ms: int = 50
    app_upload_dir: Path = Field(default_factory=_from_paths("uploads"))
    # Apply All processes every eligible job in the selected Find Matches batch, capped at the
    # same ceiling Find Matches itself uses (find_matches_max_results). One click == one batch of
    # up to this many jobs; there is no lower artificial Apply All limit.
    max_applications_per_run: int = 100
    find_matches_default_limit: int = 100
    find_matches_max_results: int = 100
    auto_apply_dry_run: bool = True
    # General-purpose writable locations (disposable, machine-local).
    logs_dir: Path = Field(default_factory=_from_paths("logs"))
    work_dir: Path = Field(default_factory=_from_paths("work"))
    screenshots_dir: Path = Field(default_factory=_from_paths("screenshots"))
    html_diagnostics_dir: Path = Field(default_factory=_from_paths("html_diagnostics"))
    cache_dir: Path = Field(default_factory=_from_paths("cache"))
    # Per-feature diagnostics. Same directory names as before, now under the resolved
    # outputs root instead of a path relative to the current working directory.
    live_test_output_dir: Path = Field(default_factory=_debug_dir("live_test"))
    easy_apply_debug_dir: Path = Field(default_factory=_debug_dir("easy_apply_debug"))
    contact_debug_dir: Path = Field(default_factory=_debug_dir("contact_debug"))
    profile_autofill_debug_dir: Path = Field(default_factory=_debug_dir("profile_autofill_debug"))
    form_navigation_debug_dir: Path = Field(default_factory=_debug_dir("form_navigation_debug"))
    screening_debug_dir: Path = Field(default_factory=_debug_dir("screening_debug"))
    resume_upload_debug_dir: Path = Field(default_factory=_debug_dir("resume_upload_debug"))
    workflow_debug_dir: Path = Field(default_factory=_debug_dir("workflow_debug"))
    employment_history_debug_dir: Path = Field(default_factory=_debug_dir("employment_history_debug"))
    find_matches_debug_dir: Path = Field(default_factory=_debug_dir("find_matches_debug"))
    find_matches_batch_debug_dir: Path = Field(default_factory=_debug_dir("find_matches_batch_debug"))
    dice_debug_dir: Path = Field(default_factory=_debug_dir("dice_debug"))
    portal_debug_dir: Path = Field(default_factory=_debug_dir("portal_debug"))
    performance_debug_dir: Path = Field(default_factory=_debug_dir("performance_debug"))
    reasoning_debug_dir: Path = Field(default_factory=_debug_dir("reasoning_debug"))
    resume_intelligence_debug_dir: Path = Field(default_factory=_debug_dir("resume_intelligence_debug"))
    form_learning_debug_dir: Path = Field(default_factory=_debug_dir("form_learning_debug"))
    generalized_form_debug_dir: Path = Field(default_factory=_debug_dir("generalized_form_debug"))
    apply_top_debug_dir: Path = Field(default_factory=_debug_dir("apply_top_debug"))
    radio_writer_debug_dir: Path = Field(default_factory=_debug_dir("radio_writer_debug"))
    api_base_url: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @model_validator(mode="after")
    def _absolutize_paths(self) -> "Settings":
        """Anchor every RELATIVE path setting to the resolved data root.

        The defaults above are already absolute, but a value supplied through ``.env`` or the
        environment may be relative (the historical ``.env`` ships
        ``APP_UPLOAD_DIR=storage/uploads``). A relative path resolves against the current
        working directory, which is precisely what breaks a packaged install launched from
        anywhere other than the source folder -- and what silently writes into Program Files.

        The anchor is the resolved data root, which in a source checkout IS the project root,
        so an existing relative value keeps resolving to exactly the same folder as before."""
        anchor = runtime_paths().data_root
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, Path) and not value.is_absolute():
                object.__setattr__(self, name, (anchor / value).resolve())
        return self

    def writable_directories(self) -> tuple[Path, ...]:
        """Every directory this configuration needs to write into, de-duplicated.

        Includes the PARENT of each portal session file, so a state file whose location was
        overridden to somewhere outside the standard layout still gets a usable directory."""
        candidates: list[Path] = [
            self.app_upload_dir,
            self.logs_dir,
            self.work_dir,
            self.screenshots_dir,
            self.html_diagnostics_dir,
            self.cache_dir,
            self.linkedin_storage_state.parent,
            self.dice_storage_state.parent,
            self.live_test_output_dir,
            self.live_test_output_dir / "screenshots",
            self.easy_apply_debug_dir,
            self.contact_debug_dir,
            self.profile_autofill_debug_dir,
            self.form_navigation_debug_dir,
            self.screening_debug_dir,
            self.resume_upload_debug_dir,
            self.workflow_debug_dir,
            self.employment_history_debug_dir,
            self.find_matches_debug_dir,
            self.find_matches_batch_debug_dir,
            self.dice_debug_dir,
            self.portal_debug_dir,
            self.performance_debug_dir,
            self.reasoning_debug_dir,
            self.resume_intelligence_debug_dir,
            self.form_learning_debug_dir,
            self.generalized_form_debug_dir,
            self.apply_top_debug_dir,
            self.radio_writer_debug_dir,
        ]
        unique: dict[str, Path] = {}
        for path in candidates:
            unique.setdefault(str(path), path)
        return tuple(unique.values())

    def ensure_directories(self) -> "Settings":
        """Create every writable location. Idempotent; safe to call more than once."""
        runtime_paths().ensure()
        for directory in self.writable_directories():
            directory.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings().ensure_directories()


def reset_settings_cache() -> None:
    """Drop the cached Settings so a changed environment is re-read. For tests and launchers."""
    get_settings.cache_clear()
