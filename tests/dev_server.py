"""Local rehearsal server: the real page and handlers, with AWS replaced by memory.

    python tests/dev_server.py            # http://localhost:8787, landlord window 120 s
    python tests/dev_server.py 8787 30    # port, landlord window in seconds

Nothing is saved; restarting clears every record. This is for rehearsing the demo and
checking the page, not proof that the deployed stack works.
"""
import base64
import json
import os
import sys
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
WINDOW = int(sys.argv[2]) if len(sys.argv) > 2 else 120
os.environ["COSIGN_WINDOW_SECONDS"] = str(WINDOW)
os.environ["COGNITO_ENDPOINT"] = "/_cognito"
os.environ["COGNITO_CLIENT_ID"] = "local"

from fake_store import FakeStore  # noqa: E402


class LocalStore(FakeStore):
    """Adds local upload/view URLs and plays the part of the state machine."""

    def presign_upload(self, key, content_type, max_bytes):
        return {"url": "/_upload", "fields": {"key": key, "Content-Type": content_type}}

    def presign_view(self, key, version_id=None):
        return f"/_view/{key}?v={version_id or ''}"

    def start_seal(self, record_id, name):
        threading.Thread(target=self._run, args=(record_id,), daemon=True).start()

    def _run(self, record_id):
        listed = workflow.handler({"step": "list", "recordId": record_id})
        hashed = [workflow.handler({"step": "hash", "photo": p}) for p in listed["photos"]]
        try:
            sealed = workflow.handler({"step": "seal", "recordId": record_id, "hashed": hashed})
        except Exception:
            workflow.handler({"step": "revert", "recordId": record_id})
            return
        token = "token-" + record_id
        self.live_tokens.add(token)
        self.sealed[token] = sealed
        workflow.handler({"step": "wait", "recordId": record_id, "taskToken": token})
        threading.Timer(WINDOW, self._timeout, args=(token,)).start()

    def _finalize(self, token, landlord=None):
        sealed = self.sealed[token]
        event = {"step": "finalize", "recordId": sealed["recordId"], "manifestSha256": sealed["manifestSha256"]}
        if landlord:
            event["landlord"] = landlord
        workflow.handler(event)

    def _timeout(self, token):
        if token in self.live_tokens:
            self.live_tokens.discard(token)
            self._finalize(token)

    def task_success(self, task_token, output):
        super().task_success(task_token, output)
        threading.Timer(1.0, self._finalize, args=(task_token, output)).start()


store = LocalStore()
store.sealed = {}
sys.modules["store"] = store

import api  # noqa: E402
import workflow  # noqa: E402


# ---- stand-in for Amazon Cognito: same request and error shapes, accounts kept in memory ----
USERS = {}
LOCAL_CODE = "123456"  # every "emailed" code in local rehearsal


def _error(kind, message):
    return 400, {"__type": kind, "message": message}


def _id_token(email):
    claims = {"sub": USERS[email]["sub"], "email": email, "email_verified": "true", "exp": int(time.time()) + 3600}
    return "local." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".local"


def claims_from(header):
    """API Gateway verifies a real signature. Locally the token is only decoded."""
    try:
        part = header.removeprefix("Bearer ").strip().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return claims if claims["exp"] > time.time() and claims["sub"] else None
    except Exception:
        return None


def fake_cognito(target, body):
    op = target.split(".")[-1]
    params = body.get("AuthParameters") or {}
    email = (body.get("Username") or params.get("USERNAME") or "").strip().lower()
    user = USERS.get(email)
    if op == "SignUp":
        if user and user["confirmed"]:
            return _error("UsernameExistsException", "An account with the given email already exists.")
        if len(body.get("Password", "")) < 8:
            return _error("InvalidPasswordException", "Password must have at least 8 characters.")
        USERS[email] = {"password": body["Password"], "confirmed": False, "sub": "local-" + os.urandom(6).hex()}
        return 200, {"UserConfirmed": False, "CodeDeliveryDetails": {"Destination": email}}
    if op in ("ConfirmSignUp", "ConfirmForgotPassword"):
        if not user or body.get("ConfirmationCode") != LOCAL_CODE:
            return _error("CodeMismatchException", "Invalid verification code provided, please try again.")
        user["confirmed"] = True
        if op == "ConfirmForgotPassword":
            user["password"] = body.get("Password", "")
        return 200, {}
    if op in ("ResendConfirmationCode", "ForgotPassword"):
        return 200, {"CodeDeliveryDetails": {"Destination": email}}
    if op == "InitiateAuth":
        if body.get("AuthFlow") == "REFRESH_TOKEN_AUTH":
            email = params.get("REFRESH_TOKEN", "").removeprefix("refresh:")
            if email not in USERS:
                return _error("NotAuthorizedException", "Refresh token is invalid.")
        elif not user or user["password"] != params.get("PASSWORD"):
            return _error("NotAuthorizedException", "Incorrect username or password.")
        elif not user["confirmed"]:
            return _error("UserNotConfirmedException", "User is not confirmed.")
        return 200, {"AuthenticationResult": {"IdToken": _id_token(email), "RefreshToken": "refresh:" + email, "ExpiresIn": 3600}}
    return _error("UnknownOperationException", op)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, headers, body):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.command == "POST" and url.path == "/_upload":
            head = b"Content-Type: " + self.headers["content-type"].encode() + b"\r\n\r\n"
            parts = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                     for p in BytesParser(policy=HTTP).parsebytes(head + raw).iter_parts()}
            store.upload(parts["key"].decode(), parts["file"])
            return self._send(204, {}, b"")
        if self.command == "GET" and url.path.startswith("/_view/"):
            key = unquote(url.path[len("/_view/"):])
            version = parse_qs(url.query).get("v", [""])[0]
            versions = store.objects.get(key) or []
            match = next((v for v in versions if v["id"] == version), versions[-1] if versions else None)
            if not match:
                return self._send(404, {}, b"")
            return self._send(200, {"content-type": "image/jpeg"}, match["body"])
        if self.command == "POST" and url.path == "/_cognito":
            status, payload = fake_cognito(self.headers.get("x-amz-target", ""), json.loads(raw or b"{}"))
            return self._send(status, {"content-type": "application/x-amz-json-1.1"}, json.dumps(payload).encode())
        context = {"http": {"method": self.command}}
        if url.path.startswith("/api/my/"):
            claims = claims_from(self.headers.get("authorization", ""))
            if not claims:  # what the API Gateway JWT authorizer answers
                return self._send(401, {"content-type": "application/json"}, b'{"message":"Unauthorized"}')
            context["authorizer"] = {"jwt": {"claims": claims}}
        api._html_cache = None  # always serve the page as it is on disk
        result = api.handler({
            "requestContext": context,
            "rawPath": url.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": raw.decode() if raw else None,
        })
        self._send(result["statusCode"], result["headers"], result["body"].encode())

    do_GET = do_POST = do_DELETE = _handle

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Move-In Proof (local rehearsal) on http://localhost:{PORT}  landlord window: {WINDOW}s")
    print(f"Accounts live in memory. The emailed confirmation code is always {LOCAL_CODE}.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
