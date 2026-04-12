"""HTTP layer: sessions, CSRF, form submission, HTML parsing, PDF download."""
from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Callable

import requests
from bs4 import BeautifulSoup

from .constants import (
    BASE,
    FORM_URL,
    HTTP_TIMEOUT,
    PDF_TIMEOUT,
    USER_AGENT,
)

# HTTP status codes that almost certainly indicate the tribunal is throttling
# us rather than a real error. We back off exponentially on these.
RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}
_BACKOFF_BASE_S = 1.5
_BACKOFF_MAX_S = 60.0
_BACKOFF_MAX_ATTEMPTS = 6


def _with_backoff(send: Callable[[], requests.Response]) -> requests.Response:
    """Call `send` and retry with exponential backoff + jitter on transient
    HTTP failures (rate-limit style statuses, read timeouts). Other
    exceptions propagate so the outer pipeline retry layer can handle them."""
    delay = _BACKOFF_BASE_S
    last_exc: Exception | None = None
    for attempt in range(1, _BACKOFF_MAX_ATTEMPTS + 1):
        try:
            r = send()
        except requests.exceptions.Timeout as e:
            last_exc = e
        except requests.exceptions.ConnectionError as e:
            last_exc = e
        else:
            if r.status_code in RETRYABLE_STATUSES:
                last_exc = requests.HTTPError(
                    f"{r.status_code} {r.reason}", response=r
                )
            else:
                return r
        if attempt >= _BACKOFF_MAX_ATTEMPTS:
            break
        # exponential backoff with jitter: 1.5s, 3s, 6s, 12s, 24s, 48s (+/- 20%)
        jitter = delay * 0.2 * (random.random() - 0.5) * 2
        time.sleep(min(_BACKOFF_MAX_S, delay + jitter))
        delay = min(_BACKOFF_MAX_S, delay * 2)
    assert last_exc is not None
    raise last_exc


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def fetch_csrftkn(session: requests.Session) -> str:
    r = _with_backoff(lambda: session.get(FORM_URL, timeout=HTTP_TIMEOUT))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    el = soup.find(id="csrftkn1")
    if not el or not el.get("value"):
        raise RuntimeError("csrftkn1 not found on case-status page")
    return el["value"]


def submit_search(
    session: requests.Session,
    csrf: str,
    captcha: str,
    bench_code: str,
    app_type: str,
    app_number: int,
    year: int,
) -> requests.Response:
    data = {
        "hp": "",
        "csrftkn": csrf,
        "c1": captcha,
        "bench_name_1": bench_code,
        "app_type_1": app_type,
        "app_number": str(app_number),
        "app_year_1": str(year),
        "bt1": "true",
    }
    r = _with_backoff(
        lambda: session.post(
            FORM_URL, data=data, allow_redirects=True, timeout=HTTP_TIMEOUT
        )
    )
    r.raise_for_status()
    return r


def extract_casedetails_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        if "casedetails" in a["href"]:
            href = a["href"]
            if not href.startswith("http"):
                href = BASE + href if href.startswith("/") else f"{BASE}/{href}"
            links.append(href)
    return links


def extract_pdf_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        if ".pdf" in a["href"].lower():
            href = a["href"]
            if not href.startswith("http"):
                href = BASE + href if href.startswith("/") else f"{BASE}/{href}"
            links.append(href)
    return links


def extract_case_info(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    info: dict[str, str] = {}
    table = soup.find("table")
    if table:
        info["headline"] = table.get_text(" ", strip=True)
        first_row = table.find("tr")
        if first_row:
            tds = first_row.find_all("td")
            if len(tds) >= 2:
                info["parties"] = tds[-1].get_text(" ", strip=True)
    text = soup.get_text(" ", strip=True)
    for key, pat in [
        ("appeal_number", r"Appeal Number:\s*([A-Z]+\s*\d+/[A-Z]+/\d+)"),
        ("filed_on", r"Filed On:\s*([0-9A-Za-z\-]+)"),
        ("assessment_year", r"Assessment Year:\s*([0-9\-]+)"),
        ("bench_alloted", r"Bench Alloted:\s*([A-Z0-9]+)"),
        ("case_status", r"Case Status:\s*([A-Za-z]+)"),
    ]:
        m = re.search(pat, text)
        if m:
            info[key] = m.group(1).strip()
    return info


def no_records(html: str) -> bool:
    lowered = html.lower()
    return "no records found" in lowered or "no record found" in lowered


def download_pdf(session: requests.Session, url: str, out_path: Path) -> int:
    r = session.get(url, stream=True, timeout=PDF_TIMEOUT)
    r.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(16384):
            f.write(chunk)
            total += len(chunk)
    return total
