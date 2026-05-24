import time
import requests
from src.logger import get_logger

_BASE_URL = "https://npiregistry.cms.hhs.gov/api/"
_API_VERSION = "2.1"
_PAGE_SIZE = 200       # hard server cap per request
_REQUEST_DELAY = 0.5   # seconds between paginated requests

logger = get_logger(__name__)


class NppesClient:
    def __init__(self, request_delay: float = _REQUEST_DELAY):
        self._delay = request_delay
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def _get(self, params: dict) -> dict:
        params.setdefault("version", _API_VERSION)
        response = self._session.get(_BASE_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("Errors"):
            raise ValueError(f"NPPES API error: {data['Errors']}")
        return data

    def search(self, params: dict, max_results: int = 200) -> list[dict]:
        """Fetch up to max_results records, auto-paginating as needed."""
        all_results: list[dict] = []
        skip = 0
        total_available = None

        while len(all_results) < max_results:
            batch_size = min(_PAGE_SIZE, max_results - len(all_results))
            data = self._get({**params, "limit": batch_size, "skip": skip})

            results = data.get("results") or []
            if total_available is None:
                total_available = data.get("result_count", 0)
                logger.info("NPPES: %d total matches found", total_available)

            all_results.extend(results)
            skip += len(results)

            if not results or len(results) < batch_size or skip >= (total_available or 0):
                break

            time.sleep(self._delay)

        logger.info("NPPES: returned %d records", len(all_results))
        return all_results
