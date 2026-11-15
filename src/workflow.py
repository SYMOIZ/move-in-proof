"""Step Functions task handlers. One function, one small job per `step`."""
import os
from datetime import timedelta

import core
import store

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "1"))
COSIGN_WINDOW_SECONDS = int(os.environ.get("COSIGN_WINDOW_SECONDS", "259200"))


def handler(event, _context=None):
    return STEPS[event["step"]](event)


def list_photos(event):
    record_id = event["recordId"]
    photos = [{**photo, "recordId": record_id} for photo in store.list_photos(record_id)]
    return {"recordId": record_id, "photos": photos}


def hash_photo(event):
    """Runs once per photo inside the Map state: fingerprint the bytes, then lock that exact version."""
    photo = event["photo"]
    hashed = store.hash_object(photo["key"])
    if hashed is None:
        return {**photo, "missing": True}
    sha256, size, version_id = hashed
    store.lock_version(photo["key"], version_id, core.retain_until(core.now(), RETENTION_DAYS))
    return {**photo, "sha256": sha256, "size": size, "versionId": version_id}


def seal_manifest(event):
    record_id = event["recordId"]
    photos = [p for p in event["hashed"] if not p.get("missing")]
    if not photos:
        raise core.NoPhotos("no uploaded photos to seal")
    record = store.get_record(record_id)
    sealed = core.now()
    manifest = core.build_manifest(record, photos, core.iso(sealed))
    until = core.retain_until(sealed, RETENTION_DAYS)
    key = f"records/{record_id}/manifest.json"
    version_id = store.put_locked(key, manifest, until)
    digest = core.sha256_hex(manifest)
    store.update_record(record_id, {
        "manifestKey": key,
        "manifestVersionId": version_id,
        "manifestSha256": digest,
        "sealedAt": core.iso(sealed),
        "retainUntil": core.iso(until),
    })
    return {"recordId": record_id, "manifestSha256": digest, "coSignWindowSeconds": COSIGN_WINDOW_SECONDS}


def wait_for_landlord(event):
    """Called with a task token. Store it and return; the workflow now waits, with nothing running."""
    deadline = core.now() + timedelta(seconds=COSIGN_WINDOW_SECONDS)
    store.update_record(
        event["recordId"],
        {
            "status": core.AWAITING_LANDLORD,
            "taskToken": event["taskToken"],
            "cosignDeadline": core.iso(deadline),
        },
        expect_status=core.SEALING,
    )
    return {}


def finalize(event):
    record_id = event["recordId"]
    landlord = event.get("landlord")
    status = core.outcome_status(landlord)
    finalized = core.now()
    seal = core.canonical({
        "version": 1,
        "recordId": record_id,
        "manifestSha256": event["manifestSha256"],
        "outcome": status,
        "landlord": landlord,
        "finalizedAt": core.iso(finalized),
    })
    key = f"records/{record_id}/seal.json"
    version_id = store.put_locked(key, seal, core.retain_until(finalized, RETENTION_DAYS))
    sets = {"status": status, "finalizedAt": core.iso(finalized), "sealKey": key, "sealVersionId": version_id}
    removes = ["taskToken"]
    if not landlord:
        # A co-sign that arrived after the window closed does not count.
        removes += ["landlordDecision", "landlordSignedName", "landlordNote", "landlordDecidedAt", "landlordVerifiedEmail"]
    store.update_record(record_id, sets, removes=removes)
    return {"recordId": record_id, "status": status}


def revert(event):
    store.update_record(event["recordId"], {"status": core.DRAFT, "lastError": "Sealing failed. Check the photos and try again."})
    return {"recordId": event["recordId"]}


STEPS = {
    "list": list_photos,
    "hash": hash_photo,
    "seal": seal_manifest,
    "wait": wait_for_landlord,
    "finalize": finalize,
    "revert": revert,
}
