#!/usr/bin/env python3
"""The government-services query set: things a resident looks for on their state's sites, for every
state. Public data, no labels: the answer is decided by rule in run.py.

    evalset.py > gov-services.tsv               (id, state code, task, form, query)
    evalset.py --heldout > gov-heldout.tsv      the held-out tasks: TEST ONLY, never trained or tuned on

20 tasks x 50 states x 2 forms: the state's name ("new jersey fishing license") and its postal code as
people type it ("nj fishing license").
"""
STATES = """AL Alabama|AK Alaska|AZ Arizona|AR Arkansas|CA California|CO Colorado|CT Connecticut|DE Delaware|FL Florida|GA Georgia|HI Hawaii|ID Idaho|IL Illinois|IN Indiana|IA Iowa|KS Kansas|KY Kentucky|LA Louisiana|ME Maine|MD Maryland|MA Massachusetts|MI Michigan|MN Minnesota|MS Mississippi|MO Missouri|MT Montana|NE Nebraska|NV Nevada|NH New Hampshire|NJ New Jersey|NM New Mexico|NY New York|NC North Carolina|ND North Dakota|OH Ohio|OK Oklahoma|OR Oregon|PA Pennsylvania|RI Rhode Island|SC South Carolina|SD South Dakota|TN Tennessee|TX Texas|UT Utah|VT Vermont|VA Virginia|WA Washington|WV West Virginia|WI Wisconsin|WY Wyoming"""
STATES = [tuple(s.split(" ", 1)) for s in STATES.split("|")]

# task -> the wording, {s} the state. The words a helping page carries are in run.py (TOPIC).
TASKS = {
    "drivers-license": "{s} drivers license renewal",
    "vehicle-registration": "renew car registration {s}",
    "real-id": "{s} real id requirements",
    "voter-registration": "register to vote in {s}",
    "unemployment": "{s} unemployment benefits apply",
    "birth-certificate": "{s} birth certificate copy",
    "business-registration": "register an llc in {s}",
    "tax-refund": "{s} state tax refund status",
    "fishing-license": "{s} fishing license",
    "firearms-permit": "{s} firearms permit",
    "food-assistance": "apply for food stamps {s}",
    "medicaid": "{s} medicaid application",
    "nursing-license": "{s} nursing license lookup",
    "state-parks": "{s} state parks camping reservations",
    "child-support": "{s} child support payment",
    "jury-duty": "{s} jury duty",
    "marriage-license": "{s} marriage license",
    "notary": "become a notary in {s}",
    "traffic-ticket": "pay traffic ticket {s}",
    "unclaimed-property": "{s} unclaimed property",
}

# Ten tasks no model or rule was ever trained, tuned or chosen on (frozen 2026-11-03, before the gov fine-tune of the
# reranker): the test of whether a change carries to government tasks it was not shown. Run once per candidate.
HELDOUT_TASKS = {
    "hunting-license": "{s} hunting license",
    "title-transfer": "{s} vehicle title transfer",
    "income-tax-forms": "{s} income tax forms",
    "boat-registration": "{s} boat registration",
    "cdl": "how to get a cdl in {s}",
    "death-certificate": "{s} death certificate",
    "workers-comp": "{s} workers compensation claim",
    "contractor-license": "{s} contractor license lookup",
    "small-claims": "{s} small claims court",
    "road-conditions": "{s} road conditions",
}


def rows(tasks=None):
    for code, name in STATES:
        for task, form in (tasks or TASKS).items():
            yield f"{code}-{task}-name", code, task, "name", form.format(s=name.lower())
            yield f"{code}-{task}-code", code, task, "code", form.format(s=code.lower())


if __name__ == "__main__":
    import sys
    for r in rows(HELDOUT_TASKS if "--heldout" in sys.argv[1:] else None):
        print("\t".join(r))
