AI Job Application Automation Platform – Project Summary
Goal

I am building a production-quality AI-powered job application automation platform inspired by ApplyCove.

This is NOT a simple LinkedIn bot.

The long-term goal is a modular multi-portal automation platform where every supported job portal behaves independently but follows the same architecture.

Initially the portals are:

LinkedIn
Dice

Later I plan to add:

Naukri
Indeed
ZipRecruiter
Monster
Glassdoor
others
Overall Philosophy

Every portal should behave like its own application while sharing a common backend architecture.

No portal should depend on another portal.

The user can work with exactly one active portal at a time.

Example:

User selects LinkedIn

↓

Connect LinkedIn

↓

Analyze Resume

↓

Find Matches

↓

Apply Jobs

↓

Disconnect LinkedIn

↓

Select Dice

↓

Connect Dice

↓

Analyze Resume

↓

Find Matches

↓

Apply Jobs

The portals must never share active sessions or accidentally mix data.

Current Backend

Python

FastAPI

Playwright

SQLAlchemy

PostgreSQL

Streamlit Dashboard

Modular services

Portal adapters

Background search batches

Resume parser

AI role detection

Ranking engine

Friction engine

Application tracking

Current Major Features
Resume Upload

Supports

PDF

DOCX

TXT

Resume is parsed into

Skills

Technologies

Education

Experience

Projects

Certifications

Keywords

Detected domains

Resume is stored in database.

AI Resume Intelligence

Current outputs

Primary Role

Suggested Roles

Confidence

Technologies

Skills

Generated Search Query

Domain Expertise

Resume Strengths

Missing Skills

Recommendations

AI Summary

Reason Matched

Search Keywords

Role Detection

Currently supports many roles including

Data Analyst

Business Intelligence Analyst

Reporting Analyst

Analytics Engineer

SQL Analyst

Data Scientist

Machine Learning Engineer

Backend Developer

Data Engineer

Frontend

Full Stack

DevOps

etc.

Primary role has highest confidence.

Other roles become secondary search roles.

Domain-specific roles should only appear when supported by strong evidence.

Examples

Healthcare roles ONLY when healthcare evidence exists.

Computer Vision ONLY when OpenCV / YOLO / CNN etc exist.

I recently tightened these rules because weak keywords were producing incorrect roles.

Find Matches Engine

This has undergone many improvements.

Current behavior

Multiple search variations

Independent searches per detected role

Long OR query is fallback only

Deduplicate

Rank

Return requested limit or maximum available

Background search batches

Current limits

Find 20

Find 50

Find 100

Custom limit

Debug logging includes

Search variations

Jobs per variation

Duplicates removed

Returned jobs

URLs

Background Search

Previously

Frontend waited for one HTTP request

Large searches timed out

Now

Search runs in background

Dashboard polls status

Shows

Running

Current variation

Jobs collected

Duplicates

Easy Apply count

Status

Refresh reconnects to active batch

Ranking Engine

Uses

Resume Match

Technical Match

Semantic Match

Industry Match

Complexity

Priority

Friction

Completion Likelihood

Estimated Steps

Screening Questions

Required Fields

Unsupported Fields

Jobs are ranked.

High-friction jobs are NOT removed.

Only ranked lower.

LinkedIn Portal

Current features

Login

Session persistence

Resume upload

Resume parsing

Find Matches

Background search

Application history

Apply Top

Batch Apply

Safe Dry Run

Live Apply

Navigation

Employment history

Dashboard

Job URLs

Application tracking

Current issue

Radio button automation still fails.

Radio Button Issue

This is the biggest remaining LinkedIn automation problem.

Symptoms

Radio visually becomes selected.

LinkedIn still says

"Please make a selection"

Examples

Authorization

Visa

Relocation

Education

Any repeated Yes/No groups

Likely cause

Visual selection occurs.

Underlying React state does not commit.

Need deeper Playwright/React investigation.

Nothing else in the application should be modified while fixing this.

Dice Portal

Recently added.

Architecture

Portal adapter

Separate portal state

Separate search

Separate application history

Separate batches

Separate jobs

Current features

Login

Resume analysis

Find Matches

Background search

Safe apply skeleton

Portal tracking

Dashboard integration

Separate storage

Current behavior

Find 20

Find 50

Find 100

Returns Dice jobs.

Current Apply

Safe only.

Unsupported forms become

needs_review

No live submission.

Portal Isolation

Very important.

LinkedIn and Dice must be completely isolated.

User cannot operate both simultaneously.

Workflow

Choose Active Portal

↓

Connect

↓

Analyze Resume

↓

Find Matches

↓

Apply

↓

Disconnect

↓

Switch Portal

Resume intelligence belongs to current portal.

Search batches belong to current portal.

Jobs belong to current portal.

Application history belongs to current portal.

Nothing should leak across portals.

Dashboard

Current features

Portal selector

Resume upload

Resume analysis

Candidate profile

Detected roles

Confidence

Technologies

Generated search query

Search controls

Find 20

Find 50

Find 100

Custom

Matched jobs

Application history

Profile

Employment History

AI Reasoning

Generalized Form Engine

Performance

Apply Automation

Current LinkedIn

Resume upload

Navigation

Multi-step forms

Screening detection

Application history

Batch apply

Needs review

Skip unsupported

Live Apply

Dry Run

Current Dice

Safe Apply only

No live submission

Logs everything

Current Long-Term Vision

I want this project to become an ApplyCove-level platform.

Not a script.

A real SaaS-quality architecture.

Future roadmap

Portal abstraction

LinkedIn

Dice

Naukri

Indeed

Monster

ZipRecruiter

Glassdoor

Resume Intelligence

Role Detection

AI Resume Analysis

Job Matching

Multi-search strategy

Semantic Matching

Application Automation

Generalized Form Engine

AI Form Learning

Saved Candidate Profile

Screening Knowledge Base

Duplicate Detection

Application Scheduler

Live Monitor

Pause / Resume

Application Analytics

Subscription-ready architecture

Admin dashboard

User dashboard

Multi-user support

Deployment-ready

Development Rules

Whenever modifying the project

Do NOT rewrite unrelated modules.

Do NOT break LinkedIn.

Do NOT break Dice.

Do NOT rewrite resume parser unless required.

Do NOT rewrite ranking unless required.

Do NOT rewrite friction engine.

Always preserve modular architecture.

Prefer improving existing modules over replacing them.

Maintain portal abstraction.

Every feature should work through the portal adapter interface.

Always add tests.

Always keep backward compatibility.

Always explain architectural decisions before large refactors.

Current Highest Priorities
Fix LinkedIn radio button issue (highest technical blocker).
Make Dice application automation reach LinkedIn feature parity (while keeping safe mode until fully reliable).
Improve role detection accuracy further so only evidence-backed roles appear.
Continue improving Find Matches quality and coverage.
Build a generalized automation engine that works across multiple job portals.

This summary should give Claude Code enough context to understand the project, its current architecture, completed work, outstanding issues, and the long-term vision without needing to reconstruct the entire history.