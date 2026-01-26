# japandev.py
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Sequence
from urllib.parse import urljoin
import time

from playwright.sync_api import sync_playwright, expect

from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Location,
    Country,
    DescriptionFormat,
    Compensation,
    CompensationInterval,
)

from jobspy.scrapers.utils import (
    create_playwright_context,
    setup_page,
    parse_proxy_string,
)

try:
    # package import
    from .japandev_enums import (
        FilterEnum,
        JdApplicantLocation,
        JdJapaneseLevel,
        JdEnglishLevel,
        JdRemoteWork,
        JdSeniority,
        JdSalary,
        JdJobType,
        JdOfficeLocation,
        JdCompanyType,
        JdSkill,
    )
except Exception:
    # local script import
    from japandev_enums import (
        FilterEnum,
        JdApplicantLocation,
        JdJapaneseLevel,
        JdEnglishLevel,
        JdRemoteWork,
        JdSeniority,
        JdSalary,
        JdJobType,
        JdOfficeLocation,
        JdCompanyType,
        JdSkill,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RawFilter:
    """
    Optional escape hatch:
      - key + token -> will click element with id "{key}-{token}".
    Useful if JapanDev adds new skills/options not yet in enums. You should really add the enum value though.
    """
    key: str
    token: str

    @property
    def selector(self) -> str:
        return f"[id='{self.key}-{self.token}']"


class JapanDev(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        site = Site(Site.JAPANDEV)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.base_url = "https://japan-dev.com/japan-jobs-relocation"

    def _parse_salary_to_comp(self, salary_text: str | None) -> Compensation | None:
        if not salary_text:
            return None

        # Examples seen on JapanDev detail pages:
        # "10M 14M yr", "8.5M 12M", etc.
        matches = re.findall(r"(\d+(?:\.\d+)?)", salary_text.replace(",", ""))
        if len(matches) < 1:
            return None

        try:
            min_amount = float(matches[0]) * 1_000_000
            max_amount = float(matches[1]) * 1_000_000 if len(matches) > 1 else None
            return Compensation(
                interval=CompensationInterval.YEARLY,
                min_amount=min_amount,
                max_amount=max_amount,
                currency="JPY",
            )
        except Exception:
            return None

    def _extract_detail_fields(self, detail_page, scraper_input: ScraperInput) -> dict:
        # Title
        title = None
        title_el = detail_page.locator("h1.job-detail__job-name").first
        if title_el.count() > 0:
            title = title_el.inner_text().strip()

        # Company
        company_name = None
        company_el = detail_page.locator("a.job-logo__company-name").first
        if company_el.count() > 0:
            company_name = company_el.inner_text().strip()

        # Location
        location_text = None
        loc_el = detail_page.locator("div.job-logo__location").first
        if loc_el.count() > 0:
            location_text = loc_el.inner_text().strip()
        else:
            summary_first = detail_page.locator("ul.job-detail__summary-list li span").first
            if summary_first.count() > 0:
                location_text = summary_first.inner_text().strip()

        # Date posted
        posted = None
        summary_spans = detail_page.locator("ul.job-detail__summary-list li span").all()
        if summary_spans:
            maybe_date = summary_spans[-1].inner_text().strip()
            try:
                posted = datetime.strptime(maybe_date, "%B %d, %Y").date()
            except Exception:
                posted = None

        # Salary tag (yen icon)
        salary_text = None
        salary_tag = detail_page.locator(
            "div.job-detail-tag-list__basic-tag:has(img[alt='yen-icon'])"
        ).first
        if salary_tag.count() > 0:
            salary_desc = salary_tag.locator("div.job-detail-tag-list__tag-desc").first
            if salary_desc.count() > 0:
                salary_text = salary_desc.inner_text().strip()

        # Apply link
        job_url_direct = None
        apply_el = detail_page.locator("a:has-text('APPLY NOW')").first
        if apply_el.count() > 0:
            job_url_direct = apply_el.get_attribute("href")

        # Description
        description = None
        body_el = detail_page.locator("div.job-detail-main-content div.body").first
        if body_el.count() == 0:
            body_el = detail_page.locator("div.job-detail-main-content").first

        if body_el.count() > 0:
            if scraper_input.description_format == DescriptionFormat.HTML:
                description = body_el.inner_html()
            else:
                description = body_el.inner_text()

        return {
            "title": title,
            "company_name": company_name,
            "location_text": location_text,
            "salary_text": salary_text,
            "job_url_direct": job_url_direct,
            "description": description,
            "date_posted": posted,
        }

    def _click_filter(self, page, option: FilterEnum | _RawFilter) -> None:
        """Ensure filter is selected (toggle ON if currently OFF)."""
        full_id = option.full_id
        loc = page.locator(f"[id='{full_id}']")
        selected_re = re.compile(r".*\bselected\b.*")
        
        # Check current state reliably
        try:
            # Wait for element to be stable
            loc.wait_for(state="visible", timeout=2000)
            
            # Check if already selected
            current_classes = loc.get_attribute("class") or ""
            is_selected = "selected" in current_classes
            
            if is_selected:
                logger.debug(f"Filter {full_id} already selected, skipping")
                return  # Don't click if already selected
                
        except Exception as e:
            logger.warning(f"Could not check state for {full_id}: {e}")
        
        # Not selected, click to select it
        for attempt in range(3):
            try:
                loc.scroll_into_view_if_needed()
                loc.click(force=(attempt > 0), no_wait_after=True)
                
                # Verify it's now selected
                expect(loc).to_have_class(selected_re, timeout=2000)
                logger.debug(f"Successfully selected filter {full_id}")
                return
                
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"Failed to select filter {full_id} after 3 attempts: {e}")
                continue


    def _convert_to_enum(self, value: str | FilterEnum, enum_class) -> FilterEnum:
        """Convert string to enum object. Supports both enum values and instances."""
        if isinstance(value, FilterEnum):
            return value
        # Try to find enum by value (e.g., "japanese_level_not_required")
        for member in enum_class:
            if member.value == value:
                return member
        # If not found, raise a helpful error
        valid_values = [m.value for m in enum_class]
        raise ValueError(f"Invalid {enum_class.__name__} value: '{value}'. Valid values: {valid_values}")

    def _apply_filters(
        self,
        page,
        scraper_input: ScraperInput,
        *,
        applicant_locations: Optional[Sequence[JdApplicantLocation | str]] = None,
        japanese_levels: Optional[Sequence[JdJapaneseLevel | str]] = None,
        english_levels: Optional[Sequence[JdEnglishLevel | str]] = None,
        remote_work: Optional[Sequence[JdRemoteWork | str]] = None,
        seniorities: Optional[Sequence[JdSeniority | str]] = None,
        salary_filters: Optional[Sequence[JdSalary | str]] = None,
        job_types: Optional[Sequence[JdJobType | str]] = None,
        office_locations: Optional[Sequence[JdOfficeLocation | str]] = None,
        company_types: Optional[Sequence[JdCompanyType | str]] = None,
        skills: Optional[Sequence[JdSkill | str]] = None,
        raw_filters: Optional[Sequence[_RawFilter]] = None,
    ) -> None:
        # Search term (Algolia search box)
        if scraper_input.search_term:
            try:
                box = page.locator(".ais-SearchBox-input").first
                if box.count() > 0:
                    box.fill(scraper_input.search_term)
                    box.press("Enter")
                    try:
                        page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
            except Exception:
                pass

        # Apply explicit filters - convert strings to enums
        filter_groups = [
            (applicant_locations, JdApplicantLocation),
            (japanese_levels, JdJapaneseLevel),
            (english_levels, JdEnglishLevel),
            (remote_work, JdRemoteWork),
            (seniorities, JdSeniority),
            (salary_filters, JdSalary),
            (job_types, JdJobType),
            (office_locations, JdOfficeLocation),
            (company_types, JdCompanyType),
            (skills, JdSkill),
        ]
        
        for group, enum_class in filter_groups:
            if not group:
                continue
            for opt in group:
                # Convert string to enum if needed
                enum_opt = self._convert_to_enum(opt, enum_class) if isinstance(opt, str) else opt
                self._click_filter(page, enum_opt)

        if raw_filters:
            for rf in raw_filters:
                self._click_filter(page, rf)

        # If caller just says "remote", apply all remote-ish options (excluding "No Remote")
        if scraper_input.is_remote and not remote_work:
            for opt in (
                JdRemoteWork.PARTIAL_REMOTE,
                JdRemoteWork.FULL_REMOTE,
                JdRemoteWork.ANYWHERE_IN_JAPAN,
                JdRemoteWork.WORLDWIDE,
            ):
                self._click_filter(page, opt)

        # Once all filters are applied, we wait for network idle settlement.
        try:
            # Wait for all filter requests to complete (networkidle)
            page.wait_for_load_state("networkidle", timeout=scraper_input.request_timeout * 1000)
        except Exception:
            pass

    def scrape(
        self,
        scraper_input: ScraperInput,
        *,
        applicant_locations: Optional[Sequence[JdApplicantLocation | str]] = None,
        japanese_levels: Optional[Sequence[JdJapaneseLevel | str]] = None,
        english_levels: Optional[Sequence[JdEnglishLevel | str]] = None,
        remote_work: Optional[Sequence[JdRemoteWork | str]] = None,
        seniorities: Optional[Sequence[JdSeniority | str]] = None,
        salary_filters: Optional[Sequence[JdSalary | str]] = None,
        job_types: Optional[Sequence[JdJobType | str]] = None,
        office_locations: Optional[Sequence[JdOfficeLocation | str]] = None,
        company_types: Optional[Sequence[JdCompanyType | str]] = None,
        skills: Optional[Sequence[JdSkill | str]] = None,
        raw_filters: Optional[Sequence[_RawFilter]] = None,
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

            page = setup_page(context, block_resources=True)

            logger.info(f"Scraping JapanDev at {self.base_url}")
            try:
                page.goto(self.base_url)
                page.wait_for_selector(".filters", timeout=scraper_input.request_timeout * 1000)
            except Exception as e:
                logger.error(f"Failed to load JapanDev listing page: {e}")
                return JobResponse(jobs=[])

            # Apply UI filters
            self._apply_filters(
                page,
                scraper_input,
                applicant_locations=applicant_locations,
                japanese_levels=japanese_levels,
                english_levels=english_levels,
                remote_work=remote_work,
                seniorities=seniorities,
                salary_filters=salary_filters,
                job_types=job_types,
                office_locations=office_locations,
                company_types=company_types,
                skills=skills,
                raw_filters=raw_filters,
            )

            # Wait for network idle after sending filter update request so it is reflected in the UI
            page.wait_for_load_state("networkidle", timeout=scraper_input.request_timeout * 1000)

            # Get listing cards
            job_cards = []
            try:
                page.wait_for_selector(".job-item, .top-jobs__job-item, .no-results", timeout=5000)
            except Exception:
                pass

            job_cards = page.locator(".job-item").all()
            if not job_cards:
                job_cards = page.locator(".top-jobs__job-item").all()

            for card in job_cards:
                if len(job_list) >= scraper_input.results_wanted:
                    break

                try:
                    title_el = card.locator(".job-item__title").first
                    if title_el.count() == 0:
                        title_el = card.locator("a.title.link").first
                    if title_el.count() == 0:
                        continue

                    title = title_el.inner_text().strip()
                    job_url_relative = title_el.get_attribute("href")
                    if not job_url_relative:
                        continue

                    job_url = urljoin(self.base_url, job_url_relative)

                    # Company name fallback (detail page overrides)
                    company_name = None
                    img_el = card.locator("img.company-logo__inner").first
                    if img_el.count() > 0:
                        company_name = img_el.get_attribute("alt")

                    detail_page = setup_page(context, block_resources=True)
                    try:
                        detail_page.goto(job_url)
                        detail = self._extract_detail_fields(detail_page, scraper_input)
                    except Exception as e:
                        logger.warning(f"Failed to extract details for {job_url}: {e}")
                        detail = {
                            "title": title,
                            "company_name": company_name,
                            "location_text": "Japan",
                            "salary_text": None,
                            "job_url_direct": None,
                            "description": None,
                            "date_posted": date.today(),
                        }
                    finally:
                        detail_page.close()

                    final_title = detail["title"] or title
                    final_company = detail["company_name"] or company_name
                    final_location_text = detail["location_text"] or "Japan"
                    comp = self._parse_salary_to_comp(detail["salary_text"])

                    loc = Location(
                        country=Country.JAPAN,
                        city=final_location_text,
                    )

                    job_list.append(
                        JobPost(
                            title=final_title,
                            company_name=final_company,
                            job_url=job_url,
                            job_url_direct=detail["job_url_direct"],
                            location=loc,
                            description=detail["description"],
                            compensation=comp,
                            date_posted=detail["date_posted"] or date.today(),
                        )
                    )

                except Exception as e:
                    logger.warning(f"Error parsing job card: {e}")
                    continue

            return JobResponse(jobs=job_list)
