import pandas as pd

from src.nppes.client import NppesClient
from src.nppes.models import Provider
from src.logger import get_logger

logger = get_logger(__name__)

# Columns returned when summary=True — enough for a list view or quick inspection
SUMMARY_COLUMNS = [
    "npi", "last_name", "first_name", "credential", "gender",
    "primary_taxonomy_code", "primary_taxonomy",
    "city", "state", "postal_code", "phone",
]

_default_client = NppesClient()


def _to_df(raw: list[dict], summary: bool) -> pd.DataFrame:
    providers = [Provider.from_dict(r) for r in raw]
    df = Provider.to_dataframe(providers)
    if summary and not df.empty:
        available = [c for c in SUMMARY_COLUMNS if c in df.columns]
        return df[available]
    return df


class NppesQuery:
    """
    Fluent builder for NPPES NPI-1 queries.

    Usage:
        df = (NppesQuery()
                .by_specialty("cardiology")
                .by_state("TX")
                .fetch(max_results=400))
    """

    def __init__(self, client: NppesClient | None = None):
        self._client = client or _default_client
        self._params: dict = {"enumeration_type": "NPI-1"}

    def by_state(self, state: str) -> "NppesQuery":
        self._params["state"] = state.upper()
        return self

    def by_city(self, city: str) -> "NppesQuery":
        self._params["city"] = city
        return self

    def by_postal_code(self, postal_code: str) -> "NppesQuery":
        self._params["postal_code"] = str(postal_code)
        return self

    def by_specialty(self, taxonomy_description: str) -> "NppesQuery":
        """Fuzzy text match — e.g. 'cardio' matches Cardiology, Cardiovascular Disease."""
        self._params["taxonomy_description"] = taxonomy_description
        return self

    def by_name(self, last_name: str = "", first_name: str = "",
                use_alias: bool = False) -> "NppesQuery":
        if last_name:
            self._params["last_name"] = last_name
        if first_name:
            self._params["first_name"] = first_name
        if use_alias:
            self._params["use_first_name_alias"] = "true"
        return self

    def address_purpose(self, purpose: str) -> "NppesQuery":
        """Filter by address type: LOCATION, MAILING, PRIMARY, SECONDARY."""
        self._params["address_purpose"] = purpose.upper()
        return self

    def _validate(self) -> None:
        non_type_keys = [k for k in self._params if k != "enumeration_type"]
        if non_type_keys == ["state"]:
            raise ValueError(
                "State cannot be the only filter. "
                "Add city, specialty, postal_code, or name."
            )
        if not non_type_keys:
            raise ValueError("At least one search filter is required.")

    def fetch(self, max_results: int = 200, summary: bool = False) -> pd.DataFrame:
        """
        Execute the query and return a DataFrame.

        Args:
            max_results: Upper bound on records to fetch (auto-paginates).
            summary:     If True, return only the key identifier/contact columns.
        """
        self._validate()
        logger.info("NPPES query params: %s", self._params)
        raw = self._client.search(self._params, max_results=max_results)
        return _to_df(raw, summary)


# ── Named convenience functions ───────────────────────────────────────────────

def get_by_npi(npi_number: str | int) -> pd.DataFrame:
    """Fetch a single Type-1 provider by NPI number."""
    raw = _default_client.search(
        {"number": str(npi_number), "enumeration_type": "NPI-1"},
        max_results=1,
    )
    return _to_df(raw, summary=False)


def search_by_specialty(
    taxonomy: str,
    state: str | None = None,
    city: str | None = None,
    max_results: int = 200,
    summary: bool = False,
) -> pd.DataFrame:
    """
    Find individual providers by specialty description.

    Args:
        taxonomy:    Fuzzy specialty text (e.g. 'cardiology', 'family medicine').
        state:       2-letter state code to narrow results.
        city:        City name to narrow results.
        max_results: Cap on total records fetched.
        summary:     If True, return a slim summary DataFrame.
    """
    q = NppesQuery().by_specialty(taxonomy)
    if state:
        q = q.by_state(state)
    if city:
        q = q.by_city(city)
    return q.fetch(max_results=max_results, summary=summary)


def search_by_name(
    last_name: str,
    first_name: str = "",
    state: str | None = None,
    use_alias: bool = False,
    max_results: int = 200,
    summary: bool = False,
) -> pd.DataFrame:
    """
    Find individual providers by name.

    Args:
        last_name:   Last name (supports trailing wildcard: 'smith*').
        first_name:  First name (optional; supports trailing wildcard).
        state:       2-letter state code to narrow results.
        use_alias:   Include first-name variants/aliases in the search.
        max_results: Cap on total records fetched.
        summary:     If True, return a slim summary DataFrame.
    """
    q = NppesQuery().by_name(last_name=last_name, first_name=first_name, use_alias=use_alias)
    if state:
        q = q.by_state(state)
    return q.fetch(max_results=max_results, summary=summary)


def search_by_location(
    state: str,
    city: str | None = None,
    postal_code: str | None = None,
    taxonomy: str | None = None,
    max_results: int = 200,
    summary: bool = False,
) -> pd.DataFrame:
    """
    Find individual providers by geographic location, optionally filtered by specialty.

    Args:
        state:       2-letter state code (required).
        city:        City name to narrow results.
        postal_code: ZIP or ZIP+4 to narrow results.
        taxonomy:    Fuzzy specialty text to narrow results.
        max_results: Cap on total records fetched.
        summary:     If True, return a slim summary DataFrame.
    """
    q = NppesQuery().by_state(state)
    if city:
        q = q.by_city(city)
    if postal_code:
        q = q.by_postal_code(postal_code)
    if taxonomy:
        q = q.by_specialty(taxonomy)
    return q.fetch(max_results=max_results, summary=summary)
