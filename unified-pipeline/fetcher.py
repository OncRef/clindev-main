"""
CT.gov Trial Fetcher (shared by conditions + drugs)

Single fetch → fan out to both processors. Supports either:
  - relative window: fetch_trials(days=N)              last N days
  - absolute window: fetch_trials(start_date, end_date) YYYY-MM-DD strings

The absolute form is used for reproducible test runs (e.g. 2026-04-01 → 2026-04-15).
"""

import csv
import time
import requests
from datetime import date, datetime, timedelta

from config import (
    CT_API, SEARCH_TERMS_FILE, FIELDS,
    RECRUITING_STATUSES, COMPLETED_STATUSES, OTHER_STATUSES,
    INTERVENTION_QUERY,
)


def load_search_terms():
    terms = []
    with open(SEARCH_TERMS_FILE, "r", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if row:
                t = row[0].strip().strip('"').strip(",").strip()
                if t and t.lower() != "search terms to give anything cancer related":
                    terms.append(t)
    return terms


def flatten_study(study):
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status = proto.get("statusModule", {})
    sponsor_mod = proto.get("sponsorCollaboratorsModule", {})
    desc = proto.get("descriptionModule", {})
    design = proto.get("designModule", {})
    conds = proto.get("conditionsModule", {})
    arms = proto.get("armsInterventionsModule", {})
    interventions = arms.get("interventions", [])
    lead = sponsor_mod.get("leadSponsor", {})

    return {
        "nct_id": ident.get("nctId"),
        "brief_title": ident.get("briefTitle"),
        "official_title": ident.get("officialTitle"),
        "conditions": conds.get("conditions", []),
        "keywords": conds.get("keywords", []),
        "overall_status": status.get("overallStatus"),
        "phase": design.get("phases", []),
        "study_type": design.get("studyType"),
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
        "start_date": status.get("startDateStruct", {}).get("date"),
        "completion_date": status.get("completionDateStruct", {}).get("date"),
        "lead_sponsor": lead.get("name"),
        "sponsor_class": lead.get("class", "UNKNOWN"),
        "brief_summary": desc.get("briefSummary"),
        "intervention_names": [i.get("name", "") for i in interventions],
        "intervention_types": [i.get("type", "") for i in interventions],
    }


def _resolve_window(days, start_date, end_date):
    """Normalize (days | start_date, end_date) into three ISO dates.

    Returns (update_start, update_end, completion_two_year_floor, completion_one_year_floor).
    """
    if start_date and end_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        end = date.today()
        start = end - timedelta(days=days)
    two_yr = end - timedelta(days=730)
    one_yr = end - timedelta(days=365)
    return start.isoformat(), end.isoformat(), two_yr.isoformat(), one_yr.isoformat()


def fetch_trials(days=7, start_date=None, end_date=None):
    """Fetch new/updated trials from CT.gov.

    Absolute mode (start_date + end_date) is used for reproducible test runs.
    Relative mode (days) is used for recurring production pulls.

    Returns dict {nct_id: flat_trial_dict}.
    """
    terms = load_search_terms()
    update_start, update_end, two_yr, one_yr = _resolve_window(days, start_date, end_date)

    print(f"Fetching trials with LastUpdatePostDate in [{update_start}, {update_end}]...")
    all_studies = {}

    for i, term in enumerate(terms, 1):
        if i % 10 == 0 or i == 1:
            print(f"  [{i}/{len(terms)}] '{term}'...", flush=True)

        param_sets = [
            {"query.cond": term, "query.intr": INTERVENTION_QUERY,
             "filter.overallStatus": ",".join(RECRUITING_STATUSES),
             "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{update_start},{update_end}] AND AREA[StudyType]INTERVENTIONAL",
             "pageSize": 1000, "format": "json", "fields": FIELDS},
            {"query.cond": term, "query.intr": INTERVENTION_QUERY,
             "filter.overallStatus": ",".join(COMPLETED_STATUSES),
             "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{update_start},{update_end}] AND AREA[CompletionDate]RANGE[{two_yr},MAX] AND AREA[StudyType]INTERVENTIONAL",
             "pageSize": 1000, "format": "json", "fields": FIELDS},
            {"query.cond": term, "query.intr": INTERVENTION_QUERY,
             "filter.overallStatus": ",".join(OTHER_STATUSES),
             "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{update_start},{update_end}] AND AREA[CompletionDate]RANGE[{one_yr},MAX] AND AREA[StudyType]INTERVENTIONAL",
             "pageSize": 1000, "format": "json", "fields": FIELDS},
        ]

        for params in param_sets:
            while True:
                try:
                    resp = requests.get(CT_API, params=params, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    print(f"    ERROR: {e}")
                    break
                for study in data.get("studies", []):
                    flat = flatten_study(study)
                    if flat and flat["nct_id"] and flat["nct_id"] not in all_studies:
                        all_studies[flat["nct_id"]] = flat
                if not data.get("nextPageToken"):
                    break
                params["pageToken"] = data["nextPageToken"]
                time.sleep(0.2)
        time.sleep(0.15)

    print(f"  Fetched: {len(all_studies)} trials")
    return all_studies
