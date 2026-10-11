"""Fictional resume/document content for exercising pdf_tools renderers."""

RESUME = {
    "name": "Jordan Ellis",
    "headline": "Retail & Operations Leader",
    "contact": {
        "location": "Springfield, IL 62701",
        "phone": "(555) 010-2034",
        "email": "jordan.ellis@example.com",
        "links": [{"label": "LinkedIn Profile", "url": "linkedin.com/in/jordan-ellis-example"}],
    },
    "summary": "Operations-minded retail leader with a decade of experience running stores and multi-site districts, "
               "from hiring and scheduling through sales strategy, payroll and merchandising standards.",
    "experience": [
        {"title": "Store Manager", "company": "Northwind Outfitters", "location": "Springfield, IL", "start": "July 2025", "end": "Present",
         "bullets": ["Manage scheduling, recruiting, hiring, and training of store employees",
                     "Supervise staff to maintain performance goals aligned with corporate objectives",
                     "Track sales performance and execute targeted sales strategies",
                     "Drive implementation of corporate strategies and visual floor plans",
                     "Cover open shifts to ensure continuous store operations"]},
        {"title": "Store Manager", "company": "Contoso Nutrition", "location": "Springfield, IL", "start": "June 2021", "end": "June 2025",
         "bullets": ["Managed recruiting, hiring, training, and day-to-day supervision of store team",
                     "Developed and executed sales strategies to meet or exceed corporate targets",
                     "Tracked key sales metrics and adjusted tactics accordingly",
                     "Oversaw staff scheduling, shift coverage, and operational continuity"]},
        {"title": "District Manager", "company": "Fabrikam Retail Services", "location": "St. Louis, MO", "start": "February 2019", "end": "June 2021",
         "bullets": ["Managed multiple retail locations, overseeing recruiting, hiring, and training",
                     "Coordinated staff scheduling and shift coverage across all locations",
                     "Monitored and reported on sales performance and KPIs district-wide",
                     "Ensured consistent execution of brand and merchandising standards"]},
        {"title": "Wireless Specialist", "company": "Adventure Works Wireless", "location": "Desloge, MO", "start": "March 2018", "end": "February 2019",
         "bullets": ["Drove individual sales performance and customer acquisition",
                     "Executed leadership-defined sales strategies on the floor"]},
        {"title": "Manager on Duty", "company": "Tailspin Market", "location": "Springfield, IL", "start": "March 2017", "end": "March 2018",
         "bullets": ["Trained and developed new front-end team members",
                     "Managed front-end scheduling and payroll submission for the store"]},
        {"title": "Teammate / Managerial Assistant", "company": "Tailspin Market", "location": "Springfield, IL", "start": "May 2015", "end": "March 2017",
         "bullets": ["Provided efficient customer service and resolution on the front end",
                     "Performed stocking, nightly bookkeeping, and cash reconciliation"]},
    ],
    "education": [{"degree": "High School Diploma", "school": "Lincoln County High School", "start": "2011", "end": "2015"}],
    "skills": [{"group": "Leadership", "items": ["Store Management", "District Management", "Team Leadership", "Recruiting & Training", "Conflict Management"]},
               {"group": "Operations", "items": ["Sales Strategy", "Profit & Loss", "Payroll Processing", "Multi-Unit Management", "Bookkeeping"]},
               {"group": "Technical", "items": ["Microsoft 365", "Excel", "Google Sheets", "Computer Networking", "IT Support"]}],
    "certifications": [
        {"name": "Technical Support Fundamentals", "issuer": "Example Academy", "date": "Nov 2022"},
        {"name": "System Administration & IT Infrastructure", "issuer": "Example Academy", "date": "Dec 2022"},
    ],
    "languages": ["English"],
}

LONG_RESUME = {
    **RESUME,
    "experience": RESUME["experience"] + [
        {"title": f"Regional Lead {i}", "company": f"Company {i}", "location": "Chicago, IL", "start": f"{2000 + i}", "end": f"{2001 + i}",
         "bullets": [f"Owned a measurable outcome number {n} for the region and reported it to leadership each quarter" for n in range(5)]}
        for i in range(6)
    ],
}

DOCUMENT = {
    "title": "Quarterly Operations Review",
    "subtitle": "Store performance, staffing and next steps",
    "byline": "Prepared by Jordan Ellis · October 2026",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Summary"},
        {"type": "paragraph", "text": "Sales finished the quarter ahead of plan while labor hours came in under budget."},
        {"type": "callout", "title": "Key takeaway", "text": "Two stores drove most of the variance; both are covered in the next section."},
        {"type": "heading", "level": 2, "text": "Results by store"},
        {"type": "table", "headers": ["Store", "Sales", "vs. plan", "Labor %"],
         "rows": [["Springfield", "$412,300", "+4.1%", "11.8%"], ["St. Louis", "$388,900", "-1.2%", "12.6%"], ["Desloge", "$201,450", "+7.9%", "10.9%"]]},
        {"type": "heading", "level": 2, "text": "Next steps"},
        {"type": "numbered", "items": ["Backfill two open shifts at St. Louis", "Roll out the new floor plan", "Review payroll timing"]},
        {"type": "bullets", "items": ["Hiring is on track", "Training completion is at 94%"]},
    ],
}
