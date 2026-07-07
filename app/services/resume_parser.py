import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pdfplumber
from docx import Document
from fastapi import UploadFile
from openai import OpenAI

from app.config import Settings


COMMON_SKILLS = {
    "excel",
    "python",
    "sql",
    "postgresql",
    "mysql",
    "aws",
    "azure",
    "gcp",
    "docker",
    "kubernetes",
    "react",
    "typescript",
    "javascript",
    "fastapi",
    "django",
    "flask",
    "pandas",
    "spark",
    "machine learning",
    "data analysis",
    "project management",
    "product management",
    "salesforce",
    "tableau",
    "power bi",
    "linux",
    "git",
    "ci/cd",
    "terraform",
    "tensorflow",
    "pytorch",
    "scikit-learn",
    "numpy",
    "rest api",
    "rest apis",
    "graphql",
    "node.js",
    "java",
    "spring boot",
    "c#",
    ".net",
    "mongodb",
    "redis",
    "airflow",
    "snowflake",
    "dbt",
    "etl",
    "reporting",
    "statistics",
    "nlp",
    "computer vision",
    "biotechnology",
    "bioinformatics",
    "molecular biology",
    "cell biology",
    "genetics",
    "genetic engineering",
    "microbiology",
    "immunology",
    "pathology",
    "biostatistics",
    "pcr",
    "cell culture",
    "genomics",
    "proteomics",
    "drug discovery",
}

TECHNOLOGY_NAMES = {
    "aws": "AWS",
    "azure": "Azure",
    "ci/cd": "CI/CD",
    "c#": "C#",
    "dbt": "dbt",
    "docker": "Docker",
    "excel": "Excel",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "gcp": "GCP",
    "git": "Git",
    "graphql": "GraphQL",
    "java": "Java",
    "javascript": "JavaScript",
    "kubernetes": "Kubernetes",
    "linux": "Linux",
    "mongodb": "MongoDB",
    "mysql": "MySQL",
    "node.js": "Node.js",
    "numpy": "NumPy",
    "pandas": "Pandas",
    "postgresql": "PostgreSQL",
    "power bi": "Power BI",
    "pytorch": "PyTorch",
    "react": "React",
    "redis": "Redis",
    "rest api": "REST APIs",
    "rest apis": "REST APIs",
    "salesforce": "Salesforce",
    "scikit-learn": "scikit-learn",
    "snowflake": "Snowflake",
    "spark": "Spark",
    "spring boot": "Spring Boot",
    "sql": "SQL",
    "tableau": "Tableau",
    "tensorflow": "TensorFlow",
    "terraform": "Terraform",
    "typescript": "TypeScript",
}

ROLE_PROFILES = {
    "Biotechnologist": {
        "signals": {
            "biotechnologist": 12,
            "biotechnology": 7,
            "molecular biology": 6,
            "cell biology": 5,
            "genetics": 4,
            "genetic engineering": 5,
            "microbiology": 4,
            "immunology": 4,
            "pathology": 4,
            "cell culture": 5,
            "pcr": 4,
            "dna": 3,
            "rna": 3,
            "biochemical assays": 4,
            "biopharmaceutical": 5,
        },
        "searches": [
            "Biotechnologist",
            "Biotechnology Research Assistant",
            "Biotechnology Research Associate",
            "Laboratory Research Assistant",
        ],
        "domains": ["Biotechnology", "Molecular Biology", "Laboratory Research"],
    },
    "Bioinformatics Analyst": {
        "signals": {
            "bioinformatics": 8,
            "sequence analysis": 6,
            "genomics": 5,
            "proteomics": 5,
            "biostatistics": 5,
            "biological database": 5,
            "gene expression": 5,
            "computational drug": 4,
            "drug discovery": 4,
            "molecular modeling": 4,
        },
        "searches": [
            "Bioinformatics Analyst",
            "Bioinformatics Research Assistant",
            "Computational Biology Intern",
            "Genomics Analyst",
        ],
        "domains": ["Bioinformatics", "Computational Biology", "Genomics"],
    },
    "Backend Developer": {
        "signals": {
            "python": 3,
            "fastapi": 4,
            "django": 3,
            "flask": 3,
            "sql": 2,
            "postgresql": 2,
            "mysql": 2,
            "rest api": 3,
            "rest apis": 3,
            "java": 2,
            "spring boot": 3,
            "node.js": 2,
            "mongodb": 1,
            "redis": 1,
        },
        "searches": ["Backend Developer", "Python Developer", "FastAPI Developer", "Software Engineer"],
        "domains": ["Backend Engineering", "API Development"],
    },
    "Data Analyst": {
        "signals": {
            "excel": 2,
            "sql": 2,
            "power bi": 3,
            "tableau": 3,
            "data analysis": 3,
            "data analytics": 3,
            "dashboard": 2,
            "dashboards": 2,
            "reporting": 2,
            "statistics": 2,
            "pandas": 2,
        },
        "titles": ["data analyst", "reporting analyst"],
        "searches": ["Data Analyst"],
        "domains": ["Data Analytics", "Business Intelligence"],
    },
    "Healthcare Data Analyst": {
        "signals": {
            "healthcare": 7,
            "clinical": 6,
            "patient": 4,
            "ehr": 5,
            "emr": 5,
            "hipaa": 4,
            "medical": 3,
            "hospital": 3,
            "claims": 3,
            "sql": 2,
            "excel": 2,
            "power bi": 2,
            "tableau": 2,
            "reporting": 2,
            "dashboard": 2,
        },
        "titles": ["healthcare data analyst", "clinical data analyst"],
        "searches": ["Healthcare Data Analyst"],
        "domains": ["Healthcare Analytics", "Clinical Analytics"],
    },
    "Clinical Data Analyst": {
        "signals": {
            "clinical data": 7,
            "clinical": 5,
            "healthcare": 4,
            "patient": 4,
            "clinical trial": 4,
            "ehr": 4,
            "emr": 4,
            "sql": 2,
            "reporting": 2,
            "quality metrics": 3,
        },
        "titles": ["clinical data analyst"],
        "searches": ["Clinical Data Analyst"],
        "domains": ["Clinical Analytics", "Healthcare Analytics"],
    },
    "Healthcare BI Analyst": {
        "signals": {
            "healthcare": 5,
            "clinical": 3,
            "power bi": 4,
            "tableau": 4,
            "dashboard": 3,
            "dashboards": 3,
            "reporting": 3,
            "business intelligence": 3,
            "sql": 2,
        },
        "titles": ["healthcare bi analyst", "healthcare business intelligence analyst"],
        "searches": ["Healthcare BI Analyst"],
        "domains": ["Healthcare Analytics", "Business Intelligence"],
    },
    "Reporting Analyst": {
        "signals": {
            "reporting": 5,
            "reports": 3,
            "dashboard": 3,
            "dashboards": 3,
            "excel": 2,
            "sql": 2,
            "power bi": 3,
            "tableau": 3,
            "metrics": 2,
            "kpi": 2,
        },
        "titles": ["reporting analyst"],
        "searches": ["Reporting Analyst"],
        "domains": ["Reporting", "Business Intelligence"],
    },
    "SQL Analyst": {
        "signals": {
            "sql analyst": 7,
            "sql": 5,
            "database": 3,
            "databases": 3,
            "queries": 3,
            "reporting": 2,
            "data analysis": 2,
            "excel": 1,
        },
        "titles": ["sql analyst"],
        "searches": ["SQL Analyst"],
        "domains": ["Data Analytics", "SQL Reporting"],
    },
    "Data Scientist": {
        "signals": {
            "data science": 6,
            "machine learning": 5,
            "statistical modeling": 5,
            "predictive modeling": 5,
            "forecasting": 4,
            "python": 2,
            "scikit-learn": 3,
            "pandas": 2,
            "numpy": 2,
            "shap": 3,
            "bayesian optimization": 3,
            "mlflow": 2,
            "nlp": 3,
            "natural language processing": 3,
            "computer vision": 3,
            "deep learning": 3,
        },
        "titles": [
            "data scientist",
            "data science analyst",
            "analyst - digital and data science",
            "analyst digital and data science",
        ],
        "searches": ["Data Scientist"],
        "domains": ["Data Science", "Predictive Analytics"],
    },
    "Machine Learning Engineer": {
        "signals": {
            "python": 2,
            "machine learning": 6,
            "tensorflow": 5,
            "pytorch": 5,
            "scikit-learn": 4,
            "numpy": 2,
            "pandas": 1,
            "nlp": 3,
            "computer vision": 3,
            "ml pipeline": 4,
            "mlflow": 4,
            "model deployment": 4,
        },
        "titles": ["machine learning engineer", "ml engineer", "ai engineer"],
        "searches": ["Machine Learning Engineer"],
        "domains": ["Artificial Intelligence", "Machine Learning"],
    },
    "AI Engineer": {
        "signals": {
            "ai engineer": 7,
            "artificial intelligence": 5,
            "generative ai": 5,
            "llm": 4,
            "rag": 4,
            "machine learning": 4,
            "python": 2,
            "model deployment": 3,
            "deep learning": 4,
        },
        "titles": ["ai engineer", "artificial intelligence engineer", "ai/ml engineer"],
        "searches": ["AI Engineer"],
        "domains": ["Artificial Intelligence", "Machine Learning"],
    },
    "NLP Engineer": {
        "signals": {
            "nlp": 7,
            "natural language processing": 7,
            "llm": 4,
            "transformer": 4,
            "bert": 4,
            "spacy": 3,
            "text classification": 4,
            "python": 2,
            "machine learning": 3,
        },
        "titles": ["nlp engineer", "natural language processing engineer"],
        "searches": ["NLP Engineer"],
        "domains": ["Natural Language Processing", "Artificial Intelligence"],
    },
    "Computer Vision Engineer": {
        "signals": {
            "computer vision": 7,
            "opencv": 4,
            "image processing": 4,
            "object detection": 4,
            "cnn": 3,
            "yolo": 3,
            "pytorch": 3,
            "tensorflow": 3,
            "python": 2,
        },
        "titles": ["computer vision engineer", "cv engineer"],
        "searches": ["Computer Vision Engineer"],
        "domains": ["Computer Vision", "Artificial Intelligence"],
    },
    "Analytics Engineer": {
        "signals": {
            "analytics": 4,
            "sql": 4,
            "data pipeline": 6,
            "data pipelines": 6,
            "data modeling": 4,
            "spark": 5,
            "dbt": 5,
            "etl": 4,
            "power bi": 2,
            "python": 2,
        },
        "titles": ["analytics engineer"],
        "searches": ["Analytics Engineer"],
        "domains": ["Analytics Engineering", "Data Platforms"],
    },
    "Business Intelligence Analyst": {
        "signals": {
            "business intelligence": 4,
            "power bi": 3,
            "tableau": 3,
            "dashboard": 2,
            "dashboards": 2,
            "reporting": 2,
            "excel": 1,
            "sql": 1,
        },
        "titles": ["business intelligence analyst", "bi analyst"],
        "searches": ["Business Intelligence Analyst"],
        "domains": ["Business Intelligence", "Reporting"],
    },
    "Data Engineer": {
        "signals": {
            "python": 2,
            "sql": 3,
            "spark": 4,
            "airflow": 4,
            "snowflake": 3,
            "dbt": 3,
            "etl": 4,
            "aws": 1,
            "azure": 1,
            "gcp": 1,
        },
        "searches": ["Data Engineer", "ETL Developer", "Analytics Engineer", "Cloud Data Engineer"],
        "domains": ["Data Engineering", "Cloud Data Platforms"],
    },
    "Frontend Developer": {
        "signals": {
            "react": 4,
            "javascript": 3,
            "typescript": 3,
            "frontend developer": 4,
        },
        "searches": ["Frontend Developer", "React Developer", "UI Developer", "Software Engineer"],
        "domains": ["Frontend Engineering", "Web Development"],
    },
    "DevOps Engineer": {
        "signals": {
            "docker": 3,
            "kubernetes": 4,
            "terraform": 4,
            "ci/cd": 3,
            "linux": 2,
            "aws": 2,
            "azure": 2,
            "gcp": 2,
            "devops engineer": 4,
        },
        "searches": ["DevOps Engineer", "Cloud Engineer", "Site Reliability Engineer", "Platform Engineer"],
        "domains": ["DevOps", "Cloud Infrastructure"],
    },
    "Full Stack Developer": {
        "signals": {
            "react": 3,
            "javascript": 2,
            "typescript": 2,
            "python": 2,
            "fastapi": 2,
            "django": 2,
            "node.js": 2,
            "sql": 1,
            "full stack developer": 4,
        },
        "searches": ["Full Stack Developer", "Software Engineer", "Web Developer", "Application Developer"],
        "domains": ["Full Stack Engineering", "Web Development"],
    },
    "Project Manager": {
        "signals": {
            "project management": 5,
            "project manager": 5,
        },
        "searches": ["Project Manager", "Technical Project Manager", "Program Coordinator"],
        "domains": ["Project Management"],
    },
    "Product Manager": {
        "signals": {
            "product management": 5,
            "product manager": 5,
        },
        "searches": ["Product Manager", "Technical Product Manager", "Product Owner"],
        "domains": ["Product Management"],
    },
}


class ResumeParser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.openai_client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def save_upload(self, upload: UploadFile) -> Path:
        suffix = Path(upload.filename or "resume").suffix.lower()
        safe_name = f"{uuid4().hex}{suffix}"
        destination = self.settings.app_upload_dir / safe_name
        with destination.open("wb") as output:
            shutil.copyfileobj(upload.file, output)
        return destination

    def extract_text(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            with pdfplumber.open(path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        if suffix in {".docx", ".doc"}:
            document = Document(path)
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        return path.read_text(encoding="utf-8", errors="ignore")

    def extract_hyperlinks(self, path: Path) -> list[str]:
        suffix = path.suffix.lower()
        links: list[str] = []
        if suffix == ".pdf":
            try:
                with pdfplumber.open(path) as pdf:
                    for page in pdf.pages:
                        for link in getattr(page, "hyperlinks", []) or []:
                            uri = link.get("uri")
                            if uri:
                                links.append(uri)
            except Exception:
                return []
        elif suffix in {".docx", ".doc"}:
            try:
                document = Document(path)
                for relationship in document.part.rels.values():
                    if "hyperlink" in relationship.reltype and relationship.target_ref:
                        links.append(relationship.target_ref)
            except Exception:
                return []
        return list(dict.fromkeys(link.strip() for link in links if link and link.strip()))

    def parse(self, raw_text: str) -> dict:
        if self.openai_client:
            try:
                profile = self._parse_with_llm(raw_text)
            except Exception:
                profile = self._parse_locally(raw_text)
        else:
            profile = self._parse_locally(raw_text)
        return self._add_targeting(profile, raw_text)

    def extract_employment_history(self, raw_text: str, fallback_location: str | None = None) -> list[dict]:
        section_lines = self._employment_section_lines(raw_text)
        chunks = self._employment_chunks(section_lines)
        records: list[dict] = []
        for index, chunk in enumerate(chunks):
            record = self._parse_employment_chunk(chunk, index, fallback_location)
            if record and (record.get("company_name") or record.get("job_title")):
                records.append(record)
        return records

    def extract_candidate_profile(
        self,
        raw_text: str,
        hyperlinks: list[str] | None = None,
    ) -> dict:
        hyperlinks = hyperlinks or []
        fields: dict[str, dict] = {}
        professional_links: dict[str, dict] = {}

        def add_field(name: str, value: str | None, source: str, confidence: float) -> None:
            value = (value or "").strip()
            if not value:
                return
            existing = fields.get(name)
            if existing and existing["confidence"] > confidence:
                return
            fields[name] = {
                "value": value,
                "source": source,
                "confidence": confidence,
            }

        email_match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw_text)
        add_field("email", email_match.group(0) if email_match else None, "Resume Visible Text", 96)

        phone_match = re.search(
            r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,5}\d{3,4}(?!\d)",
            raw_text,
        )
        if phone_match:
            phone = phone_match.group(0).strip()
            country_code_match = re.match(r"^\+(\d{1,3})", phone)
            add_field("mobile_phone", phone, "Resume Visible Text", 88)
            if country_code_match:
                add_field("phone_country_code", f"+{country_code_match.group(1)}", "Resume Visible Text", 92)
            elif self._looks_indian_phone(phone):
                add_field("phone_country_code", "+91", "Resume Visible Text", 64)
            else:
                add_field("phone_country_code", "+1", "Resume Visible Text", 45)

        visible_urls = re.findall(r"https?://[^\s)>\]]+|(?:www\.)[^\s)>\]]+", raw_text, re.I)
        all_links = [(link, "Embedded Hyperlink", 98) for link in hyperlinks] + [
            (link, "Resume Visible Text", 88) for link in visible_urls
        ]
        for link, source, confidence in all_links:
            normalized = self._normalize_url(link)
            if not normalized:
                continue
            category = self._professional_link_category(normalized)
            if category:
                existing = professional_links.get(category)
                if not existing or existing["confidence"] <= confidence:
                    professional_links[category] = {
                        "value": normalized,
                        "source": source,
                        "confidence": confidence,
                    }
                if category == "linkedin":
                    add_field("linkedin_url", normalized, source, confidence)
                elif category == "github":
                    add_field("github_url", normalized, source, confidence)
                elif category in {"portfolio", "personal_website"}:
                    add_field("portfolio_url", normalized, source, confidence)

        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        name = self._extract_candidate_name(lines)
        if name:
            parts = name.split()
            add_field("first_name", parts[0], "Resume Visible Text", 76)
            if len(parts) > 1:
                add_field("last_name", parts[-1], "Resume Visible Text", 76)

        location = self._extract_location(lines)
        add_field("current_location", location, "Resume Visible Text", 62)
        add_field("address", location, "Resume Visible Text", 50)

        job_titles = self._extract_job_titles(raw_text)
        if job_titles:
            add_field("current_job_title", job_titles[0], "Resume Visible Text", 70)
            add_field("professional_headline", job_titles[0], "Resume Visible Text", 65)

        companies = self._extract_companies(raw_text)
        if companies:
            add_field("current_company", companies[0], "Resume Visible Text", 58)

        certifications = self._extract_certifications(raw_text)
        payload = {
            "fields": fields,
            "professional_links": professional_links,
            "previous_companies": companies[1:],
            "certifications": certifications,
        }
        return payload

    def _normalize_url(self, link: str) -> str | None:
        link = link.strip().rstrip(".,;")
        if link.startswith("mailto:"):
            return None
        if link.startswith("www."):
            link = f"https://{link}"
        if not link.startswith(("http://", "https://")):
            return None
        return link

    def _professional_link_category(self, link: str) -> str | None:
        host = (urlparse(link).hostname or "").lower()
        if "linkedin.com" in host:
            return "linkedin"
        if "github.com" in host:
            return "github"
        if "kaggle.com" in host:
            return "kaggle"
        if "medium.com" in host:
            return "medium"
        if "stackoverflow.com" in host:
            return "stackoverflow"
        if host and not any(blocked in host for blocked in ("google.com", "microsoft.com", "adobe.com")):
            return "portfolio"
        return None

    def _looks_indian_phone(self, phone: str) -> bool:
        digits = re.sub(r"\D", "", phone)
        return len(digits) == 10 and digits[:1] in {"6", "7", "8", "9"}

    def _extract_candidate_name(self, lines: list[str]) -> str | None:
        for line in lines[:8]:
            clean = re.sub(r"[^A-Za-z\s.'-]", " ", line).strip()
            words = [word for word in clean.split() if len(word) > 1]
            if 2 <= len(words) <= 4 and not any(
                token.lower() in {"resume", "curriculum", "email", "phone", "linkedin", "github"}
                for token in words
            ):
                return " ".join(words)
        return None

    def _extract_location(self, lines: list[str]) -> str | None:
        location_words = re.compile(r"\b(?:india|usa|united states|remote|maharashtra|karnataka|texas|california|new york|pune|mumbai|bengaluru|bangalore|hyderabad|delhi|chennai|austin|seattle|san francisco)\b", re.I)
        for line in lines[:15]:
            if "@" in line or "linkedin" in line.lower() or "github" in line.lower():
                continue
            if location_words.search(line):
                return re.sub(r"\s+", " ", line).strip(" |,")
        return None

    def _extract_companies(self, text: str) -> list[str]:
        companies: list[str] = []
        for match in re.finditer(r"\b(?:at|@)\s+([A-Z][A-Za-z0-9&.,\-\s]{2,60})", text):
            company = re.split(r"\n| {2,}|\||,", match.group(1))[0].strip()
            if company:
                companies.append(company)
        return list(dict.fromkeys(companies))[:10]

    def _extract_certifications(self, text: str) -> list[str]:
        section = self._extract_section(text, "certifications", ["experience", "education", "skills", "projects"])
        if not section:
            return []
        items = re.split(r"[;\n•]+", section)
        return [re.sub(r"\s+", " ", item).strip(" -") for item in items if item.strip()][:20]

    def _parse_with_llm(self, raw_text: str) -> dict:
        prompt = (
            "Extract a structured candidate profile from this resume. "
            "Return strict JSON with keys: skills, experience, education, job_titles, keywords, technologies, "
            "domain_expertise. "
            "experience and education should be arrays of objects. Do not rewrite or improve the resume.\n\n"
            f"{raw_text[:12000]}"
        )
        response = self.openai_client.chat.completions.create(
            model=self.settings.openai_match_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        return self._normalize(parsed, raw_text)

    def _parse_locally(self, raw_text: str) -> dict:
        lower = raw_text.lower()
        skills = sorted(skill for skill in COMMON_SKILLS if self._contains_term(lower, skill))
        job_titles = self._extract_job_titles(raw_text)
        education = self._extract_section(raw_text, "education", ["experience", "skills", "projects"])
        experience = self._extract_section(raw_text, "experience", ["education", "skills", "projects"])
        keywords = self._extract_keywords(raw_text, skills + job_titles)
        return {
            "skills": skills,
            "experience": [{"text": experience}] if experience else [],
            "education": [{"text": education}] if education else [],
            "job_titles": job_titles,
            "keywords": keywords,
            "technologies": self._extract_technologies(raw_text),
            "domain_expertise": [],
        }

    def _employment_section_lines(self, raw_text: str) -> list[str]:
        start_headings = {
            "experience",
            "professional experience",
            "work experience",
            "relevant experience",
            "employment",
            "internships",
            "internship experience",
        }
        stop_headings = {
            "projects",
            "education",
            "certifications",
            "skills",
            "technical skills",
            "achievements",
            "awards",
            "publications",
            "summary",
            "objective",
        }
        lines = [line.strip() for line in raw_text.splitlines()]
        in_section = False
        collected: list[str] = []
        for line in lines:
            clean = re.sub(r"[:\s]+$", "", line.strip()).lower()
            if not clean:
                if in_section:
                    collected.append("")
                continue
            if clean in start_headings:
                in_section = True
                continue
            if in_section and clean in stop_headings:
                break
            if in_section:
                collected.append(line.strip())
        if collected:
            return collected
        fallback = self._extract_section(raw_text, "experience", ["education", "skills", "projects", "certifications"])
        return [line.strip() for line in re.split(r"\s{2,}|\n", fallback) if line.strip()]

    def _employment_chunks(self, lines: list[str]) -> list[list[str]]:
        chunks: list[list[str]] = []
        current: list[str] = []
        pending_headers: list[str] = []
        for line in lines:
            if not line:
                continue
            has_dates = self._date_range_match(line) is not None
            if has_dates:
                if current:
                    chunks.append(current)
                clean_pending = [item for item in pending_headers[-2:] if self._looks_like_entry_header(item)]
                current = clean_pending + [line]
                pending_headers = []
                continue
            if current:
                next_header = not self._is_bullet(line) and self._looks_like_entry_header(line)
                if next_header:
                    chunks.append(current)
                    current = []
                    pending_headers = [line]
                else:
                    current.append(line)
            else:
                pending_headers.append(line)
                pending_headers = pending_headers[-3:]
        if current:
            chunks.append(current)
        return chunks

    def _parse_employment_chunk(
        self,
        chunk: list[str],
        index: int,
        fallback_location: str | None,
    ) -> dict | None:
        combined = "\n".join(chunk)
        date_match = self._date_range_match(combined)
        if not date_match:
            return None
        start_month, start_year, end_month, end_year, current = self._parse_date_range(date_match.group(0))
        header_text = combined[: date_match.start()].strip(" |,-\n") or chunk[0]
        bullet_lines = [self._clean_bullet(line) for line in chunk if self._is_bullet(line)]
        description = "\n".join(line for line in bullet_lines if line)
        job_title, company_name, location = self._employment_header_parts(header_text, chunk)
        city, state, country = self._split_location(location or fallback_location)
        skills = [skill for skill in COMMON_SKILLS if self._contains_term(combined, skill)]
        technologies = self._extract_technologies(combined)
        duration = self._employment_duration_months(start_month, start_year, end_month, end_year, current)
        confidence = 86 if job_title and company_name and start_year else 70 if start_year else 55
        return {
            "display_order": index,
            "job_title": job_title,
            "company_name": company_name,
            "city": city,
            "state": state,
            "country": country,
            "employment_type": self._employment_type(job_title, combined),
            "start_month": start_month,
            "start_year": start_year,
            "end_month": end_month,
            "end_year": end_year,
            "currently_work_here": current,
            "description": description or self._clean_bullet(" ".join(chunk[1:]))[:1000],
            "skills_used": list(dict.fromkeys(skills))[:30],
            "technologies_used": technologies,
            "duration_months": duration,
            "confidence": confidence,
            "source": "Resume Experience",
        }

    def _date_range_match(self, text: str):
        months = r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
        endpoint = rf"(?:(?:{months})\s+)?(?:19|20)\d{{2}}"
        end = rf"(?:(?:{months})\s+)?(?:(?:19|20)\d{{2}}|Present|Current|Till Date|Currently Working)"
        pattern = rf"\b{endpoint}\s*(?:(?:-|–|—|to)\s*|\s+){end}\b"
        return re.search(pattern, text, re.I)

    def _parse_date_range(self, text: str) -> tuple[str | None, int | None, str | None, int | None, bool]:
        parts = re.split(r"\s*(?:-|–|—|to)\s*", text.strip(), maxsplit=1, flags=re.I)
        if len(parts) == 1:
            months = r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
            match = re.match(
                rf"(?P<start>(?:(?:{months})\s+)?(?:19|20)\d{{2}})\s+(?P<end>(?:(?:{months})\s+)?(?:(?:19|20)\d{{2}}|Present|Current|Till Date|Currently Working))",
                text.strip(),
                re.I,
            )
            if match:
                parts = [match.group("start"), match.group("end")]
        start = self._parse_date_endpoint(parts[0] if parts else "")
        end_raw = parts[1] if len(parts) > 1 else ""
        current = bool(re.search(r"present|current|till date|currently working", end_raw, re.I))
        end = (None, None) if current else self._parse_date_endpoint(end_raw)
        return start[0], start[1], end[0], end[1], current

    def _parse_date_endpoint(self, text: str) -> tuple[str | None, int | None]:
        month_map = {
            "jan": "January",
            "feb": "February",
            "mar": "March",
            "apr": "April",
            "may": "May",
            "jun": "June",
            "jul": "July",
            "aug": "August",
            "sep": "September",
            "oct": "October",
            "nov": "November",
            "dec": "December",
        }
        year_match = re.search(r"(19|20)\d{2}", text)
        if not year_match:
            return None, None
        month_match = re.search(r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", text, re.I)
        month = month_map.get(month_match.group(0).lower()[:3]) if month_match else None
        return month, int(year_match.group(0))

    def _employment_header_parts(self, header_text: str, chunk: list[str]) -> tuple[str | None, str | None, str | None]:
        clean_header = re.sub(r"\s+", " ", header_text).strip(" |,-")
        parts = [part.strip() for part in re.split(r"\s*\|\s*|\s+•\s+|\t+", clean_header) if part.strip()]
        if len(parts) >= 2:
            if self._looks_like_job_title(parts[0]):
                title = parts[0]
                company = parts[1]
            elif self._looks_like_job_title(parts[1]):
                company = parts[0]
                title = parts[1]
            else:
                title = parts[0]
                company = parts[1]
            location = ", ".join(parts[2:]) or None
            if location is None and "," in company:
                company_parts = [part.strip() for part in company.split(",") if part.strip()]
                company = company_parts[0]
                location = ", ".join(company_parts[1:]) or None
            return title, company, location

        header_lines = [line.strip(" |,-") for line in chunk[:3] if line.strip() and not self._is_bullet(line)]
        header_lines = [re.sub(r"\s+", " ", line) for line in header_lines]
        date_line_index = next((i for i, line in enumerate(header_lines) if self._date_range_match(line)), len(header_lines))
        candidates = header_lines[:date_line_index]
        if len(candidates) >= 2:
            first, second = candidates[-2], candidates[-1]
            if self._looks_like_job_title(second):
                return second, first, None
            return first, second, None
        if candidates:
            return candidates[0], None, None
        return None, None, None

    def _looks_like_job_title(self, text: str) -> bool:
        return bool(
            re.search(
                r"\b(analyst|engineer|developer|scientist|intern|manager|consultant|specialist|associate|lead|architect)\b",
                text,
                re.I,
            )
        )

    def _looks_like_entry_header(self, text: str) -> bool:
        if self._date_range_match(text):
            return True
        if "|" in text and len(text.split()) <= 18:
            return True
        return self._looks_like_job_title(text) and len(text.split()) <= 10

    def _split_location(self, location: str | None) -> tuple[str | None, str | None, str | None]:
        if not location:
            return None, None, None
        parts = [part.strip() for part in re.split(r",|\|", location) if part.strip()]
        if not parts:
            return None, None, None
        city = parts[0]
        state = parts[1] if len(parts) > 1 else None
        country = parts[2] if len(parts) > 2 else None
        if len(parts) == 2 and re.search(r"india|united states|usa|canada|uk|germany|europe", parts[1], re.I):
            state = None
            country = parts[1]
        return city, state, country

    def _employment_type(self, title: str | None, text: str) -> str:
        source = f"{title or ''} {text}".lower()
        if "intern" in source:
            return "Internship"
        if "contract" in source:
            return "Contract"
        if "freelance" in source:
            return "Freelance"
        if "part time" in source or "part-time" in source:
            return "Part Time"
        return "Full Time"

    def _employment_duration_months(
        self,
        start_month: str | None,
        start_year: int | None,
        end_month: str | None,
        end_year: int | None,
        current: bool,
    ) -> int | None:
        if not start_year:
            return None
        month_numbers = {
            "January": 1,
            "February": 2,
            "March": 3,
            "April": 4,
            "May": 5,
            "June": 6,
            "July": 7,
            "August": 8,
            "September": 9,
            "October": 10,
            "November": 11,
            "December": 12,
        }
        start_index = start_year * 12 + month_numbers.get(start_month or "January", 1)
        if current:
            now = datetime.utcnow()
            end_index = now.year * 12 + now.month
        elif end_year:
            end_index = end_year * 12 + month_numbers.get(end_month or "December", 12)
        else:
            return None
        return max(0, end_index - start_index + 1)

    def _is_bullet(self, line: str) -> bool:
        return bool(re.match(r"^\s*(?:[-*•]|â€¢|\u2022)\s+", line))

    def _clean_bullet(self, line: str) -> str:
        return re.sub(r"^\s*(?:[-*•]|â€¢|\u2022)\s*", "", line).strip()

    def _normalize(self, parsed: dict, raw_text: str) -> dict:
        return {
            "skills": list(dict.fromkeys(parsed.get("skills") or []))[:80],
            "experience": parsed.get("experience") or [],
            "education": parsed.get("education") or [],
            "job_titles": list(dict.fromkeys(parsed.get("job_titles") or self._extract_job_titles(raw_text)))[:30],
            "keywords": list(dict.fromkeys(parsed.get("keywords") or self._extract_keywords(raw_text, [])))[:120],
            "technologies": list(
                dict.fromkeys(parsed.get("technologies") or self._extract_technologies(raw_text))
            )[:80],
            "domain_expertise": list(dict.fromkeys(parsed.get("domain_expertise") or []))[:30],
        }

    def _add_targeting(self, profile: dict, raw_text: str) -> dict:
        source_text = self._profile_source_text(profile, raw_text)
        scored_roles: list[dict] = []
        for role, definition in ROLE_PROFILES.items():
            score, evidence = self._score_role(definition, source_text)
            if role in {"Healthcare Data Analyst", "Clinical Data Analyst", "Healthcare BI Analyst"}:
                if not self._has_healthcare_data_analyst_evidence(source_text):
                    score = 0.0
                    evidence = ["healthcare role blocked: no explicit healthcare analytics evidence"]
            if role == "Computer Vision Engineer" and not self._has_computer_vision_evidence(source_text):
                score = 0.0
                evidence = ["computer vision role blocked: no explicit computer vision evidence"]
            elif role == "Computer Vision Engineer" and not self._has_computer_vision_role_evidence(source_text):
                score *= 0.55
                evidence.append("computer vision demoted: evidence is project/skill based, not a role title or work experience")
            if role == "Data Analyst" and self._has_data_analyst_core_evidence(source_text):
                score += 10
                evidence.append("core data analyst evidence")
            if role == "Data Analyst" and self._has_healthcare_data_analyst_evidence(source_text):
                score += 18
                evidence.append("healthcare analytics supports broad data analyst primary")
            if role == "Backend Developer" and not self._has_backend_evidence(source_text):
                score *= 0.35
                if score:
                    evidence.append("backend role demoted: no API/server framework evidence")
            scored_roles.append({"role": role, "score": score, "evidence": evidence})

        scored_roles.sort(key=lambda item: item["score"], reverse=True)
        positive_roles = [item for item in scored_roles if item["score"] > 0]
        if not positive_roles:
            fallback = self._fallback_role(profile)
            suggestions = [
                {
                    "role": fallback,
                    "confidence": 42.0,
                    "role_category": "primary_role",
                    "evidence": ["resume title"],
                }
            ]
        else:
            top_roles = positive_roles[:8]
            best_score = top_roles[0]["score"]
            second_score = top_roles[1]["score"] if len(top_roles) > 1 else 0
            ambiguity = second_score / best_score if best_score else 0
            primary_cap = 92.0 if ambiguity < 0.62 else 89.0 if ambiguity < 0.82 else 86.0
            primary_confidence = min(primary_cap, 52.0 + best_score * 0.72)
            suggestions = []
            previous_confidence = 101.0
            for index, item in enumerate(top_roles):
                relative = item["score"] / best_score if best_score else 0
                confidence = primary_confidence if index == 0 else 38.0 + relative * (primary_confidence - 38.0)
                confidence = min(confidence, previous_confidence - 1.0)
                confidence = max(30.0, confidence)
                previous_confidence = confidence
                role_category = (
                    "primary_role"
                    if index == 0
                    else "strong_fit_roles"
                    if confidence >= 70
                    else "related_roles"
                    if confidence >= 60
                    else "stretch_roles"
                )
                suggestions.append(
                    {
                        "role": item["role"],
                        "confidence": round(confidence, 1),
                        "role_category": role_category,
                        "evidence": item["evidence"][:8],
                    }
                )

        detected_role = suggestions[0]["role"]
        confidence = suggestions[0]["confidence"]
        generated = []
        domain_specific_roles = {
            "Healthcare Data Analyst",
            "Clinical Data Analyst",
            "Healthcare BI Analyst",
            "Computer Vision Engineer",
            "NLP Engineer",
        }
        for item in suggestions:
            if item.get("role_category") != "stretch_roles":
                generated.append(item["role"])
            elif item["role"] in domain_specific_roles and item.get("confidence", 0) >= 58:
                generated.append(item["role"])
            elif item["role"] not in domain_specific_roles and item.get("confidence", 0) >= 50:
                generated.append(item["role"])
        if detected_role not in generated:
            generated.insert(0, detected_role)
        generated_query = " OR ".join(
            "BI Analyst" if role == "Business Intelligence Analyst" else role for role in generated
        )
        definition = ROLE_PROFILES.get(detected_role)
        technologies = profile.get("technologies") or self._extract_technologies(raw_text)
        domains = list(profile.get("domain_expertise") or [])
        if definition:
            domains.extend(definition["domains"])

        profile.update(
            {
                "technologies": technologies,
                "domain_expertise": list(dict.fromkeys(domains))[:10],
                "detected_role": detected_role,
                "role_suggestions": suggestions,
                "generated_search_keywords": generated,
                "generated_search_query": generated_query,
                "role_confidence": round(confidence, 1),
                "matched_role_signals": suggestions[0].get("evidence", []),
                "role_groups": {
                    "primary_role": [item["role"] for item in suggestions if item.get("role_category") == "primary_role"],
                    "strong_fit_roles": [item["role"] for item in suggestions if item.get("role_category") == "strong_fit_roles"],
                    "related_roles": [item["role"] for item in suggestions if item.get("role_category") == "related_roles"],
                    "stretch_roles": [item["role"] for item in suggestions if item.get("role_category") == "stretch_roles"],
                },
            }
        )
        return profile

    def _profile_source_text(self, profile: dict, raw_text: str) -> dict[str, str]:
        def flatten(items: list) -> str:
            values: list[str] = []
            for item in items:
                if isinstance(item, dict):
                    values.extend(str(value) for value in item.values())
                else:
                    values.append(str(item))
            return " ".join(values).lower()

        title_items = [str(item).lower() for item in profile.get("job_titles") or []]
        return {
            "titles": " ".join(title_items),
            "title_items": title_items,
            "experience": flatten(profile.get("experience") or []),
            "projects": self._extract_section(
                raw_text,
                "projects",
                ["education", "experience", "skills", "certifications", "achievements"],
            ).lower(),
            "education": flatten(profile.get("education") or []),
            "skills": flatten(profile.get("skills") or []),
            "technologies": flatten(profile.get("technologies") or []),
            "raw": raw_text.lower(),
        }

    def _has_backend_evidence(self, sources: dict[str, str]) -> bool:
        backend_text = " ".join(
            [
                sources.get("titles", ""),
                sources.get("experience", ""),
                sources.get("projects", ""),
                sources.get("skills", ""),
                sources.get("technologies", ""),
            ]
        )
        strong_backend_terms = [
            "backend",
            "back end",
            "fastapi",
            "django",
            "flask",
            "spring boot",
            "node.js",
            "rest api",
            "rest apis",
            "api development",
            "server",
            "microservice",
            "microservices",
        ]
        return any(self._contains_term(backend_text, term) for term in strong_backend_terms)

    def _has_healthcare_data_analyst_evidence(self, sources: dict[str, str]) -> bool:
        high_signal_text = " ".join(
            [
                sources.get("titles", ""),
                sources.get("experience", ""),
                sources.get("projects", ""),
                sources.get("skills", ""),
                sources.get("technologies", ""),
            ]
        )
        raw_text = sources.get("raw", "")
        profile_text = f"{high_signal_text} {raw_text}"
        healthcare = any(
            self._contains_term(profile_text, term)
            for term in [
                "healthcare",
                "clinical",
                "clinical data",
                "patient",
                "ehr",
                "emr",
                "hipaa",
                "hospital",
                "medical",
                "claims",
                "pharma",
                "pharmaceutical",
            ]
        )
        strong_healthcare = any(
            self._contains_term(high_signal_text, term)
            for term in [
                "healthcare",
                "clinical",
                "clinical data",
                "patient",
                "ehr",
                "emr",
                "hipaa",
                "hospital",
                "claims",
            ]
        )
        analytics = any(
            self._contains_term(profile_text, term)
            for term in ["data analyst", "data analysis", "sql", "excel", "power bi", "tableau", "reporting", "dashboard"]
        )
        explicit_healthcare_title = any(
            self._contains_term(sources.get("titles", ""), term)
            for term in ["healthcare data analyst", "clinical data analyst", "healthcare bi analyst"]
        )
        return analytics and (explicit_healthcare_title or (healthcare and strong_healthcare))

    def _has_computer_vision_evidence(self, sources: dict[str, str]) -> bool:
        high_signal_text = " ".join(
            [
                sources.get("titles", ""),
                sources.get("experience", ""),
                sources.get("projects", ""),
                sources.get("skills", ""),
                sources.get("technologies", ""),
            ]
        )
        explicit_title = any(
            self._contains_term(sources.get("titles", ""), term)
            for term in ["computer vision engineer", "cv engineer"]
        )
        strong_terms = [
            "computer vision",
            "opencv",
            "image processing",
            "object detection",
            "image segmentation",
            "semantic segmentation",
            "yolo",
            "cnn",
            "convolutional neural network",
        ]
        hits = [term for term in strong_terms if self._contains_term(high_signal_text, term)]
        model_evidence = any(
            self._contains_term(high_signal_text, term)
            for term in ["tensorflow", "pytorch", "deep learning", "machine learning", "model deployment"]
        )
        return explicit_title or len(hits) >= 2 or (self._contains_term(high_signal_text, "computer vision") and model_evidence)

    def _has_computer_vision_role_evidence(self, sources: dict[str, str]) -> bool:
        return any(
            self._contains_term(sources.get("titles", ""), term)
            for term in [
                "computer vision engineer",
                "cv engineer",
            ]
        )

    def _has_data_analyst_core_evidence(self, sources: dict[str, str]) -> bool:
        profile_text = " ".join(
            [
                sources.get("titles", ""),
                sources.get("experience", ""),
                sources.get("projects", ""),
                sources.get("skills", ""),
                sources.get("technologies", ""),
                sources.get("raw", ""),
            ]
        )
        signals = ["data analyst", "data analysis", "sql", "excel", "power bi", "tableau", "reporting", "dashboard"]
        hits = [term for term in signals if self._contains_term(profile_text, term)]
        return len(hits) >= 3

    def _score_role(self, definition: dict, sources: dict[str, str]) -> tuple[float, list[str]]:
        score = 0.0
        evidence: list[str] = []
        title_aliases = definition.get("titles", [])
        for index, alias in enumerate(title_aliases):
            matching_title_index = next(
                (
                    title_index
                    for title_index, title in enumerate(sources["title_items"])
                    if self._contains_term(title, alias)
                ),
                None,
            )
            if matching_title_index is not None:
                score += 18 if matching_title_index == 0 else 3
                evidence.append(f"job title: {alias}")
            elif self._contains_term(sources["experience"], alias):
                score += 10
                evidence.append(f"experience title: {alias}")

        weights = {
            "experience": 1.35,
            "projects": 1.15,
            "skills": 1.0,
            "technologies": 0.6,
            "education": 0.55,
        }
        for signal, value in definition["signals"].items():
            matched_sources = [
                source
                for source, weight in weights.items()
                if self._contains_term(sources[source], signal)
            ]
            if matched_sources:
                source_score = max(weights[source] for source in matched_sources)
                score += value * source_score
                evidence.append(f"{signal} ({', '.join(matched_sources)})")
            elif self._contains_term(sources["raw"], signal):
                score += value * 0.35
                evidence.append(signal)
        return round(score, 2), list(dict.fromkeys(evidence))

    def _fallback_role(self, profile: dict) -> str:
        titles = profile.get("job_titles") or []
        if titles:
            return str(titles[0]).title()
        return "Software Engineer"

    def _extract_technologies(self, text: str) -> list[str]:
        lower = text.lower()
        technologies = [
            display_name
            for term, display_name in TECHNOLOGY_NAMES.items()
            if self._contains_term(lower, term)
        ]
        return list(dict.fromkeys(technologies))[:80]

    def _contains_term(self, text: str, term: str) -> bool:
        pattern = rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None

    def _extract_job_titles(self, text: str) -> list[str]:
        title_patterns = [
            r"\b(?:senior|sr\.?|lead|principal|staff|junior|jr\.?)?\s*(software engineer|software engineering intern|data analyst|healthcare data analyst|clinical data analyst|healthcare bi analyst|data scientist|data science analyst|machine learning engineer|ml engineer|ai/ml engineer|ai engineer|artificial intelligence engineer|nlp engineer|natural language processing engineer|computer vision engineer|cv engineer|analytics engineer|business intelligence analyst|bi analyst|reporting analyst|sql analyst|power bi analyst|research intern|product manager|project manager|business analyst|devops engineer|frontend developer|backend developer|full stack developer)\b",
            r"\banalyst\s*[-–]\s*digital and data science\b",
        ]
        matches: list[tuple[int, str]] = []
        for pattern in title_patterns:
            matches.extend(
                (match.start(), match.group(0).strip())
                for match in re.finditer(pattern, text, re.IGNORECASE)
            )
        matches.sort(key=lambda item: item[0])
        return list(dict.fromkeys(title for _, title in matches))[:20]

    def _extract_section(self, text: str, heading: str, next_headings: list[str]) -> str:
        next_pattern = "|".join(re.escape(item) for item in next_headings)
        pattern = rf"{heading}\s*(.*?)(?:\n\s*(?:{next_pattern})\b|$)"
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""

    def _extract_keywords(self, text: str, seed_terms: list[str]) -> list[str]:
        words = re.findall(r"\b[a-zA-Z][a-zA-Z+#./-]{2,}\b", text.lower())
        stop = {"and", "the", "for", "with", "from", "that", "this", "are", "was", "were", "you", "your"}
        counts: dict[str, int] = {}
        for word in words:
            if word not in stop:
                counts[word] = counts.get(word, 0) + 1
        ranked = sorted(counts, key=counts.get, reverse=True)
        return list(dict.fromkeys(seed_terms + ranked))[:80]
