"""In-memory stand-in for src/store.py, shared by the tests and the local dev server."""
import hashlib
import types


class Conflict(Exception):
    pass


class TaskGone(Exception):
    pass


class FakeStore(types.ModuleType):
    """Same function names as src/store.py, backed by dictionaries."""

    def __init__(self):
        super().__init__("store")
        self.Conflict, self.TaskGone = Conflict, TaskGone
        self.reset()

    def reset(self):
        self.records, self.photos, self.objects, self.user_records = {}, {}, {}, {}
        self.executions, self.live_tokens, self.task_outputs = [], set(), {}

    # DynamoDB
    def get_record(self, record_id):
        item = self.records.get(record_id)
        return dict(item) if item else None

    def put_record(self, item):
        self.records[item["pk"]] = dict(item)

    def put_user_record(self, sub, record_id, created_at):
        self.user_records.setdefault(sub, []).insert(0, record_id)

    def list_user_records(self, sub):
        return list(self.user_records.get(sub, []))

    def update_record(self, record_id, sets, removes=(), expect_status=None):
        item = self.records[record_id]
        if expect_status and item["status"] != expect_status:
            raise Conflict()
        item.update(sets)
        for key in removes:
            item.pop(key, None)

    def put_photo(self, record_id, photo):
        self.photos.setdefault(record_id, []).append(dict(photo))

    def delete_photo(self, record_id, photo_id):
        self.photos[record_id] = [p for p in self.photos.get(record_id, []) if p["photoId"] != photo_id]

    def list_photos(self, record_id):
        return [dict(p) for p in self.photos.get(record_id, [])]

    # S3 (versioned)
    def upload(self, key, body):
        versions = self.objects.setdefault(key, [])
        versions.append({"id": f"v{len(versions) + 1}", "body": body, "until": None})
        return versions[-1]["id"]

    def presign_upload(self, key, content_type, max_bytes):
        return {"url": "https://upload.invalid/", "fields": {"key": key, "Content-Type": content_type}}

    def presign_view(self, key, version_id=None):
        return f"https://view.invalid/{key}?v={version_id}"

    def hash_object(self, key):
        versions = self.objects.get(key)
        if not versions:
            return None
        latest = versions[-1]
        return hashlib.sha256(latest["body"]).hexdigest(), len(latest["body"]), latest["id"]

    def _version(self, key, version_id):
        return next(v for v in self.objects[key] if v["id"] == version_id)

    def lock_version(self, key, version_id, until):
        self._version(key, version_id)["until"] = until

    def put_locked(self, key, body, until):
        version_id = self.upload(key, body)
        self._version(key, version_id)["until"] = until
        return version_id

    def read_object(self, key, version_id):
        return self._version(key, version_id)["body"]

    def lock_info(self, key, version_id):
        until = self._version(key, version_id)["until"]
        return {"mode": "GOVERNANCE", "retainUntil": until.strftime("%Y-%m-%dT%H:%M:%SZ")} if until else None

    # Step Functions
    def start_seal(self, record_id, name):
        self.executions.append(record_id)

    def task_success(self, task_token, output):
        if task_token not in self.live_tokens:
            raise TaskGone()
        self.live_tokens.discard(task_token)
        self.task_outputs[task_token] = output
