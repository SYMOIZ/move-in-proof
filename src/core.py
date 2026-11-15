"""Pure logic for Move-In Proof. No AWS calls here, so everything is unit-testable."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

MAX_PHOTOS = 30
MAX_PHOTO_BYTES = 10 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

DRAFT = "DRAFT"
SEALING = "SEALING"
AWAITING_LANDLORD = "AWAITING_LANDLORD"
COSIGN_RECEIVED = "COSIGN_RECEIVED"
SEALED_COSIGNED = "SEALED_COSIGNED"
SEALED_DISPUTED = "SEALED_DISPUTED"
SEALED_TENANT_ONLY = "SEALED_TENANT_ONLY"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class NoPhotos(Exception):
    """Raised by the workflow when a record has no uploaded photos. Caught by name in the state machine."""


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def token_hash(token: str) -> str:
    return sha256_hex(token.encode())


def role_for(record: dict, token: str | None = None, claims: dict | None = None) -> str:
    """Tenant = the signed-in account that owns the record. Landlord = holder of the co-sign link."""
    if claims and claims.get("sub") and claims["sub"] == record.get("ownerSub"):
        return "tenant"
    if token and hmac.compare_digest(token_hash(token), record.get("landlordTokenHash", "")):
        return "landlord"
    return "public"


def clean(value, field: str, max_len: int = 120, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be text")
    value = " ".join(value.split())
    if required and not value:
        raise ApiError(400, f"{field} is required")
    if len(value) > max_len:
        raise ApiError(400, f"{field} must be at most {max_len} characters")
    return value


def canonical(obj) -> bytes:
    """One fixed byte form per manifest, so its SHA-256 is reproducible anywhere."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def upload_order(photo: dict):
    return (photo.get("seq", ""), photo["photoId"])


def build_manifest(record: dict, photos: list, sealed_at: str) -> bytes:
    ordered = sorted(photos, key=upload_order)
    return canonical({
        "version": 1,
        "recordId": record["pk"],
        "address": record["address"],
        "tenantName": record["tenantName"],
        "landlordName": record["landlordName"],
        "createdAt": record["createdAt"],
        "sealedAt": sealed_at,
        "photos": [
            {
                "photoId": p["photoId"],
                "label": p["label"],
                "filename": p["filename"],
                "sha256": p["sha256"],
                "size": p["size"],
                "versionId": p["versionId"],
            }
            for p in ordered
        ],
    })


def retain_until(start: datetime, days: int) -> datetime:
    return start + timedelta(days=days)


def outcome_status(landlord: dict | None) -> str:
    if not landlord:
        return SEALED_TENANT_ONLY
    return SEALED_COSIGNED if landlord.get("decision") == "agree" else SEALED_DISPUTED
