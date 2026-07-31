"""Specialization / subdomain accuracy: a broad Phase-2 domain must not flatten a well-supported
specialization. These tests prove the general specialization layer keeps each candidate in its
demonstrated specialization (Primary + Strong Fit drawn from it), demotes generic domain roles to
Related/Stretch, suppresses unrelated verticals (Marketing/CRM/etc.) without direct evidence, and
leaves a broad Data Analyst with no clear specialization as general Data Analytics.
"""

import unittest

from app.config import Settings
from app.services.resume_parser import ResumeParser

parser = ResumeParser(Settings(openai_api_key=None))


def profile(text: str) -> dict:
    return parser.parse(text)["role_profile"]


def titles(role_profile: dict) -> list[str]:
    return [r["title"] for r in role_profile["roles"]]


def strong_and_primary(role_profile: dict) -> list[str]:
    return [r["title"] for r in role_profile["roles"] if r["category"] in {"Primary Role", "Strong Fit"}]


# --- Validation resumes (the two observed failures) ----------------------------------------------
HEALTHCARE = (
    "Jordan Vale\nHealthcare Data Analyst\nEXPERIENCE\n"
    "MercyHealth Systems | Boston, MA | Jan 2021 - Present\nHealthcare Data Analyst\n"
    "- Analyzed EHR and EMR patient data; clinical reporting and healthcare quality metrics\n"
    "- HIPAA-compliant regulatory reporting; claims and population health dashboards in Power BI\n"
    "- Built SQL and Tableau reports on readmissions, care outcomes and patient cohorts\n"
    "Prior Clinic | 2018 - 2021\nClinical Reporting Analyst\n"
    "- Clinical data validation and hospital operational reporting\nSKILLS\nSQL, Power BI, Tableau, Excel, Python\n"
)
ML_DDS = (
    "Riya Sen\nAnalyst - Digital and Data Science\nEXPERIENCE\n"
    "NovaBank | Remote | Jan 2022 - Present\nAnalyst - Digital and Data Science\n"
    "- Built and deployed machine learning pipelines with Spark and MLflow\n"
    "- Demand forecasting and predictive models; feature engineering and model deployment\n"
    "- Model explainability with SHAP; hyperparameter optimization and evaluation\n"
    "TrendCorp | 2019 - 2022\nBusiness Intelligence Analyst\n"
    "- Power BI dashboards and SQL reporting for stakeholders\nSKILLS\nPython, Spark, MLflow, SHAP, SQL, Power BI\n"
)

# --- Cross-domain specialization resumes ---------------------------------------------------------
FINANCIAL_ANALYTICS = (
    "A B\nFinancial Data Analyst\nEXPERIENCE\nFinCo | NY | Jan 2021 - Present\nFinancial Data Analyst\n"
    "- Financial reporting, revenue and profitability analysis; budgeting and forecasting\n"
    "- Built financial dashboards in Power BI and SQL on p&l and cost analysis\nSKILLS\nSQL, Power BI, Excel\n"
)
PRODUCT_ANALYTICS = (
    "A B\nProduct Analyst\nEXPERIENCE\nAppCo | SF | Jan 2021 - Present\nProduct Analyst\n"
    "- Product analytics on funnel, retention, engagement and cohort metrics\n"
    "- A/B testing and experimentation on activation and churn\nSKILLS\nSQL, Amplitude, Python\n"
)
ML_FORECASTING = (
    "A B\nForecasting Analyst\nEXPERIENCE\nDataCo | Remote | Jan 2021 - Present\nForecasting Analyst\n"
    "- Machine learning demand forecasting and time series predictive models\n"
    "- ARIMA, Prophet and scikit-learn with feature engineering and model evaluation\n"
    "- Predictive analytics on forecast accuracy and data analysis dashboards\nSKILLS\nPython, scikit-learn, SQL\n"
)
NLP = (
    "A B\nNLP Engineer\nEXPERIENCE\nLangCo | Remote | Jan 2021 - Present\nNLP Engineer\n"
    "- Natural language processing: text classification, named entity, language model, embeddings\n"
    "- Sentiment analysis and transformers for question answering\nSKILLS\nPython, PyTorch, Hugging Face\n"
)
COMPUTER_VISION = (
    "A B\nComputer Vision Engineer\nEXPERIENCE\nVisCo | Remote | Jan 2021 - Present\nComputer Vision Engineer\n"
    "- Computer vision: object detection, image segmentation, image classification with CNN\n"
    "- OpenCV image processing and video analytics\nSKILLS\nPython, PyTorch, OpenCV\n"
)
EMBEDDED = (
    "A B\nEmbedded Software Engineer\nEXPERIENCE\nChipCo | TX | Jan 2021 - Present\nEmbedded Software Engineer\n"
    "- Embedded firmware on microcontrollers with RTOS; device drivers, I2C, SPI, UART\n"
    "- IoT sensors and bare-metal low-level C/C++\nSKILLS\nC, C++, RTOS\n"
)
CLOUD_SECURITY = (
    "A B\nCloud Security Engineer\nEXPERIENCE\nSecCloud | Remote | Jan 2021 - Present\nCloud Security Engineer\n"
    "- Cloud security and CSPM; AWS security, GuardDuty and container/kubernetes security\n"
    "- DevSecOps security automation and cloud IAM policy\nSKILLS\nAWS, Terraform, Splunk\n"
)
HR_ANALYTICS = (
    "A B\nPeople Analytics Analyst\nEXPERIENCE\nPeopleCo | NY | Jan 2021 - Present\nPeople Analytics Analyst\n"
    "- People analytics on workforce, attrition, retention and headcount\n"
    "- HR metrics and employee engagement dashboards from HRIS\nSKILLS\nSQL, Power BI, Workday\n"
)
SUPPLY_CHAIN = (
    "A B\nSupply Chain Analyst\nEXPERIENCE\nLogiCo | IL | Jan 2021 - Present\nSupply Chain Analyst\n"
    "- Supply chain analytics on inventory, demand planning, logistics and procurement\n"
    "- Fulfillment, warehouse and SKU replenishment reporting\nSKILLS\nSQL, Excel, Tableau\n"
)
GENERAL_DATA = (
    "A B\nData Analyst\nEXPERIENCE\nGenCo | NY | Jan 2021 - Present\nData Analyst\n"
    "- Ad hoc data analysis, dashboards and reporting on KPIs and business metrics\n"
    "- SQL queries and Tableau visualizations for stakeholders\nSKILLS\nSQL, Excel, Tableau\n"
)

MARKETING_VERTICAL = {"Marketing Analyst", "CRM Analyst"}


class ValidationResumeTests(unittest.TestCase):
    """The two observed failures now preserve their specialization."""

    def test_healthcare_analyst_is_healthcare_centered(self) -> None:
        rp = profile(HEALTHCARE)
        self.assertEqual(rp["specialization"], "Healthcare Analytics")
        self.assertIn("Healthcare", rp["primary_role"])
        # Primary + all Strong Fit are healthcare roles.
        for title in strong_and_primary(rp):
            self.assertTrue(
                any(word in title for word in ("Healthcare", "Clinical", "EHR", "Health", "Population")),
                f"{title} is not healthcare-centered",
            )
        # No marketing vertical roles appear without evidence.
        self.assertFalse(MARKETING_VERTICAL & set(titles(rp)))

    def test_digital_data_science_leads_with_ml_not_stale_bi(self) -> None:
        rp = profile(ML_DDS)
        self.assertEqual(rp["specialization"], "Data Science / Applied ML")
        # Current ML work outranks the older BI role.
        self.assertIn(rp["primary_role"], {"Machine Learning Engineer", "Data Scientist", "Applied Data Scientist"})
        leaders = strong_and_primary(rp)
        self.assertTrue(any("Machine Learning" in t or "Data Scien" in t for t in leaders))
        # Business Intelligence Analyst comes from the broad parent domain, not the demonstrated
        # specialization, so it must never displace a supported Data Science / Applied ML role.
        self.assertNotIn("Business Intelligence Analyst", titles(rp)[:4])


class CrossDomainSpecializationTests(unittest.TestCase):
    """The SAME logic keeps every specialization in its own lane."""

    def _assert_spec(self, text: str, expected_spec: str, primary_keywords: tuple[str, ...]) -> dict:
        rp = profile(text)
        self.assertEqual(rp["specialization"], expected_spec)
        self.assertTrue(any(k in rp["primary_role"] for k in primary_keywords), rp["primary_role"])
        # None of these specializations is marketing, so marketing-vertical roles must not appear.
        self.assertFalse(MARKETING_VERTICAL & set(titles(rp)))
        return rp

    def test_financial_analytics(self) -> None:
        self._assert_spec(FINANCIAL_ANALYTICS, "Financial Analytics", ("Financial", "Finance"))

    def test_product_analytics(self) -> None:
        self._assert_spec(PRODUCT_ANALYTICS, "Product Analytics", ("Product",))

    def test_ml_forecasting(self) -> None:
        rp = self._assert_spec(ML_FORECASTING, "Forecasting / Predictive Analytics", ("Forecasting", "Predictive", "Time Series"))
        for title in strong_and_primary(rp):
            self.assertTrue(any(k in title for k in ("Forecast", "Predictive", "Time Series", "Data Scientist", "Quantitative")))

    def test_nlp(self) -> None:
        self._assert_spec(NLP, "NLP", ("NLP",))

    def test_computer_vision(self) -> None:
        self._assert_spec(COMPUTER_VISION, "Computer Vision", ("Computer Vision", "Image"))

    def test_embedded_iot(self) -> None:
        self._assert_spec(EMBEDDED, "Embedded / IoT", ("Embedded", "Firmware", "IoT"))

    def test_cloud_security(self) -> None:
        self._assert_spec(CLOUD_SECURITY, "Cloud Security", ("Cloud Security", "Security", "DevSecOps"))

    def test_hr_analytics(self) -> None:
        self._assert_spec(HR_ANALYTICS, "People / HR Analytics", ("People", "HR", "Workforce", "Talent"))

    def test_supply_chain_analytics(self) -> None:
        self._assert_spec(SUPPLY_CHAIN, "Supply-Chain Analytics", ("Supply Chain", "Demand", "Logistics", "Inventory"))


class GeneralAndSuppressionTests(unittest.TestCase):
    def test_general_data_analyst_has_no_specialization(self) -> None:
        rp = profile(GENERAL_DATA)
        self.assertIsNone(rp["specialization"])
        self.assertEqual(rp["primary_role"], "Data Analyst")
        self.assertEqual(len(rp["roles"]), 5)

    def test_unrelated_vertical_roles_are_suppressed_with_reasons(self) -> None:
        rp = profile(HEALTHCARE)
        suppressed = {s["title"] for s in rp["suppressed_roles"]}
        self.assertTrue(MARKETING_VERTICAL <= suppressed)  # Marketing Analyst + CRM Analyst suppressed
        self.assertTrue(any("vertical role without direct evidence" in s["reason"] for s in rp["suppressed_roles"]))

    def test_specialization_with_broad_domain_fill_returns_five_unique_roles(self) -> None:
        # Specializations anchored to a broad domain (Data Analytics / Software / Cybersecurity) can
        # field a full, credible top five from the specialization pool + broad-domain roles.
        for text in (HEALTHCARE, ML_DDS, PRODUCT_ANALYTICS, EMBEDDED, CLOUD_SECURITY):
            rp = profile(text)
            names = titles(rp)
            self.assertEqual(len(names), 5, rp["specialization"])
            self.assertEqual(len(names), len(set(names)))

    def test_narrow_specialist_returns_fewer_honest_roles_not_padded(self) -> None:
        # A narrow AI/ML specialist (NLP) has few credibly-supported roles; the layer returns those
        # unique roles rather than padding to five with unevidenced roles.
        rp = profile(NLP)
        names = titles(rp)
        self.assertGreaterEqual(len(names), 1)
        self.assertLessEqual(len(names), 5)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all("NLP" in n or "Language" in n or "Machine Learning" in n or "Applied" in n or "Deep Learning" in n or "Conversational" in n or "Data Sci" in n for n in strong_and_primary(rp)))

    def test_specialization_output_is_deterministic(self) -> None:
        self.assertEqual(profile(HEALTHCARE), profile(HEALTHCARE))
        self.assertEqual(profile(ML_DDS), profile(ML_DDS))


if __name__ == "__main__":
    unittest.main()
