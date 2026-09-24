"""Decide where Google News should look, based on what Meera's note mentions.

Rule: India by default. Other countries, or a global search, are allowed only when the note
mentions them (a country, its people or a major city) or talks about international/global scope.
Detection is keyword-based and deterministic, so the model can't widen the search on its own.
"""

import re
from dataclasses import dataclass

DEFAULT_REGION = "IN"
GLOBAL = "GLOBAL"

# Countries with a real English Google News edition (verified: each returns local sources).
EDITIONS = {"IN", "US", "GB", "CA", "AU", "NZ", "SG", "MY", "PH", "PK", "NG", "KE", "ZA", "IE", "IL"}

# ISO code -> (display name, keywords). Keywords are matched case-insensitively on word
# boundaries, except all-caps acronyms (US, UK, UAE), which must appear in capitals so the
# word "us" in "let us" doesn't count. Names that are also everyday words or first names
# (Chad, Jordan, Georgia, turkey) are left out or only matched in capitals.
COUNTRIES: dict[str, tuple[str, list[str]]] = {
    "IN": ("India", ["India", "Indian", "Indians", "Bharat", "Mumbai", "Delhi", "Bengaluru", "Bangalore",
                     "Hyderabad", "Chennai", "Pune", "Kolkata", "Gurugram", "Gurgaon", "Noida"]),
    "US": ("United States", ["US", "USA", "U.S.", "America", "American", "Americans", "United States",
                             "Silicon Valley", "New York", "San Francisco", "Seattle", "Wall Street"]),
    "GB": ("United Kingdom", ["UK", "U.K.", "Britain", "British", "England", "London",
                              "Scotland", "Wales"]),
    "CA": ("Canada", ["Canada", "Canadian", "Toronto", "Vancouver"]),
    "AU": ("Australia", ["Australia", "Australian", "Sydney", "Melbourne"]),
    "NZ": ("New Zealand", ["New Zealand", "Auckland"]),
    "SG": ("Singapore", ["Singapore", "Singaporean"]),
    "MY": ("Malaysia", ["Malaysia", "Malaysian", "Kuala Lumpur"]),
    "PH": ("Philippines", ["Philippines", "Filipino", "Manila"]),
    "PK": ("Pakistan", ["Pakistan", "Pakistani", "Karachi", "Lahore"]),
    "NG": ("Nigeria", ["Nigeria", "Nigerian", "Lagos"]),
    "KE": ("Kenya", ["Kenya", "Kenyan", "Nairobi"]),
    "ZA": ("South Africa", ["South Africa", "South African", "Johannesburg", "Cape Town"]),
    "IE": ("Ireland", ["Ireland", "Irish", "Dublin"]),
    "IL": ("Israel", ["Israel", "Israeli", "Tel Aviv"]),
    "AE": ("United Arab Emirates", ["UAE", "Emirates", "Emirati", "Dubai", "Abu Dhabi"]),
    "SA": ("Saudi Arabia", ["Saudi", "Saudi Arabia", "Riyadh"]),
    "BD": ("Bangladesh", ["Bangladesh", "Bangladeshi", "Dhaka"]),
    "LK": ("Sri Lanka", ["Sri Lanka", "Sri Lankan", "Colombo"]),
    "NP": ("Nepal", ["Nepal", "Nepali", "Nepalese", "Kathmandu"]),
    "CN": ("China", ["China", "Chinese", "Beijing", "Shanghai", "Shenzhen"]),
    "HK": ("Hong Kong", ["Hong Kong"]),
    "JP": ("Japan", ["Japan", "Japanese", "Tokyo"]),
    "KR": ("South Korea", ["South Korea", "Korea", "Korean", "Seoul"]),
    "ID": ("Indonesia", ["Indonesia", "Indonesian", "Jakarta"]),
    "VN": ("Vietnam", ["Vietnam", "Vietnamese"]),
    "TH": ("Thailand", ["Thailand", "Thai", "Bangkok"]),
    "DE": ("Germany", ["Germany", "German", "Berlin", "Munich"]),
    "FR": ("France", ["France", "French", "Paris"]),
    "NL": ("Netherlands", ["Netherlands", "Dutch", "Amsterdam"]),
    "ES": ("Spain", ["Spain", "Spanish", "Madrid"]),
    "IT": ("Italy", ["Italy", "Italian", "Milan"]),
    "CH": ("Switzerland", ["Switzerland", "Swiss", "Zurich"]),
    "SE": ("Sweden", ["Sweden", "Swedish", "Stockholm"]),
    "BR": ("Brazil", ["Brazil", "Brazilian", "São Paulo", "Sao Paulo"]),
    "MX": ("Mexico", ["Mexico", "Mexican"]),
    "RU": ("Russia", ["Russia", "Russian", "Moscow"]),
    "TR": ("Türkiye", ["Türkiye", "Turkish", "Istanbul", "Turkey"]),
    "EG": ("Egypt", ["Egypt", "Egyptian", "Cairo"]),
}

# Wording that signals an international or multi-region angle -> global search allowed.
GLOBAL_KEYWORDS = [
    "global", "globally", "international", "internationally", "worldwide", "world over", "across the world",
    "around the world", "overseas", "abroad", "cross-border", "multinational", "foreign", "other countries",
    "Europe", "European", "EU", "Asia", "Asian", "APAC", "Africa", "African", "Middle East", "Gulf",
    "Latin America", "LATAM", "Southeast Asia", "Western countries",
]

CASE_SENSITIVE = {"US", "USA", "U.S.", "UK", "U.K.", "UAE", "EU", "APAC", "LATAM", "Turkey", "Gulf"}


def _first_mention(text: str, keyword: str) -> int | None:
    """Position of the first whole-word match of `keyword`, or None."""
    flags = 0 if keyword in CASE_SENSITIVE else re.IGNORECASE
    m = re.search(rf"(?<![\w.]){re.escape(keyword)}(?![\w])", text, flags)
    return m.start() if m else None


@dataclass(frozen=True)
class SearchScope:
    mentioned: tuple[str, ...]  # ISO codes of countries named in the note, in first-seen order
    international: bool         # note talks about global / international / multi-region scope

    @property
    def foreign(self) -> tuple[str, ...]:
        return tuple(c for c in self.mentioned if c != DEFAULT_REGION)

    @property
    def allowed_regions(self) -> list[str]:
        """India is always allowed. Mentioned countries are added; GLOBAL when the note goes beyond India."""
        regions = [DEFAULT_REGION, *self.foreign]
        if self.international or self.foreign:
            regions.append(GLOBAL)
        return regions

    @property
    def default_region(self) -> str:
        if self.international and not self.foreign:
            return GLOBAL
        if len(self.foreign) == 1 and DEFAULT_REGION not in self.mentioned:
            return self.foreign[0]
        if len(self.foreign) > 1:
            return GLOBAL
        return DEFAULT_REGION

    def describe(self) -> str:
        """Plain-language briefing that goes to the model alongside the note."""
        if not self.foreign and not self.international:
            return ("No country other than India and no international angle is mentioned. "
                    "Search India only: region=\"IN\".")
        names = ", ".join(f"{COUNTRIES[c][0]} ({c})" for c in self.foreign) or "none"
        return (f"Countries mentioned besides India: {names}. International/global angle: "
                f"{'yes' if self.international else 'no'}. Allowed regions: "
                f"{', '.join(self.allowed_regions)}. Suggested region: {self.default_region}. "
                "Pick the region that matches where Meera's point is set.")


def detect_scope(note: str) -> SearchScope:
    first_seen = {}
    for code, (_, keywords) in COUNTRIES.items():
        hits = [pos for k in keywords if (pos := _first_mention(note, k)) is not None]
        if hits:
            first_seen[code] = min(hits)
    international = any(_first_mention(note, k) is not None for k in GLOBAL_KEYWORDS)
    return SearchScope(mentioned=tuple(sorted(first_seen, key=first_seen.get)), international=international)


def rss_params(region: str) -> tuple[dict, str | None]:
    """Google News query params for a region, plus a country name to add to the query when the
    country has no English edition (those searches run on the US-English edition instead)."""
    if region in EDITIONS:
        return {"hl": f"en-{region}", "gl": region, "ceid": f"{region}:en"}, None
    extra = COUNTRIES[region][0] if region in COUNTRIES else None
    return {"hl": "en-US", "gl": "US", "ceid": "US:en"}, extra
