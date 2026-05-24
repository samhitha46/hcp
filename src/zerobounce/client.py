import requests
from src.config import ZEROBOUNCE_API_KEY
from src.logger import get_logger
from src.zerobounce.models import ValidationResult

_BASE_URL = "https://api.zerobounce.net/v2"
_BATCH_SIZE = 200  # ZeroBounce hard cap per batch request

logger = get_logger(__name__)


class ZeroBounceClient:
    def __init__(self, api_key: str = ZEROBOUNCE_API_KEY):
        if not api_key:
            raise ValueError("ZEROBOUNCE_API_KEY is not set")
        self._api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def get_credits(self) -> int:
        """Return remaining validation credits on the account."""
        resp = self._session.get(
            f"{_BASE_URL}/getcredits",
            params={"api_key": self._api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        credits = data.get("Credits", -1)
        try:
            return int(credits)
        except (ValueError, TypeError):
            return -1

    def validate(self, email: str, ip_address: str = "") -> ValidationResult:
        """Validate a single email address."""
        resp = self._session.get(
            f"{_BASE_URL}/validate",
            params={
                "api_key": self._api_key,
                "email": email,
                "ip_address": ip_address,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return ValidationResult.from_dict(resp.json())

    def validate_batch(
        self, emails: list[str], ip_address: str = ""
    ) -> list[ValidationResult]:
        """
        Validate a list of email addresses, chunking into batches of 200.
        Results are returned in the same order as the input list.
        """
        results: list[ValidationResult] = []

        for chunk_start in range(0, len(emails), _BATCH_SIZE):
            chunk = emails[chunk_start : chunk_start + _BATCH_SIZE]
            batch_results = self._validate_chunk(chunk, ip_address)
            results.extend(batch_results)
            logger.debug(
                "ZeroBounce batch %d–%d complete",
                chunk_start + 1,
                chunk_start + len(chunk),
            )

        return results

    def _validate_chunk(
        self, emails: list[str], ip_address: str
    ) -> list[ValidationResult]:
        """Send one batch request (≤200 emails) and return ordered results."""
        payload = {
            "api_key": self._api_key,
            "email_batch": [
                {"email_address": e, "ip_address": ip_address} for e in emails
            ],
        }
        resp = self._session.post(
            f"{_BASE_URL}/validatebatch",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        # Index results by address for order-preserving reassembly
        by_address = {
            r["address"]: ValidationResult.from_dict(r)
            for r in data.get("email_batch", [])
        }

        for err in data.get("errors", []):
            addr = err.get("emailAddress", "")
            by_address[addr] = ValidationResult.error_result(addr, err.get("error", "unknown error"))

        return [
            by_address.get(e, ValidationResult.error_result(e, "missing from response"))
            for e in emails
        ]
