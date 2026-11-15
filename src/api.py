"""HTTP API handler: serves the single page at / and the JSON API under /api.

Two doors lead to the same code:
  /api/...     open to anyone; the caller has no account identity
  /api/my/...  behind the API Gateway JWT authorizer; the caller's Cognito claims are trusted
Owner-only actions need claims, so they can only succeed through the second door.
"""
import base64
import json
import os
import secrets
import time

import core
import store

_HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
_html_cache = None
PENDING = (core.SEALING, core.AWAITING_LANDLORD, core.COSIGN_RECEIVED)
MAX_LISTED = 50


def handler(event, _context=None):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/").rstrip("/") or "/"
    try:
        if method == "GET" and path == "/":
            return _html()
        if method == "GET" and path == "/api/config":
            return _json(200, config())
        who = {"token": (event.get("headers") or {}).get("x-mip-token"), "claims": None}
        if path.startswith("/api/my/"):
            who["claims"] = _claims(event)
            path = "/api/" + path[len("/api/my/"):]
        parts = path.strip("/").split("/")
        if parts[:2] != ["api", "records"] or len(parts) > 5:
            raise core.ApiError(404, "Not found")
        if len(parts) == 2 and method == "POST":
            return _json(201, create_record(who, _body(event)))
        if len(parts) == 2 and method == "GET":
            return _json(200, list_records(who))
        if len(parts) == 3 and method == "GET":
            return _json(200, view_record(parts[2], who))
        if len(parts) == 4 and method == "POST":
            action = {"photos": add_photo, "seal": seal, "cosign": cosign}.get(parts[3])
            if action:
                return _json(200, action(parts[2], who, _body(event)))
        if len(parts) == 5 and parts[3] == "photos" and method == "DELETE":
            return _json(200, remove_photo(parts[2], who, parts[4]))
        raise core.ApiError(404, "Not found")
    except core.ApiError as err:
        return _json(err.status, {"error": err.message})


# ---- actions --------------------------------------------------------------

def config():
    """What the page needs to talk to Cognito. None of this is secret."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "clientId": os.environ.get("COGNITO_CLIENT_ID", ""),
        "endpoint": os.environ.get("COGNITO_ENDPOINT") or f"https://cognito-idp.{region}.amazonaws.com/",
    }


def create_record(who, body):
    claims = _signed_in(who)
    record_id = secrets.token_urlsafe(16)
    landlord_token = secrets.token_urlsafe(24)
    created = core.iso(core.now())
    store.put_record({
        "pk": record_id,
        "status": core.DRAFT,
        "address": core.clean(body.get("address"), "Address", 200),
        "tenantName": core.clean(body.get("tenantName"), "Tenant name"),
        "landlordName": core.clean(body.get("landlordName"), "Landlord name"),
        "createdAt": created,
        "ownerSub": claims["sub"],
        "ownerEmail": claims.get("email", ""),
        # Kept so the owner can always get the co-sign link again. Only the owner is ever sent it.
        "landlordToken": landlord_token,
        "landlordTokenHash": core.token_hash(landlord_token),
    })
    store.put_user_record(claims["sub"], record_id, created)
    return {"id": record_id}


def list_records(who):
    claims = _signed_in(who)
    records = []
    for record_id in store.list_user_records(claims["sub"])[:MAX_LISTED]:
        record = store.get_record(record_id)
        if record and record.get("ownerSub") == claims["sub"]:
            records.append({
                "id": record_id,
                "address": record["address"],
                "landlordName": record["landlordName"],
                "status": record["status"],
                "createdAt": record["createdAt"],
                "sealedAt": record.get("sealedAt"),
            })
    return {"records": records}


def add_photo(record_id, who, body):
    record = _load(record_id)
    _require(record, who, "tenant")
    if record["status"] != core.DRAFT:
        raise core.ApiError(409, "This record is sealed. No photos can be added.")
    content_type = body.get("contentType")
    if content_type not in core.ALLOWED_TYPES:
        raise core.ApiError(400, "Photos must be JPEG, PNG or WebP")
    if len(store.list_photos(record_id)) >= core.MAX_PHOTOS:
        raise core.ApiError(400, f"A record holds at most {core.MAX_PHOTOS} photos")
    photo_id = secrets.token_hex(8)
    key = f"records/{record_id}/photos/{photo_id}"
    store.put_photo(record_id, {
        "photoId": photo_id,
        "key": key,
        "label": core.clean(body.get("label"), "Room or item", 80),
        "filename": core.clean(body.get("filename"), "File name", 200),
        "contentType": content_type,
        "addedAt": core.iso(core.now()),
        "seq": f"{time.time_ns():020d}",  # upload order, finer than the one-second timestamp
    })
    return {"photoId": photo_id, "upload": store.presign_upload(key, content_type, core.MAX_PHOTO_BYTES)}


def remove_photo(record_id, who, photo_id):
    record = _load(record_id)
    _require(record, who, "tenant")
    if record["status"] != core.DRAFT:
        raise core.ApiError(409, "This record is sealed. Photos cannot be removed.")
    if len(photo_id) > 32:
        raise core.ApiError(404, "Not found")
    # Only the list entry is removed. An uploaded file with no entry is never sealed or shown.
    store.delete_photo(record_id, photo_id)
    return {"removed": photo_id}


def seal(record_id, who, _body_unused):
    record = _load(record_id)
    _require(record, who, "tenant")
    if not store.list_photos(record_id):
        raise core.ApiError(400, "Add at least one photo before sealing")
    try:
        store.update_record(record_id, {"status": core.SEALING}, removes=["lastError"], expect_status=core.DRAFT)
    except store.Conflict:
        raise core.ApiError(409, "This record is already sealed or being sealed")
    try:
        store.start_seal(record_id, f"{record_id}-{int(time.time())}")
    except Exception:
        store.update_record(record_id, {"status": core.DRAFT})
        raise
    return {"status": core.SEALING}


def cosign(record_id, who, body):
    record = _load(record_id)
    _require(record, who, "landlord")
    decision = body.get("decision")
    if decision not in ("agree", "dispute"):
        raise core.ApiError(400, "Decision must be agree or dispute")
    claims = who["claims"] or {}
    landlord = {
        "decision": decision,
        "name": core.clean(body.get("name"), "Your name"),
        "note": core.clean(body.get("note"), "Note", 500, required=False),
        "decidedAt": core.iso(core.now()),
        # Present only when the landlord answered while signed in to an account with a confirmed email.
        "verifiedEmail": claims.get("email", "") if str(claims.get("email_verified")).lower() == "true" else "",
    }
    if decision == "dispute" and not landlord["note"]:
        raise core.ApiError(400, "Say what you dispute, so the record shows your reason")
    closed = core.ApiError(409, "This record is not waiting for a co-signature")
    try:
        # One answer only: the status flip is conditional, so a second click or a replay loses.
        store.update_record(record_id, {
            "status": core.COSIGN_RECEIVED,
            "landlordDecision": landlord["decision"],
            "landlordSignedName": landlord["name"],
            "landlordNote": landlord["note"],
            "landlordDecidedAt": landlord["decidedAt"],
            "landlordVerifiedEmail": landlord["verifiedEmail"],
        }, expect_status=core.AWAITING_LANDLORD)
    except store.Conflict:
        raise closed
    try:
        store.task_success(record["taskToken"], landlord)
    except store.TaskGone:
        raise closed
    return {"status": core.COSIGN_RECEIVED}


def view_record(record_id, who):
    record = _load(record_id)
    role = _role(record, who)
    can_see_photos = role in ("tenant", "landlord")
    out = {
        "id": record_id,
        "role": role,
        "status": record["status"],
        "pending": record["status"] in PENDING,
        "address": record["address"],
        "tenantName": record["tenantName"],
        "landlordName": record["landlordName"],
        "tenantEmail": record.get("ownerEmail") if can_see_photos else None,
        "landlordToken": record.get("landlordToken") if role == "tenant" else None,
        "createdAt": record["createdAt"],
        "sealedAt": record.get("sealedAt"),
        "cosignDeadline": record.get("cosignDeadline"),
        "finalizedAt": record.get("finalizedAt"),
        "lastError": record.get("lastError"),
        "manifestSha256": record.get("manifestSha256"),
        "manifest": None,
        "lock": None,
        "landlord": None,
    }
    if record.get("landlordDecision"):
        out["landlord"] = {
            "decision": record["landlordDecision"],
            "name": record.get("landlordSignedName"),
            "note": record.get("landlordNote"),
            "decidedAt": record.get("landlordDecidedAt"),
            "verifiedEmail": (record.get("landlordVerifiedEmail") or None) if can_see_photos else None,
            "verified": bool(record.get("landlordVerifiedEmail")),
        }
    if record.get("manifestKey"):
        # After sealing, the locked manifest in S3 is the only source of truth for the photo list.
        raw = store.read_object(record["manifestKey"], record["manifestVersionId"])
        out["manifest"] = raw.decode()
        out["lock"] = store.lock_info(record["manifestKey"], record["manifestVersionId"])
        photos = []
        for p in json.loads(raw)["photos"]:
            key = f"records/{record_id}/photos/{p['photoId']}"
            photos.append({**p, "url": store.presign_view(key, p["versionId"]) if can_see_photos else None})
        out["photos"] = photos
    else:
        out["photos"] = [
            {
                "photoId": p["photoId"],
                "label": p["label"],
                "filename": p["filename"],
                "url": store.presign_view(p["key"]) if can_see_photos else None,
            }
            for p in sorted(store.list_photos(record_id), key=core.upload_order)
        ]
    return out


# ---- helpers --------------------------------------------------------------

def _claims(event):
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    if not claims.get("sub"):
        raise core.ApiError(401, "Sign in to continue")
    return claims


def _signed_in(who):
    if not who["claims"]:
        raise core.ApiError(401, "Sign in to continue")
    return who["claims"]


def _role(record, who):
    return core.role_for(record, who["token"], who["claims"])


def _load(record_id):
    record = store.get_record(record_id) if len(record_id) <= 64 and not record_id.startswith("USER") else None
    if not record:
        raise core.ApiError(404, "Record not found")
    return record


def _require(record, who, role):
    if _role(record, who) != role:
        if role == "tenant":
            raise core.ApiError(403, "Only the account that created this record can do that")
        raise core.ApiError(403, "This needs the landlord's co-sign link")


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw)
    except ValueError:
        raise core.ApiError(400, "Body must be JSON")
    if not isinstance(body, dict):
        raise core.ApiError(400, "Body must be a JSON object")
    return body


def _json(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(payload),
    }


def _html():
    global _html_cache
    if _html_cache is None:
        with open(_HTML_PATH, encoding="utf-8") as handle:
            _html_cache = handle.read()
    return {
        "statusCode": 200,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-cache",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
        },
        "body": _html_cache,
    }
