from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LOCAL_PATH = _PROJECT_ROOT / "configs" / "countries_master.json"
_DEFAULT_BLOB_PATH = "mma/reference/countries/countries_master.json"
_COUNTRY_MASTER_PAYLOAD_CACHE: dict[str, Any] = {
    "value": None,
    "expires_at": 0.0,
}


def _normalize_country_key(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _country_master_blob_path() -> str:
    return os.environ.get("COUNTRY_MASTER_BLOB_PATH", _DEFAULT_BLOB_PATH).strip().lstrip("/")


def _country_master_local_path() -> Path:
    override = os.environ.get("COUNTRY_MASTER_JSON_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_LOCAL_PATH


def _country_master_cache_ttl_seconds() -> int:
    raw = os.environ.get("COUNTRY_MASTER_CACHE_TTL_SECONDS", "600").strip()
    try:
        return max(60, int(raw))
    except Exception:
        return 600


def _read_country_master_from_azure() -> dict[str, Any] | None:
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip()
    key = os.environ.get("AZURE_STORAGE_KEY", "").strip()
    container = os.environ.get("AZURE_STORAGE_CONTAINER", "fightprophet-dashboard").strip()
    blob_name = _country_master_blob_path()
    if not account or not key or not blob_name:
        return None

    try:
        from azure.storage.blob import BlobServiceClient

        client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
        blob = client.get_blob_client(container=container, blob=blob_name)
        payload = json.loads(blob.download_blob().readall())
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _read_country_master_from_local() -> dict[str, Any]:
    path = _country_master_local_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Country master at {path} is not a JSON object")
    return payload


def _merge_country_master_payloads(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Merge fallback country records into the primary payload by iso2/name."""
    primary_countries = primary.get("countries", [])
    fallback_countries = fallback.get("countries", [])
    if not isinstance(primary_countries, list) or not isinstance(fallback_countries, list):
        return primary

    merged = dict(primary)
    countries: list[dict[str, Any]] = [
        dict(entry) for entry in primary_countries if isinstance(entry, dict)
    ]
    seen: set[str] = set()
    for entry in countries:
        iso2 = str(entry.get("iso2", "") or "").strip().upper()
        name = str(entry.get("canonical_name", "") or "").strip().upper()
        if iso2:
            seen.add(f"iso2:{iso2}")
        if name:
            seen.add(f"name:{name}")

    for entry in fallback_countries:
        if not isinstance(entry, dict):
            continue
        iso2 = str(entry.get("iso2", "") or "").strip().upper()
        name = str(entry.get("canonical_name", "") or "").strip().upper()
        keys = [key for key in (f"iso2:{iso2}" if iso2 else "", f"name:{name}" if name else "") if key]
        if any(key in seen for key in keys):
            continue
        countries.append(dict(entry))
        seen.update(keys)

    countries.sort(key=lambda item: str(item.get("canonical_name", "") or "").casefold())
    merged["countries"] = countries
    merged["country_count"] = len(countries)
    return merged


def _read_country_master_payload() -> dict[str, Any]:
    local_payload = _read_country_master_from_local()
    azure_payload = _read_country_master_from_azure()
    if azure_payload is not None:
        return _merge_country_master_payloads(azure_payload, local_payload)
    return local_payload


def _fallback_country_record(value: object) -> dict[str, Any] | None:
    key = _normalize_country_key(value)
    if key in {"MEXICO", "MÉXICO", "MEX", "MX"}:
        return {
            "canonical_name": "Mexico",
            "iso2": "MX",
            "aliases": ["MEXICO", "Mexico", "mexico", "MEX", "MX", "México", "MÉXICO"],
        }
    return None


def country_master_payload() -> dict[str, Any]:
    now = time.time()
    cached_value = _COUNTRY_MASTER_PAYLOAD_CACHE.get("value")
    expires_at = float(_COUNTRY_MASTER_PAYLOAD_CACHE.get("expires_at") or 0.0)
    if isinstance(cached_value, dict) and now < expires_at:
        return cached_value

    payload = _read_country_master_payload()
    _COUNTRY_MASTER_PAYLOAD_CACHE["value"] = payload
    _COUNTRY_MASTER_PAYLOAD_CACHE["expires_at"] = now + _country_master_cache_ttl_seconds()
    return payload


def country_master_index() -> dict[str, Any]:
    payload = country_master_payload()
    countries = payload.get("countries", [])
    by_alias: dict[str, dict[str, Any]] = {}
    by_iso2: dict[str, dict[str, Any]] = {}

    for entry in countries:
        if not isinstance(entry, dict):
            continue
        canonical_name = str(entry.get("canonical_name", "") or "").strip()
        iso2 = str(entry.get("iso2", "") or "").strip().upper()
        aliases = entry.get("aliases", [])
        record = {
            "canonical_name": canonical_name,
            "iso2": iso2,
            "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
        }
        if iso2:
            by_iso2[iso2] = record
        for alias in [canonical_name] + record["aliases"] + ([iso2] if iso2 else []):
            key = _normalize_country_key(alias)
            if key:
                by_alias[key] = record

    return {
        "payload": payload,
        "by_alias": by_alias,
        "by_iso2": by_iso2,
    }


def refresh_country_master_cache() -> None:
    _COUNTRY_MASTER_PAYLOAD_CACHE["value"] = None
    _COUNTRY_MASTER_PAYLOAD_CACHE["expires_at"] = 0.0


def country_record(value: object) -> dict[str, Any] | None:
    key = _normalize_country_key(value)
    if not key:
        return None
    idx = country_master_index()
    return idx["by_alias"].get(key) or _fallback_country_record(value)


def canonical_country_name(value: object) -> str:
    record = country_record(value)
    if record:
        return record["canonical_name"]
    return "" if value is None else str(value).strip()


def country_iso2(value: object) -> str:
    record = country_record(value)
    if record:
        return record["iso2"]

    raw = "" if value is None else str(value).strip()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return ""


def country_flag(value: object) -> str:
    code = country_iso2(value)
    if len(code) != 2 or not code.isalpha():
        return ""
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


def country_short_label(value: object) -> str:
    code = country_iso2(value)
    if code:
        return code
    raw = canonical_country_name(value)
    if not raw:
        return ""
    return "".join(part[0] for part in raw.split() if part)[:3].upper()


def sync_country_master_artifacts(paths: list[Path]) -> dict[str, Any]:
    payload = _read_country_master_payload()
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    refresh_country_master_cache()
    return payload
