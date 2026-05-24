from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class Address:
    purpose: str
    address_1: str
    address_2: str
    city: str
    state: str
    postal_code: str
    country_code: str
    phone: str
    fax: str

    @classmethod
    def from_dict(cls, d: dict) -> "Address":
        return cls(
            purpose=d.get("address_purpose", ""),
            address_1=d.get("address_1", ""),
            address_2=d.get("address_2", ""),
            city=d.get("city", ""),
            state=d.get("state", ""),
            postal_code=d.get("postal_code", ""),
            country_code=d.get("country_code", "US"),
            phone=d.get("telephone_number", ""),
            fax=d.get("fax_number", ""),
        )


@dataclass
class Taxonomy:
    code: str
    description: str
    primary: bool
    license: str
    state: str

    @classmethod
    def from_dict(cls, d: dict) -> "Taxonomy":
        # API returns the description field as "desc" or "taxonomy" depending on version
        description = d.get("desc") or d.get("taxonomy") or ""
        return cls(
            code=d.get("code", ""),
            description=description,
            primary=bool(d.get("primary", False)),
            license=d.get("license", ""),
            state=d.get("state", ""),
        )


@dataclass
class Identifier:
    code: str
    identifier: str
    state: str
    issuer: str

    @classmethod
    def from_dict(cls, d: dict) -> "Identifier":
        return cls(
            code=d.get("code", ""),
            identifier=d.get("identifier", ""),
            state=d.get("state", ""),
            issuer=d.get("issuer", ""),
        )


@dataclass
class Provider:
    npi: str
    enumeration_type: str
    created_date: str
    last_updated_date: str
    first_name: str
    last_name: str
    middle_name: str
    credential: str
    gender: str
    sole_proprietor: str
    addresses: list[Address] = field(default_factory=list)
    taxonomies: list[Taxonomy] = field(default_factory=list)
    identifiers: list[Identifier] = field(default_factory=list)
    other_names: list[dict] = field(default_factory=list)
    practice_locations: list[dict] = field(default_factory=list)
    endpoints: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Provider":
        basic = d.get("basic", {})
        return cls(
            npi=str(d.get("number", "")),
            enumeration_type=d.get("enumeration_type", ""),
            created_date=d.get("created_date", ""),
            last_updated_date=d.get("last_updated_date", ""),
            first_name=basic.get("first_name", ""),
            last_name=basic.get("last_name", ""),
            middle_name=basic.get("middle_name", ""),
            credential=basic.get("credential", ""),
            gender=basic.get("gender", ""),
            sole_proprietor=basic.get("sole_proprietor", ""),
            addresses=[Address.from_dict(a) for a in d.get("addresses", [])],
            taxonomies=[Taxonomy.from_dict(t) for t in d.get("taxonomies", [])],
            identifiers=[Identifier.from_dict(i) for i in d.get("identifiers", [])],
            other_names=d.get("other_names", []),
            practice_locations=d.get("practice_locations", []),
            endpoints=d.get("endpoints", []),
        )

    @property
    def primary_taxonomy(self) -> Optional[Taxonomy]:
        for t in self.taxonomies:
            if t.primary:
                return t
        return self.taxonomies[0] if self.taxonomies else None

    @property
    def location_address(self) -> Optional[Address]:
        for a in self.addresses:
            if a.purpose == "LOCATION":
                return a
        return self.addresses[0] if self.addresses else None

    def to_record(self) -> dict:
        """Flat dict for a single DataFrame row — primary address + taxonomy in columns."""
        pt = self.primary_taxonomy
        la = self.location_address
        return {
            "npi": self.npi,
            "last_name": self.last_name,
            "first_name": self.first_name,
            "middle_name": self.middle_name,
            "credential": self.credential,
            "gender": self.gender,
            "sole_proprietor": self.sole_proprietor,
            "primary_taxonomy_code": pt.code if pt else "",
            "primary_taxonomy": pt.description if pt else "",
            "taxonomy_license_state": pt.state if pt else "",
            "address_1": la.address_1 if la else "",
            "address_2": la.address_2 if la else "",
            "city": la.city if la else "",
            "state": la.state if la else "",
            "postal_code": la.postal_code if la else "",
            "phone": la.phone if la else "",
            "fax": la.fax if la else "",
            "created_date": self.created_date,
            "last_updated_date": self.last_updated_date,
            # Nested lists — available for detailed inspection
            "all_taxonomies": [
                {"code": t.code, "desc": t.description, "primary": t.primary, "state": t.state}
                for t in self.taxonomies
            ],
            "all_addresses": [
                {"purpose": a.purpose, "address_1": a.address_1, "city": a.city,
                 "state": a.state, "postal_code": a.postal_code, "phone": a.phone}
                for a in self.addresses
            ],
            "identifiers": [
                {"code": i.code, "id": i.identifier, "issuer": i.issuer, "state": i.state}
                for i in self.identifiers
            ],
            "practice_locations": self.practice_locations,
            "endpoints": self.endpoints,
        }

    @staticmethod
    def to_dataframe(providers: list["Provider"]) -> pd.DataFrame:
        if not providers:
            return pd.DataFrame()
        return pd.DataFrame([p.to_record() for p in providers])
