from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EmailStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch-all"
    UNKNOWN = "unknown"
    SPAMTRAP = "spamtrap"
    ABUSE = "abuse"
    DO_NOT_MAIL = "do_not_mail"


@dataclass
class ValidationResult:
    address: str
    status: str
    sub_status: str
    free_email: bool
    did_you_mean: str
    account: str
    domain: str
    domain_age_days: str
    smtp_provider: str
    mx_found: bool
    mx_record: str
    firstname: str
    lastname: str
    gender: str
    country: str
    region: str
    city: str
    zipcode: str
    processed_at: str
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationResult":
        mx_raw = d.get("mx_found", "")
        if isinstance(mx_raw, bool):
            mx_found = mx_raw
        else:
            mx_found = str(mx_raw).lower() == "true"

        free_raw = d.get("free_email", False)
        free_email = bool(free_raw) if isinstance(free_raw, bool) else str(free_raw).lower() == "true"

        return cls(
            address=d.get("address", ""),
            status=d.get("status", ""),
            sub_status=d.get("sub_status", ""),
            free_email=free_email,
            did_you_mean=d.get("did_you_mean", "") or "",
            account=d.get("account", "") or "",
            domain=d.get("domain", "") or "",
            domain_age_days=str(d.get("domain_age_days", "") or ""),
            smtp_provider=d.get("smtp_provider", "") or "",
            mx_found=mx_found,
            mx_record=d.get("mx_record", "") or "",
            firstname=d.get("firstname", "") or "",
            lastname=d.get("lastname", "") or "",
            gender=d.get("gender", "") or "",
            country=d.get("country", "") or "",
            region=d.get("region", "") or "",
            city=d.get("city", "") or "",
            zipcode=d.get("zipcode", "") or "",
            processed_at=d.get("processed_at", "") or "",
        )

    @classmethod
    def error_result(cls, address: str, message: str) -> "ValidationResult":
        return cls(
            address=address,
            status="error",
            sub_status="",
            free_email=False,
            did_you_mean="",
            account="",
            domain="",
            domain_age_days="",
            smtp_provider="",
            mx_found=False,
            mx_record="",
            firstname="",
            lastname="",
            gender="",
            country="",
            region="",
            city="",
            zipcode="",
            processed_at="",
            error=message,
        )

    def to_record(self) -> dict:
        return {
            "zb_address": self.address,
            "zb_status": self.status,
            "zb_sub_status": self.sub_status,
            "zb_free_email": self.free_email,
            "zb_did_you_mean": self.did_you_mean,
            "zb_mx_found": self.mx_found,
            "zb_mx_record": self.mx_record,
            "zb_smtp_provider": self.smtp_provider,
            "zb_domain_age_days": self.domain_age_days,
            "zb_country": self.country,
            "zb_region": self.region,
            "zb_city": self.city,
            "zb_firstname": self.firstname,
            "zb_lastname": self.lastname,
            "zb_gender": self.gender,
            "zb_error": self.error,
        }
