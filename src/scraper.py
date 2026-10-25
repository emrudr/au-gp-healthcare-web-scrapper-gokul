import asyncio
import re
import random
import urllib.parse
from typing import List, Dict, Any, Optional, Set

import httpx
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Route,
)

from src.parser import (
    parse_doctor_name,
    parse_doctor_card,
    parse_role_and_gender,
    parse_qualifications,
    parse_address,
    format_phone_number,
)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

BLOCKED_DOMAINS = [
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "facebook.com",
    "sentry.io",
    "rudderstack.com",
    "segment.com",
    "hotjar.com",
    "capi.hotdoc.com.au",
]


class HotDocScraper:
    def __init__(self, concurrency: int = 20, headless: bool = True):
        self.concurrency = concurrency
        self.headless = headless
        self.clinic_sem = asyncio.Semaphore(concurrency)
        self.search_sem = asyncio.Semaphore(4)
        self.ua_index = 0

        # Cross-hub cache for clinics so we never re-fetch the same clinic twice
        self.clinic_cache: Dict[str, Dict[str, Any]] = {}
        self.in_flight_clinics: Dict[str, asyncio.Task] = {}

        # High performance HTTP/2 client
        self.http_client: Optional[httpx.AsyncClient] = None

        # Playwright browser instance for fallback rendering (lazy loaded only on need)
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

    def get_headers(self, accept_version: str) -> Dict[str, str]:
        self.ua_index = (self.ua_index + 1) % len(USER_AGENTS)
        ua = USER_AGENTS[self.ua_index]
        return {
            "User-Agent": ua,
            "Accept": accept_version,
            "App-Version": "5.0069.0",
            "App-Platform": "web",
        }

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def seed_cache(self, records: List[Dict[str, Any]]):
        """Pre-seeds clinic_cache from existing scraped records to eliminate duplicate network calls."""
        for row in records:
            purl = str(row.get("profile_url", "") or "")
            pname = str(row.get("practice_name", "") or "").strip()
            c_key = self.get_clinic_key(purl, pname)
            if not c_key:
                continue

            if c_key not in self.clinic_cache:
                self.clinic_cache[c_key] = {
                    "phone": str(row.get("phone_number", "") or "").strip(),
                    "address": str(row.get("address", "") or "").strip(),
                    "doctors": {}
                }

            phone = str(row.get("phone_number", "") or "").strip()
            if phone and not self.clinic_cache[c_key]["phone"]:
                self.clinic_cache[c_key]["phone"] = phone

            d_name = str(row.get("doctor_name", "") or "").strip()
            if d_name:
                doc_slug = ""
                if purl:
                    m_slug = re.search(r"/doctors/([^/?#]+)", purl)
                    if m_slug:
                        doc_slug = m_slug.group(1).lower().strip()
                norm_name = re.sub(r"[^\w\s]", "", d_name.lower())
                d_info = {
                    "gender": str(row.get("gender", "") or "").strip(),
                    "qualifications": str(row.get("qualification_codes", "") or "").strip(),
                    "role": str(row.get("role", "") or "").strip()
                }
                if doc_slug:
                    self.clinic_cache[c_key]["doctors"][doc_slug] = d_info
                self.clinic_cache[c_key]["doctors"][norm_name] = d_info

    async def start(self):
        """Initializes the high-speed HTTP/2 client with optimized connection pooling."""
        limits = httpx.Limits(
            max_keepalive_connections=50,
            max_connections=100,
            keepalive_expiry=30.0
        )
        self.http_client = httpx.AsyncClient(
            http2=True,
            timeout=12.0,
            limits=limits,
            follow_redirects=True
        )

    async def ensure_playwright(self):
        """Lazy loads headless Playwright browser only if API fallback is needed."""
        if self.context:
            return
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-networking",
                ]
            )
            self.context = await self.browser.new_context(
                user_agent=USER_AGENTS[0],
                viewport={"width": 1280, "height": 800}
            )

            async def route_interceptor(route: Route):
                req = route.request
                if req.resource_type in ["image", "font", "media", "stylesheet"] or any(b in req.url for b in BLOCKED_DOMAINS):
                    await route.abort()
                else:
                    await route.continue_()

            await self.context.route("**/*", route_interceptor)
        except Exception:
            pass

    async def close(self):
        """Closes all clients and browsers cleanly."""
        if self.http_client:
            await self.http_client.aclose()
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

    def get_clinic_key(self, listing_path: str, practice_name: str) -> str:
        """Derives a normalized clinic identifier."""
        if listing_path:
            m = re.search(r"/medical-centres/[^/]+/([^/]+)", listing_path)
            if m:
                return m.group(1).lower().strip()
        return practice_name.lower().strip()

    async def fetch_clinic_profile(self, listing_path: str, clinic_key: str) -> Dict[str, Any]:
        """
        Fetches clinic details (phone, doctor qualifications, gender) via the API.
        Deduplicates in-flight calls and caches results across the entire scraping run.
        """
        if not clinic_key or not listing_path:
            return {}

        if clinic_key in self.clinic_cache:
            return self.clinic_cache[clinic_key]

        if clinic_key in self.in_flight_clinics:
            return await self.in_flight_clinics[clinic_key]

        async def _do_fetch() -> Dict[str, Any]:
            try:
                async with self.clinic_sem:
                    if clinic_key in self.clinic_cache:
                        return self.clinic_cache[clinic_key]

                    enc = urllib.parse.quote(urllib.parse.quote(listing_path, safe=""), safe="")
                    url = f"https://www.hotdoc.com.au/api/patient/pages?path={enc}"
                    result_data: Dict[str, Any] = {
                        "phone": "",
                        "address": "",
                        "doctors": {}
                    }

                    if self.http_client:
                        for retry in range(4):
                            try:
                                headers = self.get_headers("application/au.com.hotdoc.v6")
                                resp = await self.http_client.get(url, headers=headers)
                                if resp.status_code == 200:
                                    data = resp.json()
                                    c_obj = data.get("clinic", {}) or {}
                                    result_data["phone"] = c_obj.get("phone", "")
                                    result_data["address"] = c_obj.get("street_address", "")

                                    for d in data.get("doctors", []):
                                        d_slug = (d.get("slug") or "").lower().strip()
                                        d_name = (d.get("full_name") or "").strip()
                                        g = d.get("gender")
                                        quals = d.get("qualifications", [])
                                        q_str = ", ".join(quals) if isinstance(quals, list) else str(quals)
                                        spec = d.get("specialty") or "General Practitioner"

                                        d_info = {
                                            "gender": g.capitalize() if g else "",
                                            "qualifications": q_str,
                                            "role": spec
                                        }
                                        if d_slug:
                                            result_data["doctors"][d_slug] = d_info
                                        if d_name:
                                            norm_name = re.sub(r"[^\w\s]", "", d_name.lower())
                                            result_data["doctors"][norm_name] = d_info

                                    self.clinic_cache[clinic_key] = result_data
                                    return result_data
                                elif resp.status_code == 429:
                                    # Rotate UA immediately on 429
                                    await asyncio.sleep(1.0)
                                else:
                                    await asyncio.sleep(0.3)
                            except Exception:
                                await asyncio.sleep(0.5)

                    self.clinic_cache[clinic_key] = result_data
                    return result_data
            finally:
                self.in_flight_clinics.pop(clinic_key, None)

        task = asyncio.create_task(_do_fetch())
        self.in_flight_clinics[clinic_key] = task
        return await task

    async def scrape_hub_api(self, hub_info: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        Superfast direct API extraction for a medical hub.
        Paginates through search API with rate pacing and concurrently enriches clinic profiles.
        """
        suburb_slug = hub_info.get("url_slug", "")
        base_url = f"https://www.hotdoc.com.au/api/patient/search?entities=doctors&filters=specialty-27&suburb={suburb_slug}"
        url = base_url

        all_doctors: List[Dict[str, Any]] = []
        all_clinics: Dict[int, Dict[str, Any]] = {}
        all_suburbs: Dict[int, Dict[str, Any]] = {}

        if not self.http_client:
            return []

        # 1. Paginate through search pages under search semaphore to avoid burst limit
        first_page = True
        while True:
            resp = None
            async with self.search_sem:
                for retry in range(4):
                    try:
                        headers = self.get_headers("application/au.com.hotdoc.v5")
                        resp = await self.http_client.get(url, headers=headers)
                        if resp.status_code == 200:
                            break
                        elif resp.status_code == 400 and first_page and "-city-" in suburb_slug:
                            suburb_slug = suburb_slug.replace("-city-", "-")
                            base_url = f"https://www.hotdoc.com.au/api/patient/search?entities=doctors&filters=specialty-27&suburb={suburb_slug}"
                            url = base_url
                            continue
                        elif resp.status_code == 429:
                            await asyncio.sleep(1.5)
                        else:
                            await asyncio.sleep(0.3)
                    except Exception:
                        await asyncio.sleep(0.5)

            first_page = False
            if not resp or resp.status_code != 200:
                break

            try:
                data = resp.json()
            except Exception:
                break

            docs = data.get("doctors", [])
            if not docs:
                break

            all_doctors.extend(docs)
            for c in data.get("clinics", []):
                all_clinics[c["id"]] = c
            for s in data.get("suburbs", []):
                all_suburbs[s["id"]] = s

            cursor = data.get("meta", {}).get("paging", {}).get("next_cursor")
            if not cursor:
                break
            url = f"{base_url}&cursor={urllib.parse.quote(cursor)}"
            await asyncio.sleep(0.05)

        if not all_doctors:
            return []

        # 2. Concurrently fetch clinic profiles for all clinics in this hub
        needed_clinic_ids = set(d.get("clinic_id") for d in all_doctors if d.get("clinic_id"))
        clinic_tasks = []
        for cid in needed_clinic_ids:
            c = all_clinics.get(cid)
            if not c:
                continue
            c_path = c.get("listing_path", "")
            c_name = c.get("name", "")
            c_key = self.get_clinic_key(c_path, c_name)
            if c_key and c_path:
                clinic_tasks.append(self.fetch_clinic_profile(c_path, c_key))

        if clinic_tasks:
            await asyncio.gather(*clinic_tasks, return_exceptions=True)

        # 3. Assemble full 12-column records
        records: List[Dict[str, Any]] = []
        for doc in all_doctors:
            d_name, d_role = parse_doctor_name(doc.get("full_name", ""))
            cid = doc.get("clinic_id")
            c_obj = all_clinics.get(cid, {}) if cid else {}
            practice_name = c_obj.get("name", "")

            c_path = c_obj.get("listing_path", "")
            c_key = self.get_clinic_key(c_path, practice_name)
            c_enriched = self.clinic_cache.get(c_key, {})

            phone = format_phone_number(c_enriched.get("phone", ""))

            # Suburb / State / Postcode
            sid = c_obj.get("suburb_id")
            s_obj = all_suburbs.get(sid, {}) if sid else {}
            suburb = s_obj.get("name") or hub_info.get("suburb", "")
            state = s_obj.get("state") or hub_info.get("state", "")
            postcode = s_obj.get("postcode") or hub_info.get("postcode", "")

            street_address = c_enriched.get("address") or c_obj.get("street_address", "")
            st, sub, st_state, pc = parse_address(street_address, suburb, state, postcode)

            # Doctor-specific gender, qualifications, role
            doc_slug = (doc.get("slug") or "").lower().strip()
            norm_name = re.sub(r"[^\w\s]", "", d_name.lower())
            doc_details = c_enriched.get("doctors", {}).get(doc_slug) or c_enriched.get("doctors", {}).get(norm_name, {})

            gender = doc_details.get("gender", "")
            qual_codes = doc_details.get("qualifications", "")
            role = doc_details.get("role") or d_role or "General Practitioner"

            listing_path = doc.get("listing_path", "")
            profile_url = f"https://www.hotdoc.com.au{listing_path}" if listing_path else ""

            records.append({
                "doctor_name": d_name,
                "role": role,
                "gender": gender,
                "qualification_codes": qual_codes,
                "practice_name": practice_name,
                "address": st or street_address,
                "suburb": sub or suburb,
                "state": st_state or state,
                "postcode": pc or postcode,
                "phone_number": phone,
                "search_hub_reference": hub_info.get("reference_label", ""),
                "profile_url": profile_url,
            })

        return records

    async def scrape_hub_playwright(self, hub_info: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        Playwright DOM scraping fallback if the API is unreachable.
        """
        await self.ensure_playwright()
        if not self.context:
            return []

        url_slug = hub_info.get("url_slug", "")
        records: List[Dict[str, Any]] = []
        page = await self.context.new_page()

        try:
            url = f"https://www.hotdoc.com.au/search?as=practitioner&filters=specialty-27&in={url_slug}"
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)

            try:
                await page.wait_for_selector(".ProviderCard", timeout=8000)
            except Exception:
                base_url = f"https://www.hotdoc.com.au/search?filters=specialty-27&in={url_slug}"
                await page.goto(base_url, wait_until="domcontentloaded", timeout=12000)
                try:
                    p_link = await page.wait_for_selector('a[href*="as=practitioner"], a:has-text("Practitioners")', timeout=6000)
                    if p_link:
                        await p_link.click()
                        await page.wait_for_selector(".ProviderCard", timeout=8000)
                except Exception:
                    pass

            while True:
                try:
                    await page.wait_for_selector(".ProviderCard", timeout=5000)
                except Exception:
                    break

                raw_cards = await page.evaluate("""() => {
                    const cards = Array.from(document.querySelectorAll('.ProviderCard'));
                    return cards.map(c => {
                        const linkEl = c.querySelector('a.ProviderCard-wrapper') || c.querySelector('a');
                        const href = linkEl ? linkEl.getAttribute('href') : '';
                        const titleEl = c.querySelector('.ProviderCard-title');
                        const title = titleEl ? titleEl.innerText.trim() : '';
                        const lineEl = c.querySelector('.ProviderCard-line');
                        const line = lineEl ? lineEl.innerText.trim() : '';
                        const fullText = c.innerText.trim();
                        return { href, title, line, fullText };
                    });
                }""")

                if not raw_cards:
                    break

                for card_data in raw_cards:
                    doctor_rec = parse_doctor_card(card_data, hub_info)
                    records.append(doctor_rec)

                next_btn = await page.query_selector("a.SearchResultsPage-pagingLink--right")
                if next_btn:
                    try:
                        await next_btn.click()
                        await page.wait_for_timeout(1000)
                    except Exception:
                        break
                else:
                    break

        except Exception:
            pass
        finally:
            await page.close()

        return records

    async def scrape_hub(self, hub_info: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        Orchestrates scraping for a single hub.
        Uses the superfast API by default, falling back to Playwright if needed.
        """
        records = await self.scrape_hub_api(hub_info)
        if not records:
            records = await self.scrape_hub_playwright(hub_info)
        return records
