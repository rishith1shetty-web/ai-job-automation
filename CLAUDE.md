# CLAUDE.md
# AI Recruitment Platform
## Project Overview
This project is a full-stack AI Recruitment Platform inspired by ApplyCove.
The system automates the complete recruitment workflow while keeping every module independent and reusable.
Current supported portals:
- LinkedIn Easy Apply
- Dice
Future portals:
- Indeed
- ZipRecruiter
- Glassdoor
- Monster
- Naukri
- Other portals through the shared BasePortal architecture
The long-term goal is to support multiple recruitment websites through a single automation platform while sharing common infrastructure.
# Core Workflow
Resume Upload
↓
Resume Analysis
↓
Target Role Detection
↓
Job Search
↓
Job Ranking
↓
Application Automation
↓
Application History
↓
Dashboard
Every module should remain independent.
Never tightly couple portal implementations.
# Repository Structure
app/
Main FastAPI backend.
Contains:
- API endpoints
- Services
- Database
- Models
- Schemas
- Utilities
- Portal implementations
frontend/
React + TypeScript application.
Contains:
- Pages
- Components
- Hooks
- API layer
- Styling
tests/
Backend tests.
frontend/src/
Frontend source.
storage/
Resume storage
LinkedIn session
Temporary uploads
outputs/
Logs
Screenshots
Evidence
Debug HTML
Database
PostgreSQL
SQLAlchemy ORM
# Tech Stack
Backend
- FastAPI
- Python
- SQLAlchemy
- Playwright
- Pydantic
- PostgreSQL
Frontend
- React
- TypeScript
- React Query
- Vite
Automation
- Playwright
Database
- PostgreSQL
# Architecture
Every portal must inherit the shared BasePortal interface.
Never duplicate portal-independent logic.
Portal-specific behavior belongs only inside its own adapter.
Architecture:
BasePortal
↓
LinkedInPortal
DicePortal
Future portals
Shared modules must never depend on one portal.
# Resume Rules
The analyzed resume is the source of truth.
Never rewrite the resume.
Never modify uploaded resumes.
Never fabricate resume data.
Always preserve resume identity.
Resume matching must always use the currently analyzed resume.
Never silently switch resumes.
# LinkedIn Rules
LinkedIn implementation is independent.
Workflow:
Open job
↓
Open Easy Apply
↓
Fill forms
↓
Answer screening questions
↓
Resume handling
↓
Review
↓
Submit (Live only)
Dry Run must never submit.
Live mode submits only after every validation passes.
# Dice Rules
Dice implementation is independent.
Workflow:
Open application
↓
Fill profile
↓
Fill questions
↓
Review
↓
Submit
Dry Run:
Stop before final Submit.
Live:
Submit only after every existing validation succeeds.
Return APPLIED only after confirmation.
Never mark APPLIED without confirmation.
# Dry Run Rules
Dry Run must:
- Fill every field
- Upload resume
- Answer questions
- Reach submit-ready
- Never click final Submit
- Return DRY_RUN_READY
This behavior must never change.
# Live Apply Rules
Live mode may submit only after:
- All required fields complete
- Resume validated
- Screening questions completed
- No validation errors
- Submit button enabled
- Positive confirmation detected
Never fabricate success.
# Portal Independence
LinkedIn changes must not affect Dice.
Dice changes must not affect LinkedIn.
Every portal owns:
- Search
- Apply
- Resume handling
- Screening
- Status mapping
- Selectors
Never reuse portal-specific selectors.
# Backend Rules
Reuse existing services whenever possible.
Avoid duplicated code.
Prefer helper functions.
Keep functions small.
Keep modules focused.
Never introduce portal hacks.
Search existing implementations before writing new code.
# Frontend Rules
Use existing React architecture.
Use existing API hooks.
Do not bypass React Query.
Do not duplicate components.
Keep UI changes localized.
Never break existing layouts unnecessarily.
# Database Rules
Avoid schema changes unless required.
Never change existing relationships without approval.
Keep migrations backward compatible.
Application history is immutable.
# Status Rules
Supported statuses:
- MATCHED
- RUNNING
- COMPLETED
- APPLIED
- FAILED
- SKIPPED
- NEEDS_REVIEW
- DRY_RUN_READY
Never invent new statuses without approval.
# Automation Rules
Automation should always:
- Search
- Validate
- Fill
- Verify
- Continue
Never silently continue after validation failures.
Never fabricate successful completion.
# Error Handling
Always identify the root cause.
Do not patch symptoms.
Preserve evidence.
Capture screenshots when useful.
Save debug HTML when useful.
Return meaningful failure reasons.
# Logging
Logs should preserve:
- Current step
- Portal
- Status
- Reason
- Evidence path
Avoid noisy logs.
# Testing Rules
Before completing any implementation:
- Run focused tests.
- Run full backend suite.
- Run frontend tests if affected.
- Run production build if frontend changed.
- Do not modify unrelated tests.
- Never perform real submissions during automated tests.
# Development Rules
Before changing code:
- Understand the existing implementation.
- Search for similar logic.
- Reuse existing helpers.
- Avoid introducing duplicate workflows.
- Prefer extension over replacement.
- Keep changes as small as possible.
# Code Quality
Readable code over clever code.
Explicit logic over hidden behavior.
Strong typing where appropriate.
Avoid magic values.
Avoid unnecessary abstractions.
Keep portal behavior deterministic.
# Reporting
Every completed task should report:
- Root cause
- Files changed
- Implementation summary
- Test results
- Behavior changes
- Assumptions made
- Remaining limitations
# Safety Rules
Never fabricate successful application submissions.
Never mark APPLIED without confirmation.
Never silently ignore validation failures.
Never modify unrelated workflows.
Never change another portal while fixing one.
Keep changes isolated.
# Current Development Philosophy
Implement narrowly scoped changes.
Preserve existing behavior whenever possible.
Respect portal independence.
Prefer fixing root causes over workarounds.
Minimize regressions.
Every change should leave the project cleaner than before.