#!/usr/bin/env python3
"""
Doctor Scraper — Real data from Yellow Pages Canada public listings.
Targets GTA physicians for the 3291 Harasym Tr, Oakville outreach campaign.

CASL COMPLIANCE: Business contact info only, sourced from public directories.
No personal emails or residential data collected.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import json
import os
import logging
from datetime import datetime
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

LISTING_COORDS = (43.4697, -79.7195)   # 3291 Harasym Tr, Oakville
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "data")
os.makedirs(OUTPUT_DIR, exist_ok=True)

YP_BASE = "https://www.yellowpages.ca"

# Search terms × cities × pages
SEARCHES = [
    ("physician", "Oakville+ON",     4),
    ("doctor",    "Oakville+ON",     3),
    ("physician", "Burlington+ON",   3),
    ("doctor",    "Burlington+ON",   2),
    ("physician", "Milton+ON",       2),
    ("physician", "Mississauga+ON",  3),
    ("physician", "Brampton+ON",     2),
    ("physician", "Georgetown+ON",   1),
]

CITY_APPROX_COORDS = {
    "Oakville":     (43.4675, -79.6877),
    "Burlington":   (43.3255, -79.7990),
    "Milton":       (43.5183, -79.8774),
    "Mississauga":  (43.5890, -79.6441),
    "Brampton":     (43.7315, -79.7624),
    "Georgetown":   (43.6529, -79.9289),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}

# YP category tags that mean it's a real individual doctor (not a spa, physio, clinic chain)
DOCTOR_CATEGORIES = {
    "physician", "surgeon", "medical center", "medical", "family medicine",
    "general practice", "specialist", "internist", "cardiologist",
    "dermatologist", "psychiatrist", "pediatrician", "gynecologist",
    "ophthalmologist", "urologist", "neurologist", "oncologist",
    "gastroenterologist", "endocrinologist", "rheumatologist",
    "nephrologist", "anesthesiologist", "radiologist", "orthopedic",
}

# Clinic/non-doctor words that should disqualify a listing
NON_DOCTOR_SKIP = {
    "acupuncture", "tcm", "chiropractic", "chiropractor", "physiotherapy",
    "physio", "massage", "laser", "skincare", "cosmetic", "pharmacy",
    "footcare", "podiatry", "optometry", "optometrist", "dental", "dentist",
    "hearing", "audiolog", "sleep management", "vasectomy clinic",
    "naturopath", "homeopath", "reiki", "therapy associates",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def is_doctor_name(name: str) -> bool:
    """True if the listing name looks like an individual doctor rather than a clinic."""
    n = name.lower()
    # Explicit Dr prefix/suffix
    if re.search(r'\bdr\.?\b', n):
        return True
    # Pattern: "Lastname Firstname Dr" (YP format for individual doctors)
    if n.endswith(" dr"):
        return True
    # Has a recognisable medical suffix
    if re.search(r'\b(m\.?d\.?|m\.b\.b\.s|f\.r\.c\.s|f\.r\.c\.p)\b', n):
        return True
    return False


def is_non_doctor(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in NON_DOCTOR_SKIP)


def parse_yp_name(raw: str) -> tuple[str, str, str]:
    """
    Convert YP name format to (full_name, first_name, last_name).
    Handles:
      'Liske Sabine Dr'     → Dr. Sabine Liske
      'Dr Helen Pyle'       → Dr. Helen Pyle
      'Dr. Yasmine Hussain' → Dr. Yasmine Hussain
      'Khan M Viqar Dr'     → Dr. M Viqar Khan
    """
    name = re.sub(r'^\d+', '', raw).strip()

    # Remove trailing "Dr" / "Dr." suffix
    if re.search(r'\bDr\.?\s*$', name, re.I):
        name = re.sub(r'\bDr\.?\s*$', '', name, flags=re.I).strip()
        parts = name.split()
        if len(parts) >= 2:
            last = parts[0]
            first = " ".join(parts[1:])
        else:
            last = name
            first = ""
        full = f"Dr. {first} {last}".strip()
        return full, first, last

    # Already has Dr. prefix
    if re.match(r'^Dr\.?\s+', name, re.I):
        name = re.sub(r'^Dr\.?\s+', '', name, flags=re.I).strip()
        parts = name.split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        full = f"Dr. {name}"
        return full, first, last

    # Fallback
    parts = name.split()
    first = parts[0] if parts else name
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return f"Dr. {name}", first, last


def extract_city_from_address(address: str) -> str:
    """Pull the city name from a YP address string like '375-2525 Old Bronte Rd, Oakville, L6M 4J2'."""
    parts = [p.strip() for p in address.split(",")]
    for i, part in enumerate(parts):
        # City is typically between street and postal/province
        if i > 0 and not re.match(r'^L\d|^M\d|^ON$', part, re.I):
            if len(part) > 2:
                return part
    return ""


def parse_address_parts(address: str) -> tuple[str, str, str]:
    """Return (street, city, postal) from a YP address string."""
    parts = [p.strip() for p in address.split(",")]
    street = parts[0] if parts else address
    city = ""
    postal = ""
    for part in parts[1:]:
        part = part.strip()
        # ON province — skip
        if part.upper() == "ON":
            continue
        # Postal code pattern: L6M 4J2
        if re.match(r'^[A-Z]\d[A-Z]\s*\d[A-Z]\d$', part, re.I):
            postal = part
        else:
            if not city and len(part) > 2:
                city = part
    return street, city, postal


def scrape_yp_listing_page(session: requests.Session, term: str, location: str, page: int) -> list[dict]:
    """Fetch one YP search results page and return raw listing dicts."""
    url = f"{YP_BASE}/search/si/{page}/{requests.utils.quote(term)}/{location}"
    log.info(f"  GET {url}")
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"  Failed: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    listings = soup.select("div.listing")
    results = []

    for listing in listings:
        # Name
        name_el = listing.select_one("a.listing__name--link, .listing__name, [itemprop=name]")
        if not name_el:
            continue
        raw_name = re.sub(r'^\d+', '', name_el.get_text(strip=True)).strip()

        # Skip if clearly not a doctor or is a non-doctor business
        if not is_doctor_name(raw_name) and "medical" not in raw_name.lower():
            continue
        if is_non_doctor(raw_name):
            continue

        # Address
        addr_parts = []
        for el in listing.select("[itemprop=streetAddress], [itemprop=addressLocality], [itemprop=postalCode]"):
            addr_parts.append(el.get_text(strip=True))
        address_raw = ", ".join(addr_parts) if addr_parts else ""
        if not address_raw:
            addr_el = listing.select_one(".listing__address--full, .address")
            address_raw = addr_el.get_text(strip=True) if addr_el else ""
        address_raw = re.sub(r'Get directions.*$', '', address_raw, flags=re.I).strip()

        # Phone (from text — YP injects it into the DOM)
        phone_match = re.search(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', listing.get_text())
        phone = phone_match.group().strip() if phone_match else ""

        # Detail page URL
        url_el = listing.select_one("a[href*='/bus/']")
        detail_path = url_el.get("href", "").split("?")[0] if url_el else ""

        results.append({
            "raw_name": raw_name,
            "address_raw": address_raw,
            "phone": phone,
            "detail_path": detail_path,
        })

    log.info(f"  → {len(results)} doctor candidates on page {page}")
    return results


def scrape_yp_detail(session: requests.Session, path: str) -> dict:
    """
    Fetch the individual YP listing page to extract specialty, website, email.
    Returns partial dict to merge into the main record.
    """
    if not path:
        return {}
    url = YP_BASE + path
    try:
        time.sleep(1.2)
        r = session.get(url, timeout=12)
        r.raise_for_status()
    except Exception as e:
        log.debug(f"  Detail fetch failed {path}: {e}")
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    # Specialty from "Products and Services" section
    specialty = ""
    prod_match = re.search(
        r'Products and Services\s+([\w\s,&/]+?)(?:\s+Location|\s+View map|\s+Details)',
        text, re.I
    )
    if prod_match:
        raw_cats = prod_match.group(1).strip()
        # Normalise categories to a known specialty
        specialty = normalise_specialty(raw_cats)

    # Website
    website = ""
    website_match = re.search(r'Website\s+([\w.\-]+\.[a-z]{2,})', text, re.I)
    if website_match:
        site = website_match.group(1).strip()
        # Filter out government/aggregator sites
        if not any(b in site for b in ["gov.on.ca", "yellowpages", "canada411", "pages"]):
            website = "https://" + site if not site.startswith("http") else site

    # Email (rarely present on YP, but try)
    email_match = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
    practice_email = ""
    if email_match:
        candidate = email_match.group()
        blocked = {"cpso", "yellowpages", "gov.on.ca", "example", "noreply"}
        if not any(b in candidate.lower() for b in blocked):
            practice_email = candidate

    return {
        "specialty": specialty,
        "website": website,
        "practice_email": practice_email,
    }


SPECIALTY_MAP = {
    "surgeon": "Surgery",
    "surgery": "Surgery",
    "orthopedic": "Orthopedic Surgery",
    "family medicine": "Family Medicine",
    "general practice": "Family Medicine",
    "general practitioner": "Family Medicine",
    "internal medicine": "Internal Medicine",
    "internist": "Internal Medicine",
    "cardiol": "Cardiology",
    "dermatol": "Dermatology",
    "psychiatr": "Psychiatry",
    "pediatr": "Pediatrics",
    "paediatr": "Pediatrics",
    "gynecol": "OB/GYN",
    "obstetr": "OB/GYN",
    "ophthalmol": "Ophthalmology",
    "urolog": "Urology",
    "neurolog": "Neurology",
    "oncolog": "Oncology",
    "gastroenterol": "Gastroenterology",
    "endocrinol": "Endocrinology",
    "rheumatol": "Rheumatology",
    "nephrol": "Nephrology",
    "anesthesiol": "Anesthesiology",
    "radiolog": "Radiology",
    "emergency": "Emergency Medicine",
    "plastic": "Plastic Surgery",
    "vascular": "Vascular Surgery",
    "neurosurger": "Neurosurgery",
    "allerg": "Allergy & Immunology",
    "endoscop": "Gastroenterology",
    "vasectomy": "Urology",
    "sleep": "Sleep Medicine",
    "sports": "Sports Medicine",
    "cosmetic": "Plastic Surgery",
    "medical center": "General Practice",
    "physician": "General Practice",
    "physicians": "General Practice",
}


def normalise_specialty(raw: str) -> str:
    r = raw.lower()
    for keyword, mapped in SPECIALTY_MAP.items():
        if keyword in r:
            return mapped
    return "General Practice"


def geocode_address(geolocator, address: str, city: str) -> tuple[float, float]:
    """Return (lat, lon) using Nominatim, fallback to city centroid."""
    fallback = CITY_APPROX_COORDS.get(city, (43.4675, -79.6877))
    try:
        loc = geolocator.geocode(address + ", Ontario, Canada", timeout=10)
        if loc:
            return round(loc.latitude, 6), round(loc.longitude, 6)
    except Exception as e:
        log.debug(f"Geocode failed for {address!r}: {e}")
    return fallback


def calc_distance(lat: float, lon: float) -> float:
    return round(geodesic(LISTING_COORDS, (lat, lon)).km, 1)


def hospital_for_city(city: str) -> str:
    MAP = {
        "Oakville":     "Oakville Trafalgar Memorial Hospital",
        "Burlington":   "Joseph Brant Hospital",
        "Milton":       "Milton District Hospital",
        "Mississauga":  "Trillium Health Partners",
        "Brampton":     "William Osler Health System - Brampton Civic",
        "Georgetown":   "Halton Healthcare - Georgetown",
    }
    return MAP.get(city, "")


def main():
    session = make_session()
    geolocator = Nominatim(user_agent="medical_outreach_scraper_v2")

    seen_keys: set[str] = set()
    raw_records: list[dict] = []

    # ── Phase 1: collect all listing cards ──────────────────────
    for term, location, max_pages in SEARCHES:
        city_name = location.replace("+ON", "")
        log.info(f"Searching '{term}' in {city_name} ({max_pages} pages)...")
        for page in range(1, max_pages + 1):
            batch = scrape_yp_listing_page(session, term, location, page)
            for rec in batch:
                key = (rec["raw_name"].lower(), rec["address_raw"].lower()[:30])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                rec["city_hint"] = city_name
                raw_records.append(rec)
            time.sleep(2.0)

    log.info(f"\nCollected {len(raw_records)} unique doctor candidates. Enriching detail pages...")

    # ── Phase 2: enrich via detail pages + geocoding ─────────────
    doctors = []
    for i, rec in enumerate(raw_records):
        log.info(f"[{i+1}/{len(raw_records)}] {rec['raw_name']}")

        full_name, first_name, last_name = parse_yp_name(rec["raw_name"])
        street, city, postal = parse_address_parts(rec["address_raw"])
        if not city:
            city = rec.get("city_hint", "Oakville")

        # Fetch detail page (specialty, website, email)
        detail = scrape_yp_detail(session, rec.get("detail_path", ""))

        # Geocode
        address_str = f"{street}, {city}, ON {postal}".strip(", ")
        lat, lon = geocode_address(geolocator, address_str, city)
        distance = calc_distance(lat, lon)
        time.sleep(1.1)  # Nominatim rate limit

        doctors.append({
            "full_name":               full_name,
            "first_name":              first_name,
            "last_name":               last_name,
            "specialty":               detail.get("specialty") or "General Practice",
            "practice_address":        f"{street}, {city}, ON {postal}".strip(", "),
            "street":                  street,
            "city":                    city,
            "province":                "ON",
            "postal_code":             postal,
            "practice_phone":          rec["phone"],
            "hospital_affiliation":    hospital_for_city(city),
            "registration_year":       "",
            "latitude":                lat,
            "longitude":               lon,
            "distance_from_listing_km": distance,
            "practice_email":          detail.get("practice_email", ""),
            "website":                 detail.get("website", ""),
            "source":                  "Yellow Pages Canada (Public)",
            "scraped_date":            datetime.now().strftime("%Y-%m-%d"),
            "outreach_status":         "not_contacted",
            "notes":                   "",
        })

    log.info(f"\nScraped {len(doctors)} doctors total.")

    # ── Phase 3: sort, deduplicate, save ────────────────────────
    df = pd.DataFrame(doctors)
    df = df.sort_values("distance_from_listing_km").reset_index(drop=True)

    # CSV
    csv_path = os.path.join(OUTPUT_DIR, "doctors.csv")
    df.to_csv(csv_path, index=False)
    log.info(f"Saved CSV → {csv_path}")

    # JS data file for the dashboard
    records = df.to_dict(orient="records")
    js_content = (
        f"// Auto-generated by scrape_doctors.py — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"// Source: Yellow Pages Canada public listings\n"
        f"// CASL: Business contact only — public directory\n\n"
        f"window.DOCTORS_DATA = {json.dumps(records, indent=2)};\n"
    )
    js_path = os.path.join(OUTPUT_DIR, "doctors.js")
    with open(js_path, "w") as f:
        f.write(js_content)
    log.info(f"Saved JS   → {js_path}")

    # Summary
    print("\n" + "=" * 60)
    print(f"TOTAL DOCTORS:       {len(df)}")
    print(f"Within  5km:         {len(df[df.distance_from_listing_km <= 5])}")
    print(f"Within 10km:         {len(df[df.distance_from_listing_km <= 10])}")
    print(f"Within 25km:         {len(df[df.distance_from_listing_km <= 25])}")
    print(f"With phone:          {df['practice_phone'].astype(bool).sum()}")
    print(f"With website:        {df['website'].astype(bool).sum()}")
    print(f"With practice email: {df['practice_email'].astype(bool).sum()}")
    print("\nBy city:")
    print(df["city"].value_counts().to_string())
    print("\nTop specialties:")
    print(df["specialty"].value_counts().head(8).to_string())
    print("\nClosest 10 to listing:")
    print(df[["full_name", "specialty", "city", "distance_from_listing_km", "practice_phone"]]
          .head(10).to_string(index=False))
    print("=" * 60)


if __name__ == "__main__":
    main()
