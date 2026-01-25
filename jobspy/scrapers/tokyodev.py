from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional, List, Tuple
from urllib.parse import urljoin, urlencode

from playwright.sync_api import sync_playwright

from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Location,
    Country,
    DescriptionFormat,
    JobType,
    Compensation,
    CompensationInterval,
)
from jobspy.scrapers.utils import (
    create_playwright_context,
    setup_page,
    parse_proxy_string,
    wait_for_cloudflare_to_clear,
)

from enum import Enum
from jobspy.scrapers.tokyodev_enums import JapaneseLevel, EnglishLevel, ApplicantLocation, Seniority, Salary

logger = logging.getLogger(__name__)


@dataclass
class JobSeed:
    job_url: str
    company_name: str | None
    skills: list[str]
    is_remote_hint: bool
    salary_text_hint: str | None
    tag_texts: list[str]


class TokyoDev(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        site = Site(Site.TOKYODEV)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.base_url = "https://www.tokyodev.com"

    def _parse_salary_to_comp(self, salary_text: str | None) -> Compensation | None:
        """
        Accepts list-page style: "¥7.5M ~ ¥14M"
        or detail-page style: "¥14M ~ ¥21M annually"
        """
        if not salary_text:
            return None

        text = salary_text.replace(",", "").strip()

        # TokyoDev examples show JPY with ¥ in the header/list tags.
        currency = "JPY" if "¥" in text or "JPY" in text.upper() else None

        # Handle both "14M" and "14.5M" forms (and tolerate trailing words like "annually").
        nums = re.findall(r"(\d+(?:\.\d+)?)\s*M", text.upper())
        if not nums:
            return None

        amounts = [float(x) * 1_000_000 for x in nums]
        min_amount = amounts[0]
        max_amount = amounts[1] if len(amounts) > 1 else None

        return Compensation(
            interval=CompensationInterval.YEARLY,
            min_amount=min_amount,
            max_amount=max_amount,
            currency=currency or "JPY",
        )

    def _build_jobs_url(
        self,
        scraper_input: ScraperInput,
        min_salary: Optional[Salary | str],
        japanese_requirements: Optional[List[JapaneseLevel | str]],
        english_requirements: Optional[List[EnglishLevel | str]],
        applicant_locations: Optional[List[ApplicantLocation | str]],
        seniorities: Optional[List[Seniority | str]],
        categories: Optional[List[str]],
    ) -> str:
        query_params: list[tuple[str, str]] = []

        # 1. Query
        query_params.append(("query[]", scraper_input.search_term or ""))

        # 2. Japanese Requirements
        if japanese_requirements:
            for req in japanese_requirements:
                val = req.value if isinstance(req, Enum) else req
                query_params.append(("japanese_requirement[]", val))

        # 3. English Requirements
        if english_requirements:
            for req in english_requirements:
                val = req.value if isinstance(req, Enum) else req
                query_params.append(("english_requirement[]", val))

        # 4. Remote Policy (Derived from scraper_input.is_remote)
        if scraper_input.is_remote:
            query_params.append(("remote_policy[]", "fully_remote"))
            query_params.append(("remote_policy[]", "partially_remote"))

        # 5. Applicant Locations
        if applicant_locations:
            for loc in applicant_locations:
                val = loc.value if isinstance(loc, Enum) else loc
                query_params.append(("applicant_location[]", val))

        # 6. Seniority
        if seniorities:
            for s in seniorities:
                val = s.value if isinstance(s, Enum) else s
                query_params.append(("seniority[]", val))

        # 7. Categories
        if categories:
            for c in categories:
                query_params.append(("category[]", c))

        # 8. Salary (Always append, even if empty, to match target format)
        salary_val = ""
        if min_salary:
            salary_val = min_salary.value if isinstance(min_salary, Enum) else str(min_salary)
        query_params.append(("salary", salary_val))

        return f"{self.base_url}/jobs?{urlencode(query_params)}"


    def _extract_seeds_from_list_page(self, page, results_wanted: int) -> list[JobSeed]:
        """
        Extract (job_url, company_name, skills/tags) from the aggregation page.
        """
        seeds: list[JobSeed] = []
        company_cards = page.locator("ul.list-inside > li").all()

        for card in company_cards:
            if len(seeds) >= results_wanted:
                break

            try:
                company_name = card.locator("h3 a").first.inner_text().strip()
            except Exception:
                company_name = None

            # Each job row is a div[data-collapsable-list-target='item'] containing h4 a.
            job_items = card.locator("div[data-collapsable-list-target='item']").all()

            for item in job_items:
                if len(seeds) >= results_wanted:
                    break

                try:
                    title_link = item.locator("h4 a").first
                    href = title_link.get_attribute("href")
                    if not href:
                        continue
                    job_url = urljoin(self.base_url, href)

                    tag_links = item.locator("div.flex.gap-2 a").all()
                    tag_texts: list[str] = []
                    salary_text_hint: str | None = None
                    is_remote_hint = False
                    skills: list[str] = []

                    for a in tag_links:
                        t = (a.inner_text() or "").strip()
                        tag_texts.append(t)
                        href2 = a.get_attribute("href") or ""
                        lt = t.lower()

                        # Salary tag commonly links to /jobs/salary-data.
                        if "/jobs/salary-data" in href2:
                            salary_text_hint = t
                            continue

                        if "remote" in lt:
                            is_remote_hint = True
                            continue

                        # Treat remaining tags as skills/categories (Android, iOS, Backend, etc.).
                        if t and "japanese" not in lt and "resident" not in lt:
                            skills.append(" ".join(t.split()))

                    seeds.append(
                        JobSeed(
                            job_url=job_url,
                            company_name=company_name,
                            skills=skills,
                            is_remote_hint=is_remote_hint,
                            salary_text_hint=salary_text_hint,
                            tag_texts=tag_texts,
                        )
                    )
                except Exception:
                    continue

        # Deduplicate by job_url
        uniq: dict[str, JobSeed] = {}
        for s in seeds:
            uniq.setdefault(s.job_url, s)
        return list(uniq.values())

    def _extract_header_requirements(self, detail_page) -> dict:
        """
        Extracts structured-ish header info without emojis:
        - company (safe selector within #job-header)
        - salary line containing ¥
        - japanese/english requirement strings (text contains 'Japanese'/'English')
        - remote boolean (header contains 'remote')
        """
        header = detail_page.locator("#job-header")
        out: dict = {
            "company_name": None,
            "salary_text": None,
            "japanese_req_text": None,
            "english_req_text": None,
            "is_remote": None,
        }

        # Company: link to /companies/... contains <span class="font-bold">Company</span>.
        try:
            out["company_name"] = (
                header.locator("a[href^='/companies/'] span.font-bold").first.inner_text().strip()
            )
        except Exception:
            pass

        # Salary: find a span containing "¥" within the header.
        try:
            salary_candidates = header.locator("xpath=.//span[contains(., '¥')]").all_inner_texts()
            # Prefer the one that looks like a range.
            for s in salary_candidates:
                s2 = " ".join(s.split())
                if "¥" in s2 and ("~" in s2 or "M" in s2.upper()):
                    out["salary_text"] = s2
                    break
        except Exception:
            pass

        # Language requirements: tooltip spans show "Business Japanese" etc.
        try:
            tooltip_texts = header.locator("[data-controller='tooltip']").all_inner_texts()
            for t in tooltip_texts:
                t2 = " ".join(t.split())
                if "Japanese" in t2 and not out["japanese_req_text"]:
                    out["japanese_req_text"] = t2
                if "English" in t2 and not out["english_req_text"]:
                    out["english_req_text"] = t2
        except Exception:
            pass

        # Remote: keyword check in header text (e.g., "Fully remote ...").
        try:
            out["is_remote"] = "remote" in header.inner_text().lower()
        except Exception:
            pass

        return out

    def scrape(
        self,
        scraper_input: ScraperInput,
        min_salary: Optional[str] = None,
        japanese_requirements: Optional[List[str]] = ["none", "basic"],
        english_requirements: Optional[List[str]] = None,
        applicant_locations: Optional[List[str]] = ["apply_from_abroad"],
        seniorities: Optional[List[str]] = ["intern", "junior", "intermediate"],
        categories: Optional[List[str]] = None,
    ) -> JobResponse:
        job_list: List[JobPost] = []

        proxy_str = None
        if self.proxies:
            if isinstance(self.proxies, list) and len(self.proxies) > 0:
                proxy_str = self.proxies[0]
            elif isinstance(self.proxies, str):
                proxy_str = self.proxies
        proxy = parse_proxy_string(proxy_str) if proxy_str else None

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            context = create_playwright_context(
                browser,
                proxy=proxy,
                user_agent=self.user_agent,
                request_timeout=scraper_input.request_timeout,
            )

            # Safer vs Cloudflare: do NOT block fonts/stylesheets; your utils only blocks images/media anyway.
            list_page = setup_page(context, block_resources=False)

            url = self._build_jobs_url(
                scraper_input,
                min_salary,
                japanese_requirements,
                english_requirements,
                applicant_locations,
                seniorities,
                categories,
            )

            logger.info(f"Scraping TokyoDev at {url}")

            try:
                list_page.goto(url)
                wait_for_cloudflare_to_clear(list_page, timeout_ms=scraper_input.request_timeout * 1000)
                list_page.wait_for_selector("ul.list-inside", timeout=scraper_input.request_timeout * 1000)
            except Exception as e:
                logger.error(f"Failed to load TokyoDev listing page: {e}")
                return JobResponse(jobs=[])

            seeds = self._extract_seeds_from_list_page(list_page, scraper_input.results_wanted)

            for seed in seeds:
                if len(job_list) >= scraper_input.results_wanted:
                    break

                detail_page = setup_page(context, block_resources=False)
                try:
                    detail_page.goto(seed.job_url)
                    wait_for_cloudflare_to_clear(detail_page, timeout_ms=scraper_input.request_timeout * 1000)

                    try:
                        title = detail_page.locator("h1").first.inner_text().strip()
                    except Exception:
                        title = seed.job_url.rsplit("/", 1)[-1].replace("-", " ").title()

                    header_info = self._extract_header_requirements(detail_page)

                    # Prefer detail header company name; fallback to list card company name.
                    company_name = header_info.get("company_name") or seed.company_name

                    # Description: keep using .prose if present (this is why we must visit the job page).
                    description = None
                    prose = detail_page.locator(".prose")
                    if prose.count() > 0:
                        description = (
                            prose.first.inner_html()
                            if scraper_input.description_format == DescriptionFormat.HTML
                            else prose.first.inner_text()
                        )
                    else:
                        description = detail_page.locator("body").inner_text()

                    # Merge language requirements into description (JobPost has no dedicated fields).
                    lang_bits = []
                    if header_info.get("japanese_req_text"):
                        lang_bits.append(header_info["japanese_req_text"])
                    if header_info.get("english_req_text"):
                        lang_bits.append(header_info["english_req_text"])
                    if lang_bits:
                        description = "Language requirements: " + " | ".join(lang_bits) + "\n\n" + (description or "")

                    # Salary: prefer detail header salary; fallback to list salary tag.
                    salary_text = header_info.get("salary_text") or seed.salary_text_hint
                    compensation = self._parse_salary_to_comp(salary_text)

                    # Remote: prefer detail header evaluation; fallback to list tags.
                    is_remote = header_info.get("is_remote")
                    if is_remote is None:
                        is_remote = seed.is_remote_hint

                    # Apply link: some jobs are modal-based; keep best-effort external link if present.
                    job_url_direct = None
                    try:
                        apply_a = detail_page.locator("a:has-text('Apply')").first
                        if apply_a.count() > 0:
                            job_url_direct = apply_a.get_attribute("href")
                    except Exception:
                        pass

                    job_post = JobPost(
                        title=title,
                        company_name=company_name,
                        job_url=seed.job_url,
                        job_url_direct=job_url_direct,
                        location=Location(country=Country.JAPAN, city="Tokyo"),
                        description=description,
                        is_remote=is_remote,
                        date_posted=date.today(),
                        job_type=[],
                        compensation=compensation,
                        skills=seed.skills,
                    )
                    job_list.append(job_post)

                except Exception as e:
                    logger.error(f"Failed to process job {seed.job_url}: {e}")
                finally:
                    detail_page.close()
                    time.sleep(0.3)

            return JobResponse(jobs=job_list)
