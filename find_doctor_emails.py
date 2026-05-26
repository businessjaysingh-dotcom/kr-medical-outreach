#!/usr/bin/env python3
"""
Doctor Email Finder
Searches DuckDuckGo for each doctor's practice website, extracts contact emails.
Runs on top 25 doctors by proximity to 3291 Harasym Tr.
"""

import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
import random
import logging
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

CSV_PATH = "data/doctors.csv"

BLOCKED_DOMAINS = {
    "cpso.on.ca", "gov.on.ca", "ontario.ca", "canada.ca",
    "oha.com", "mohltc.gov.on.ca", "healthforce.ca",
    "hpco.ca", "cma.ca", "cfpc.ca", "royalcollege.ca",
    "wikipedia.org", "yellowpages.ca", "canada411.ca",
    "healthgrades.com", "ratemds.com", "doximity.com",
    "vitals.com", "zocdoc.com", "mapquest.com",
    "facebook.com", "linkedin.com", "twitter.com",
    "instagram.com", "youtube.com",
}

BLOCKED_EMAIL_DOMAINS = {
    "cpso.on.ca", "gov.on.ca", "ontario.ca", "canada.ca",
    "mohltc.gov.on.ca", "oha.com", "cma.ca", "cfpc.ca",
    "royalcollege.ca", "healthforce.ca", "hpco.ca",
}

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def ddg_search(query: str, max_results: int = 5) -> list[str]:
    """Return up to max_results URLs from DuckDuckGo HTML search."""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query, "kl": "ca-en"}
    try:
        r = SESSION.get(url, params=params, timeout=12)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"DDG search failed for '{query}': {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for a in soup.select("a.result__url"):
        href = a.get("href", "")
        if href.startswith("http"):
            results.append(href)
        if len(results) >= max_results:
            break

    # Fallback: result__a links
    if not results:
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if href.startswith("http"):
                results.append(href)
            if len(results) >= max_results:
                break

    return results


def is_blocked_domain(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)
    except Exception:
        return True


def extract_emails_from_url(url: str) -> list[str]:
    """Fetch page and extract non-blocked emails."""
    try:
        r = SESSION.get(url, timeout=10, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.debug(f"  Fetch failed {url}: {e}")
        return []

    text = r.text
    raw = EMAIL_RE.findall(text)
    seen = set()
    clean = []
    for email in raw:
        email = email.lower().strip(".,;\"'")
        if email in seen:
            continue
        seen.add(email)
        domain = email.split("@")[-1]
        if any(domain == d or domain.endswith("." + d) for d in BLOCKED_EMAIL_DOMAINS):
            continue
        # Skip image/asset file extensions falsely matched
        if re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", domain):
            continue
        clean.append(email)
    return clean


def find_email_for_doctor(name: str, city: str, street: str) -> str | None:
    """
    Try multiple search queries, visit first non-blocked result,
    return the best practice email or None.
    """
    # Strip "Dr." prefix for cleaner name
    clean_name = re.sub(r"^Dr\.?\s*", "", name).strip()
    last_name = clean_name.split()[-1] if clean_name else ""

    queries = [
        f'"{clean_name}" {city} Ontario doctor email contact',
        f'"{clean_name}" physician {city} Ontario "email"',
        f'"{last_name}" {street.split()[0]} {city} clinic contact',
    ]

    for query in queries:
        log.info(f"  Searching: {query}")
        urls = ddg_search(query, max_results=5)
        time.sleep(random.uniform(1.5, 2.5))

        for url in urls:
            if is_blocked_domain(url):
                log.debug(f"  Skipping blocked domain: {url}")
                continue
            log.info(f"  Visiting: {url}")
            emails = extract_emails_from_url(url)
            time.sleep(random.uniform(0.8, 1.5))

            if emails:
                # Prefer emails that contain the last name or look like clinic emails
                preferred = [e for e in emails if last_name.lower() in e]
                chosen = preferred[0] if preferred else emails[0]
                log.info(f"  Found: {chosen}")
                return chosen
            break  # Only try first valid result per query, then next query

    return None


def main():
    df = pd.read_csv(CSV_PATH)

    # Add practice_email column if missing
    if "practice_email" not in df.columns:
        df["practice_email"] = ""

    # Top 25 by proximity
    top25 = df.sort_values("distance_from_listing_km").head(25).copy()

    found = 0
    skipped = 0
    failed = 0

    for idx, row in top25.iterrows():
        name = row["full_name"]
        city = row.get("city", "")
        street = row.get("street", "")
        existing = str(row.get("practice_email", "")).strip()

        if existing and existing not in ("", "nan"):
            log.info(f"[{idx+1}/25] SKIP (already has email): {name}")
            skipped += 1
            continue

        log.info(f"[{idx+1}/25] Searching: {name} — {city}")
        email = find_email_for_doctor(name, city, street)

        if email:
            df.at[idx, "practice_email"] = email
            found += 1
        else:
            df.at[idx, "practice_email"] = ""
            failed += 1

        # Save after every doctor so progress isn't lost
        df.to_csv(CSV_PATH, index=False)
        time.sleep(random.uniform(2.0, 3.5))

    log.info(f"\n{'='*50}")
    log.info(f"DONE — Top 25 doctors processed")
    log.info(f"  Emails found:  {found}")
    log.info(f"  Already had:   {skipped}")
    log.info(f"  Not found:     {failed}")
    log.info(f"{'='*50}")

    # Print summary table
    result_df = df.sort_values("distance_from_listing_km").head(25)[
        ["full_name", "city", "distance_from_listing_km", "practice_email"]
    ]
    print("\n" + result_df.to_string(index=False))
    print(f"\nEmails found: {found} / 25")


if __name__ == "__main__":
    main()
