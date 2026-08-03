"""Phase 3: deterministic top-five role ranking and evidence-based confidence scoring.

This layer sits on top of the Phase-1 structured resume (titles, responsibilities, durations,
projects, skills, education) and the Phase-2 domain profile (primary domain, supported adjacent
domains, strongest job families, seniority). It produces AT MOST five unique, realistic roles in
two positional categories -- 1 Primary Role and up to 4 Strong Fit -- each with an evidence-based
confidence score and a full evidence / missing-evidence / penalty breakdown.

It used to produce twelve (1 Primary / 4 Strong Fit / 4 Related / 3 Stretch), and Find Matches
searched 4, 8 or 12 of them depending on the preset. The Related and Stretch tiers existed to fill
a twelve-slot layout, and searching them widened results without improving them. The layout is now
the five roles worth searching, and the preset only caps how many JOBS come back. Every candidate
is still ranked and filtered first -- the list is never a truncation of an unfiltered twelve. See
:mod:`app.services.role_selection` for the filtering and limiting rules.

It is intentionally SEPARATE from, and never mutates, the legacy ``ROLE_PROFILES`` scorer that still
drives ``detected_role`` / ``role_suggestions`` / ``generated_search_*`` (those feed the search plan,
which is Phase 4 and must stay unchanged here). The output is attached to the parsed profile as
``role_profile`` for display only.

Design guarantees:
    * every one of the 19 Phase-2 domains is self-sufficient (>= 12 realistic canonical roles), so a
      supported primary domain fills the whole selection by itself;
    * every selected role comes from the primary domain (or its detected specialization) -- a role
      from an adjacent domain is a different professional family and is never searched;
    * fewer than five roles is a normal outcome for a narrow resume; nothing is padded;
    * professional titles and responsibilities outweigh tools and education (evidence hierarchy);
    * specialised roles (ML/Data/DevOps/Cloud/Security engineers) need real professional or
      substantial project evidence -- tools or certifications alone fail the gate;
    * weakly-supported roles get LOWER confidence rather than being replaced by unrelated roles;
    * confidence is derived from evidence, not from the category;
    * output is deterministic for identical input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.domain_detection import (
    TAXONOMY,
    DomainEvidence,
    _contains,
)
from app.services.role_selection import (
    MAX_SEARCH_ROLES,
    RoleCandidate,
    normalize_title,
    select_roles,
)

_DOMAIN_BY_LABEL = {domain.label: domain for domain in TAXONOMY}

CATEGORY_LAYOUT = (
    ("Primary Role", 1),
    ("Strong Fit", MAX_SEARCH_ROLES - 1),
)
TOTAL_ROLES = sum(count for _, count in CATEGORY_LAYOUT)  # == MAX_SEARCH_ROLES
# Every returned position must come from the PRIMARY domain; there are no adjacent-domain slots.
PRIMARY_ONLY_SLOTS = TOTAL_ROLES

_SENIORITY_ORDER = ["Intern", "Entry-level", "Junior", "Mid-level", "Senior", "Lead", "Manager"]
_LEVEL_TO_SENIORITY = {"lead": "Lead", "manager": "Manager"}


@dataclass(frozen=True)
class RoleDef:
    title: str
    family: str
    terms: tuple[str, ...]
    specialized: bool = False   # requires a professional responsibility or substantial project
    level: str = "ic"           # "ic" | "lead" | "manager"
    vertical: str = ""          # optional specialization vertical (for cross-vertical suppression)


def _r(title, family, terms, specialized=False, level="ic", vertical="") -> RoleDef:
    return RoleDef(title=title, family=family, terms=tuple(terms), specialized=specialized, level=level, vertical=vertical)


# --- Role taxonomy: >= 12 realistic canonical roles per Phase-2 domain ---------------------------
# Ordered within a domain from core to peripheral; order only breaks confidence ties. Terms are
# domain-specific -- bare generic verbs (built/developed/designed/managed) are never signals.
ROLE_TAXONOMY: dict[str, tuple[RoleDef, ...]] = {
    "Finance / Financial Analysis": (
        _r("Financial Analyst", "Corporate Finance / Financial Analysis", ["financial analyst", "financial analysis", "financial model", "financial performance"]),
        _r("FP&A Analyst", "FP&A", ["fp&a", "budgeting", "budget", "forecasting", "forecast", "variance analysis", "financial planning"]),
        _r("Investment Analyst", "Investment Analysis", ["investment analysis", "investment", "valuation", "dcf", "equity research", "securities"]),
        _r("Portfolio Analyst", "Portfolio Analysis", ["portfolio", "asset allocation", "returns", "benchmark"]),
        _r("Financial Data Analyst", "Finance Analytics", ["financial reporting", "financial data", "data analysis", "dashboard", "sql", "power bi"]),
        _r("Financial Reporting Analyst", "Financial Reporting", ["financial reporting", "financial statements", "month-end close", "gaap", "balance sheet"]),
        _r("Business Finance Analyst", "Corporate Finance / Financial Analysis", ["business finance", "financial performance", "cost analysis", "revenue", "financial planning"]),
        _r("Valuation Analyst", "Valuation", ["valuation", "dcf", "discounted cash flow", "ebitda", "comparable company"]),
        _r("Treasury Analyst", "Treasury", ["treasury", "cash flow", "liquidity", "working capital"]),
        _r("Investment Risk Analyst", "Financial Risk", ["financial risk", "credit risk", "market risk", "risk analysis", "investment risk"]),
        _r("Finance BI Analyst", "Finance Analytics", ["business intelligence", "power bi", "tableau", "dashboard", "reporting"]),
        _r("Revenue / Profitability Analyst", "Corporate Finance / Financial Analysis", ["revenue", "profitability", "margin", "cost analysis", "p&l"]),
        _r("Finance Manager", "Corporate Finance / Financial Analysis", ["finance manager", "financial planning", "budgeting", "led a team", "team of"], level="manager"),
    ),
    "Investment and Portfolio Management": (
        _r("Investment Analyst", "Investment Analysis", ["investment analysis", "investment thesis", "due diligence", "equity research"]),
        _r("Portfolio Analyst", "Portfolio Management", ["portfolio", "asset allocation", "benchmark", "returns"]),
        _r("Equity Research Analyst", "Equity Research", ["equity research", "securities", "valuation", "coverage"]),
        _r("Fixed Income Analyst", "Fixed Income", ["fixed income", "bonds", "credit", "yield"]),
        _r("Research Analyst", "Investment Analysis", ["research analyst", "market research", "investment research"]),
        _r("Buy-Side Analyst", "Investment Analysis", ["buy-side", "investment thesis", "due diligence"]),
        _r("Sell-Side Analyst", "Equity Research", ["sell-side", "equity research", "coverage"]),
        _r("Quantitative Analyst", "Investment Analysis", ["quantitative", "quant", "statistical modeling", "python"]),
        _r("Investment Associate", "Portfolio Management", ["investment associate", "portfolio", "asset allocation"]),
        _r("Wealth Management Analyst", "Portfolio Management", ["wealth management", "client portfolio", "financial planning"]),
        _r("Asset Management Analyst", "Portfolio Management", ["asset management", "aum", "portfolio"]),
        _r("Portfolio Manager", "Portfolio Management", ["portfolio manager", "asset allocation", "led a team"], level="manager"),
    ),
    "Accounting": (
        _r("Staff Accountant", "General Accounting", ["accountant", "general ledger", "journal entries", "reconciliation"]),
        _r("Senior Accountant", "General Accounting", ["senior accountant", "general ledger", "accruals", "reconciliation"]),
        _r("Financial Reporting Accountant", "Financial Reporting", ["financial reporting", "financial statements", "month-end close", "gaap"]),
        _r("Accounts Payable Analyst", "Accounts Payable / Receivable", ["accounts payable", "invoicing", "reconciliation"]),
        _r("Accounts Receivable Analyst", "Accounts Payable / Receivable", ["accounts receivable", "collections", "invoicing"]),
        _r("Auditor", "Audit", ["audit", "internal audit", "compliance"]),
        _r("Internal Auditor", "Audit", ["internal audit", "controls", "compliance", "risk"]),
        _r("Tax Analyst", "Tax", ["tax", "tax return", "tax analyst"]),
        _r("Cost Accountant", "General Accounting", ["cost accounting", "cost analysis", "accruals"]),
        _r("Payroll Analyst", "General Accounting", ["payroll", "payroll processing", "compensation"]),
        _r("General Ledger Accountant", "General Accounting", ["general ledger", "journal entries", "reconciliation", "month-end close"]),
        _r("Accounting Manager", "Financial Reporting", ["accounting manager", "financial reporting", "led a team"], level="manager"),
    ),
    "Software Engineering": (
        _r("Software Engineer", "Application Development", ["software engineer", "software development", "software application", "object-oriented", "algorithms"]),
        _r("Backend Developer", "Backend Development", ["backend", "back end", "rest api", "microservices", "server", "fastapi", "django", "spring boot"]),
        _r("Frontend Developer", "Frontend Development", ["frontend", "front end", "react", "angular", "user interface", "css"]),
        _r("Full Stack Developer", "Full Stack Development", ["full stack", "full-stack", "frontend and backend", "end-to-end"]),
        _r("API Developer", "API Development", ["rest api", "rest apis", "graphql", "api development", "endpoints"]),
        _r("Application Developer", "Application Development", ["application development", "software development", "feature development"]),
        _r("Software Development Engineer", "Application Development", ["software development engineer", "sde", "algorithms", "distributed systems"]),
        _r("Web Developer", "Frontend Development", ["web application", "html", "css", "javascript", "web pages"]),
        _r("Services Engineer", "Backend Development", ["web services", "distributed systems", "microservices", "server-side"]),
        _r("Systems Software Engineer", "Application Development", ["systems software", "low-level", "concurrency", "performance"]),
        _r("Integration Engineer", "API Development", ["integration", "api integration", "middleware", "webhooks"]),
        _r("Software Engineer in Test", "QA Automation", ["software engineer in test", "sdet", "test framework", "quality engineering"]),
        _r("Mobile Developer", "Mobile Development", ["android", "ios", "react native", "flutter", "mobile application", "mobile app"], specialized=True),
        _r("QA Automation Engineer", "QA Automation", ["test automation", "selenium", "qa automation", "pytest", "automated tests"], specialized=True),
        _r("Platform Engineer", "DevOps / Cloud Application Engineering", ["ci/cd", "docker", "kubernetes", "deployment pipeline", "platform engineering"], specialized=True),
    ),
    "Web Development": (
        _r("Frontend Developer", "Frontend Development", ["frontend", "front end", "react", "angular", "vue", "user interface"]),
        _r("Web Developer", "Frontend Development", ["web application", "html", "css", "javascript", "web pages"]),
        _r("Full Stack Web Developer", "Full Stack Web Development", ["full stack", "node.js", "backend", "api"]),
        _r("UI Engineer", "UI Engineering", ["ui", "user interface", "component", "design system"]),
        _r("JavaScript Developer", "Frontend Development", ["javascript", "typescript", "es6"]),
        _r("React Developer", "Frontend Development", ["react", "redux", "hooks", "component"]),
        _r("Angular Developer", "Frontend Development", ["angular", "rxjs", "typescript"]),
        _r("Vue Developer", "Frontend Development", ["vue", "vuex", "nuxt"]),
        _r("Frontend Engineer", "Frontend Development", ["frontend engineer", "web performance", "accessibility", "responsive"]),
        _r("Web Application Developer", "Full Stack Web Development", ["web application", "single page application", "spa"]),
        _r("UI/UX Developer", "UI Engineering", ["ui/ux", "user experience", "wireframe", "prototype"]),
        _r("WordPress Developer", "Frontend Development", ["wordpress", "cms", "php", "theme"]),
    ),
    "Data Analytics": (
        _r("Data Analyst", "Data Analysis", ["data analyst", "data analysis", "data analytics", "ad hoc analysis", "insights"]),
        _r("Business Intelligence Analyst", "Business Intelligence", ["business intelligence", "power bi", "tableau", "looker", "self-service"]),
        _r("Reporting Analyst", "Reporting Analytics", ["reporting", "reports", "dashboard", "kpi", "metrics"]),
        _r("Product Analyst", "Product Analytics", ["product analytics", "funnel", "retention", "engagement", "cohort"], vertical="Product"),
        _r("Marketing Analyst", "Marketing Analytics", ["marketing analytics", "campaign", "attribution", "conversion"], vertical="Marketing"),
        _r("Insights Analyst", "Data Analysis", ["insights", "trend analysis", "data-driven", "stakeholder"]),
        _r("BI Developer", "Business Intelligence", ["power bi", "tableau", "dashboard", "data visualization"]),
        _r("Operations Analyst", "Reporting Analytics", ["operations", "process", "metrics", "efficiency"], vertical="Operations"),
        _r("SQL Analyst", "Data Analysis", ["sql", "queries", "database", "reporting"]),
        _r("Data Visualization Analyst", "Business Intelligence", ["data visualization", "dashboard", "tableau", "storytelling"]),
        _r("Analytics Consultant", "Data Analysis", ["analytics", "stakeholder", "recommendations", "insights"]),
        _r("CRM Analyst", "Marketing Analytics", ["crm", "customer", "segmentation", "salesforce"], vertical="Marketing"),
    ),
    "Data Engineering": (
        _r("Data Engineer", "ETL / Pipeline Engineering", ["data engineer", "etl", "elt", "data pipeline", "pipelines", "ingestion"], specialized=True),
        _r("ETL Developer", "ETL / Pipeline Engineering", ["etl", "elt", "data pipeline", "ingestion", "transformations"], specialized=True),
        _r("Analytics Engineer", "Data Platform Engineering", ["analytics engineer", "dbt", "data modeling", "warehouse"], specialized=True),
        _r("Data Warehouse Engineer", "Data Warehousing", ["data warehouse", "warehousing", "snowflake", "redshift", "bigquery"], specialized=True),
        _r("Big Data Engineer", "Streaming / Batch Processing", ["spark", "kafka", "batch processing", "stream processing", "hadoop"], specialized=True),
        _r("Data Platform Engineer", "Data Platform Engineering", ["data platform", "data lake", "orchestration", "airflow"], specialized=True),
        _r("Data Pipeline Engineer", "ETL / Pipeline Engineering", ["data pipeline", "pipelines", "orchestration", "ingestion"], specialized=True),
        _r("Streaming Data Engineer", "Streaming / Batch Processing", ["stream processing", "kafka", "real-time", "streaming"], specialized=True),
        _r("Cloud Data Engineer", "Data Warehousing", ["cloud data", "bigquery", "redshift", "snowflake", "data warehouse"], specialized=True),
        _r("Database Engineer", "Data Platform Engineering", ["database engineer", "database", "sql", "query optimization"], specialized=True),
        _r("Data Infrastructure Engineer", "Data Platform Engineering", ["data infrastructure", "orchestration", "data lake", "pipelines"], specialized=True),
        _r("Data Operations Analyst", "ETL / Pipeline Engineering", ["data operations", "data quality", "monitoring", "pipeline monitoring"]),
    ),
    "AI / Machine Learning": (
        _r("Machine Learning Engineer", "Machine Learning Engineering", ["machine learning", "model deployment", "ml pipeline", "machine learning model"], specialized=True),
        _r("Data Scientist", "Data Science", ["data science", "predictive model", "statistical modeling", "experimentation", "machine learning"], specialized=True),
        _r("ML Engineer", "Machine Learning Engineering", ["model training", "model evaluation", "feature engineering", "inference"], specialized=True),
        _r("Computer Vision Engineer", "Computer Vision", ["computer vision", "opencv", "object detection", "segmentation", "image"], specialized=True),
        _r("NLP Engineer", "NLP", ["natural language processing", "nlp", "text classification", "language model", "embeddings"], specialized=True),
        _r("Deep Learning Engineer", "Deep Learning", ["deep learning", "neural network", "transformer", "cnn", "rnn"], specialized=True),
        _r("MLOps Engineer", "MLOps", ["mlops", "model deployment", "model monitoring", "mlflow"], specialized=True),
        _r("Applied Scientist", "Data Science", ["applied scientist", "machine learning", "experimentation", "research"], specialized=True),
        _r("AI Engineer", "Machine Learning Engineering", ["ai engineer", "machine learning", "model deployment", "inference"], specialized=True),
        _r("Machine Learning Researcher", "Deep Learning", ["research", "deep learning", "publications", "novel model"], specialized=True),
        _r("AI/ML Analyst", "Data Science", ["ai", "machine learning", "analysis", "model evaluation"]),
        _r("Data Science Analyst", "Data Science", ["data science", "analysis", "statistical", "python"]),
    ),
    "Cloud / DevOps": (
        _r("DevOps Engineer", "CI/CD Engineering", ["devops", "ci/cd", "continuous integration", "continuous deployment", "jenkins"], specialized=True),
        _r("Cloud Engineer", "Cloud Infrastructure", ["cloud infrastructure", "aws", "azure", "gcp", "provisioning"], specialized=True),
        _r("Site Reliability Engineer", "Site Reliability", ["site reliability", "monitoring", "observability", "uptime", "high availability"], specialized=True),
        _r("Platform Engineer", "Cloud Operations", ["platform", "automation", "scaling", "provisioning"], specialized=True),
        _r("Infrastructure Engineer", "Infrastructure as Code", ["infrastructure as code", "terraform", "ansible", "cloudformation"], specialized=True),
        _r("Kubernetes Engineer", "Container Orchestration", ["kubernetes", "containers", "docker", "helm"], specialized=True),
        _r("Cloud Operations Engineer", "Cloud Operations", ["cloud operations", "monitoring", "scaling", "deployment"], specialized=True),
        _r("Release Engineer", "CI/CD Engineering", ["release engineering", "ci/cd", "deployment", "release pipeline"], specialized=True),
        _r("Automation Engineer", "CI/CD Engineering", ["automation", "ci/cd", "scripting", "pipeline automation"], specialized=True),
        _r("Cloud Architect", "Cloud Infrastructure", ["cloud architect", "cloud infrastructure", "aws", "solution design"], specialized=True, level="lead"),
        _r("Systems Engineer", "Cloud Operations", ["systems engineer", "linux", "automation", "infrastructure"]),
        _r("Cloud Support Engineer", "Cloud Operations", ["cloud support", "troubleshoot", "cloud", "monitoring"]),
        _r("Build Engineer", "CI/CD Engineering", ["build engineer", "ci/cd", "build automation", "jenkins"], specialized=True),
    ),
    "Cybersecurity": (
        _r("Cybersecurity Analyst", "Security Operations (SOC)", ["cybersecurity", "security analyst", "security monitoring", "siem"], specialized=True),
        _r("SOC Analyst", "Security Operations (SOC)", ["soc", "security operations", "siem", "splunk", "monitoring"], specialized=True),
        _r("Incident Response Analyst", "Incident Response", ["incident response", "threat detection", "intrusion detection", "malware"], specialized=True),
        _r("Vulnerability Analyst", "Vulnerability Management", ["vulnerability management", "vulnerability", "penetration testing", "nessus"], specialized=True),
        _r("IAM Analyst", "Identity and Access Management", ["iam", "identity and access", "access control", "authentication"], specialized=True),
        _r("Network Security Engineer", "Network Security", ["network security", "firewall", "intrusion", "vpn"], specialized=True),
        _r("Security Engineer", "Security Operations (SOC)", ["security engineer", "threat", "security monitoring", "hardening"], specialized=True),
        _r("Threat Intelligence Analyst", "Incident Response", ["threat intelligence", "threat", "ioc", "adversary"], specialized=True),
        _r("Application Security Engineer", "Vulnerability Management", ["application security", "appsec", "sast", "owasp"], specialized=True),
        _r("Security Operations Analyst", "Security Operations (SOC)", ["security operations", "siem", "monitoring", "alerts"], specialized=True),
        _r("GRC Analyst", "Governance, Risk and Compliance", ["compliance", "risk assessment", "nist", "iso 27001", "governance"]),
        _r("Information Security Analyst", "Security Operations (SOC)", ["information security", "security monitoring", "controls", "risk"]),
        _r("Penetration Tester", "Vulnerability Management", ["penetration testing", "ethical hacking", "exploit", "vulnerability"], specialized=True),
    ),
    "IT Support": (
        _r("IT Support Specialist", "Technical Support", ["it support", "technical support", "troubleshoot", "user support"]),
        _r("Desktop Support Engineer", "Desktop Support", ["desktop support", "hardware", "software installation", "imaging"]),
        _r("Help Desk Technician", "Help Desk", ["help desk", "helpdesk", "ticket", "ticketing", "service desk"]),
        _r("Technical Support Engineer", "Technical Support", ["technical support", "troubleshooting", "incident", "user support"]),
        _r("Endpoint Support Technician", "Endpoint Support", ["endpoint", "workstation", "laptops", "devices"]),
        _r("IT Operations Analyst", "IT Operations", ["it operations", "monitoring", "asset management", "provisioning"]),
        _r("Systems Administrator", "Systems Administration", ["active directory", "system administration", "server", "group policy"]),
        _r("Service Desk Analyst", "Help Desk", ["service desk", "ticketing", "sla", "escalation"]),
        _r("Network Support Technician", "Network Support", ["network", "vpn", "connectivity", "lan"]),
        _r("IT Technician", "Desktop Support", ["it technician", "hardware", "troubleshoot", "repair"]),
        _r("Technical Support Specialist", "Technical Support", ["technical support specialist", "customer support", "troubleshoot"]),
        _r("IT Support Analyst", "IT Operations", ["it support analyst", "incident", "ticketing", "user support"]),
    ),
    "Networking": (
        _r("Network Engineer", "Network Engineering", ["network engineer", "routing", "switching", "network configuration"]),
        _r("Network Administrator", "Network Operations", ["network administrator", "network monitoring", "lan", "wan"]),
        _r("Network Analyst", "Network Engineering", ["network analyst", "tcp/ip", "network"]),
        _r("NOC Engineer", "Network Operations", ["noc", "network monitoring", "uptime"]),
        _r("Network Operations Analyst", "Network Operations", ["network operations", "monitoring", "incident"]),
        _r("Systems & Network Engineer", "Network Engineering", ["systems and network", "routing", "server", "infrastructure"]),
        _r("Wireless Network Engineer", "Network Engineering", ["wireless", "wifi", "access points", "rf"]),
        _r("Network Support Engineer", "Network Operations", ["network support", "connectivity", "troubleshoot"]),
        _r("Infrastructure Engineer", "Network Engineering", ["infrastructure", "routing", "switching"]),
        _r("Network Architect", "Network Engineering", ["network architect", "network design", "topology"], level="lead"),
        _r("Network Security Engineer", "Network Security", ["network security", "firewall", "vpn"], specialized=True),
        _r("Cloud Network Engineer", "Network Engineering", ["cloud network", "vpc", "aws networking"], specialized=True),
    ),
    "Business Analysis": (
        _r("Business Analyst", "Requirements Analysis", ["business analyst", "requirements", "business requirements", "user stories"]),
        _r("Systems Analyst", "Systems Analysis", ["systems analysis", "functional specification", "uat"]),
        _r("Process Analyst", "Process Analysis", ["process improvement", "business process", "workflow", "gap analysis"]),
        _r("Requirements Analyst", "Requirements Analysis", ["requirements", "use cases", "user stories"]),
        _r("Product Owner", "Requirements Analysis", ["product owner", "backlog", "prioritization"]),
        _r("Business Systems Analyst", "Systems Analysis", ["business systems", "systems analysis", "requirements"]),
        _r("Functional Analyst", "Systems Analysis", ["functional analyst", "functional specification", "configuration"]),
        _r("Operations Analyst", "Process Analysis", ["operations", "process", "efficiency", "metrics"]),
        _r("Business Process Analyst", "Process Analysis", ["business process", "process mapping", "bpmn", "workflow"]),
        _r("IT Business Analyst", "Requirements Analysis", ["it business analyst", "requirements", "stakeholder", "sdlc"]),
        _r("Reporting Analyst", "Process Analysis", ["reporting", "dashboard", "kpi", "metrics"]),
        _r("Data Analyst", "Process Analysis", ["data analysis", "sql", "reporting", "insights"]),
    ),
    "Product Management": (
        _r("Product Manager", "Product Strategy", ["product manager", "product roadmap", "roadmap", "product strategy"]),
        _r("Associate Product Manager", "Product Ownership", ["associate product manager", "backlog", "user stories"]),
        _r("Product Owner", "Product Ownership", ["product owner", "backlog", "prioritization", "user stories"]),
        _r("Technical Product Manager", "Product Strategy", ["technical product manager", "product requirements", "roadmap"]),
        _r("Growth Product Manager", "Growth / Go-to-Market", ["go-to-market", "product launch", "growth"]),
        _r("Product Analyst", "Product Strategy", ["product analytics", "metrics", "experimentation"]),
        _r("Senior Product Manager", "Product Strategy", ["senior product manager", "product strategy", "vision", "roadmap"]),
        _r("Platform Product Manager", "Product Strategy", ["platform product", "api product", "roadmap"]),
        _r("Product Operations Manager", "Product Ownership", ["product operations", "process", "backlog"]),
        _r("Digital Product Manager", "Product Strategy", ["digital product", "roadmap", "customer needs"]),
        _r("Product Marketing Manager", "Growth / Go-to-Market", ["product marketing", "go-to-market", "positioning"]),
        _r("Group Product Manager", "Product Strategy", ["group product manager", "product strategy", "led a team"], level="manager"),
    ),
    "Project Management": (
        _r("Project Manager", "Project Delivery", ["project manager", "project plan", "timelines", "milestones", "deliverables"]),
        _r("Project Coordinator", "Project Delivery", ["project coordinator", "coordination", "scheduling"]),
        _r("Scrum Master", "Agile Delivery", ["scrum master", "sprint planning", "kanban", "agile"]),
        _r("Agile Project Manager", "Agile Delivery", ["agile", "scrum", "sprint", "backlog"]),
        _r("Technical Project Manager", "Project Delivery", ["technical project manager", "technical delivery", "milestones"]),
        _r("PMO Analyst", "Program Management", ["pmo", "portfolio", "governance", "reporting"]),
        _r("Implementation Manager", "Project Delivery", ["implementation", "onboarding", "rollout", "stakeholder"]),
        _r("Release Manager", "Project Delivery", ["release management", "release", "deployment coordination"]),
        _r("Project Analyst", "Project Delivery", ["project analyst", "project plan", "tracking", "reporting"]),
        _r("Program Coordinator", "Program Management", ["program coordinator", "coordination", "scheduling"]),
        _r("Program Manager", "Program Management", ["program manager", "cross-functional", "coordination", "led a team"], level="manager"),
        _r("Delivery Manager", "Project Delivery", ["delivery manager", "delivery", "stakeholder", "led a team"], level="manager"),
    ),
    "Operations": (
        _r("Operations Analyst", "Business Operations", ["operations", "process improvement", "efficiency", "kpi"]),
        _r("Business Operations Analyst", "Business Operations", ["business operations", "process", "metrics"]),
        _r("Supply Chain Analyst", "Supply Chain / Logistics", ["supply chain", "logistics", "inventory"]),
        _r("Logistics Coordinator", "Supply Chain / Logistics", ["logistics", "fulfillment", "shipping"]),
        _r("Procurement Analyst", "Vendor / Procurement", ["procurement", "vendor", "sourcing"]),
        _r("Operations Coordinator", "Business Operations", ["operations coordinator", "coordination", "scheduling"]),
        _r("Process Improvement Analyst", "Business Operations", ["process improvement", "lean", "six sigma", "efficiency"]),
        _r("Inventory Analyst", "Supply Chain / Logistics", ["inventory", "stock", "demand planning"]),
        _r("Fulfillment Operations Analyst", "Supply Chain / Logistics", ["fulfillment", "warehouse", "order management"]),
        _r("Operations Associate", "Business Operations", ["operations associate", "operations", "support"]),
        _r("Vendor Manager", "Vendor / Procurement", ["vendor management", "vendor", "contracts", "led a team"], level="manager"),
        _r("Operations Manager", "Business Operations", ["operations manager", "operations", "led a team", "team of"], level="manager"),
    ),
    "Marketing": (
        _r("Marketing Analyst", "Marketing Analytics", ["marketing analyst", "marketing analytics", "attribution", "conversion"]),
        _r("Digital Marketing Specialist", "Digital Marketing", ["digital marketing", "seo", "sem", "google ads", "campaign"]),
        _r("Content Marketing Specialist", "Content Marketing", ["content marketing", "content", "social media", "brand"]),
        _r("Growth Marketer", "Digital Marketing", ["growth", "conversion", "funnel", "acquisition"]),
        _r("SEO Specialist", "Digital Marketing", ["seo", "search engine optimization", "keywords"]),
        _r("Social Media Manager", "Content Marketing", ["social media", "engagement", "community", "content calendar"]),
        _r("Email Marketing Specialist", "Digital Marketing", ["email marketing", "campaign", "automation", "newsletter"]),
        _r("Brand Manager", "Content Marketing", ["brand", "brand strategy", "positioning"]),
        _r("Performance Marketing Analyst", "Marketing Analytics", ["performance marketing", "roas", "paid media", "attribution"]),
        _r("Campaign Manager", "Content Marketing", ["campaign management", "campaign", "audience"]),
        _r("Marketing Coordinator", "Content Marketing", ["marketing coordinator", "coordination", "campaign support"]),
        _r("Marketing Manager", "Content Marketing", ["marketing manager", "marketing strategy", "led a team"], level="manager"),
    ),
    "Human Resources": (
        _r("Recruiter", "Talent Acquisition", ["recruiter", "recruitment", "recruiting", "talent acquisition", "sourcing"]),
        _r("HR Generalist", "HR Operations", ["hr generalist", "employee relations", "hr policies", "onboarding"]),
        _r("Talent Acquisition Specialist", "Talent Acquisition", ["talent acquisition", "hiring", "sourcing", "recruitment"]),
        _r("HR Coordinator", "HR Operations", ["hr coordinator", "onboarding", "payroll", "benefits"]),
        _r("People Operations Analyst", "People Management", ["people operations", "engagement", "retention"]),
        _r("HR Business Partner", "People Management", ["hr business partner", "hrbp", "employee relations", "workforce"]),
        _r("Compensation & Benefits Analyst", "HR Operations", ["compensation", "benefits", "payroll", "total rewards"]),
        _r("HRIS Analyst", "HR Operations", ["hris", "workday", "hr systems", "reporting"]),
        _r("Learning & Development Specialist", "People Management", ["learning and development", "training", "l&d"]),
        _r("Employee Relations Specialist", "People Management", ["employee relations", "grievance", "engagement"]),
        _r("Technical Recruiter", "Talent Acquisition", ["technical recruiter", "sourcing", "engineering hiring"]),
        _r("HR Manager", "People Management", ["hr manager", "performance management", "led a team"], level="manager"),
    ),
    "Healthcare Technology": (
        _r("Clinical Data Analyst", "Clinical Analytics", ["clinical data", "clinical", "patient", "quality metrics"]),
        _r("Healthcare Data Analyst", "Healthcare Reporting", ["healthcare", "claims", "reporting", "dashboard"]),
        _r("Health Informatics Analyst", "Health Informatics", ["health informatics", "ehr", "emr", "hl7", "interoperability"]),
        _r("Clinical Systems Analyst", "Health Informatics", ["clinical systems", "ehr", "workflow"]),
        _r("Healthcare BI Analyst", "Healthcare Reporting", ["healthcare", "power bi", "tableau", "dashboard", "reporting"]),
        _r("Healthcare Reporting Analyst", "Healthcare Reporting", ["healthcare", "reporting", "metrics", "claims"]),
        _r("EHR Analyst", "Health Informatics", ["ehr", "emr", "epic", "cerner"]),
        _r("Clinical Applications Analyst", "Health Informatics", ["clinical applications", "ehr", "configuration"]),
        _r("Health Data Specialist", "Clinical Analytics", ["health data", "patient data", "hipaa"]),
        _r("Healthcare Business Analyst", "Healthcare Reporting", ["healthcare business analyst", "requirements", "workflow"]),
        _r("Population Health Analyst", "Clinical Analytics", ["population health", "care", "outcomes"]),
        _r("Clinical Informatics Specialist", "Health Informatics", ["clinical informatics", "ehr", "interoperability"]),
    ),
}


# One normalizer for the whole role pipeline: ranking deduplicates with it, and selection compares
# and deduplicates search titles with it, so a title can never be "distinct" in one place and a
# duplicate in the other.
_norm_title = normalize_title


@dataclass
class _Scored:
    role: RoleDef
    domain: str
    is_primary_domain: bool
    confidence: float
    raw_score: float
    evidence: list[str]
    missing_evidence: list[str]
    penalties: list[str]
    seniority_fit: str
    order_index: int  # taxonomy position, for deterministic tie-breaks
    has_professional_evidence: bool = False  # exact title, a responsibility hit, or a project hit
    # Weaker bar than the above: ANY hit at all -- title, responsibility, project, tool or
    # education. False means the resume says nothing whatsoever about this role, which is what
    # makes it filler rather than merely a weaker fit.
    has_supporting_evidence: bool = False
    # How far the role reaches ABOVE the candidate's demonstrated seniority (0.0 = reachable).
    # Kept as a number, not just a penalty sentence, so selection can reject on it.
    seniority_penalty: float = 0.0
    # 0 = the professional family the resume demonstrates, 1 = the broader parent domain behind a
    # detected specialization. Selection orders within a band, never across one.
    family_rank: int = 0


def _seniority_fit(candidate_seniority: str, role: RoleDef) -> tuple[str, float]:
    """Return (human-readable fit, score penalty). Only lead/manager roles constrain seniority."""
    if role.level == "ic":
        return f"{candidate_seniority} (individual contributor match)", 0.0
    cand_idx = _SENIORITY_ORDER.index(candidate_seniority) if candidate_seniority in _SENIORITY_ORDER else 3
    role_idx = _SENIORITY_ORDER.index(_LEVEL_TO_SENIORITY[role.level])
    if cand_idx >= role_idx:
        return f"{candidate_seniority} (matches {_LEVEL_TO_SENIORITY[role.level].lower()} scope)", 0.0
    gap = role_idx - cand_idx
    label = "stretch up" if gap == 1 else "significant stretch up"
    return f"{candidate_seniority} -> {_LEVEL_TO_SENIORITY[role.level]} ({label})", gap * 5.0


def _score_role(
    role: RoleDef,
    domain: str,
    is_primary_domain: bool,
    evidence: DomainEvidence,
    strong_families: set[str],
    seniority: str,
    order_index: int,
    recency_boost: bool = False,
) -> _Scored | None:
    title_text = " ".join([exp.title for exp in evidence.experiences] + evidence.titles)
    current_titles = " ".join(exp.title for exp in evidence.experiences if exp.is_current) or (
        evidence.experiences[0].title if evidence.experiences else ""
    )
    current_responsibilities = " ".join(exp.responsibilities for exp in evidence.experiences if exp.is_current)
    experience_text = " ".join(exp.responsibilities for exp in evidence.experiences)
    professional_text = f"{title_text} {experience_text}"
    tools_text = " ".join(evidence.skills + evidence.technologies)

    prof_hits = [term for term in role.terms if _contains(professional_text, term)]
    project_hits = [term for term in role.terms if _contains(evidence.projects, term)]
    tool_hits = [term for term in role.terms if _contains(tools_text, term)]
    edu_hits = [term for term in role.terms if _contains(evidence.education, term)]

    # Specialised-role gate: a real professional responsibility OR a substantial (>=2 hit) project.
    # Tools or certifications alone (tool_hits / edu_hits) are deliberately NOT sufficient.
    if role.specialized and not prof_hits and len(project_hits) < 2:
        return None

    exact_current = _contains(current_titles, role.title.lower())
    exact_any = exact_current or _contains(title_text, role.title.lower())

    confidence = 55.0
    evidence_notes: list[str] = []
    if exact_current:
        confidence += 28.0
        evidence_notes.append(f"current job title matches '{role.title}'")
    elif exact_any:
        # On the specialization path a stale PRIOR title is weighted lower, so current specialized
        # work is not outranked by an older role's exact title.
        confidence += 10.0 if recency_boost else 20.0
        evidence_notes.append(f"prior job title matches '{role.title}'")
    if recency_boost:
        # Recency-weighted responsibilities: current-role duties count full, older-role duties half,
        # so demonstrated CURRENT work drives Primary/Strong Fit (specialization path only).
        current_hits = [term for term in role.terms if _contains(current_responsibilities, term)]
        older_hits = [term for term in prof_hits if term not in current_hits]
        if current_hits:
            confidence += 5.0 * min(len(current_hits), 4)
            evidence_notes.append("current-role responsibilities: " + ", ".join(current_hits[:4]))
        if older_hits:
            confidence += 2.5 * min(len(older_hits), 4)
            evidence_notes.append("previous-role responsibilities: " + ", ".join(older_hits[:3]))
    elif prof_hits:
        confidence += 5.0 * min(len(prof_hits), 4)
        evidence_notes.append("professional responsibilities: " + ", ".join(prof_hits[:4]))
    if project_hits:
        confidence += 3.0 * min(len(project_hits), 3)
        evidence_notes.append("project evidence: " + ", ".join(project_hits[:3]))
    if tool_hits:
        confidence += 1.0 * min(len(tool_hits), 3)
        evidence_notes.append("supporting tools: " + ", ".join(tool_hits[:3]))
    if edu_hits:
        confidence += 0.5 * min(len(edu_hits), 2)

    penalties: list[str] = []
    if is_primary_domain:
        confidence += 6.0
    else:
        confidence -= 6.0
        penalties.append("adjacent-domain role (-6)")
    if role.family in strong_families:
        confidence += 3.0
        evidence_notes.append(f"within strongest job family '{role.family}'")

    fit_label, seniority_penalty = _seniority_fit(seniority, role)
    if seniority_penalty:
        confidence -= seniority_penalty
        penalties.append(f"seniority {fit_label} (-{seniority_penalty:.0f})")

    confidence = round(max(0.0, min(99.0, confidence)), 1)
    raw_score = confidence + (2.0 if exact_current else 0.0)  # stable secondary ordering key
    missing = [term for term in role.terms if term not in prof_hits + project_hits + tool_hits + edu_hits][:4]
    return _Scored(
        role=role,
        domain=domain,
        is_primary_domain=is_primary_domain,
        confidence=confidence,
        raw_score=raw_score,
        evidence=evidence_notes[:6],
        missing_evidence=missing,
        penalties=penalties,
        seniority_fit=fit_label,
        order_index=order_index,
        has_professional_evidence=bool(exact_any or prof_hits or project_hits),
        has_supporting_evidence=bool(exact_any or prof_hits or project_hits or tool_hits or edu_hits),
        seniority_penalty=seniority_penalty,
    )


def _rank(items: list[_Scored]) -> list[_Scored]:
    return sorted(items, key=lambda item: (-item.confidence, -item.raw_score, item.order_index))


# --- Rare / unsupported-domain fallback ----------------------------------------------------------
_STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "were", "was", "are", "your", "you",
    "into", "across", "using", "within", "their", "them", "have", "has", "had", "which", "while",
    "including", "various", "multiple", "team", "teams", "work", "worked", "working", "role",
    "responsible", "responsibilities", "company", "provided", "provide", "support", "supported",
    "ensure", "ensured", "based", "over", "per", "also", "well", "new", "used", "use",
}


def _all_domain_vocabulary() -> set[str]:
    """Every single word that appears anywhere in the Phase-2 domain vocabulary (for gap detection)."""
    vocab: set[str] = set()
    for domain in TAXONOMY:
        for group in (domain.title_terms, domain.responsibility_terms, domain.terminology_terms, domain.tool_terms):
            for term in group:
                vocab.update(term.split())
    return vocab


_DOMAIN_VOCAB = _all_domain_vocabulary()


def _professional_titles(evidence: DomainEvidence) -> list[str]:
    """Distinct professional job titles, current/most-recent first, then any headline titles.

    Used for the domain-fit trigger; may include a company name when Phase-1 could not recognise a
    rare title (that is fine here -- an unrecognised title simply fails the domain-fit check)."""
    ordered = sorted(evidence.experiences, key=lambda exp: (0 if exp.is_current else 1, exp.recency_rank))
    titles: list[str] = []
    for exp in ordered:
        title = (exp.title or "").strip()
        if title and title not in titles:
            titles.append(title)
    for title in evidence.titles:
        title = (title or "").strip()
        if title and title not in titles:
            titles.append(title)
    return titles


# A broad set of profession suffixes/nouns -- generic enough to recognise a title in any field
# without naming individual professions, so a company name never masquerades as a role.
_PROFESSION_SUFFIX_RE = re.compile(
    r"\b(analyst|engineer|developer|manager|specialist|designer|consultant|administrator|technician|"
    r"associate|architect|scientist|coordinator|officer|representative|advisor|adviser|accountant|"
    r"auditor|actuary|paralegal|executive|director|nurse|teacher|professor|attorney|lawyer|pharmacist|"
    r"therapist|planner|strategist|recruiter|controller|supervisor|programmer|technologist|examiner|"
    r"bookkeeper|clerk|agent|buyer|estimator|surveyor|writer|editor|researcher|assistant|counsel)\b",
    re.I,
)
_HEADLINE_SKIP = {
    "experience", "work experience", "professional experience", "education", "skills", "technical skills",
    "projects", "summary", "objective", "certifications", "contact", "profile", "achievements",
}


def _headline_title(raw_text: str) -> str:
    """The professional headline: the first short, title-like line after the candidate's name."""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines[1:5]:  # skip line 1 (the name)
        if len(line.split()) > 6 or "|" in line or "@" in line or re.search(r"\d", line):
            continue
        if line.lower() in _HEADLINE_SKIP:
            continue
        return line
    return ""


def _provisional_titles(evidence: DomainEvidence) -> list[str]:
    """Clean professional titles for the provisional path: the resume headline plus any Phase-1 /
    employment titles that read like real job titles. Company names (no profession suffix, not the
    headline) are excluded so they never surface as a role."""
    headline = _headline_title(evidence.raw_text)
    ordered = sorted(evidence.experiences, key=lambda exp: (0 if exp.is_current else 1, exp.recency_rank))
    candidates = [headline] if headline else []
    for title in [*evidence.titles, *(exp.title for exp in ordered)]:
        title = (title or "").strip()
        if not title:
            continue
        if _PROFESSION_SUFFIX_RE.search(title) or (headline and _contains(headline.lower(), title.lower())):
            candidates.append(title)
    seen: set[str] = set()
    result: list[str] = []
    for title in candidates:
        key = title.lower()
        if key not in seen:
            seen.add(key)
            result.append(title)
    return result


def _title_explained_by_domain(titles: list[str], primary_domain: str) -> bool:
    """True when at least one professional title actually belongs to the detected domain -- i.e. it
    matches one of the domain's canonical role titles or Phase-2 domain title terms. This stops a
    rare profession (e.g. Mechanical Engineer) that merely tripped a generic verb from being forced
    into an unrelated supported domain."""
    domain = _DOMAIN_BY_LABEL.get(primary_domain)
    roles = ROLE_TAXONOMY.get(primary_domain, ())
    for title in titles:
        lowered = title.lower()
        if any(_contains(lowered, role.title.lower()) for role in roles):
            return True
        if domain and any(_contains(lowered, term) for term in domain.title_terms):
            return True
    return False


def _unmatched_domain_terms(evidence: DomainEvidence) -> list[str]:
    """Frequent professional-responsibility words absent from the whole supported taxonomy -- the
    terms a future taxonomy expansion would need. Deterministic (frequency desc, then alphabetical)."""
    text = " ".join([exp.responsibilities for exp in evidence.experiences] + evidence.titles).lower()
    counts: dict[str, int] = {}
    for word in re.findall(r"[a-z][a-z+/&-]{3,}", text):
        if word in _STOPWORDS or word in _DOMAIN_VOCAB:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:10]]


# Honest, sub-80 confidence ladder: a provisional (out-of-taxonomy) match is never a confident one.
_PROVISIONAL_CONFIDENCE = (74.0, 70.0, 66.0, 62.0, 58.0, 54.0, 50.0, 47.0, 45.0, 43.0, 42.0, 41.0)


def _dedupe_near_identical(titles: list[str]) -> list[str]:
    """Keep the first of each group of titles that normalize to the same search key."""
    seen: set[str] = set()
    kept: list[str] = []
    for title in titles:
        key = _norm_title(title)
        if key in seen:
            continue
        seen.add(key)
        kept.append(title)
    return kept


def _provisional_role_profile(evidence: DomainEvidence, domain_profile: dict, status: str) -> dict:
    """Infer roles from the candidate's own professional titles when no supported domain fits.

    Never forces a supported domain; returns FEWER than five roles rather than inventing weak ones,
    and records the unmatched terms a future taxonomy expansion would need. There is nothing to
    filter here -- every title came off the candidate's own resume -- but near-identical titles are
    still folded, so "Actuary" and "Senior Actuary" never become two identical portal searches."""
    seniority = domain_profile.get("seniority", "Mid-level")
    titles = _dedupe_near_identical(_provisional_titles(evidence))
    unmatched = _unmatched_domain_terms(evidence)

    if not titles:
        return {
            "taxonomy_status": "unsupported_domain",
            "specialization": None,
            "primary_role": None,
            "primary_role_confidence": 0.0,
            "roles": [],
            "suppressed_roles": [],
            "unmatched_terms": unmatched,
            "suggested_taxonomy_additions": [],
        }

    responsibilities_by_title: dict[str, str] = {}
    for exp in evidence.experiences:
        responsibilities_by_title.setdefault((exp.title or "").strip(), exp.responsibilities or "")

    scored: list[tuple[float, str]] = []
    for index, title in enumerate(titles[:TOTAL_ROLES]):
        base = _PROVISIONAL_CONFIDENCE[index] if index < len(_PROVISIONAL_CONFIDENCE) else 40.0
        responsibilities = responsibilities_by_title.get(title, "")
        depth_bonus = min(len(responsibilities.split()), 40) / 40 * 6.0  # richer duties -> a little higher
        scored.append((round(min(79.0, base + depth_bonus), 1), title))

    roles: list[dict] = []
    rank = 0
    for category, count in CATEGORY_LAYOUT:
        for _ in range(count):
            if rank >= len(scored):
                break
            confidence, title = scored[rank]
            responsibilities = responsibilities_by_title.get(title, "")
            evidence_notes = [f"professional job title: {title}"]
            if responsibilities:
                evidence_notes.append("responsibilities: " + " ".join(responsibilities.split()[:8]))
            roles.append({
                "rank": rank + 1,
                "title": title,
                "category": category,
                "confidence": confidence,
                "domain": "Provisional (outside supported taxonomy)",
                "job_family": title,
                "seniority_fit": f"{seniority} (individual contributor match)",
                "evidence": evidence_notes,
                "missing_evidence": [],
                "penalties": ["provisional: professional domain is outside the supported taxonomy"],
            })
            rank += 1

    return {
        "taxonomy_status": status,
        "specialization": None,
        "primary_role": roles[0]["title"] if roles else None,
        "primary_role_confidence": roles[0]["confidence"] if roles else 0.0,
        "roles": roles,
        "suppressed_roles": [],
        "unmatched_terms": unmatched,
        "suggested_taxonomy_additions": list(dict.fromkeys(titles))[:6],
    }


# --- Specialization / subdomain layer ------------------------------------------------------------
# A broad Phase-2 domain (e.g. "Data Analytics") can hide a well-supported specialization (Healthcare
# Analytics, Applied ML, ...). This layer detects the specialization from the exact CURRENT title,
# current-role responsibilities (recency-weighted), repeated terminology and substantial projects --
# NEVER from tools/certs alone -- and then lets specialization-specific roles compete for the top
# slots on their own evidence. It is additive: when nothing dominates, detection returns None and the
# generic domain ranking below is used unchanged.


@dataclass(frozen=True)
class Specialization:
    label: str
    vertical: str                        # matches RoleDef.vertical tags for cross-vertical suppression
    anchor_domains: tuple[str, ...]      # only considered when the primary domain is one of these
    title_signals: tuple[str, ...]       # title fragments that indicate this specialization
    responsibility_signals: tuple[str, ...]  # responsibility/terminology clusters
    roles: tuple[RoleDef, ...]           # specialization-specific role pool


SPEC_DETECTION_THRESHOLD = 18.0   # roughly a current-title match, or ~3 current responsibility hits
SPEC_DOMINANCE_RATIO = 1.35       # the winner must clearly beat the runner-up, else stay general
SPEC_ALIGNMENT_BONUS = 5.0        # a modest edge for SUPPORTED specialization roles (never for membership alone)


SPECIALIZATIONS: tuple[Specialization, ...] = (
    Specialization(
        label="Healthcare Analytics", vertical="Healthcare",
        anchor_domains=("Data Analytics", "Healthcare Technology"),
        title_signals=("healthcare", "clinical", "ehr", "emr", "patient", "health informatics", "hospital"),
        responsibility_signals=(
            "ehr", "emr", "patient", "clinical", "clinical data", "hipaa", "healthcare", "hospital", "claims",
            "medical records", "population health", "care outcomes", "readmission", "regulatory reporting",
            "quality metrics", "health informatics",
        ),
        roles=(
            _r("Healthcare Data Analyst", "Healthcare Analytics", ["healthcare", "patient", "clinical data", "claims", "healthcare data"]),
            _r("Clinical Data Analyst", "Healthcare Analytics", ["clinical", "clinical data", "patient", "quality metrics"]),
            _r("Healthcare BI Analyst", "Healthcare Analytics", ["healthcare", "power bi", "tableau", "dashboard", "reporting"]),
            _r("Healthcare Reporting Analyst", "Healthcare Analytics", ["healthcare", "reporting", "metrics", "claims", "regulatory reporting"]),
            _r("Health Informatics Analyst", "Healthcare Analytics", ["health informatics", "ehr", "emr", "hl7", "interoperability"]),
            _r("EHR Data Analyst", "Healthcare Analytics", ["ehr", "emr", "epic", "cerner", "patient"]),
            _r("Population Health Analyst", "Healthcare Analytics", ["population health", "care outcomes", "readmission", "cohort"]),
            _r("Clinical Reporting Analyst", "Healthcare Analytics", ["clinical reporting", "clinical", "reporting", "hospital"]),
            _r("Healthcare Data Quality Analyst", "Healthcare Analytics", ["data quality", "clinical data", "validation", "data validation"]),
        ),
    ),
    Specialization(
        label="Financial Analytics", vertical="Financial",
        anchor_domains=("Data Analytics",),
        title_signals=("financial", "finance"),
        responsibility_signals=(
            "financial reporting", "revenue", "profitability", "budgeting", "forecasting", "financial data",
            "p&l", "cost analysis", "financial metrics", "margin", "gross margin",
        ),
        roles=(
            _r("Financial Data Analyst", "Financial Analytics", ["financial data", "financial reporting", "data analysis", "power bi", "sql"]),
            _r("Finance BI Analyst", "Financial Analytics", ["financial", "business intelligence", "power bi", "tableau", "dashboard"]),
            _r("Financial Reporting Analyst", "Financial Analytics", ["financial reporting", "financial statements", "p&l", "month-end"]),
            _r("Revenue Analyst", "Financial Analytics", ["revenue", "bookings", "growth", "revenue analysis"]),
            _r("Profitability Analyst", "Financial Analytics", ["profitability", "margin", "cost analysis", "gross margin"]),
            _r("Financial Planning Analyst", "Financial Analytics", ["budgeting", "forecasting", "financial planning", "variance"]),
            _r("Business Finance Analyst", "Financial Analytics", ["business finance", "financial performance", "cost analysis"]),
        ),
    ),
    Specialization(
        label="Product Analytics", vertical="Product",
        anchor_domains=("Data Analytics",),
        title_signals=("product analyst", "product"),
        responsibility_signals=(
            "product analytics", "funnel", "retention", "engagement", "cohort", "activation", "a/b test",
            "a/b testing", "experimentation", "user behavior", "churn", "conversion rate",
        ),
        roles=(
            _r("Product Analyst", "Product Analytics", ["product analytics", "funnel", "retention", "engagement", "cohort"]),
            _r("Product Data Analyst", "Product Analytics", ["product analytics", "product metrics", "user behavior", "churn"]),
            _r("Growth Analyst", "Product Analytics", ["growth", "activation", "conversion", "acquisition"]),
            _r("Behavioral Analyst", "Product Analytics", ["user behavior", "behavioral", "engagement", "cohort"]),
            _r("Experimentation Analyst", "Product Analytics", ["experimentation", "a/b test", "a/b testing", "hypothesis"]),
            _r("User Insights Analyst", "Product Analytics", ["user insights", "insights", "retention", "engagement"]),
        ),
    ),
    Specialization(
        label="Marketing Analytics", vertical="Marketing",
        anchor_domains=("Data Analytics",),
        title_signals=("marketing analyst", "marketing"),
        responsibility_signals=(
            "campaign", "attribution", "conversion", "marketing analytics", "roas", "ctr", "acquisition",
            "paid media", "email marketing", "channel", "audience",
        ),
        roles=(
            _r("Marketing Analyst", "Marketing Analytics", ["marketing analytics", "campaign", "attribution", "conversion"]),
            _r("Marketing Data Analyst", "Marketing Analytics", ["marketing", "campaign", "channel", "audience"]),
            _r("Campaign Analyst", "Marketing Analytics", ["campaign", "campaign performance", "audience"]),
            _r("Performance Marketing Analyst", "Marketing Analytics", ["performance marketing", "roas", "paid media", "attribution"]),
            _r("Growth Marketing Analyst", "Marketing Analytics", ["growth", "acquisition", "funnel", "conversion"]),
            _r("CRM Analyst", "Marketing Analytics", ["crm", "customer", "segmentation", "lifecycle"]),
        ),
    ),
    Specialization(
        label="Supply-Chain Analytics", vertical="Operations",
        anchor_domains=("Data Analytics", "Operations"),
        title_signals=("supply chain", "logistics", "operations analyst"),
        responsibility_signals=(
            "supply chain", "inventory", "demand planning", "logistics", "procurement", "fulfillment",
            "warehouse", "sku", "replenishment", "supplier", "demand forecast",
        ),
        roles=(
            _r("Supply Chain Analyst", "Supply-Chain Analytics", ["supply chain", "inventory", "logistics", "supplier"]),
            _r("Demand Planning Analyst", "Supply-Chain Analytics", ["demand planning", "demand forecast", "replenishment", "forecast"]),
            _r("Logistics Analyst", "Supply-Chain Analytics", ["logistics", "fulfillment", "shipping", "transportation"]),
            _r("Inventory Analyst", "Supply-Chain Analytics", ["inventory", "stock", "sku", "replenishment"]),
            _r("Supply Chain Data Analyst", "Supply-Chain Analytics", ["supply chain", "data analysis", "dashboard", "reporting"]),
            _r("Procurement Analyst", "Supply-Chain Analytics", ["procurement", "supplier", "sourcing", "purchasing"]),
        ),
    ),
    Specialization(
        label="People / HR Analytics", vertical="HR",
        anchor_domains=("Data Analytics", "Human Resources"),
        title_signals=("people analytics", "hr analyst", "workforce"),
        responsibility_signals=(
            "people analytics", "workforce", "attrition", "retention", "headcount", "hr metrics",
            "employee engagement", "hris", "talent analytics", "diversity", "turnover",
        ),
        roles=(
            _r("People Analytics Analyst", "People / HR Analytics", ["people analytics", "workforce", "attrition", "headcount"]),
            _r("HR Data Analyst", "People / HR Analytics", ["hr metrics", "hris", "headcount", "turnover"]),
            _r("Workforce Analytics Analyst", "People / HR Analytics", ["workforce", "workforce planning", "headcount", "capacity"]),
            _r("HR Reporting Analyst", "People / HR Analytics", ["hr reporting", "hr metrics", "dashboard", "reporting"]),
            _r("Talent Analytics Analyst", "People / HR Analytics", ["talent analytics", "recruiting metrics", "attrition"]),
            _r("People Operations Analyst", "People / HR Analytics", ["people operations", "engagement", "retention"]),
        ),
    ),
    Specialization(
        label="Data Science / Applied ML", vertical="Data Science",
        anchor_domains=("Data Analytics",),
        title_signals=("data science", "data scientist", "machine learning", "digital and data science"),
        responsibility_signals=(
            "machine learning", "model deployment", "predictive model", "feature engineering", "ml pipeline",
            "machine learning pipeline", "statistical modeling", "experimentation", "model evaluation",
            "deep learning", "forecasting", "predictive",
        ),
        roles=(
            _r("Data Scientist", "Data Science / Applied ML", ["data science", "machine learning", "predictive model", "statistical modeling"], specialized=True),
            _r("Machine Learning Engineer", "Data Science / Applied ML", ["machine learning", "model deployment", "ml pipeline", "feature engineering"], specialized=True),
            _r("Applied Data Scientist", "Data Science / Applied ML", ["machine learning", "model deployment", "experimentation", "applied"], specialized=True),
            _r("Predictive Analytics Analyst", "Data Science / Applied ML", ["predictive", "predictive model", "forecasting", "propensity"]),
            _r("Forecasting Analyst", "Data Science / Applied ML", ["forecasting", "demand forecast", "time series", "predictive"]),
            _r("Data Science Analyst", "Data Science / Applied ML", ["data science", "machine learning", "analysis", "modeling"]),
        ),
    ),
    Specialization(
        label="Forecasting / Predictive Analytics", vertical="Forecasting",
        anchor_domains=("Data Analytics", "AI / Machine Learning"),
        title_signals=("forecasting", "predictive", "demand forecasting"),
        responsibility_signals=(
            "forecasting", "demand forecasting", "time series", "predictive model", "arima", "prophet",
            "seasonality", "predictive analytics", "demand planning", "forecast accuracy",
        ),
        roles=(
            _r("Forecasting Analyst", "Forecasting / Predictive Analytics", ["forecasting", "demand forecasting", "time series", "forecast"]),
            _r("Predictive Analytics Analyst", "Forecasting / Predictive Analytics", ["predictive analytics", "predictive model", "propensity"]),
            _r("Demand Forecasting Analyst", "Forecasting / Predictive Analytics", ["demand forecasting", "demand planning", "seasonality"]),
            _r("Time Series Analyst", "Forecasting / Predictive Analytics", ["time series", "arima", "prophet", "seasonality"]),
            _r("Data Scientist", "Forecasting / Predictive Analytics", ["machine learning", "predictive model", "forecasting"], specialized=True),
            _r("Quantitative Analyst", "Forecasting / Predictive Analytics", ["quantitative", "statistical", "modeling", "forecast"]),
        ),
    ),
    Specialization(
        label="NLP", vertical="NLP",
        anchor_domains=("AI / Machine Learning",),
        title_signals=("nlp", "natural language"),
        responsibility_signals=(
            "natural language processing", "nlp", "text classification", "named entity", "language model",
            "embeddings", "sentiment analysis", "tokenization", "transformers", "question answering",
        ),
        roles=(
            _r("NLP Engineer", "NLP", ["natural language processing", "nlp", "text classification", "language model"], specialized=True),
            _r("NLP Scientist", "NLP", ["nlp", "language model", "embeddings", "research"], specialized=True),
            _r("Machine Learning Engineer", "NLP", ["machine learning", "model deployment", "nlp"], specialized=True),
            _r("Conversational AI Engineer", "NLP", ["conversational", "chatbot", "dialogue", "intent"], specialized=True),
            _r("Computational Linguist", "NLP", ["linguistics", "tokenization", "morphology", "corpus"], specialized=True),
            _r("Applied Scientist", "NLP", ["applied scientist", "machine learning", "nlp", "research"], specialized=True),
        ),
    ),
    Specialization(
        label="Computer Vision", vertical="Computer Vision",
        anchor_domains=("AI / Machine Learning",),
        title_signals=("computer vision", "cv engineer"),
        responsibility_signals=(
            "computer vision", "image processing", "object detection", "image segmentation", "opencv", "cnn",
            "image classification", "video analytics", "ocr", "semantic segmentation",
        ),
        roles=(
            _r("Computer Vision Engineer", "Computer Vision", ["computer vision", "object detection", "segmentation", "opencv"], specialized=True),
            _r("Computer Vision Scientist", "Computer Vision", ["computer vision", "image", "research", "cnn"], specialized=True),
            _r("Machine Learning Engineer", "Computer Vision", ["machine learning", "model deployment", "computer vision"], specialized=True),
            _r("Image Processing Engineer", "Computer Vision", ["image processing", "opencv", "image", "ocr"], specialized=True),
            _r("Deep Learning Engineer", "Computer Vision", ["deep learning", "cnn", "neural network", "computer vision"], specialized=True),
            _r("Applied Scientist", "Computer Vision", ["applied scientist", "machine learning", "computer vision"], specialized=True),
        ),
    ),
    Specialization(
        label="Embedded / IoT", vertical="Embedded",
        anchor_domains=("Software Engineering",),
        title_signals=("embedded", "firmware", "iot"),
        responsibility_signals=(
            "embedded", "firmware", "rtos", "microcontroller", "iot", "device driver", "bare-metal", "sensors",
            "hardware", "i2c", "spi", "uart", "low-level",
        ),
        roles=(
            _r("Embedded Software Engineer", "Embedded / IoT", ["embedded", "firmware", "microcontroller", "rtos"], specialized=True),
            _r("Firmware Engineer", "Embedded / IoT", ["firmware", "bare-metal", "device driver", "microcontroller"], specialized=True),
            _r("IoT Engineer", "Embedded / IoT", ["iot", "sensors", "connectivity", "mqtt"], specialized=True),
            _r("Embedded Systems Engineer", "Embedded / IoT", ["embedded", "rtos", "hardware", "low-level"], specialized=True),
            _r("Device Driver Engineer", "Embedded / IoT", ["device driver", "kernel", "i2c", "spi"], specialized=True),
            _r("Embedded C Developer", "Embedded / IoT", ["embedded", "c/c++", "microcontroller", "firmware"], specialized=True),
        ),
    ),
    Specialization(
        label="Cloud Security", vertical="Cloud Security",
        anchor_domains=("Cybersecurity", "Cloud / DevOps"),
        title_signals=("cloud security", "devsecops"),
        responsibility_signals=(
            "cloud security", "aws security", "cspm", "kubernetes security", "container security", "devsecops",
            "cloud posture", "guardduty", "security automation", "iam policy", "cloud iam",
        ),
        roles=(
            _r("Cloud Security Engineer", "Cloud Security", ["cloud security", "aws security", "cspm", "container security"], specialized=True),
            _r("Cloud Security Analyst", "Cloud Security", ["cloud security", "cloud posture", "monitoring", "guardduty"], specialized=True),
            _r("DevSecOps Engineer", "Cloud Security", ["devsecops", "security automation", "ci/cd", "pipeline security"], specialized=True),
            _r("Cloud Security Architect", "Cloud Security", ["cloud security", "architecture", "security design"], specialized=True, level="lead"),
            _r("Application Security Engineer", "Cloud Security", ["application security", "appsec", "sast", "owasp"], specialized=True),
            _r("Security Engineer", "Cloud Security", ["security engineer", "security", "hardening", "iam policy"], specialized=True),
        ),
    ),
)

_SPEC_INDEX = {spec.label: index for index, spec in enumerate(SPECIALIZATIONS)}


def _spec_score(spec: Specialization, evidence: DomainEvidence) -> tuple[float, bool]:
    """Score a specialization from title + recency-weighted responsibilities + terminology + projects
    (never tools). Returns (score, substantial) where `substantial` means a current/prior title match
    or >= 2 professional responsibility hits -- required before a specialization may fire."""
    ordered = sorted(evidence.experiences, key=lambda exp: (0 if exp.is_current else 1, exp.recency_rank))
    current_title = next((exp.title for exp in ordered if exp.is_current), ordered[0].title if ordered else "")
    all_titles = " ".join([exp.title for exp in ordered] + evidence.titles).lower()
    current_resp = " ".join(exp.responsibilities for exp in ordered if exp.is_current).lower()
    previous_resp = " ".join(exp.responsibilities for exp in ordered if not exp.is_current).lower()
    pool_titles = [role.title.lower() for role in spec.roles]

    score = 0.0
    title_hit = False
    if any(_contains(current_title.lower(), sig) for sig in spec.title_signals) or any(
        _contains(current_title.lower(), title) for title in pool_titles
    ):
        score += 30.0
        title_hit = True
    elif any(_contains(all_titles, sig) for sig in spec.title_signals):
        score += 10.0
        title_hit = True

    current_hits = [sig for sig in spec.responsibility_signals if _contains(current_resp, sig)]
    score += 6.0 * min(len(current_hits), 4)
    previous_hits = [sig for sig in spec.responsibility_signals if _contains(previous_resp, sig) and sig not in current_hits]
    score += 3.0 * min(len(previous_hits), 4)
    project_hits = [sig for sig in spec.responsibility_signals if _contains(evidence.projects, sig)]
    score += 2.0 * min(len(project_hits), 3)

    substantial = title_hit or (len(current_hits) + len(previous_hits)) >= 2
    return round(score, 2), substantial


def detect_specialization(evidence: DomainEvidence, primary_domain: str) -> Specialization | None:
    """Detect the dominant specialization for the primary domain, or None to stay general.

    A specialization fires only when it is substantial (title or >=2 responsibilities), clears the
    detection threshold, and clearly dominates the runner-up -- so a broad resume with mixed weak
    signals (or no specialization) keeps the generic domain ranking."""
    scored = []
    for spec in SPECIALIZATIONS:
        if primary_domain not in spec.anchor_domains:
            continue
        score, substantial = _spec_score(spec, evidence)
        if score > 0:
            scored.append((score, substantial, spec))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], _SPEC_INDEX[item[2].label]))
    best_score, best_substantial, best = scored[0]
    if not best_substantial or best_score < SPEC_DETECTION_THRESHOLD:
        return None
    if len(scored) > 1 and best_score < SPEC_DOMINANCE_RATIO * scored[1][0]:
        return None  # no clear dominant specialization -> stay general
    return best


def _score_specialized_pool(
    evidence: DomainEvidence,
    domain_profile: dict,
    primary_domain: str,
    spec: Specialization,
    start_index: int,
) -> tuple[list[_Scored], list[dict], int]:
    """Score the primary-domain roles + the specialization pool with recency weighting, keep only
    roles that carry meaningful evidence, suppress unrelated verticals, and give supported
    specialization roles a modest alignment edge. Returns (ranked_candidates, suppressed, next_index)."""
    seniority = domain_profile.get("seniority", "Mid-level")
    strong_families = set(domain_profile.get("strongest_job_families") or [])
    order_index = start_index
    suppressed: list[dict] = []
    spec_candidates: list[_Scored] = []
    domain_candidates: list[_Scored] = []

    # Specialization pool: only roles with real evidence may compete, and each supported role gets a
    # small alignment edge -- never a bonus for mere membership.
    for role in spec.roles:
        scored = _score_role(role, primary_domain, True, evidence, strong_families, seniority, order_index, recency_boost=True)
        order_index += 1
        if scored is None:
            continue
        if not scored.has_professional_evidence:
            continue  # membership alone is not enough to take a slot
        scored.confidence = round(min(99.0, scored.confidence + SPEC_ALIGNMENT_BONUS), 1)
        scored.raw_score += SPEC_ALIGNMENT_BONUS
        scored.evidence = [f"specialization: {spec.label}"] + scored.evidence
        spec_candidates.append(scored)

    spec_titles = {_norm_title(item.role.title) for item in spec_candidates}

    # Broad primary-domain roles fill Related/Stretch (and Strong Fit when the specialization is thin),
    # but a role tied to a DIFFERENT vertical is dropped unless the resume directly supports it.
    for role in ROLE_TAXONOMY.get(primary_domain, ()):
        scored = _score_role(role, primary_domain, True, evidence, strong_families, seniority, order_index, recency_boost=True)
        order_index += 1
        if scored is None:
            suppressed.append({"title": role.title, "domain": primary_domain, "reason": "specialised role requires professional responsibility or substantial project evidence"})
            continue
        if _norm_title(role.title) in spec_titles:
            continue  # already offered by the specialization pool
        if role.vertical and role.vertical != spec.vertical and not scored.has_professional_evidence:
            suppressed.append({
                "title": role.title, "domain": primary_domain,
                "reason": f"{role.vertical} vertical role without direct evidence; detected specialization is {spec.label}",
            })
            continue
        domain_candidates.append(scored)

    spec_ranked = _rank(spec_candidates)
    domain_ranked = _rank(domain_candidates)

    # Primary = the single strongest-evidence role overall (a domain role only wins here when it has
    # more direct current evidence than any specialization role -- e.g. an exact current title).
    combined = _rank(spec_ranked + domain_ranked)
    if not combined:
        return [], suppressed, order_index
    primary = combined[0]

    # Strong Fit: prefer SUPPORTED specialization roles; fall back to the strongest broad-domain roles
    # only when fewer than four specialization roles are individually supported.
    strong: list[_Scored] = [item for item in spec_ranked if item is not primary][: PRIMARY_ONLY_SLOTS - 1]
    if len(strong) < PRIMARY_ONLY_SLOTS - 1:
        for item in domain_ranked:
            if item is primary or item in strong:
                continue
            strong.append(item)
            if len(strong) >= PRIMARY_ONLY_SLOTS - 1:
                break

    chosen = [primary] + strong
    rest = _rank([item for item in combined if item not in chosen])
    # Everything beyond `chosen` sits in the broader parent domain rather than the demonstrated
    # specialization. Marking the band keeps that decision intact through selection: a broad
    # "Data Analyst" can never outrank a supported "Clinical Data Analyst" for a healthcare
    # analytics resume, however high its own confidence happens to be.
    for item in rest:
        item.family_rank = 1
    return chosen + rest, suppressed, order_index


def build_role_profile(evidence: DomainEvidence, domain_profile: dict) -> dict:
    """Produce the deterministic top-five role profile from Phase-1 evidence + the Phase-2 domain."""
    primary_domain = domain_profile.get("primary_domain", "")
    seniority = domain_profile.get("seniority", "Mid-level")
    strong_families = set(domain_profile.get("strongest_job_families") or [])
    professional_titles = _professional_titles(evidence)

    # Rare / unsupported-domain fallback. Trigger when the detected domain is not in the taxonomy
    # (Phase-2 "Undetermined"), or when the candidate HAS professional titles but none of them are
    # explained by the detected domain -- a rare profession must never be forced into an unrelated
    # supported domain just because a generic verb matched.
    if primary_domain not in ROLE_TAXONOMY:
        return _provisional_role_profile(evidence, domain_profile, status="unsupported_domain")
    # A detected specialization (e.g. "Forecasting Analyst") explains a title the broad domain's role
    # list does not, so it must be considered BEFORE falling back to the provisional path.
    specialization = detect_specialization(evidence, primary_domain)
    if (
        specialization is None
        and professional_titles
        and not _title_explained_by_domain(professional_titles, primary_domain)
    ):
        return _provisional_role_profile(evidence, domain_profile, status="provisional")

    suppressed: list[dict] = []
    order_index = 0

    def score_domain(domain_label: str, is_primary: bool) -> list[_Scored]:
        nonlocal order_index
        results: list[_Scored] = []
        for role in ROLE_TAXONOMY.get(domain_label, ()):
            scored = _score_role(role, domain_label, is_primary, evidence, strong_families, seniority, order_index)
            order_index += 1
            if scored is None:
                suppressed.append({
                    "title": role.title,
                    "domain": domain_label,
                    "reason": "specialised role requires professional responsibility or substantial project evidence; tools/certifications alone are insufficient",
                })
            else:
                results.append(scored)
        return _rank(results)

    # Specialization / subdomain layer: when a well-supported specialization dominates the broad
    # domain, specialization roles compete for the top slots on their own evidence (unsupported ones
    # never take a slot); otherwise the generic domain ranking is used unchanged.
    if specialization is not None:
        ordered, spec_suppressed, order_index = _score_specialized_pool(
            evidence, domain_profile, primary_domain, specialization, order_index
        )
        suppressed.extend(spec_suppressed)
    else:
        # Primary domain supplies Primary + all Strong Fit (and normally the whole list).
        ordered = score_domain(primary_domain, True)

    # NOTE: there is deliberately no adjacent-domain fallback here. A role from an adjacent domain
    # belongs to a different professional family, and a search plan of at most five roles has no
    # room to spend on one. A narrow resume ends up with fewer than five roles instead -- that is
    # honest, and it is what the selection below reports.

    # Deduplicate near-identical titles, keeping the highest-confidence occurrence.
    unique: list[_Scored] = []
    seen: set[str] = set()
    for item in ordered:
        key = _norm_title(item.role.title)
        if key in seen:
            suppressed.append({"title": item.role.title, "domain": item.domain, "reason": "duplicate of a higher-ranked role"})
            continue
        seen.add(key)
        unique.append(item)

    # Filter, then limit -- never the other way round. Everything the scorer learned about a role
    # (which domain it came from, whether real professional evidence backs it, how far above the
    # candidate's level it sits) decides whether it may be searched at all; only the survivors
    # compete for the four related slots.
    selection = select_roles(
        [
            RoleCandidate(
                title=item.role.title,
                domain=item.domain,
                job_family=item.role.family,
                confidence=item.confidence,
                order_index=index,
                family_rank=item.family_rank,
                in_primary_family=item.is_primary_domain,
                has_supporting_evidence=item.has_supporting_evidence,
                seniority_compatible=item.seniority_penalty <= 0,
                payload=item,
            )
            for index, item in enumerate(unique)
        ]
    )
    top = [candidate.payload for candidate in selection.selected]
    suppressed = selection.rejected + suppressed

    roles: list[dict] = []
    rank = 0
    for category, count in CATEGORY_LAYOUT:
        for _ in range(count):
            if rank >= len(top):
                break
            item = top[rank]
            roles.append({
                "rank": rank + 1,
                "title": item.role.title,
                "category": category,
                "confidence": item.confidence,
                "domain": item.domain,
                "job_family": item.role.family,
                "seniority_fit": item.seniority_fit,
                "evidence": item.evidence,
                "missing_evidence": item.missing_evidence,
                "penalties": item.penalties,
            })
            rank += 1

    return {
        "taxonomy_status": "supported",
        "specialization": specialization.label if specialization is not None else None,
        "primary_role": roles[0]["title"] if roles else None,
        "primary_role_confidence": roles[0]["confidence"] if roles else 0.0,
        "roles": roles,
        # Selection rejections lead the record and it holds more entries than the old top-12
        # layout needed: with only five searched roles, WHY a credible role was not searched is
        # the interesting part of the analysis.
        "suppressed_roles": suppressed[:24],
        "unmatched_terms": [],
        "suggested_taxonomy_additions": [],
    }
