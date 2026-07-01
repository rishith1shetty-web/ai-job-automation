import json

from openai import OpenAI
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.config import Settings
from app.models import JobOpportunity, ResumeProfile


class MatchScorer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.openai_client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def score_jobs(self, resume: ResumeProfile, jobs: list[JobOpportunity]) -> list[JobOpportunity]:
        if not jobs:
            return []
        if self.openai_client:
            for job in jobs:
                try:
                    score, rationale = self._score_with_llm(resume, job)
                    job.match_score = score
                    job.match_rationale = rationale
                except Exception:
                    self._score_locally(resume, [job])
        else:
            self._score_locally(resume, jobs)
        return sorted(jobs, key=lambda job: (job.match_score, job.easy_apply), reverse=True)

    def _resume_text(self, resume: ResumeProfile) -> str:
        fields = [
            resume.raw_text,
            " ".join(resume.skills),
            " ".join(resume.job_titles),
            " ".join(resume.keywords),
        ]
        return "\n".join(fields)

    def _score_locally(self, resume: ResumeProfile, jobs: list[JobOpportunity]) -> None:
        corpus = [self._resume_text(resume)] + [f"{job.title}\n{job.description}" for job in jobs]
        vectors = TfidfVectorizer(stop_words="english", max_features=3000).fit_transform(corpus)
        scores = cosine_similarity(vectors[0:1], vectors[1:]).flatten()
        for job, score in zip(jobs, scores, strict=False):
            easy_apply_bonus = 0.05 if job.easy_apply else 0
            job.match_score = round(min(float(score) + easy_apply_bonus, 1.0) * 100, 2)
            job.match_rationale = "TF-IDF similarity against resume text plus Easy Apply prioritization."

    def _score_with_llm(self, resume: ResumeProfile, job: JobOpportunity) -> tuple[float, str]:
        prompt = f"""
Compare this candidate resume profile to the job. Score only fit; do not suggest resume edits.
Return strict JSON: {{"score": number from 0 to 100, "rationale": "one sentence"}}.

Resume skills: {resume.skills}
Resume titles: {resume.job_titles}
Resume keywords: {resume.keywords}
Resume text excerpt:
{resume.raw_text[:6000]}

Job:
Title: {job.title}
Company: {job.company}
Easy Apply: {job.easy_apply}
Description:
{job.description[:6000]}
"""
        response = self.openai_client.chat.completions.create(
            model=self.settings.openai_match_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        score = max(0, min(float(parsed.get("score", 0)), 100))
        if job.easy_apply:
            score = min(score + 3, 100)
        return round(score, 2), parsed.get("rationale", "LLM resume-to-job comparison.")
