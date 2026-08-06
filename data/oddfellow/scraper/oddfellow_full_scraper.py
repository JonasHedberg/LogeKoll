#!/usr/bin/env python3
"""
Samlad Odd Fellow-scraper med verifieringsskonsam hämtning.

En körning kan:
  1. iterera b1-b200, r1-r150, bl1-bl30 och rl1-rl30,
  2. hämta institutionsuppgifter,
  3. hämta möteskalendrar,
  4. skriva UTF-8-kodad JSON och JSONL,
  5. återuppta från cache och kontrollpunkt,
  6. vid omkörning inom 12 timmar endast återförsöka tidigare misslyckade poster,
  7. radera möten före dagens datum vid varje start.

Standardprofilen minimerar risken att webbplatsens verifieringssida aktiveras:
- institutions- och kalenderhämtning delar samma långlivade session,
- en gemensam, konservativ anropskö används för båda sidtyperna,
- högst 24 sidnavigeringar per tiominutersfönster,
- minst 8 sekunder plus liten desynkroniseringsmarginal mellan navigeringar,
- paus efter varje mindre batch,
- persistent Chromium-profil i auto-/browserläge,
- lokal cache och villkorade HTTP-anrop med ETag/Last-Modified,
- olika aktualitetsintervall för institutionsdata och kalenderdata,
- omedelbart kretsavbrott när en verifieringssida upptäcks.

Scrapern försöker inte lösa CAPTCHA, rotera proxyservrar, imitera identiteter eller
på annat sätt kringgå webbplatsens åtkomstkontroller. Ingen inställning kan ge en
absolut garanti mot en serverstyrd verifieringssida, men standardprofilen undviker
fortsatta anrop när en sådan signal upptäcks.

Installera:
  pip install requests beautifulsoup4 playwright
  playwright install chromium

Rekommenderad körning:
  python oddfellow_full_scraper.py --mode auto --headed
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import robotparser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:
    raise SystemExit("Installera beroenden: pip install requests beautifulsoup4") from exc

ROOT = "https://oddfellow.se"
INSTITUTION_URL = ROOT + "/hitta/institution/?inst={inst}"
CALENDAR_URL = ROOT + "/hitta/institution/?inst={inst}&p=calendar"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
DATE_TOKEN_RE = re.compile(
    r"^(?P<day>[1-9]|[12]\d|3[01])\s*[.]?\s*"
    r"(?P<mon>Jan|Feb|Mar|Apr|Maj|Jun|Jul|Aug|Sep|Okt|Nov|Dec)[.]?$",
    re.I,
)
DAY_RE = re.compile(r"^(?:[1-9]|[12]\d|3[01])$")
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
VERIFY_MARKERS = (
    "please wait while your request is being verified",
    "one moment, please",
    "verifying you are human",
    "security verification",
    "cf-chl-",
)

SUCCESS_STATUSES = {"ok", "empty", "not_found", "skipped_not_found", "web_index_verified"}


class VerificationRequired(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def stable_id(prefix: str, *parts: Any) -> str:
    joined = "\x1f".join("" if p is None else str(p).strip() for p in parts)
    return f"{prefix}_{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:20]}"


def institution_ids() -> list[str]:
    return (
        [f"b{i}" for i in range(1, 201)]
        + [f"r{i}" for i in range(1, 151)]
        + [f"bl{i}" for i in range(1, 31)]
        + [f"rl{i}" for i in range(1, 31)]
    )


def institution_prefix(inst: str) -> str:
    for prefix in ("bl", "rl", "b", "r"):
        if inst.startswith(prefix):
            return prefix
    raise ValueError(f"Okänt institutions-ID: {inst}")


def institution_number(inst: str) -> int:
    prefix = institution_prefix(inst)
    return int(inst[len(prefix):])


def institution_sort_key(inst: str) -> tuple[int, int]:
    order = {"b": 0, "r": 1, "bl": 2, "rl": 3}
    prefix = institution_prefix(inst)
    return order[prefix], institution_number(inst)


def target_group_for(inst: str) -> tuple[str, str]:
    if inst.startswith("b"):
        return "men", "Män"
    if inst.startswith("r"):
        return "women", "Kvinnor"
    raise ValueError(f"Okänt institutions-ID: {inst}")


def institution_type_for(inst: str) -> tuple[str, str]:
    prefix = institution_prefix(inst)
    return {
        "b": ("brodraloge", "Brödraloge"),
        "r": ("rebeckaloge", "Rebeckaloge"),
        "bl": ("brodralager", "Brödraläger"),
        "rl": ("rebeckalager", "Rebeckaläger"),
    }[prefix]


def is_verification_page(html: str) -> bool:
    low_html = html.lower()
    text = normalize_space(BeautifulSoup(html, "html.parser").get_text(" ")).lower()
    return any(marker in low_html or marker in text for marker in VERIFY_MARKERS)


def retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class CollectionPaused(RuntimeError):
    """Körningen har stoppats för att undvika fler anrop efter verifieringssignal."""

    def __init__(self, institution_id: str, endpoint_type: str, message: str):
        super().__init__(message)
        self.institution_id = institution_id
        self.endpoint_type = endpoint_type


@dataclass
class FetchResult:
    html: str
    http_status: str
    method: str
    collected_at: str
    from_cache: bool = False
    etag: str = ""
    last_modified: str = ""


class RequestPacer:
    """Gemensam anropskö för institutions- och kalendersidor."""

    def __init__(
        self,
        minimum_interval: float,
        jitter_seconds: float,
        max_requests_per_window: int,
        window_seconds: float,
        batch_size: int,
        batch_pause: float,
    ):
        self.minimum_interval = max(0.0, minimum_interval)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self.max_requests_per_window = max(1, max_requests_per_window)
        self.window_seconds = max(1.0, window_seconds)
        self.batch_size = max(0, batch_size)
        self.batch_pause = max(0.0, batch_pause)
        self._timestamps: deque[float] = deque()
        self._last_request = 0.0
        self._request_count = 0
        self._last_batch_pause_at = -1
        self._random = random.SystemRandom()

    @property
    def request_count(self) -> int:
        return self._request_count

    def wait(self, endpoint_type: str) -> None:
        now = time.monotonic()

        if (
            self.batch_size > 0
            and self._request_count > 0
            and self._request_count % self.batch_size == 0
            and self._last_batch_pause_at != self._request_count
        ):
            self._last_batch_pause_at = self._request_count
            print(
                f"Skonsam batchpaus efter {self._request_count} navigeringar: "
                f"{self.batch_pause:.0f} s.",
                flush=True,
            )
            time.sleep(self.batch_pause)
            now = time.monotonic()

        while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_requests_per_window:
            wait_for = self.window_seconds - (now - self._timestamps[0]) + 0.25
            if wait_for > 0:
                print(
                    "Anropsfönstrets gräns är nådd; väntar "
                    f"{wait_for:.0f} s före nästa {endpoint_type}-sida.",
                    flush=True,
                )
                time.sleep(wait_for)
                now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                self._timestamps.popleft()

        spacing = self.minimum_interval
        if self.jitter_seconds:
            spacing += self._random.uniform(0.0, self.jitter_seconds)
        remaining = spacing - (now - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

        request_time = time.monotonic()
        self._last_request = request_time
        self._timestamps.append(request_time)
        self._request_count += 1


class DiskCache:
    def __init__(self, directory: Path, max_age_hours: float):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_age = timedelta(hours=max_age_hours)

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.directory / f"{key}.html", self.directory / f"{key}.json"

    def _read(self, url: str) -> Optional[tuple[FetchResult, datetime]]:
        html_path, meta_path = self._paths(url)
        if not html_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(meta["collected_at"].replace("Z", "+00:00"))
            result = FetchResult(
                html=html_path.read_text(encoding="utf-8"),
                http_status=str(meta.get("http_status", "")),
                method="disk-cache",
                collected_at=meta["collected_at"],
                from_cache=True,
                etag=str(meta.get("etag", "")),
                last_modified=str(meta.get("last_modified", "")),
            )
            return result, fetched
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def get(self, url: str) -> Optional[FetchResult]:
        cached = self._read(url)
        if cached is None:
            return None
        result, fetched = cached
        if datetime.now(timezone.utc) - fetched > self.max_age:
            return None
        return result

    def get_stale(self, url: str) -> Optional[FetchResult]:
        cached = self._read(url)
        return cached[0] if cached else None

    def put(self, url: str, result: FetchResult) -> None:
        html_path, meta_path = self._paths(url)
        html_tmp = html_path.with_suffix(".html.tmp")
        meta_tmp = meta_path.with_suffix(".json.tmp")
        html_tmp.write_text(result.html, encoding="utf-8")
        meta_tmp.write_text(
            json.dumps(
                {
                    "url": url,
                    "http_status": result.http_status,
                    "method": result.method,
                    "collected_at": result.collected_at,
                    "etag": result.etag,
                    "last_modified": result.last_modified,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(html_tmp, html_path)
        os.replace(meta_tmp, meta_path)


class RobotsPolicy:
    def __init__(self, user_agent: str, timeout: float):
        self.user_agent = user_agent
        self.timeout = timeout
        self.parser: Optional[robotparser.RobotFileParser] = None

    def load(self, fetcher: "HttpFetcher") -> None:
        url = ROOT + "/robots.txt"
        fetcher.pacer.wait("robots")
        try:
            response = fetcher.session.get(url, timeout=self.timeout, allow_redirects=True)
            if response.status_code in {403, 429} or is_verification_page(response.text):
                raise VerificationRequired(
                    "Verifieringssignal upptäcktes redan vid robots.txt; masshämtning startades inte."
                )
            if response.status_code == 200:
                parser = robotparser.RobotFileParser()
                parser.set_url(url)
                parser.parse(response.text.splitlines())
                self.parser = parser
                print("robots.txt inläst.", flush=True)
            else:
                print(
                    f"Varning: robots.txt gav HTTP {response.status_code}; fortsätter försiktigt.",
                    file=sys.stderr,
                )
        except VerificationRequired:
            raise
        except requests.RequestException as exc:
            print(
                f"Varning: robots.txt kunde inte läsas ({exc}); fortsätter försiktigt.",
                file=sys.stderr,
            )

    def allowed(self, url: str) -> bool:
        return True if self.parser is None else self.parser.can_fetch(self.user_agent, url)


class HttpFetcher:
    def __init__(
        self,
        timeout: float,
        retries: int,
        backoff_base: float,
        cache: DiskCache,
        pacer: RequestPacer,
        user_agent: str,
    ):
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff_base = max(1.0, backoff_base)
        self.cache = cache
        self.pacer = pacer
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
                "Connection": "keep-alive",
                "Cache-Control": "max-age=0",
            }
        )

    def fetch(self, url: str, endpoint_type: str, use_cache: bool = True) -> FetchResult:
        if use_cache:
            fresh = self.cache.get(url)
            if fresh is not None:
                return fresh

        stale = self.cache.get_stale(url) if use_cache else None
        request_headers: dict[str, str] = {}
        if stale is not None:
            if stale.etag:
                request_headers["If-None-Match"] = stale.etag
            if stale.last_modified:
                request_headers["If-Modified-Since"] = stale.last_modified

        last_error = ""
        for attempt in range(self.retries + 1):
            self.pacer.wait(endpoint_type)
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers=request_headers,
                )
                collected_at = utc_now()

                if response.status_code == 304 and stale is not None:
                    result = FetchResult(
                        html=stale.html,
                        http_status="304",
                        method="requests-conditional-cache",
                        collected_at=collected_at,
                        etag=response.headers.get("ETag", stale.etag),
                        last_modified=response.headers.get("Last-Modified", stale.last_modified),
                    )
                    self.cache.put(url, result)
                    return result

                html = response.text
                if response.status_code == 200:
                    if is_verification_page(html):
                        raise VerificationRequired(
                            f"Verifieringssida upptäcktes på {endpoint_type}-adressen."
                        )
                    result = FetchResult(
                        html=html,
                        http_status="200",
                        method="requests",
                        collected_at=collected_at,
                        etag=response.headers.get("ETag", ""),
                        last_modified=response.headers.get("Last-Modified", ""),
                    )
                    self.cache.put(url, result)
                    return result

                if response.status_code in {403, 429}:
                    raise VerificationRequired(
                        f"HTTP {response.status_code} från {endpoint_type}-adressen; "
                        "körningen stoppas utan nytt försök."
                    )

                last_error = f"HTTP {response.status_code}"
                if response.status_code in {500, 502, 503, 504} and attempt < self.retries:
                    server_wait = retry_after_seconds(response.headers.get("Retry-After"))
                    delay = (
                        server_wait
                        if server_wait is not None
                        else min(600.0, self.backoff_base * (2**attempt))
                    )
                    print(
                        f"{url}: {last_error}; väntar {delay:.0f} s.",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(last_error)
            except VerificationRequired:
                raise
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    delay = min(600.0, self.backoff_base * (2**attempt))
                    time.sleep(delay)
                    continue
                raise RuntimeError(last_error) from exc
        raise RuntimeError(last_error or "Okänt HTTP-fel")

    def import_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
        for cookie in cookies:
            try:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue


class BrowserFetcher:
    def __init__(
        self,
        timeout: float,
        profile_dir: Path,
        headless: bool,
        manual_verification_seconds: int,
        cache: DiskCache,
        pacer: RequestPacer,
    ):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright saknas. Installera: pip install playwright && playwright install chromium"
            ) from exc

        self.cache = cache
        self.pacer = pacer
        self.manual_verification_seconds = max(0, manual_verification_seconds)
        self._pw = sync_playwright().start()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            locale="sv-SE",
            viewport={"width": 1280, "height": 900},
        )

        def reduce_nonessential_load(route: Any) -> None:
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        self._context.route("**/*", reduce_nonessential_load)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(int(timeout * 1000))

    def fetch(self, url: str, endpoint_type: str, use_cache: bool = True) -> FetchResult:
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached

        self.pacer.wait(endpoint_type)
        response = self._page.goto(url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(750)
        html = self._page.content()
        status_code = response.status if response else None

        if status_code in {403, 429} or is_verification_page(html):
            if self.manual_verification_seconds <= 0:
                raise VerificationRequired(
                    f"Verifieringssignal upptäcktes på {endpoint_type}-adressen; "
                    "körningen stoppas utan ytterligare navigering."
                )
            print(
                "Verifieringssida visas. Slutför den manuellt i det öppna "
                "webbläsarfönstret. Inga nya adresser öppnas under väntetiden.",
                file=sys.stderr,
                flush=True,
            )
            deadline = time.monotonic() + self.manual_verification_seconds
            while time.monotonic() < deadline:
                self._page.wait_for_timeout(2000)
                html = self._page.content()
                if not is_verification_page(html):
                    break
            else:
                raise VerificationRequired(
                    "Manuell verifiering slutfördes inte inom angiven tid."
                )

        result = FetchResult(
            html=html,
            http_status=str(status_code or ""),
            method="playwright-persistent-session",
            collected_at=utc_now(),
        )
        self.cache.put(url, result)
        return result

    def cookies(self) -> list[dict[str, Any]]:
        return self._context.cookies()

    def close(self) -> None:
        self._context.close()
        self._pw.stop()


class FetchCoordinator:
    """Väljer en enda transport för hela sessionen och byter inte efter verifiering."""

    def __init__(
        self,
        http: HttpFetcher,
        mode: str,
        browser_factory: Callable[[], BrowserFetcher],
    ):
        self.http = http
        self.mode = mode
        self.browser_factory = browser_factory
        self.browser: Optional[BrowserFetcher] = None
        self._auto_fell_back_to_http = False

    def _browser(self) -> BrowserFetcher:
        if self.browser is None:
            self.browser = self.browser_factory()
        return self.browser

    def fetch(self, url: str, endpoint_type: str) -> FetchResult:
        if self.mode == "requests":
            return self.http.fetch(url, endpoint_type)
        if self.mode == "browser":
            return self._browser().fetch(url, endpoint_type)

        # auto: använd persistent webbläsarsession från början. HTTP används endast
        # om Playwright/Chromium inte kan startas, aldrig som nytt försök efter en
        # verifieringssignal.
        try:
            result = self._browser().fetch(url, endpoint_type)
            self.http.import_browser_cookies(self._browser().cookies())
            return result
        except VerificationRequired:
            raise
        except RuntimeError as exc:
            if self.browser is None:
                if not self._auto_fell_back_to_http:
                    print(
                        f"Webbläsarläge kunde inte startas ({exc}); använder den "
                        "gemensamma HTTP-sessionen i stället.",
                        file=sys.stderr,
                    )
                    self._auto_fell_back_to_http = True
                return self.http.fetch(url, endpoint_type)
            raise

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()


def tokens_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [normalize_space(x) for x in soup.stripped_strings if normalize_space(x)]


def first_index(tokens: list[str], label: str, start: int = 0) -> Optional[int]:
    target = label.casefold()
    for i in range(start, len(tokens)):
        if tokens[i].casefold() == target:
            return i
    return None


def value_after(tokens: list[str], label: str, stop_labels: set[str], start: int = 0) -> str:
    idx = first_index(tokens, label, start)
    if idx is None or idx + 1 >= len(tokens):
        return ""
    value = tokens[idx + 1]
    return "" if value in stop_labels else value


def segment_between(tokens: list[str], start_label: str, end_labels: set[str]) -> list[str]:
    start = first_index(tokens, start_label)
    if start is None:
        return []
    end = len(tokens)
    for i in range(start + 1, len(tokens)):
        if tokens[i] in end_labels:
            end = i
            break
    return tokens[start:end]


def designation_for(inst: str) -> str:
    _, label = institution_type_for(inst)
    return f"{label} {institution_number(inst)}"


def institution_name(tokens: list[str], designation: str) -> str:
    idx = first_index(tokens, designation)
    if idx is None:
        return ""
    excluded = {
        "Vår loge", "Vårt läger", "Historik", "Ordenshuset", "Information",
        "Möteskalender", "Hänt i logen", "Hänt i lägret", "Bilder", "Kontakt", "Fakta",
    }
    for candidate in tokens[idx + 1:idx + 8]:
        if candidate not in excluded and candidate != designation:
            return candidate
    return ""


def parse_institution(html: str, inst: str, source_url: str, collected_at: str) -> tuple[Optional[dict[str, Any]], str]:
    tokens = tokens_from_html(html)
    designation = designation_for(inst)
    has_designation = first_index(tokens, designation) is not None
    has_facts = first_index(tokens, "Fakta") is not None
    if not has_designation and not has_facts:
        return None, "Institutionens benämning och Fakta-avsnitt saknades."

    facts = segment_between(tokens, "Fakta", {"Besöksadress"})
    visit = segment_between(tokens, "Besöksadress", {"Postadress"})
    contact = segment_between(tokens, "Kontaktinformation", {"Bankgiro", "Odd Fellow Orden"})

    district_text = value_after(facts, "Distrikt", {"Instituerad", "Installering", "Mötesdagar", "Säte", "Län"})
    try:
        district: int | None = int(district_text) if district_text else None
    except ValueError:
        district = None

    institution_phone = value_after(
        contact, "Telefon Institution",
        {"Telefon Kontakt", "E-post", "Bankgiro", "Plusgiro", "Odd Fellow Orden"}
    )
    contact_phone = value_after(
        contact, "Telefon Kontakt",
        {"Telefon Institution", "E-post", "Bankgiro", "Plusgiro", "Odd Fellow Orden"}
    )
    email = value_after(
        contact, "E-post",
        {"Telefon Institution", "Telefon Kontakt", "Bankgiro", "Plusgiro", "Odd Fellow Orden"}
    )
    summary = " | ".join(x for x in (institution_phone, contact_phone, email) if x)

    institution_type, institution_type_label_sv = institution_type_for(inst)
    target_group, target_group_label_sv = target_group_for(inst)
    record = {
        "record_id": f"institution_{inst}",
        "record_type": "institution",
        "institution_id": inst,
        "name": institution_name(tokens, designation),
        "designation": designation,
        "institution_type": institution_type,
        "institution_type_label_sv": institution_type_label_sv,
        "target_group": target_group,
        "target_group_label_sv": target_group_label_sv,
        "district": district,
        "seat": value_after(facts, "Säte", {"Län", "Besöksadress"}),
        "county": value_after(facts, "Län", {"Besöksadress"}),
        "contact": {
            "institution_phone": institution_phone,
            "contact_phone": contact_phone,
            "email": email,
            "summary": summary,
        },
        "visit_address": {
            "street": value_after(visit, "Adress", {"Ort", "Postadress"}),
            "city": value_after(visit, "Ort", {"Postadress", "Kontaktinformation"}),
        },
        "status": "ok",
        "notes": [],
        "source_url": source_url,
        "collected_at": collected_at,
    }
    return record, ""


def date_marker(tokens: list[str], idx: int) -> Optional[tuple[int, str, int]]:
    token = normalize_space(tokens[idx])
    match = DATE_TOKEN_RE.match(token)
    if match:
        return int(match.group("day")), match.group("mon").title(), 1
    if DAY_RE.match(token) and idx + 1 < len(tokens):
        month = normalize_space(tokens[idx + 1]).strip(".").lower()
        if month in MONTHS:
            return int(token), month.title(), 2
    return None


def calendar_segment(tokens: list[str]) -> list[str]:
    starts = [i for i, token in enumerate(tokens) if token.casefold() == "möteskalender"]
    if not starts:
        return []
    start = starts[-1] + 1
    end = len(tokens)
    for i in range(start, len(tokens)):
        if tokens[i].casefold() == "odd fellow orden":
            end = i
            break
    return tokens[start:end]


def split_title_clothing_time(parts: list[str]) -> tuple[str, str, str, str]:
    clean = [normalize_space(x) for x in parts if normalize_space(x)]
    raw = " | ".join(clean)
    if not clean:
        return "", "", "", raw

    time_index = None
    match = None
    for idx in range(len(clean) - 1, -1, -1):
        found = TIME_RE.search(clean[idx])
        if found:
            time_index, match = idx, found
            break
    if match is None or time_index is None:
        return normalize_space(" ".join(clean)), "", "", raw

    meeting_time = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    detail = clean[time_index]
    before_time = detail[:match.start()].strip(" –—-,:;")
    title_parts = clean[:time_index]
    clothing = before_time

    if not title_parts and clothing:
        clothing_words = (
            "kläd", "kostym", "frack", "dräkt", "slips",
            "vårdad", "fritid", "logeklädsel", "mörk kostym",
        )
        if not any(word in clothing.casefold() for word in clothing_words):
            title_parts = [clothing]
            clothing = ""

    return normalize_space(" ".join(title_parts)), clothing, meeting_time, raw


def infer_event_dates(events: list[dict[str, Any]], scrape_date: date) -> None:
    if not events:
        return

    def build(base_year: int) -> list[date]:
        result: list[date] = []
        year = base_year
        previous_month: Optional[int] = None
        for event in events:
            month = MONTHS[event["month"].lower()]
            if previous_month is not None and month < previous_month:
                year += 1
            result.append(date(year, month, event["day"]))
            previous_month = month
        return result

    candidates: list[tuple[int, list[date]]] = []
    for base_year in (scrape_date.year - 1, scrape_date.year, scrape_date.year + 1):
        dates = build(base_year)
        nearest = min(abs((value - scrape_date).days) for value in dates)
        candidates.append((nearest, dates))
    _, chosen = min(candidates, key=lambda item: item[0])
    nearest = min(abs((value - scrape_date).days) for value in chosen)
    confidence = "high" if nearest <= 120 else ("medium" if nearest <= 300 else "low")
    for event, inferred in zip(events, chosen):
        event["date_iso"] = inferred.isoformat()
        event["year_inference_confidence"] = confidence


def parse_calendar(
    html: str,
    inst: str,
    source_url: str,
    collected_at: str,
    fallback_name: str = "",
) -> tuple[list[dict[str, Any]], str]:
    tokens = tokens_from_html(html)
    segment = calendar_segment(tokens)
    if not segment:
        return [], "Möteskalender saknades eller var tom."

    events: list[dict[str, Any]] = []
    i = 0
    while i < len(segment):
        marker = date_marker(segment, i)
        if marker is None:
            i += 1
            continue
        day, month, consumed = marker
        i += consumed
        parts: list[str] = []
        while i < len(segment) and date_marker(segment, i) is None:
            parts.append(segment[i])
            i += 1
        title, clothing, meeting_time, raw = split_title_clothing_time(parts)
        if title or clothing or meeting_time:
            events.append({
                "day": day,
                "month": month,
                "title": title,
                "dress_code": clothing,
                "time": meeting_time,
                "raw_event_text": raw,
            })

    scrape_date = datetime.fromisoformat(collected_at.replace("Z", "+00:00")).date()
    infer_event_dates(events, scrape_date)
    records = []
    for event in events:
        record_id = stable_id(
            "meeting", inst, event["date_iso"], event["time"],
            event["title"], event["dress_code"]
        )
        target_group, target_group_label_sv = target_group_for(inst)
        institution_type, institution_type_label_sv = institution_type_for(inst)
        records.append({
            "record_id": record_id,
            "record_type": "meeting",
            "institution_id": inst,
            "institution_name": fallback_name,
            "institution_type": institution_type,
            "institution_type_label_sv": institution_type_label_sv,
            "target_group": target_group,
            "target_group_label_sv": target_group_label_sv,
            "date_displayed": f"{event['day']} {event['month']}",
            "date_iso": event["date_iso"],
            "year_inferred": True,
            "year_inference_confidence": event["year_inference_confidence"],
            "time": event["time"],
            "title": event["title"],
            "dress_code": event["dress_code"],
            "raw_event_text": event["raw_event_text"],
            "source_url": source_url,
            "notes": [],
            "collected_at": collected_at,
        })
    return records, "" if records else "Inga kalenderposter kunde tolkas."


def status_record(
    endpoint_type: str,
    inst: str,
    source_url: str,
    collected_at: str,
    status: str,
    record_count: int,
    method: str = "",
    http_status: str = "",
    institution_name: str = "",
    notes: str = "",
) -> dict[str, Any]:
    target_group, target_group_label_sv = target_group_for(inst)
    institution_type, institution_type_label_sv = institution_type_for(inst)
    return {
        "record_id": f"source_{endpoint_type}_{inst}",
        "record_type": "source_status",
        "endpoint_type": endpoint_type,
        "institution_id": inst,
        "institution_type": institution_type,
        "institution_type_label_sv": institution_type_label_sv,
        "target_group": target_group,
        "target_group_label_sv": target_group_label_sv,
        "institution_name": institution_name,
        "status": status,
        "record_count": record_count,
        "http_status": http_status,
        "method": method,
        "notes": notes,
        "source_url": source_url,
        "collected_at": collected_at,
    }


def empty_database() -> dict[str, Any]:
    return {
        "metadata": {
            "schema_version": "1.1.0",
            "format": "JSON",
            "encoding": "UTF-8",
            "generated_at": utc_now(),
            "source_site": ROOT,
            "description": "Textbaserad databas över Odd Fellow-institutioner, möten och insamlingsstatus.",
            "id_ranges": {
                "brodraloge": "b1-b200",
                "rebeckaloge": "r1-r150",
                "brodralager": "bl1-bl30",
                "rebeckalager": "rl1-rl30",
            },
            "record_counts": {},
        },
        "institutions": [],
        "meetings": [],
        "source_status": [],
    }


def normalize_database(data: dict[str, Any]) -> dict[str, Any]:
    data.setdefault("metadata", {})
    data["metadata"]["schema_version"] = "1.1.0"
    data["metadata"]["id_ranges"] = {
        "brodraloge": "b1-b200",
        "rebeckaloge": "r1-r150",
        "brodralager": "bl1-bl30",
        "rebeckalager": "rl1-rl30",
    }
    for key in ("institutions", "meetings", "source_status"):
        data.setdefault(key, [])
        for record in data[key]:
            inst = record.get("institution_id", "")
            if not inst:
                continue
            target_group, target_group_label_sv = target_group_for(inst)
            institution_type, institution_type_label_sv = institution_type_for(inst)
            record["target_group"] = target_group
            record["target_group_label_sv"] = target_group_label_sv
            record["institution_type"] = institution_type
            record["institution_type_label_sv"] = institution_type_label_sv
    return data


def load_database(path: Path) -> dict[str, Any]:
    if not path.exists():
        return normalize_database(empty_database())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("institutions", "meetings", "source_status"):
            if not isinstance(data.get(key), list):
                data[key] = []
        return normalize_database(data)
    except (OSError, json.JSONDecodeError):
        return normalize_database(empty_database())


def upsert(records: list[dict[str, Any]], new_records: list[dict[str, Any]]) -> None:
    by_id = {record["record_id"]: record for record in records}
    for record in new_records:
        by_id[record["record_id"]] = record
    records[:] = list(by_id.values())


def remove_meetings_for_institution(records: list[dict[str, Any]], inst: str) -> None:
    records[:] = [record for record in records if record.get("institution_id") != inst]


def write_database(path: Path, data: dict[str, Any]) -> None:
    normalize_database(data)
    data["institutions"].sort(key=lambda x: institution_sort_key(x["institution_id"]))
    data["meetings"].sort(
        key=lambda x: (x.get("date_iso", ""), x.get("time", ""), institution_sort_key(x["institution_id"]), x["record_id"])
    )
    data["source_status"].sort(
        key=lambda x: (*institution_sort_key(x["institution_id"]), x["endpoint_type"])
    )
    data["metadata"]["generated_at"] = utc_now()
    data["metadata"]["record_counts"] = {
        "institutions": len(data["institutions"]),
        "meetings": len(data["meetings"]),
        "source_status": len(data["source_status"]),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_jsonl(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "metadata",
            "record_id": "metadata",
            "collected_at": data["metadata"]["generated_at"],
            **data["metadata"],
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        for key in ("institutions", "meetings", "source_status"):
            for record in data[key]:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path)



def status_records(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (record["endpoint_type"], record["institution_id"]): record
        for record in data["source_status"]
    }


def parsed_utc(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def local_today(time_zone_name: str) -> date:
    try:
        zone = ZoneInfo(time_zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Okänd tidszon: {time_zone_name}") from exc
    return datetime.now(zone).date()


def purge_past_meetings(
    records: list[dict[str, Any]],
    today: date,
) -> tuple[int, int]:
    """Ta bort möten före today; behåll dagens och framtida möten."""
    retained: list[dict[str, Any]] = []
    removed_count = 0
    invalid_date_count = 0
    for record in records:
        raw_date = str(record.get("date_iso", "")).strip()
        try:
            meeting_date = date.fromisoformat(raw_date[:10])
        except (TypeError, ValueError):
            invalid_date_count += 1
            retained.append(record)
            continue
        if meeting_date < today:
            removed_count += 1
        else:
            retained.append(record)
    records[:] = retained
    return removed_count, invalid_date_count


def synchronize_calendar_record_counts(data: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for meeting in data.get("meetings", []):
        inst = str(meeting.get("institution_id", ""))
        if inst:
            counts[inst] = counts.get(inst, 0) + 1
    for status in data.get("source_status", []):
        if status.get("endpoint_type") != "calendar":
            continue
        if str(status.get("status", "")) in SUCCESS_STATUSES:
            status["record_count"] = counts.get(str(status.get("institution_id", "")), 0)


def previous_run_started_at(data: dict[str, Any]) -> Optional[datetime]:
    metadata = data.get("metadata", {})
    return (
        parsed_utc(str(metadata.get("last_run_started_at", "")))
        or parsed_utc(str(metadata.get("last_run_finished_at", "")))
    )


def is_retry_only_run(
    data: dict[str, Any],
    run_started_at: datetime,
    window_hours: float,
    refresh: bool,
) -> bool:
    if refresh or window_hours <= 0:
        return False
    previous = previous_run_started_at(data)
    if previous is None:
        return False
    elapsed = run_started_at - previous
    return timedelta(0) <= elapsed < timedelta(hours=window_hours)


def finalize_run_metadata(
    data: dict[str, Any],
    status: str,
    retry_only_failed: bool,
    navigation_count: Optional[int] = None,
) -> None:
    metadata = data.setdefault("metadata", {})
    metadata["last_run_finished_at"] = utc_now()
    metadata["last_run_status"] = status
    metadata["last_run_mode"] = (
        "retry_failed_only" if retry_only_failed else "normal"
    )
    if navigation_count is not None:
        metadata["last_run_navigation_count"] = navigation_count


def status_is_due(
    record: Optional[dict[str, Any]],
    endpoint_type: str,
    refresh: bool,
    retry_only_failed: bool,
    institution_max_age_days: float,
    calendar_max_age_hours: float,
    not_found_max_age_days: float,
    failure_retry_hours: float,
) -> bool:
    if refresh or record is None:
        return True

    status = str(record.get("status", ""))
    if retry_only_failed:
        return status not in SUCCESS_STATUSES

    collected = parsed_utc(str(record.get("collected_at", "")))
    if collected is None:
        return True
    age_hours = (datetime.now(timezone.utc) - collected).total_seconds() / 3600.0

    if status in {"not_found", "skipped_not_found"}:
        return age_hours >= not_found_max_age_days * 24.0
    if status in {"ok", "web_index_verified"}:
        ttl = institution_max_age_days * 24.0 if endpoint_type == "institution" else calendar_max_age_hours
        return age_hours >= ttl
    if status == "empty":
        return age_hours >= calendar_max_age_hours
    return age_hours >= failure_retry_hours


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="oddfellow_database.json")
    parser.add_argument("--jsonl", default="oddfellow_database.jsonl")
    parser.add_argument("--mode", choices=("requests", "browser", "auto"), default="auto")
    parser.add_argument("--headed", action="store_true", help="Visa den persistenta Chromium-sessionen.")
    parser.add_argument(
        "--allow-manual-verification",
        action="store_true",
        help="Tillåt att en upptäckt verifieringssida slutförs manuellt i headed-läge.",
    )
    parser.add_argument("--manual-verification-seconds", type=int, default=300)
    parser.add_argument(
        "--minimum-interval",
        type=float,
        default=8.0,
        help="Minsta tid mellan alla institutions- och kalendernavigeringar.",
    )
    parser.add_argument(
        "--jitter-seconds",
        type=float,
        default=3.0,
        help="Liten desynkroniseringsmarginal ovanpå minimiintervallet.",
    )
    parser.add_argument("--max-requests-per-window", type=int, default=24)
    parser.add_argument("--window-seconds", type=float, default=600.0)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--batch-pause", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff-base", type=float, default=20.0)
    parser.add_argument("--cache-dir", default=".oddfellow-cache")
    parser.add_argument("--cache-max-age-hours", type=float, default=24.0)
    parser.add_argument("--browser-profile-dir", default=".oddfellow-browser-profile")
    parser.add_argument("--institution-max-age-days", type=float, default=30.0)
    parser.add_argument("--calendar-max-age-hours", type=float, default=12.0)
    parser.add_argument("--not-found-max-age-days", type=float, default=30.0)
    parser.add_argument("--failure-retry-hours", type=float, default=12.0)
    parser.add_argument(
        "--retry-only-window-hours",
        type=float,
        default=12.0,
        help=(
            "Vid omkörning inom detta tidsfönster hämtas endast poster vars "
            "senaste status inte var lyckad."
        ),
    )
    parser.add_argument(
        "--local-time-zone",
        default="Europe/Stockholm",
        help="Tidszon som avgör dagens datum vid rensning av passerade möten.",
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignorera aktualitetsintervallen och hämta valda poster på nytt.",
    )
    parser.add_argument("--no-robots-check", action="store_true")
    parser.add_argument(
        "--skip-network-preflight",
        action="store_true",
        help="Hoppa över DNS-/nätverkskontrollen före masskörning.",
    )
    parser.add_argument("--start", help="Första ID, exempelvis b1 eller bl1.")
    parser.add_argument("--end", help="Sista ID, exempelvis r150 eller rl30.")
    parser.add_argument(
        "--user-agent",
        default="LogeKollDataCollector/2.3 (serial; cached; contact: local-operator)",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    jsonl_path = Path(args.jsonl)
    data = normalize_database(empty_database()) if args.no_resume else load_database(output_path)

    run_started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    retry_only_failed = is_retry_only_run(
        data, run_started_dt, args.retry_only_window_hours, args.refresh
    )
    previous_started = previous_run_started_at(data)
    cleanup_today = local_today(args.local_time_zone)
    removed_meetings, invalid_meeting_dates = purge_past_meetings(
        data["meetings"], cleanup_today
    )
    synchronize_calendar_record_counts(data)
    data["metadata"]["last_run_started_at"] = (
        run_started_dt.isoformat().replace("+00:00", "Z")
    )
    data["metadata"]["last_run_status"] = "running"
    data["metadata"]["last_run_mode"] = (
        "retry_failed_only" if retry_only_failed else "normal"
    )
    data["metadata"]["retry_only_window_hours"] = args.retry_only_window_hours
    data["metadata"]["previous_run_started_at"] = (
        previous_started.isoformat().replace("+00:00", "Z")
        if previous_started is not None else ""
    )
    data["metadata"]["last_meeting_cleanup"] = {
        "at": utc_now(),
        "time_zone": args.local_time_zone,
        "today": cleanup_today.isoformat(),
        "removed_through_date": (cleanup_today - timedelta(days=1)).isoformat(),
        "removed_count": removed_meetings,
        "invalid_date_count": invalid_meeting_dates,
    }
    write_database(output_path, data)
    write_jsonl(jsonl_path, data)
    statuses = status_records(data)

    print(
        f"Körläge: {'endast tidigare misslyckade poster' if retry_only_failed else 'normal uppdatering'}. "
        f"Rensade {removed_meetings} passerade möten före {cleanup_today.isoformat()}.",
        flush=True,
    )

    ids = institution_ids()
    if args.start:
        if args.start not in ids:
            raise SystemExit(f"Ogiltigt --start: {args.start}")
        ids = ids[ids.index(args.start):]
    if args.end:
        if args.end not in ids:
            raise SystemExit(f"Ogiltigt --end: {args.end}")
        ids = ids[: ids.index(args.end) + 1]

    if not args.skip_network_preflight:
        try:
            socket.getaddrinfo("oddfellow.se", 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            now = utc_now()
            note = f"Nätverksförkontroll misslyckades: {exc}"
            for inst in ids:
                for endpoint_type, source_url in (
                    ("institution", INSTITUTION_URL.format(inst=inst)),
                    ("calendar", CALENDAR_URL.format(inst=inst)),
                ):
                    key = (endpoint_type, inst)
                    if key in statuses and not status_is_due(
                        statuses[key], endpoint_type, args.refresh, retry_only_failed,
                        args.institution_max_age_days, args.calendar_max_age_hours,
                        args.not_found_max_age_days, args.failure_retry_hours,
                    ):
                        continue
                    record = status_record(
                        endpoint_type, inst, source_url, now,
                        "network_unavailable", 0,
                        method="network-preflight", notes=note,
                    )
                    upsert(data["source_status"], [record])
                    statuses[key] = record
            data["metadata"]["last_network_error"] = {"at": now, "message": note}
            finalize_run_metadata(data, "network_unavailable", retry_only_failed, 0)
            write_database(output_path, data)
            write_jsonl(jsonl_path, data)
            print(note, file=sys.stderr)
            print("Inga webbplatsanrop genomfördes.")
            return 2

    pacer = RequestPacer(
        minimum_interval=args.minimum_interval,
        jitter_seconds=args.jitter_seconds,
        max_requests_per_window=args.max_requests_per_window,
        window_seconds=args.window_seconds,
        batch_size=args.batch_size,
        batch_pause=args.batch_pause,
    )
    cache = DiskCache(Path(args.cache_dir), args.cache_max_age_hours)
    http = HttpFetcher(
        timeout=args.timeout,
        retries=args.retries,
        backoff_base=args.backoff_base,
        cache=cache,
        pacer=pacer,
        user_agent=args.user_agent,
    )

    robots = RobotsPolicy(args.user_agent, args.timeout)
    if not args.no_robots_check:
        try:
            robots.load(http)
        except VerificationRequired as exc:
            now = utc_now()
            data["metadata"]["collection_pause"] = {
                "at": now,
                "reason": str(exc),
                "endpoint_type": "robots",
            }
            finalize_run_metadata(data, "verification_paused", retry_only_failed, http.pacer.request_count)
            write_database(output_path, data)
            write_jsonl(jsonl_path, data)
            print(str(exc), file=sys.stderr)
            return 75

    def browser_factory() -> BrowserFetcher:
        return BrowserFetcher(
            timeout=args.timeout,
            profile_dir=Path(args.browser_profile_dir),
            headless=not args.headed,
            manual_verification_seconds=(
                args.manual_verification_seconds
                if args.headed and args.allow_manual_verification
                else 0
            ),
            cache=cache,
            pacer=pacer,
        )

    coordinator = FetchCoordinator(http=http, mode=args.mode, browser_factory=browser_factory)
    data["metadata"]["collection_policy"] = {
        "version": "2.3",
        "mode": args.mode,
        "minimum_interval_seconds": args.minimum_interval,
        "jitter_seconds": args.jitter_seconds,
        "max_requests_per_window": args.max_requests_per_window,
        "window_seconds": args.window_seconds,
        "batch_size": args.batch_size,
        "batch_pause_seconds": args.batch_pause,
        "institution_max_age_days": args.institution_max_age_days,
        "calendar_max_age_hours": args.calendar_max_age_hours,
        "not_found_max_age_days": args.not_found_max_age_days,
        "failure_retry_hours": args.failure_retry_hours,
        "retry_only_window_hours": args.retry_only_window_hours,
        "retry_only_failed_this_run": retry_only_failed,
        "meeting_cleanup_time_zone": args.local_time_zone,
        "stop_on_verification": not args.allow_manual_verification,
    }
    data["metadata"].pop("collection_pause", None)

    try:
        for position, inst in enumerate(ids, start=1):
            institution_url = INSTITUTION_URL.format(inst=inst)
            calendar_url = CALENDAR_URL.format(inst=inst)

            institution_record = next(
                (row for row in data["institutions"] if row["institution_id"] == inst),
                None,
            )
            institution_key = ("institution", inst)
            institution_status = statuses.get(institution_key)
            should_fetch_institution = status_is_due(
                    institution_status,
                    "institution",
                    args.refresh,
                    retry_only_failed,
                    args.institution_max_age_days,
                    args.calendar_max_age_hours,
                    args.not_found_max_age_days,
                    args.failure_retry_hours,
                )
            if (
                institution_record is None
                and str((institution_status or {}).get("status", "")) in {"ok", "web_index_verified"}
            ):
                should_fetch_institution = True

            if should_fetch_institution:
                if not robots.allowed(institution_url):
                    raise SystemExit(f"robots.txt tillåter inte hämtning av {institution_url}")
                try:
                    result = coordinator.fetch(institution_url, "institution")
                    parsed, note = parse_institution(
                        result.html, inst, institution_url, result.collected_at
                    )
                    if parsed is None:
                        status = status_record(
                            "institution", inst, institution_url, result.collected_at,
                            "not_found", 0, result.method, result.http_status, notes=note,
                        )
                        institution_record = None
                    else:
                        upsert(data["institutions"], [parsed])
                        institution_record = parsed
                        status = status_record(
                            "institution", inst, institution_url, result.collected_at,
                            "ok", 1, result.method, result.http_status,
                            institution_name=parsed["name"], notes=note,
                        )
                    upsert(data["source_status"], [status])
                    statuses[institution_key] = status
                except VerificationRequired as exc:
                    now = utc_now()
                    status = status_record(
                        "institution", inst, institution_url, now,
                        "verification_paused", 0,
                        method="circuit-breaker", notes=str(exc),
                    )
                    upsert(data["source_status"], [status])
                    statuses[institution_key] = status
                    raise CollectionPaused(inst, "institution", str(exc)) from exc
                except Exception as exc:
                    status = status_record(
                        "institution", inst, institution_url, utc_now(),
                        "error", 0, notes=str(exc),
                    )
                    upsert(data["source_status"], [status])
                    statuses[institution_key] = status

            institution_status_value = str(statuses.get(institution_key, {}).get("status", ""))
            calendar_key = ("calendar", inst)

            if institution_status_value == "not_found":
                existing_calendar_status = statuses.get(calendar_key)
                if existing_calendar_status is None or str(existing_calendar_status.get("status")) != "skipped_not_found":
                    calendar_status = status_record(
                        "calendar", inst, calendar_url, utc_now(),
                        "skipped_not_found", 0,
                        notes="Kalenderanrop hoppades över eftersom institutionssidan saknades.",
                    )
                    upsert(data["source_status"], [calendar_status])
                    statuses[calendar_key] = calendar_status
            else:
                calendar_status = statuses.get(calendar_key)
                should_fetch_calendar = status_is_due(
                    calendar_status,
                    "calendar",
                    args.refresh,
                    retry_only_failed,
                    args.institution_max_age_days,
                    args.calendar_max_age_hours,
                    args.not_found_max_age_days,
                    args.failure_retry_hours,
                )
                if should_fetch_calendar:
                    if not robots.allowed(calendar_url):
                        raise SystemExit(f"robots.txt tillåter inte hämtning av {calendar_url}")
                    try:
                        result = coordinator.fetch(calendar_url, "calendar")
                        name = institution_record["name"] if institution_record else ""
                        parsed_meetings, note = parse_calendar(
                            result.html, inst, calendar_url, result.collected_at, name
                        )
                        remove_meetings_for_institution(data["meetings"], inst)
                        upsert(data["meetings"], parsed_meetings)
                        newly_removed, current_invalid_dates = purge_past_meetings(
                            data["meetings"], cleanup_today
                        )
                        synchronize_calendar_record_counts(data)
                        cleanup_metadata = data["metadata"]["last_meeting_cleanup"]
                        cleanup_metadata["removed_count"] += newly_removed
                        cleanup_metadata["invalid_date_count"] = current_invalid_dates
                        status = status_record(
                            "calendar", inst, calendar_url, result.collected_at,
                            "ok" if parsed_meetings else "empty",
                            len(parsed_meetings), result.method, result.http_status,
                            institution_name=name, notes=note,
                        )
                        upsert(data["source_status"], [status])
                        statuses[calendar_key] = status
                    except VerificationRequired as exc:
                        name = institution_record["name"] if institution_record else ""
                        status = status_record(
                            "calendar", inst, calendar_url, utc_now(),
                            "verification_paused", 0,
                            method="circuit-breaker", institution_name=name,
                            notes=str(exc),
                        )
                        upsert(data["source_status"], [status])
                        statuses[calendar_key] = status
                        raise CollectionPaused(inst, "calendar", str(exc)) from exc
                    except Exception as exc:
                        name = institution_record["name"] if institution_record else ""
                        status = status_record(
                            "calendar", inst, calendar_url, utc_now(),
                            "error", 0, institution_name=name, notes=str(exc),
                        )
                        upsert(data["source_status"], [status])
                        statuses[calendar_key] = status

            write_database(output_path, data)
            write_jsonl(jsonl_path, data)
            print(
                f"[{position:03d}/{len(ids)}] {inst}: "
                f"institution={statuses.get(institution_key, {}).get('status', 'okänt')}, "
                f"calendar={statuses.get(calendar_key, {}).get('status', 'okänt')}, "
                f"navigeringar={pacer.request_count}",
                flush=True,
            )
    except CollectionPaused as exc:
        now = utc_now()
        data["metadata"]["collection_pause"] = {
            "at": now,
            "institution_id": exc.institution_id,
            "endpoint_type": exc.endpoint_type,
            "reason": str(exc),
            "recommended_resume_after": (
                datetime.now(timezone.utc) + timedelta(hours=args.failure_retry_hours)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        finalize_run_metadata(data, "verification_paused", retry_only_failed, pacer.request_count)
        write_database(output_path, data)
        write_jsonl(jsonl_path, data)
        print(
            "Körningen pausades omedelbart för att inte generera fler anrop efter "
            f"verifieringssignalen ({exc.institution_id}, {exc.endpoint_type}).",
            file=sys.stderr,
        )
        return 75
    finally:
        coordinator.close()

    data["metadata"].pop("collection_pause", None)
    finally_removed, final_invalid_dates = purge_past_meetings(
        data["meetings"], cleanup_today
    )
    synchronize_calendar_record_counts(data)
    data["metadata"]["last_meeting_cleanup"]["removed_count"] += finally_removed
    data["metadata"]["last_meeting_cleanup"]["invalid_date_count"] = final_invalid_dates
    finalize_run_metadata(data, "completed", retry_only_failed, pacer.request_count)
    write_database(output_path, data)
    write_jsonl(jsonl_path, data)
    print(f"Databas: {output_path}")
    print(f"JSONL: {jsonl_path}")
    print(f"Navigeringar i körningen: {pacer.request_count}")
    print(json.dumps(data["metadata"]["record_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
