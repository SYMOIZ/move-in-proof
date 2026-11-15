"""End-to-end tests of the API and workflow handlers against an in-memory stand-in for AWS.

Run from the project folder:  python -m unittest discover -s tests -v
No AWS account, boto3 or network is needed. These prove the logic, not the deployed wiring.
"""
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from fake_store import FakeStore  # noqa: E402

store = FakeStore()
sys.modules["store"] = store

import api  # noqa: E402
import core  # noqa: E402
import workflow  # noqa: E402

ASHA = {"sub": "user-asha", "email": "asha@example.test", "email_verified": "true"}
RAO = {"sub": "user-rao", "email": "rao@example.test", "email_verified": "true"}
STRANGER = {"sub": "user-other", "email": "other@example.test", "email_verified": "true"}


def call(method, path, body=None, token=None, user=None):
    """user=claims sends the request through the signed-in door (/api/my/...), as API Gateway would."""
    context = {"http": {"method": method}}
    if user:
        path = path.replace("/api/", "/api/my/", 1)
        context["authorizer"] = {"jwt": {"claims": user}}
    event = {
        "requestContext": context,
        "rawPath": path,
        "headers": {"x-mip-token": token} if token else {},
        "body": json.dumps(body) if body is not None else None,
    }
    response = api.handler(event)
    is_json = response["headers"]["content-type"].startswith("application/json")
    return response["statusCode"], json.loads(response["body"]) if is_json else response["body"]


def run_seal_workflow(record_id):
    """Walks the same states as statemachine/seal.asl.json up to the landlord wait."""
    listed = workflow.handler({"step": "list", "recordId": record_id})
    hashed = [workflow.handler({"step": "hash", "photo": p}) for p in listed["photos"]]
    try:
        sealed = workflow.handler({"step": "seal", "recordId": record_id, "hashed": hashed})
    except core.NoPhotos:
        workflow.handler({"step": "revert", "recordId": record_id})
        return None
    task_token = "token-" + record_id
    store.live_tokens.add(task_token)
    workflow.handler({"step": "wait", "recordId": record_id, "taskToken": task_token})
    return {**sealed, "taskToken": task_token}


def finalize(state, timed_out=False):
    event = {"step": "finalize", "recordId": state["recordId"], "manifestSha256": state["manifestSha256"]}
    if timed_out:
        store.live_tokens.discard(state["taskToken"])
    else:
        event["landlord"] = store.task_outputs[state["taskToken"]]
    return workflow.handler(event)


class FlowTest(unittest.TestCase):
    def setUp(self):
        store.reset()
        status, created = call("POST", "/api/records", {
            "address": "Flat 302, Lake View Residency", "tenantName": "Asha", "landlordName": "Mr Rao"}, user=ASHA)
        self.assertEqual(status, 201)
        self.id = created["id"]
        self.landlord = self.view(user=ASHA)["landlordToken"]
        self.base = f"/api/records/{self.id}"

    def add_photo(self, label, body=None):
        status, result = call("POST", self.base + "/photos",
                              {"label": label, "filename": label + ".jpg", "contentType": "image/jpeg"}, user=ASHA)
        self.assertEqual(status, 200, result)
        if body is not None:
            store.upload(result["upload"]["fields"]["key"], body)
        return result

    def seal(self):
        status, _ = call("POST", self.base + "/seal", {}, user=ASHA)
        self.assertEqual(status, 200)
        return run_seal_workflow(self.id)

    def view(self, token=None, user=None):
        status, record = call("GET", f"/api/records/{self.id}", token=token, user=user)
        self.assertEqual(status, 200)
        return record

    def test_agree_path_seals_and_cosigns(self):
        self.add_photo("Kitchen", b"kitchen-bytes")
        self.add_photo("Bathroom", b"bathroom-bytes")
        state = self.seal()
        record = self.view()
        self.assertEqual(record["status"], core.AWAITING_LANDLORD)
        self.assertEqual(hashlib.sha256(record["manifest"].encode()).hexdigest(), record["manifestSha256"])
        self.assertEqual(sorted(p["sha256"] for p in record["photos"]),
                         sorted(hashlib.sha256(b).hexdigest() for b in (b"kitchen-bytes", b"bathroom-bytes")))
        self.assertIsNotNone(record["lock"])

        status, _ = call("POST", self.base + "/cosign", {"decision": "agree", "name": "Mr Rao", "note": ""}, self.landlord)
        self.assertEqual(status, 200)
        self.assertEqual(finalize(state)["status"], core.SEALED_COSIGNED)
        final = self.view()
        self.assertEqual(final["status"], core.SEALED_COSIGNED)
        self.assertEqual(final["landlord"]["decision"], "agree")
        self.assertFalse(final["landlord"]["verified"])
        self.assertNotIn("taskToken", store.records[self.id])

    def test_signed_in_landlord_answer_carries_a_verified_email(self):
        self.add_photo("Hall", b"hall")
        state = self.seal()
        status, _ = call("POST", self.base + "/cosign", {"decision": "agree", "name": "Mr Rao"}, self.landlord, user=RAO)
        self.assertEqual(status, 200)
        finalize(state)
        self.assertEqual(self.view(user=ASHA)["landlord"]["verifiedEmail"], "rao@example.test")
        public = self.view()["landlord"]
        self.assertTrue(public["verified"])
        self.assertIsNone(public["verifiedEmail"])  # the address itself is not shown to the public

    def test_unconfirmed_email_is_not_recorded_as_verified(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        call("POST", self.base + "/cosign", {"decision": "agree", "name": "x"}, self.landlord,
             user={**RAO, "email_verified": "false"})
        self.assertEqual(store.records[self.id]["landlordVerifiedEmail"], "")

    def test_dispute_is_recorded_as_disputed(self):
        self.add_photo("Hall", b"hall")
        state = self.seal()
        call("POST", self.base + "/cosign", {"decision": "dispute", "name": "Mr Rao", "note": "Wall crack is missing"}, self.landlord)
        self.assertEqual(finalize(state)["status"], core.SEALED_DISPUTED)
        self.assertEqual(self.view()["landlord"]["note"], "Wall crack is missing")

    def test_dispute_needs_a_reason(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        status, _ = call("POST", self.base + "/cosign", {"decision": "dispute", "name": "Mr Rao", "note": " "}, self.landlord)
        self.assertEqual(status, 400)
        self.assertEqual(store.records[self.id]["status"], core.AWAITING_LANDLORD)

    def test_timeout_seals_tenant_only_and_rejects_late_cosign(self):
        self.add_photo("Hall", b"hall")
        state = self.seal()
        store.live_tokens.discard(state["taskToken"])  # the workflow's wait timed out
        status, body = call("POST", self.base + "/cosign", {"decision": "agree", "name": "Mr Rao"}, self.landlord)
        self.assertEqual(status, 409, body)
        self.assertEqual(finalize(state, timed_out=True)["status"], core.SEALED_TENANT_ONLY)
        final = self.view()
        self.assertEqual(final["status"], core.SEALED_TENANT_ONLY)
        self.assertIsNone(final["landlord"])

    def test_roles_and_what_each_one_sees(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        public = self.view()
        self.assertEqual(public["role"], "public")
        self.assertIsNone(public["photos"][0]["url"])
        self.assertIsNone(public["landlordToken"])
        self.assertIsNone(public["tenantEmail"])
        owner = self.view(user=ASHA)
        self.assertEqual(owner["role"], "tenant")
        self.assertIsNotNone(owner["photos"][0]["url"])
        landlord = self.view(self.landlord)
        self.assertEqual(landlord["role"], "landlord")
        self.assertIsNone(landlord["landlordToken"])
        self.assertEqual(self.view(user=STRANGER)["role"], "public")

    def test_swapping_a_photo_after_sealing_does_not_change_the_record(self):
        upload = self.add_photo("Wall", b"clean wall")
        self.seal()
        key = upload["upload"]["fields"]["key"]
        store.upload(key, b"wall with a crack painted in")  # a still-valid upload link is reused
        photo = self.view(user=ASHA)["photos"][0]
        self.assertEqual(photo["sha256"], hashlib.sha256(b"clean wall").hexdigest())
        self.assertEqual(photo["versionId"], "v1")
        self.assertIn("v=v1", photo["url"])

    def test_no_photos_can_be_added_after_sealing(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        status, _ = call("POST", self.base + "/photos",
                         {"label": "Late", "filename": "late.jpg", "contentType": "image/jpeg"}, user=ASHA)
        self.assertEqual(status, 409)

    def test_record_cannot_be_sealed_twice(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        self.assertEqual(call("POST", self.base + "/seal", {}, user=ASHA)[0], 409)
        self.assertEqual(len(store.executions), 1)

    def test_landlord_can_answer_only_once(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        answer = {"decision": "agree", "name": "Mr Rao"}
        self.assertEqual(call("POST", self.base + "/cosign", answer, self.landlord)[0], 200)
        again = {**answer, "decision": "dispute", "note": "changed my mind"}
        self.assertEqual(call("POST", self.base + "/cosign", again, self.landlord)[0], 409)
        self.assertEqual(store.records[self.id]["landlordDecision"], "agree")

    def test_only_the_landlord_link_can_cosign(self):
        self.add_photo("Hall", b"hall")
        self.seal()
        answer = {"decision": "agree", "name": "x"}
        self.assertEqual(call("POST", self.base + "/cosign", answer, user=ASHA)[0], 403)  # the tenant cannot
        self.assertEqual(call("POST", self.base + "/cosign", answer)[0], 403)
        self.assertEqual(call("POST", self.base + "/cosign", answer, "wrong")[0], 403)

    def test_only_the_owning_account_can_add_remove_or_seal(self):
        photo = self.add_photo("Hall", b"hall")
        new = {"label": "x", "filename": "x.jpg", "contentType": "image/jpeg"}
        for who in ({"user": STRANGER}, {"token": self.landlord}, {}):
            self.assertEqual(call("POST", self.base + "/photos", new, **who)[0], 403)
            self.assertEqual(call("DELETE", self.base + "/photos/" + photo["photoId"], **who)[0], 403)
            self.assertEqual(call("POST", self.base + "/seal", {}, **who)[0], 403)
        self.assertEqual(store.records[self.id]["status"], core.DRAFT)

    def test_tenant_can_remove_a_photo_only_while_in_draft(self):
        keep = self.add_photo("Kitchen", b"kitchen")
        drop = self.add_photo("Blurry", b"blurry")
        self.assertEqual(call("DELETE", self.base + "/photos/" + drop["photoId"], user=ASHA)[0], 200)
        self.seal()
        self.assertEqual([p["label"] for p in self.view()["photos"]], ["Kitchen"])
        self.assertEqual(call("DELETE", self.base + "/photos/" + keep["photoId"], user=ASHA)[0], 409)

    def test_photo_that_never_uploaded_is_left_out(self):
        self.add_photo("Kitchen", b"kitchen")
        self.add_photo("Ghost")  # link issued, file never uploaded
        self.seal()
        self.assertEqual([p["label"] for p in self.view()["photos"]], ["Kitchen"])

    def test_sealing_with_no_uploaded_files_returns_to_draft(self):
        self.add_photo("Ghost")
        self.assertIsNone(self.seal())
        record = self.view(user=ASHA)
        self.assertEqual(record["status"], core.DRAFT)
        self.assertTrue(record["lastError"])

    def test_creating_and_listing_records_needs_an_account(self):
        new = {"address": "a", "tenantName": "b", "landlordName": "c"}
        self.assertEqual(call("POST", "/api/records", new)[0], 401)
        self.assertEqual(call("GET", "/api/records")[0], 401)
        self.assertEqual(call("GET", "/api/my/records")[0], 401)  # signed-in door, but no claims arrived

    def test_my_records_lists_only_my_own_newest_first(self):
        call("POST", "/api/records", {"address": "Second flat", "tenantName": "Asha", "landlordName": "x"}, user=ASHA)
        call("POST", "/api/records", {"address": "Not mine", "tenantName": "o", "landlordName": "x"}, user=STRANGER)
        status, body = call("GET", "/api/records", user=ASHA)
        self.assertEqual(status, 200)
        self.assertEqual([r["address"] for r in body["records"]], ["Second flat", "Flat 302, Lake View Residency"])

    def test_input_validation(self):
        self.assertEqual(call("POST", "/api/records", {"address": "", "tenantName": "a", "landlordName": "b"}, user=ASHA)[0], 400)
        self.assertEqual(call("POST", self.base + "/photos",
                              {"label": "x", "filename": "x.pdf", "contentType": "application/pdf"}, user=ASHA)[0], 400)
        self.assertEqual(call("POST", self.base + "/seal", {}, user=ASHA)[0], 400)  # no photos yet
        self.assertEqual(call("GET", "/api/records/does-not-exist")[0], 404)
        self.assertEqual(call("GET", "/api/other")[0], 404)

    def test_page_and_config_are_served(self):
        status, body = call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Move-In Proof", body)
        status, config = call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("cognito-idp", config["endpoint"])


class CoreTest(unittest.TestCase):
    def test_manifest_bytes_do_not_depend_on_input_order(self):
        record = {"pk": "r1", "address": "a", "tenantName": "t", "landlordName": "l", "createdAt": "c"}
        one = {"photoId": "1", "label": "x", "filename": "x", "sha256": "aa", "size": 1, "versionId": "v", "seq": "1"}
        two = {**one, "photoId": "2", "seq": "2"}
        self.assertEqual(core.build_manifest(record, [one, two], "s"), core.build_manifest(record, [two, one], "s"))

    def test_any_change_changes_the_fingerprint(self):
        record = {"pk": "r1", "address": "a", "tenantName": "t", "landlordName": "l", "createdAt": "c"}
        photo = {"photoId": "1", "label": "x", "filename": "x", "sha256": "aa", "size": 1, "versionId": "v"}
        base = core.sha256_hex(core.build_manifest(record, [photo], "s"))
        self.assertNotEqual(base, core.sha256_hex(core.build_manifest(record, [{**photo, "sha256": "ab"}], "s")))
        self.assertNotEqual(base, core.sha256_hex(core.build_manifest(record, [{**photo, "label": "y"}], "s")))


if __name__ == "__main__":
    unittest.main()
