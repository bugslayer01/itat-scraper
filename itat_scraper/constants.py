"""Static data: endpoints, user-agent, bench and appeal-type codes."""

BASE = "https://itat.gov.in"
FORM_URL = f"{BASE}/judicial/casestatus"
IMG_URL = f"{BASE}/captcha/show"
AUDIO_URL = f"{BASE}/captcha/listen/"
CHECK_URL = f"{BASE}/Ajax/checkCaptcha"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# (connect, read) timeouts in seconds
HTTP_TIMEOUT = (10, 60)
PDF_TIMEOUT = (10, 180)

BENCH_CODES: dict[str, str] = {
    "Agra": "203", "Ahmedabad": "205", "Allahabad": "207", "Amritsar": "209",
    "Bangalore": "211", "Chandigarh": "215", "Chennai": "217", "Cochin": "219",
    "Cuttack": "221", "Dehradun": "260", "Delhi": "201", "Guwahati": "223",
    "Hyderabad": "225", "Indore": "227", "Jabalpur": "229", "Jaipur": "231",
    "Jodhpur": "233", "Kolkata": "235", "Lucknow": "237", "Mumbai": "199",
    "Nagpur": "239", "Panaji": "241", "Patna": "243", "Pune": "245",
    "Raipur": "247", "Rajkot": "249", "Ranchi": "251", "Surat": "256",
    "Varanasi": "258", "Visakhapatnam": "253",
}

APPEAL_TYPE_LABELS: dict[str, str] = {
    "ITA": "Income Tax Appeal",
    "CO": "Cross Objection",
    "ITSSA": "Income Tax (Search & Seizure) Appeal",
    "ITTPA": "Income Tax (Transfer Pricing) Appeal",
    "ITITA": "Income Tax (International Taxation) Appeal",
    "WTA": "Wealth Tax Appeal",
    "BMA": "Black Money Appeal",
    "EDA": "Estate Duty Appeal",
    "INTTA": "Interest Tax Appeal",
    "GTA": "Gift Tax Appeal",
    "TDS": "TDS Appeal",
    "STTA": "Security Transaction Tax Appeal",
    "ETA": "Expenditure Tax Appeal",
    "STA": "Sur Tax Appeal",
    "HCD": "High Court Decision",
    "SA": "Stay Application",
    "MA": "Miscellaneous Application",
    "RA": "Reference Application",
}
