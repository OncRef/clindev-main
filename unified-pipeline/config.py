"""
Unified Pipeline — Configuration

Shared config for the conditions + drugs pipeline.
Designed so local runs today and a future GCP deployment read from the
same place (env vars override file paths, etc.).
"""

import os
from dotenv import load_dotenv

DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(DIR, ".env"))

# === API Keys ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CT_API = "https://clinicaltrials.gov/api/v2/studies"

# === Paths ===
DICT_DIR = os.path.join(DIR, "dictionaries")
OUTPUT_DIR = os.path.join(DIR, "output")
LOG_DIR = os.path.join(DIR, "logs")
SEARCH_TERMS_FILE = os.path.join(DIR, "Search-Terms-for-Cancer.csv")

DRUG_MASTER_FILE = os.path.join(DICT_DIR, "drug_master.json")
CONQUEST_MASTER_FILE = os.path.join(OUTPUT_DIR, "conquest_master.json")

# Condition dictionaries
LINE_OF_THERAPY_FILE = os.path.join(DICT_DIR, "line_of_therapy.json")
STAGE_FILE = os.path.join(DICT_DIR, "stage.json")
GENES_FILE = os.path.join(DICT_DIR, "genes.csv")
EXTRACTED_CANCER_FILE = os.path.join(DICT_DIR, "extracted_cancer_to_broad.json")
ONCOTREE_MAPPING_FILE = os.path.join(DICT_DIR, "oncotree_to_broad_cancer_mapping.csv")
BROAD_CANCER_MAPPING_FILE = os.path.join(DICT_DIR, "broad_cancer_mapping.csv")
LLM_CONDITIONS_CACHE_FILE = os.path.join(DICT_DIR, "llm_classified_conditions.json")

# === CT.gov Fetch Settings ===
RECRUITING_STATUSES = [
    "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING", "RECRUITING",
    "AVAILABLE", "APPROVED_FOR_MARKETING", "ACTIVE_NOT_RECRUITING",
]
COMPLETED_STATUSES = ["COMPLETED"]
OTHER_STATUSES = ["SUSPENDED", "TERMINATED", "WITHDRAWN", "UNKNOWN"]
INTERVENTION_QUERY = "drug OR biological OR combination product"
FIELDS = (
    "NCTId,BriefTitle,OfficialTitle,Condition,OverallStatus,"
    "Phase,StudyType,EnrollmentCount,StartDate,CompletionDate,"
    "LeadSponsorName,LeadSponsorClass,BriefSummary,"
    "InterventionName,InterventionType"
)

# === Drug Classification ===
CANCER_TREATMENT_CLASSES = {
    "chemotherapy", "radiation_therapy", "hormone_therapy", "targeted_therapy",
    "immunotherapy", "stem_cell_transplant", "photodynamic_therapy", "hyperthermia",
    "gene_therapy", "radioligand_therapy", "ablation_therapy", "nanomedicine",
    "oncolytic_virus_therapy", "cancer_vaccine", "other",
}
EXCLUDED_CLASSES = {"imaging_agent", "supportive_care", "not_cancer_related"}
ALL_DRUG_CLASSES = CANCER_TREATMENT_CLASSES | EXCLUDED_CLASSES

# === Parallelism ===
# Conditions + drugs run on separate threads against the same fetched trials.
PARALLEL_WORKERS = 2
