import csv
from jobspy import scrape_jobs
from jobspy.scrapers.tokyodev_enums import JapaneseLevel, EnglishLevel, ApplicantLocation, Seniority, Salary
from jobspy.scrapers.japandev_enums import *

if __name__ == "__main__":
    # jobs = scrape_jobs(
    #     # site_name=["google"], # google isn't very consistent with job postings on search results page, and also the constants can get outdated and replaced (this can be found in the request headers for the most part)
    #     site_name=["indeed", "linkedin", "glassdoor"], # "google", "bayt", "naukri", "bdjobs"
    #     search_term="software engineer",
    #     location="Tokyo",
    #     results_wanted=20,
    #     hours_old=24*7,
        
    #     # google_search_term="software engineer jobs near San Francisco, CA since yesterday",
    #     country_indeed='Japan',

    #     linkedin_fetch_description=True # gets more info such as description, direct job url (slower)
    #     # proxies=["208.195.175.46:65095", "208.195.175.45:65095", "localhost"],
    # )
    # print(f"Found {len(jobs)} jobs")
    # print(jobs.head())
    # jobs.to_csv("jobs.csv", quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False) # to_excel

    # jobs = scrape_jobs(site_name="tokyodev", results_wanted=50, location="Tokyo", japanese_requirements=[JapaneseLevel.NONE, JapaneseLevel.BASIC], applicant_locations=[ApplicantLocation.APPLY_FROM_ABROAD], seniorities=[Seniority.JUNIOR, Seniority.INTERMEDIATE])
    # print(jobs.head())
    # jobs.to_csv("tokyodev.csv", quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False) # to_excel
    
    jobs = scrape_jobs(site_name="japandev", results_wanted=10, applicant_locations=[JdApplicantLocation.ANYWHERE], japanese_levels=[JdJapaneseLevel.NOT_REQUIRED], seniorities=[JdSeniority.NEW_GRAD, JdSeniority.JUNIOR, JdSeniority.MID_LEVEL])
    print(jobs.head())
    jobs.to_csv("japandev.csv", quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False) # to_excel
