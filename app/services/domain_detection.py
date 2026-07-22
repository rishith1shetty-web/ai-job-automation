"""Phase 2: evidence-based primary-domain, adjacent-domain, job-family and seniority detection.

This layer is deliberately SEPARATE from the Phase-1 title extraction and from the legacy
`ROLE_PROFILES` role scorer in ``resume_parser``. It consumes the structured resume evidence that
Phase 1 already produces (normalized titles, per-role responsibilities, durations, recency,
current-vs-previous, plus skills / technologies / projects / education) and answers four questions:

    1. primary professional domain          (strongest demonstrated career identity)
    2. adjacent domains                      (0-3, related + backed by multiple signals)
    3. strongest job families               (3-8, within the primary domain)
    4. seniority                             (Intern .. Manager)

Evidence hierarchy (higher normally outweighs lower):

    professional title > repeated responsibilities > years / recency > domain terminology
    > employer/industry > achievements > substantial projects > tools > certifications > education

The module is domain-agnostic and deterministic: no candidate names, employer names, file names or
fixture-specific rules. Tools and education can only ever SUPPORT a domain -- never make it primary
on their own -- and specialised domains (AI/ML, Data Engineering, Cloud/DevOps, Cybersecurity)
require a domain-specific substantial signal before they are eligible at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Evidence weighting -------------------------------------------------------------------------
# Titles dominate; tools and education can only support. Every constant below is a relative weight,
# not a magic threshold -- the ONLY comparisons that matter are between domains scored identically.
W_TITLE = 12.0          # a professional title matching the domain (the strongest single signal)
W_RESPONSIBILITY = 4.0  # per distinct domain responsibility term found within a real role
W_TERMINOLOGY = 1.6     # per distinct domain/business term found in experience or raw text
W_PROJECT = 1.4         # per distinct domain term found in the projects section
W_TOOL = 0.7            # per distinct supporting tool/technology (capped; never decisive alone)
W_EDUCATION = 0.4       # per distinct domain term found only in education

RESPONSIBILITY_CAP = 4  # diminishing returns: only the first N distinct responsibility terms count
TERMINOLOGY_CAP = 4
PROJECT_CAP = 3
TOOL_CAP = 5

# Recency multipliers: recent professional experience outweighs older work; projects/education weak.
RECENCY_CURRENT = 1.0
RECENCY_RECENT = 0.85
RECENCY_OLDER = 0.6


# --- Taxonomy structures ------------------------------------------------------------------------
@dataclass(frozen=True)
class JobFamily:
    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Domain:
    label: str
    title_terms: tuple[str, ...]           # substantial when found inside a job title
    responsibility_terms: tuple[str, ...]  # substantial when found inside role responsibilities
    terminology_terms: tuple[str, ...]     # moderate business/domain jargon
    tool_terms: tuple[str, ...]            # supporting only
    families: tuple[JobFamily, ...]
    # neighbour-domain label -> how that neighbour is PRESENTED when adjacent to THIS domain.
    adjacent: dict[str, str] = field(default_factory=dict)
    # specialised domains require at least one of these in real professional/project text to qualify.
    required_terms: tuple[str, ...] = ()


@dataclass
class ExperienceEvidence:
    title: str
    responsibilities: str
    duration_months: int | None
    is_current: bool
    recency_rank: int  # 0 == most recent


@dataclass
class DomainEvidence:
    titles: list[str]
    experiences: list[ExperienceEvidence]
    skills: list[str]
    technologies: list[str]
    education: str
    projects: str
    raw_text: str


# --- Domain taxonomy ----------------------------------------------------------------------------
# Ordered; the index also serves as a deterministic tie-break when two domains score identically.
TAXONOMY: tuple[Domain, ...] = (
    Domain(
        label="Finance / Financial Analysis",
        title_terms=(
            "financial analyst", "finance analyst", "fp&a analyst", "finance associate",
            "financial associate", "finance manager", "financial planning analyst", "corporate finance",
            "treasury analyst", "finance", "budget analyst", "cost analyst",
        ),
        responsibility_terms=(
            "budgeting", "budget", "forecasting", "forecast", "financial model", "financial modeling",
            "financial modelling", "valuation", "dcf", "discounted cash flow", "variance analysis",
            "financial reporting", "financial statements", "p&l", "profit and loss", "cash flow",
            "month-end close", "balance sheet", "capital budgeting", "working capital", "ebitda",
            "cost analysis", "revenue analysis", "financial planning", "fp&a",
        ),
        terminology_terms=(
            "gaap", "ifrs", "accruals", "capex", "opex", "roi", "npv", "irr", "liquidity",
            "profitability", "financial performance", "cost center", "general ledger",
        ),
        tool_terms=("excel", "power bi", "tableau", "sql", "sap", "oracle financials", "quickbooks", "hyperion"),
        families=(
            JobFamily("Corporate Finance / Financial Analysis", ("financial analyst", "financial analysis", "corporate finance", "financial model", "financial performance")),
            JobFamily("FP&A", ("fp&a", "budgeting", "budget", "forecasting", "forecast", "variance analysis", "financial planning")),
            JobFamily("Investment Analysis", ("investment analysis", "equity research", "securities", "dcf", "valuation model")),
            JobFamily("Portfolio Analysis", ("portfolio analysis", "portfolio", "asset allocation", "returns")),
            JobFamily("Financial Reporting", ("financial reporting", "financial statements", "month-end close", "balance sheet", "gaap")),
            JobFamily("Valuation", ("valuation", "dcf", "discounted cash flow", "comparable company", "ebitda")),
            JobFamily("Treasury", ("treasury", "cash flow", "liquidity", "working capital")),
            JobFamily("Financial Risk", ("financial risk", "credit risk", "market risk", "risk analysis")),
            JobFamily("Finance Analytics", ("power bi", "tableau", "dashboard", "financial dashboard", "data analysis")),
        ),
        adjacent={
            "Investment and Portfolio Management": "Investment and Portfolio Analysis",
            "Data Analytics": "Finance Analytics / Business Intelligence",
            "Accounting": "Accounting",
            "Business Analysis": "Business Analysis",
        },
    ),
    Domain(
        label="Investment and Portfolio Management",
        title_terms=(
            "investment analyst", "portfolio analyst", "portfolio manager", "equity analyst",
            "equity research", "investment associate", "research analyst", "fund analyst",
        ),
        responsibility_terms=(
            "portfolio", "asset allocation", "equity research", "securities", "fixed income",
            "investment analysis", "investment thesis", "due diligence", "valuation", "trading",
            "hedge", "returns", "benchmark",
        ),
        terminology_terms=("aum", "alpha", "beta", "sharpe", "capital markets", "wealth management", "derivatives"),
        tool_terms=("bloomberg", "excel", "factset", "morningstar", "python"),
        families=(
            JobFamily("Investment Analysis", ("investment analysis", "investment thesis", "due diligence", "equity research")),
            JobFamily("Portfolio Management", ("portfolio", "asset allocation", "benchmark", "returns")),
            JobFamily("Equity Research", ("equity research", "securities", "valuation", "coverage")),
            JobFamily("Fixed Income", ("fixed income", "bonds", "credit", "yield")),
        ),
        adjacent={
            "Finance / Financial Analysis": "Finance / Financial Analysis",
            "Data Analytics": "Investment Analytics",
        },
    ),
    Domain(
        label="Accounting",
        title_terms=(
            "accountant", "staff accountant", "senior accountant", "auditor", "audit associate",
            "bookkeeper", "accounts payable", "accounts receivable", "controller", "tax analyst",
        ),
        responsibility_terms=(
            "accounts payable", "accounts receivable", "general ledger", "journal entries",
            "reconciliation", "month-end close", "audit", "tax", "bookkeeping", "payroll",
            "financial statements", "accruals",
        ),
        terminology_terms=("gaap", "ifrs", "trial balance", "depreciation", "invoicing", "compliance"),
        tool_terms=("quickbooks", "sap", "excel", "netsuite", "xero", "oracle"),
        families=(
            JobFamily("General Accounting", ("general ledger", "journal entries", "reconciliation", "accruals")),
            JobFamily("Accounts Payable / Receivable", ("accounts payable", "accounts receivable", "invoicing")),
            JobFamily("Audit", ("audit", "internal audit", "compliance")),
            JobFamily("Tax", ("tax", "tax return", "tax analyst")),
            JobFamily("Financial Reporting", ("financial statements", "month-end close", "gaap")),
        ),
        adjacent={
            "Finance / Financial Analysis": "Finance / Financial Analysis",
            "Data Analytics": "Accounting Analytics",
        },
    ),
    Domain(
        label="Software Engineering",
        title_terms=(
            "software engineer", "software developer", "backend developer", "back end developer",
            "frontend developer", "full stack developer", "application developer", "programmer",
            "software development engineer", "sde", "mobile developer", "api developer",
        ),
        responsibility_terms=(
            "developed", "built", "implemented", "designed", "rest api", "rest apis", "microservices",
            "microservice", "web services", "backend", "back end", "code review", "unit tests",
            "application", "software development", "object-oriented", "algorithms", "debugging",
            "distributed systems", "database schema",
        ),
        terminology_terms=("agile", "scrum", "ci/cd", "git", "design patterns", "sdlc", "oop"),
        tool_terms=(
            "python", "java", "c#", "c++", "javascript", "typescript", "go", "spring boot", "django",
            "fastapi", "flask", "node.js", "react", ".net", "postgresql", "mysql", "mongodb",
        ),
        families=(
            JobFamily("Backend Development", ("backend", "back end", "rest api", "microservices", "fastapi", "django", "spring boot", "server")),
            JobFamily("Frontend Development", ("frontend", "front end", "react", "angular", "vue", "ui", "css")),
            JobFamily("Full Stack Development", ("full stack", "full-stack", "end-to-end", "frontend and backend")),
            JobFamily("API Development", ("rest api", "rest apis", "graphql", "api development", "endpoints")),
            JobFamily("Mobile Development", ("android", "ios", "react native", "flutter", "mobile")),
            JobFamily("Application Development", ("application development", "developed application", "software development")),
            JobFamily("QA Automation", ("test automation", "selenium", "unit tests", "qa automation", "pytest")),
            JobFamily("DevOps / Cloud Application Engineering", ("ci/cd", "docker", "kubernetes", "deployment")),
        ),
        adjacent={
            "Cloud / DevOps": "Cloud / DevOps",
            "Web Development": "Web Development",
            "Data Engineering": "Data Engineering",
        },
    ),
    Domain(
        label="Web Development",
        title_terms=("web developer", "frontend developer", "front end developer", "ui developer", "ui engineer"),
        responsibility_terms=(
            "responsive", "web application", "html", "css", "frontend", "front end", "user interface",
            "single page application", "web pages", "cross-browser",
        ),
        terminology_terms=("seo", "accessibility", "wcag", "web performance", "ux"),
        tool_terms=("react", "angular", "vue", "javascript", "typescript", "html", "css", "tailwind", "next.js"),
        families=(
            JobFamily("Frontend Development", ("frontend", "front end", "react", "angular", "vue", "user interface")),
            JobFamily("Full Stack Web Development", ("full stack", "node.js", "backend", "api")),
            JobFamily("UI Engineering", ("ui", "user interface", "component", "design system")),
        ),
        adjacent={
            "Software Engineering": "Software Engineering",
            "Data Analytics": "Web Analytics",
        },
    ),
    Domain(
        label="Data Analytics",
        title_terms=(
            "data analyst", "reporting analyst", "business intelligence analyst", "bi analyst",
            "analytics analyst", "insights analyst", "product analyst", "marketing analyst",
        ),
        responsibility_terms=(
            "data analysis", "data analytics", "dashboard", "dashboards", "reporting", "reports",
            "kpi", "metrics", "insights", "visualization", "visualisation", "ad hoc analysis",
            "business intelligence", "trend analysis", "data-driven",
        ),
        terminology_terms=("stakeholder", "self-service analytics", "data storytelling", "cohort", "segmentation"),
        tool_terms=("sql", "excel", "power bi", "tableau", "looker", "python", "pandas", "google analytics"),
        families=(
            JobFamily("Data Analysis", ("data analysis", "data analytics", "ad hoc analysis", "insights")),
            JobFamily("Business Intelligence", ("business intelligence", "power bi", "tableau", "looker", "self-service")),
            JobFamily("Reporting Analytics", ("reporting", "reports", "dashboard", "kpi", "metrics")),
            JobFamily("Product Analytics", ("product analytics", "funnel", "retention", "engagement", "cohort")),
            JobFamily("Marketing Analytics", ("marketing analytics", "campaign", "attribution", "conversion")),
            JobFamily("Data Engineering", ("etl", "data pipeline", "warehouse")),
            JobFamily("Data Science", ("machine learning", "predictive", "statistical modeling")),
        ),
        adjacent={
            "Data Engineering": "Data Engineering",
            "AI / Machine Learning": "Data Science / Machine Learning",
            "Business Analysis": "Business Analysis",
        },
    ),
    Domain(
        label="Data Engineering",
        title_terms=("data engineer", "etl developer", "analytics engineer", "big data engineer", "data platform engineer"),
        responsibility_terms=(
            "etl", "elt", "data pipeline", "data pipelines", "pipelines", "data warehouse", "warehousing",
            "orchestration", "batch processing", "stream processing", "data lake", "ingestion",
            "data modeling", "data platform",
        ),
        terminology_terms=("schema", "partitioning", "data quality", "throughput", "scalable"),
        tool_terms=("spark", "airflow", "kafka", "dbt", "snowflake", "redshift", "bigquery", "hadoop", "databricks", "sql"),
        families=(
            JobFamily("ETL / Pipeline Engineering", ("etl", "elt", "data pipeline", "pipelines", "ingestion")),
            JobFamily("Data Warehousing", ("data warehouse", "warehousing", "snowflake", "redshift", "bigquery")),
            JobFamily("Streaming / Batch Processing", ("stream processing", "batch processing", "kafka", "spark")),
            JobFamily("Data Platform Engineering", ("data platform", "data lake", "orchestration", "airflow")),
        ),
        required_terms=(
            "etl", "elt", "data pipeline", "pipelines", "data warehouse", "warehousing", "orchestration",
            "airflow", "spark", "kafka", "dbt", "data lake", "batch processing", "stream processing",
        ),
        adjacent={
            "Data Analytics": "Data Analytics",
            "Cloud / DevOps": "Cloud Data Platforms",
            "Software Engineering": "Backend Engineering",
        },
    ),
    Domain(
        label="AI / Machine Learning",
        title_terms=(
            "machine learning engineer", "ml engineer", "ai engineer", "data scientist",
            "research scientist", "nlp engineer", "computer vision engineer", "deep learning engineer",
        ),
        responsibility_terms=(
            "model training", "model development", "trained", "machine learning model", "deep learning",
            "feature engineering", "model evaluation", "model deployment", "neural network",
            "natural language processing", "computer vision", "predictive model", "hyperparameter",
            "inference", "fine-tuning",
        ),
        terminology_terms=("supervised", "unsupervised", "reinforcement learning", "embeddings", "transformer", "accuracy", "f1"),
        tool_terms=("tensorflow", "pytorch", "scikit-learn", "keras", "hugging face", "pandas", "numpy", "opencv", "mlflow"),
        families=(
            JobFamily("Machine Learning Engineering", ("model deployment", "ml pipeline", "mlops", "machine learning model")),
            JobFamily("Data Science", ("data science", "predictive model", "statistical modeling", "experimentation")),
            JobFamily("Computer Vision", ("computer vision", "opencv", "image", "object detection", "segmentation")),
            JobFamily("NLP", ("natural language processing", "nlp", "text classification", "language model", "embeddings")),
            JobFamily("Deep Learning", ("deep learning", "neural network", "transformer", "cnn", "rnn")),
            JobFamily("MLOps", ("mlops", "model deployment", "model monitoring", "mlflow")),
        ),
        required_terms=(
            "model training", "model development", "machine learning model", "deep learning",
            "feature engineering", "model evaluation", "model deployment", "neural network",
            "natural language processing", "computer vision", "tensorflow", "pytorch", "predictive model",
        ),
        adjacent={
            "Data Engineering": "Data Engineering",
            "Data Analytics": "Data Analytics",
            "Cloud / DevOps": "MLOps / Cloud",
        },
    ),
    Domain(
        label="Cloud / DevOps",
        title_terms=(
            "devops engineer", "cloud engineer", "site reliability engineer", "platform engineer",
            "infrastructure engineer", "sre", "cloud architect",
        ),
        responsibility_terms=(
            "ci/cd", "continuous integration", "continuous deployment", "infrastructure as code",
            "cloud infrastructure", "kubernetes", "containers", "containerization", "provisioning",
            "deployment pipeline", "monitoring", "scaling", "orchestration", "high availability",
        ),
        terminology_terms=("uptime", "observability", "reliability", "automation", "microservices"),
        tool_terms=("docker", "kubernetes", "terraform", "ansible", "jenkins", "aws", "azure", "gcp", "helm", "prometheus", "cloudformation"),
        families=(
            JobFamily("Cloud Infrastructure", ("cloud infrastructure", "aws", "azure", "gcp", "provisioning")),
            JobFamily("CI/CD Engineering", ("ci/cd", "continuous integration", "continuous deployment", "jenkins", "pipeline")),
            JobFamily("Container Orchestration", ("kubernetes", "containers", "docker", "helm")),
            JobFamily("Site Reliability", ("site reliability", "monitoring", "observability", "uptime", "high availability")),
            JobFamily("Infrastructure as Code", ("infrastructure as code", "terraform", "ansible", "cloudformation")),
            JobFamily("Cloud Operations", ("cloud operations", "scaling", "automation", "provisioning")),
        ),
        required_terms=(
            "ci/cd", "kubernetes", "docker", "terraform", "infrastructure as code", "cloud infrastructure",
            "deployment pipeline", "ansible", "containerization", "provisioning", "continuous deployment",
        ),
        adjacent={
            "Software Engineering": "Platform Engineering",
            "Cybersecurity": "Cloud Security",
            "Networking": "Networking",
        },
    ),
    Domain(
        label="Cybersecurity",
        title_terms=(
            "cybersecurity analyst", "security analyst", "security engineer", "soc analyst",
            "information security analyst", "penetration tester", "security consultant",
        ),
        responsibility_terms=(
            "siem", "incident response", "vulnerability management", "vulnerability", "threat detection",
            "threat", "security monitoring", "penetration testing", "malware", "intrusion detection",
            "iam", "identity and access", "firewall", "security operations", "risk assessment",
        ),
        terminology_terms=("soc", "nist", "iso 27001", "phishing", "endpoint protection", "compliance", "zero trust"),
        tool_terms=("splunk", "wireshark", "nessus", "metasploit", "crowdstrike", "qualys", "burp suite", "snort"),
        families=(
            JobFamily("Security Operations (SOC)", ("soc", "security operations", "siem", "security monitoring", "splunk")),
            JobFamily("Incident Response", ("incident response", "threat detection", "intrusion detection", "malware")),
            JobFamily("Vulnerability Management", ("vulnerability management", "vulnerability", "penetration testing", "nessus")),
            JobFamily("Identity and Access Management", ("iam", "identity and access", "access control", "authentication")),
            JobFamily("Network Security", ("network security", "firewall", "intrusion", "vpn")),
            JobFamily("Governance, Risk and Compliance", ("compliance", "risk assessment", "nist", "iso 27001", "governance")),
        ),
        required_terms=(
            "siem", "incident response", "vulnerability", "threat", "security operations", "soc", "iam",
            "penetration testing", "malware", "intrusion detection", "security monitoring", "firewall",
        ),
        adjacent={
            "Cloud / DevOps": "Cloud Security",
            "Networking": "Network Security",
            "IT Support": "IT Operations",
        },
    ),
    Domain(
        label="IT Support",
        title_terms=(
            "it support specialist", "desktop support engineer", "technical support engineer",
            "help desk", "helpdesk", "service desk", "it technician", "support technician",
            "systems support", "it support",
        ),
        responsibility_terms=(
            "troubleshoot", "troubleshooting", "ticket", "tickets", "ticketing", "help desk", "service desk",
            "desktop support", "end user", "end-user", "hardware", "software installation", "active directory",
            "technical support", "user support", "incident", "onboarding", "remote support",
        ),
        terminology_terms=("sla", "itil", "escalation", "asset management", "imaging", "provisioning"),
        tool_terms=("windows", "office 365", "active directory", "servicenow", "jira", "zendesk", "vpn", "remote desktop"),
        families=(
            JobFamily("Help Desk", ("help desk", "helpdesk", "ticket", "ticketing", "service desk")),
            JobFamily("Desktop Support", ("desktop support", "hardware", "software installation", "imaging")),
            JobFamily("Technical Support", ("technical support", "troubleshoot", "troubleshooting", "user support")),
            JobFamily("Endpoint Support", ("endpoint", "workstation", "laptops", "devices")),
            JobFamily("IT Operations", ("it operations", "monitoring", "asset management", "provisioning")),
            JobFamily("Systems Administration", ("active directory", "system administration", "server", "group policy")),
            JobFamily("Network Support", ("network", "vpn", "connectivity", "lan")),
        ),
        required_terms=(
            "troubleshoot", "troubleshooting", "ticket", "help desk", "service desk", "desktop support",
            "technical support", "end user", "end-user", "active directory", "user support",
            "it support specialist", "desktop support engineer", "help desk", "technical support engineer",
        ),
        adjacent={
            "Networking": "Networking",
            "Cloud / DevOps": "IT Operations",
            "Cybersecurity": "Security Operations",
        },
    ),
    Domain(
        label="Networking",
        title_terms=("network engineer", "network administrator", "network analyst", "noc engineer"),
        responsibility_terms=(
            "routing", "switching", "firewall", "vpn", "lan", "wan", "tcp/ip", "network configuration",
            "network monitoring", "bgp", "ospf", "load balancing",
        ),
        terminology_terms=("cisco", "subnet", "dns", "dhcp", "network topology"),
        tool_terms=("cisco", "juniper", "wireshark", "palo alto", "fortinet"),
        families=(
            JobFamily("Network Engineering", ("routing", "switching", "network configuration", "bgp", "ospf")),
            JobFamily("Network Operations", ("network monitoring", "noc", "uptime")),
            JobFamily("Network Security", ("firewall", "vpn", "network security")),
        ),
        required_terms=("routing", "switching", "network configuration", "tcp/ip", "lan", "wan", "bgp", "ospf", "firewall"),
        adjacent={
            "IT Support": "IT Support",
            "Cloud / DevOps": "Cloud Networking",
            "Cybersecurity": "Network Security",
        },
    ),
    Domain(
        label="Business Analysis",
        title_terms=("business analyst", "systems analyst", "process analyst", "requirements analyst"),
        responsibility_terms=(
            "requirements", "business requirements", "stakeholder", "process improvement", "user stories",
            "gap analysis", "workflow", "business process", "functional specification", "use cases",
        ),
        terminology_terms=("agile", "scrum", "bpmn", "uat", "sdlc", "kpi"),
        tool_terms=("jira", "confluence", "visio", "excel", "sql"),
        families=(
            JobFamily("Requirements Analysis", ("requirements", "business requirements", "user stories", "use cases")),
            JobFamily("Process Analysis", ("process improvement", "business process", "workflow", "gap analysis")),
            JobFamily("Systems Analysis", ("systems analysis", "functional specification", "uat")),
        ),
        adjacent={
            "Data Analytics": "Data Analytics",
            "Product Management": "Product Management",
            "Project Management": "Project Management",
        },
    ),
    Domain(
        label="Product Management",
        title_terms=("product manager", "product owner", "associate product manager", "technical product manager"),
        responsibility_terms=(
            "product roadmap", "roadmap", "product strategy", "user stories", "backlog", "prioritization",
            "product requirements", "go-to-market", "stakeholder", "product launch", "customer needs",
        ),
        terminology_terms=("agile", "scrum", "mvp", "kpi", "user research", "a/b testing"),
        tool_terms=("jira", "figma", "amplitude", "mixpanel", "confluence"),
        families=(
            JobFamily("Product Strategy", ("product strategy", "product roadmap", "roadmap", "vision")),
            JobFamily("Product Ownership", ("product owner", "backlog", "user stories", "prioritization")),
            JobFamily("Growth / Go-to-Market", ("go-to-market", "product launch", "growth")),
        ),
        adjacent={
            "Project Management": "Project Management",
            "Business Analysis": "Business Analysis",
            "Data Analytics": "Product Analytics",
        },
    ),
    Domain(
        label="Project Management",
        title_terms=("project manager", "program manager", "project coordinator", "scrum master", "delivery manager"),
        responsibility_terms=(
            "project management", "project plan", "timelines", "milestones", "deliverables", "stakeholder",
            "resource allocation", "risk management", "budget", "scope", "sprint planning", "coordination",
        ),
        terminology_terms=("agile", "scrum", "waterfall", "pmp", "kanban", "gantt"),
        tool_terms=("jira", "asana", "ms project", "trello", "confluence"),
        families=(
            JobFamily("Project Delivery", ("project plan", "timelines", "milestones", "deliverables", "scope")),
            JobFamily("Agile Delivery", ("scrum", "sprint planning", "kanban", "agile")),
            JobFamily("Program Management", ("program management", "cross-functional", "coordination")),
        ),
        adjacent={
            "Product Management": "Product Management",
            "Business Analysis": "Business Analysis",
            "Operations": "Operations",
        },
    ),
    Domain(
        label="Operations",
        title_terms=("operations analyst", "operations manager", "operations associate", "business operations"),
        responsibility_terms=(
            "operations", "process improvement", "supply chain", "logistics", "inventory", "vendor",
            "efficiency", "workflow", "kpi", "sla", "fulfillment",
        ),
        terminology_terms=("lean", "six sigma", "throughput", "cost reduction"),
        tool_terms=("excel", "sap", "erp", "sql"),
        families=(
            JobFamily("Business Operations", ("business operations", "process improvement", "efficiency", "kpi")),
            JobFamily("Supply Chain / Logistics", ("supply chain", "logistics", "inventory", "fulfillment")),
            JobFamily("Vendor / Procurement", ("vendor", "procurement", "sourcing")),
        ),
        adjacent={
            "Data Analytics": "Operations Analytics",
            "Project Management": "Project Management",
        },
    ),
    Domain(
        label="Marketing",
        title_terms=("marketing analyst", "marketing manager", "digital marketing", "marketing specialist", "growth marketer"),
        responsibility_terms=(
            "campaign", "campaigns", "seo", "sem", "content marketing", "social media", "brand",
            "lead generation", "email marketing", "conversion", "engagement", "marketing strategy",
        ),
        terminology_terms=("ctr", "roas", "funnel", "attribution", "audience"),
        tool_terms=("google analytics", "hubspot", "mailchimp", "google ads", "meta ads", "salesforce"),
        families=(
            JobFamily("Digital Marketing", ("digital marketing", "seo", "sem", "google ads", "campaign")),
            JobFamily("Content Marketing", ("content marketing", "content", "social media", "brand")),
            JobFamily("Marketing Analytics", ("marketing analytics", "attribution", "conversion", "funnel")),
        ),
        adjacent={
            "Data Analytics": "Marketing Analytics",
            "Product Management": "Product Marketing",
        },
    ),
    Domain(
        label="Human Resources",
        title_terms=("hr", "human resources", "recruiter", "talent acquisition", "hr generalist", "hr manager"),
        responsibility_terms=(
            "recruitment", "recruiting", "talent acquisition", "onboarding", "employee relations",
            "hr policies", "payroll", "benefits", "performance management", "hiring",
        ),
        terminology_terms=("hris", "compliance", "compensation", "engagement", "retention"),
        tool_terms=("workday", "greenhouse", "lever", "bamboohr", "excel"),
        families=(
            JobFamily("Talent Acquisition", ("recruitment", "recruiting", "talent acquisition", "hiring", "sourcing")),
            JobFamily("HR Operations", ("onboarding", "payroll", "benefits", "hr policies", "hris")),
            JobFamily("People Management", ("employee relations", "performance management", "engagement")),
        ),
        adjacent={
            "Operations": "People Operations",
            "Data Analytics": "People Analytics",
        },
    ),
    Domain(
        label="Healthcare Technology",
        title_terms=("clinical data analyst", "healthcare data analyst", "health informatics", "clinical analyst"),
        responsibility_terms=(
            "clinical", "patient", "ehr", "emr", "hipaa", "healthcare", "clinical data", "hospital",
            "claims", "medical records", "health informatics",
        ),
        terminology_terms=("icd", "hl7", "interoperability", "care", "population health"),
        tool_terms=("epic", "cerner", "sql", "power bi", "tableau"),
        families=(
            JobFamily("Clinical Analytics", ("clinical data", "clinical", "patient", "quality metrics")),
            JobFamily("Health Informatics", ("health informatics", "ehr", "emr", "hl7", "interoperability")),
            JobFamily("Healthcare Reporting", ("healthcare", "claims", "reporting", "dashboard")),
        ),
        adjacent={
            "Data Analytics": "Healthcare Analytics",
            "Business Analysis": "Clinical Systems Analysis",
        },
    ),
)

_DOMAIN_INDEX = {domain.label: index for index, domain in enumerate(TAXONOMY)}

# Title words that denote seniority rather than a domain (used only by seniority detection).
_INTERN_RE = re.compile(r"\b(intern|internship|trainee|co-op)\b", re.I)
_JUNIOR_RE = re.compile(r"\b(junior|jr|associate|entry[- ]level|graduate)\b", re.I)
_SENIOR_RE = re.compile(r"\b(senior|sr)\b", re.I)
_LEAD_RE = re.compile(r"\b(lead|principal|staff)\b", re.I)
_MANAGER_RE = re.compile(r"\b(manager|head|director|vp|vice president|chief)\b", re.I)
# Genuine people/scope leadership only -- NOT routine task verbs like "led the budget process",
# which must never by themselves promote a plain individual-contributor title to Senior.
_LEADERSHIP_RE = re.compile(
    r"\b(managed a team|led a team|team of \d|direct reports|mentored|supervised|people management|"
    r"line management|managed \d+ (?:engineers|analysts|developers|people))\b",
    re.I,
)


def _contains(text: str, term: str) -> bool:
    """Word-boundary match that tolerates the punctuation common in tech/finance terms."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text.lower()) is not None


def _distinct_hits(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if _contains(text, term)]


def _recency_multiplier(exp: ExperienceEvidence) -> float:
    if exp.is_current or exp.recency_rank == 0:
        return RECENCY_CURRENT
    if exp.recency_rank == 1:
        return RECENCY_RECENT
    return RECENCY_OLDER


def _duration_multiplier(duration_months: int | None) -> float:
    if not duration_months or duration_months <= 0:
        return 1.0
    return 1.0 + min(duration_months, 36) / 36 * 0.5  # up to 1.5x for >= 3 years in one role


@dataclass
class _DomainScore:
    domain: Domain
    score: float = 0.0
    substantial: int = 0            # count of substantial evidence sources (title/responsibility/project)
    professional: int = 0           # substantial sources coming from real employment records only
    supporting: int = 0             # count of supporting evidence sources (terminology/tools/education)
    matched_titles: list[str] = field(default_factory=list)
    matched_responsibilities: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    matched_tools: list[str] = field(default_factory=list)
    has_required: bool = True       # specialised-domain minimum-evidence gate

    @property
    def total_sources(self) -> int:
        return self.substantial + self.supporting

    @property
    def eligible(self) -> bool:
        # A domain may only be selected with a substantial signal AND at least two sources overall,
        # and (for specialised domains) its minimum-evidence gate satisfied. Tools/education alone
        # can never make total_sources reach the substantial requirement.
        return self.substantial >= 1 and self.total_sources >= 2 and self.has_required


def _score_domain(domain: Domain, evidence: DomainEvidence) -> _DomainScore:
    result = _DomainScore(domain=domain)
    tool_bag = " ".join([*evidence.skills, *evidence.technologies]).lower()

    # 1. Titles + responsibilities, per real employment record (weighted by recency and duration).
    for exp in evidence.experiences:
        recency = _recency_multiplier(exp)
        duration = _duration_multiplier(exp.duration_months)
        title_hit = next((term for term in domain.title_terms if _contains(exp.title, term)), None)
        if title_hit:
            result.score += W_TITLE * recency * duration
            result.substantial += 1
            result.professional += 1
            result.matched_titles.append(exp.title.strip())
        resp_hits = _distinct_hits(exp.responsibilities, domain.responsibility_terms)
        if resp_hits:
            result.score += W_RESPONSIBILITY * min(len(resp_hits), RESPONSIBILITY_CAP) * recency * duration
            result.substantial += 1
            result.professional += 1
            result.matched_responsibilities.extend(resp_hits)

    # Titles that were parsed as headline titles but not tied to a dated role still count (weaker).
    headline_text = " ".join(evidence.titles).lower()
    for term in domain.title_terms:
        if _contains(headline_text, term) and not any(_contains(t, term) for t in result.matched_titles):
            result.score += W_TITLE * 0.5
            result.substantial += 1
            result.matched_titles.append(term)

    # 2. Domain terminology across experience + raw text (moderate; supporting evidence).
    experience_text = " ".join(exp.responsibilities for exp in evidence.experiences)
    term_hits = _distinct_hits(f"{experience_text} {evidence.raw_text}", domain.terminology_terms)
    if term_hits:
        result.score += W_TERMINOLOGY * min(len(term_hits), TERMINOLOGY_CAP)
        result.supporting += 1
        result.matched_terms.extend(term_hits)

    # 3. Substantial projects: repeated domain evidence in the projects section.
    project_hits = _distinct_hits(evidence.projects, domain.responsibility_terms + domain.terminology_terms)
    if project_hits:
        result.score += W_PROJECT * min(len(project_hits), PROJECT_CAP)
        if len(project_hits) >= 2:
            result.substantial += 1  # a substantial, repeated project counts as a real source
        else:
            result.supporting += 1
        result.matched_responsibilities.extend(term for term in project_hits if term not in result.matched_responsibilities)

    # 4. Tools / technologies (supporting only, capped -- never decisive on their own).
    tool_hits = _distinct_hits(tool_bag, domain.tool_terms)
    if tool_hits:
        result.score += W_TOOL * min(len(tool_hits), TOOL_CAP)
        result.supporting += 1
        result.matched_tools.extend(tool_hits)

    # 5. Education (weakest supporting evidence).
    edu_hits = _distinct_hits(evidence.education, domain.title_terms + domain.terminology_terms)
    if edu_hits:
        result.score += W_EDUCATION * len(edu_hits)
        result.supporting += 1

    # Specialised minimum-evidence gate: the domain needs a genuine professional/project signal,
    # not merely a tool in a skills list or a keyword in the raw text.
    if domain.required_terms:
        professional_text = " ".join([headline_text, experience_text, evidence.projects])
        result.has_required = any(_contains(professional_text, term) for term in domain.required_terms)

    result.score = round(result.score, 3)
    result.matched_responsibilities = list(dict.fromkeys(result.matched_responsibilities))
    result.matched_terms = list(dict.fromkeys(result.matched_terms))
    result.matched_tools = list(dict.fromkeys(result.matched_tools))
    result.matched_titles = list(dict.fromkeys(result.matched_titles))
    return result


def _detect_seniority(evidence: DomainEvidence) -> str:
    """Seniority from titles, total professional experience and scope -- never tools or education."""
    experiences = sorted(evidence.experiences, key=lambda exp: exp.recency_rank)
    current_title = ""
    if experiences:
        current = next((exp for exp in experiences if exp.is_current), experiences[0])
        current_title = current.title or ""
    elif evidence.titles:
        current_title = evidence.titles[0]

    total_months = sum(exp.duration_months or 0 for exp in evidence.experiences)
    responsibilities_text = " ".join(exp.responsibilities for exp in evidence.experiences)
    has_leadership = bool(_LEADERSHIP_RE.search(responsibilities_text))
    only_internships = bool(experiences) and all(_INTERN_RE.search(exp.title or "") for exp in experiences)

    # Explicit title indicators dominate (checked most-senior first).
    if _MANAGER_RE.search(current_title):
        return "Manager"
    if _LEAD_RE.search(current_title):
        return "Lead"
    if _SENIOR_RE.search(current_title):
        return "Senior"
    if _INTERN_RE.search(current_title) or only_internships:
        return "Intern"
    if _JUNIOR_RE.search(current_title):
        return "Junior"

    # Plain individual-contributor title: fall back to tenure, capped at Mid unless real scope shown.
    if not experiences and not evidence.titles:
        return "Entry-level"
    if total_months == 0:
        return "Entry-level"
    if total_months < 12:
        return "Entry-level"
    if total_months < 24:
        return "Junior"
    if total_months < 60:
        return "Mid-level"
    # 5+ years: only rises to Senior when leadership/scope is evidenced; otherwise stays Mid-level.
    return "Senior" if has_leadership else "Mid-level"


def _select_job_families(domain: Domain, evidence: DomainEvidence) -> list[str]:
    experience_text = " ".join(exp.responsibilities for exp in evidence.experiences)
    title_text = " ".join([*(exp.title for exp in evidence.experiences), *evidence.titles])
    tool_bag = " ".join([*evidence.skills, *evidence.technologies])
    haystack = " ".join([title_text, experience_text, evidence.projects, tool_bag, evidence.raw_text])
    scored: list[tuple[float, int, str]] = []
    for index, family in enumerate(domain.families):
        hits = _distinct_hits(haystack, family.terms)
        if hits:
            scored.append((len(hits), index, family.label))
    # Strongest first; taxonomy order breaks ties for determinism. Keep 3-8, never forcing weak ones.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [label for _, _, label in scored[:8]]


def detect_domain_profile(evidence: DomainEvidence) -> dict:
    """Return the Phase-2 structured domain profile. Deterministic for identical input."""
    scores = [_score_domain(domain, evidence) for domain in TAXONOMY]
    by_label = {item.domain.label: item for item in scores}
    eligible = [item for item in scores if item.eligible and item.score > 0]
    eligible.sort(key=lambda item: (-item.score, _DOMAIN_INDEX[item.domain.label]))

    seniority = _detect_seniority(evidence)

    if not eligible:
        # Fresher / sparse resume: fall back to the best-scoring domain (usually project/education
        # driven) with low confidence, and never invent professional experience.
        ranked = sorted(scores, key=lambda item: (-item.score, _DOMAIN_INDEX[item.domain.label]))
        # Only infer from the fallback when SOME substantial signal exists (a real project or a
        # headline title). Tools/education alone must never yield a confident primary domain.
        best = next((item for item in ranked if item.score > 0 and item.substantial >= 1), None)
        if best is None:
            return {
                "primary_domain": "Undetermined",
                "primary_domain_confidence": 0.0,
                "adjacent_domains": [],
                "seniority": seniority,
                "strongest_job_families": [],
                "evidence_summary": "No decisive professional-domain evidence found in the resume.",
                "supporting_job_titles": [],
                "supporting_responsibilities": [],
                "supporting_domain_terms": [],
                "supporting_tools": [],
                "suppressed_domains": [],
                "suppression_reasons": [],
            }
        confidence = round(min(45.0, 20.0 + best.score * 1.2), 1)
        return {
            "primary_domain": best.domain.label,
            "primary_domain_confidence": confidence,
            "adjacent_domains": [],
            "seniority": seniority if seniority not in {"Mid-level", "Senior"} else "Entry-level",
            "strongest_job_families": _select_job_families(best.domain, evidence),
            "evidence_summary": (
                "Primary domain inferred mainly from projects/education (no dominant professional "
                "history); low confidence."
            ),
            "supporting_job_titles": best.matched_titles,
            "supporting_responsibilities": best.matched_responsibilities,
            "supporting_domain_terms": best.matched_terms,
            "supporting_tools": best.matched_tools,
            "suppressed_domains": [],
            "suppression_reasons": [],
        }

    primary = eligible[0]
    runner_up = eligible[1].score if len(eligible) > 1 else 0.0
    margin = (primary.score - runner_up) / primary.score if primary.score else 0.0
    title_backed = bool(primary.matched_titles)
    base = 58.0 if title_backed else 48.0
    confidence = round(min(94.0, base + primary.score * 0.55 + margin * 18.0), 1)
    # A primary backed only by projects/education (no professional employment evidence) must not
    # read as a high-confidence career identity -- cap it well below a title-backed classification.
    if primary.professional == 0:
        confidence = round(min(confidence, 60.0), 1)

    # Adjacent domains: related to the primary, eligible or with multiple supporting signals, and
    # scoring a meaningful fraction of the primary. Presented with the primary's neighbour label.
    adjacent: list[str] = []
    adjacency = primary.domain.adjacent
    for item in eligible[1:]:
        label = item.domain.label
        if label not in adjacency:
            continue
        multiple_signals = item.substantial >= 1 or item.supporting >= 2
        if multiple_signals and item.score >= max(0.2 * primary.score, 2.0):
            presentation = adjacency[label]
            if presentation not in adjacent:
                adjacent.append(presentation)
        if len(adjacent) >= 3:
            break

    # Suppressed domains: commonly-confused domains that scored but were NOT chosen, with a reason.
    adjacent_source_labels = {item.domain.label for item in eligible[1:] if adjacency.get(item.domain.label) in adjacent}
    suppressed: list[dict] = []
    for item in scores:
        label = item.domain.label
        if label == primary.domain.label or label in adjacent_source_labels or item.score <= 0:
            continue
        if not item.has_required:
            reason = "tools/keywords only; lacks a substantial professional or project signal for this domain"
        elif item.substantial < 1:
            reason = "supported only by tools/education; no domain title or responsibility"
        elif not item.eligible:
            reason = "insufficient evidence (fewer than two sources)"
        else:
            reason = f"ranked below the primary domain ({primary.domain.label}); related evidence only"
        suppressed.append({"domain": label, "score": item.score, "reason": reason})
    suppressed.sort(key=lambda entry: (-entry["score"], _DOMAIN_INDEX[entry["domain"]]))
    suppressed = suppressed[:6]

    return {
        "primary_domain": primary.domain.label,
        "primary_domain_confidence": confidence,
        "adjacent_domains": adjacent,
        "seniority": seniority,
        "strongest_job_families": _select_job_families(primary.domain, evidence),
        "evidence_summary": _build_evidence_summary(primary, seniority),
        "supporting_job_titles": primary.matched_titles,
        "supporting_responsibilities": primary.matched_responsibilities,
        "supporting_domain_terms": primary.matched_terms,
        "supporting_tools": primary.matched_tools,
        "suppressed_domains": [entry["domain"] for entry in suppressed],
        "suppression_reasons": [f"{entry['domain']} — {entry['reason']}" for entry in suppressed],
    }


def _build_evidence_summary(primary: _DomainScore, seniority: str) -> str:
    parts = [f"Primary domain '{primary.domain.label}' ({seniority})"]
    if primary.matched_titles:
        parts.append("title evidence: " + ", ".join(primary.matched_titles[:3]))
    if primary.matched_responsibilities:
        parts.append("responsibilities: " + ", ".join(primary.matched_responsibilities[:5]))
    if primary.matched_tools:
        parts.append("supporting tools: " + ", ".join(primary.matched_tools[:5]))
    return "; ".join(parts) + "."
