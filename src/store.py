"""Every AWS call lives here. The tests replace this module with an in-memory fake."""
import hashlib
import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("TABLE", "")
BUCKET = os.environ.get("BUCKET", "")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")

_table = boto3.resource("dynamodb").Table(TABLE) if TABLE else None
# Regional endpoint + SigV4: a presigned URL for a new bucket must not bounce through the global endpoint.
_s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
_sfn = boto3.client("stepfunctions")


class Conflict(Exception):
    """The record was not in the state this change requires."""


class TaskGone(Exception):
    """The workflow is no longer waiting on this task token (timed out or already answered)."""


# ---- DynamoDB -------------------------------------------------------------

def get_record(record_id):
    return _table.get_item(Key={"pk": record_id, "sk": "RECORD"}, ConsistentRead=True).get("Item")


def put_record(item):
    _table.put_item(Item={**item, "sk": "RECORD"}, ConditionExpression="attribute_not_exists(pk)")


def put_user_record(sub, record_id, created_at):
    _table.put_item(Item={"pk": "USER#" + sub, "sk": "RECORD#" + record_id, "recordId": record_id, "createdAt": created_at})


def list_user_records(sub):
    result = _table.query(
        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":pk": "USER#" + sub, ":prefix": "RECORD#"},
    )
    return [item["recordId"] for item in sorted(result["Items"], key=lambda i: i["createdAt"], reverse=True)]


def update_record(record_id, sets, removes=(), expect_status=None):
    names, values, parts = {}, {}, []
    for i, (key, value) in enumerate(sets.items()):
        names[f"#s{i}"] = key
        values[f":s{i}"] = value
        parts.append(f"#s{i} = :s{i}")
    expression = "SET " + ", ".join(parts)
    if removes:
        for i, key in enumerate(removes):
            names[f"#r{i}"] = key
        expression += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(removes)))
    kwargs = {}
    if expect_status:
        names["#st"] = "status"
        values[":expected"] = expect_status
        kwargs["ConditionExpression"] = "#st = :expected"
    try:
        _table.update_item(
            Key={"pk": record_id, "sk": "RECORD"},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            **kwargs,
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict() from err
        raise


def put_photo(record_id, photo):
    _table.put_item(Item={**photo, "pk": record_id, "sk": "PHOTO#" + photo["photoId"]})


def delete_photo(record_id, photo_id):
    _table.delete_item(Key={"pk": record_id, "sk": "PHOTO#" + photo_id})


def list_photos(record_id):
    result = _table.query(
        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":pk": record_id, ":prefix": "PHOTO#"},
        ConsistentRead=True,
    )
    return [{k: v for k, v in item.items() if k not in ("pk", "sk")} for item in result["Items"]]


# ---- S3 -------------------------------------------------------------------

def presign_upload(key, content_type, max_bytes):
    return _s3.generate_presigned_post(
        BUCKET,
        key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=600,
    )


def presign_view(key, version_id=None):
    params = {"Bucket": BUCKET, "Key": key}
    if version_id:
        params["VersionId"] = version_id
    return _s3.generate_presigned_url("get_object", Params=params, ExpiresIn=300)


def hash_object(key):
    """Stream the object and return (sha256, size, versionId), or None if it was never uploaded."""
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
    except ClientError as err:
        if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    digest, size = hashlib.sha256(), 0
    for chunk in obj["Body"].iter_chunks(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size, obj["VersionId"]


def lock_version(key, version_id, until):
    _s3.put_object_retention(
        Bucket=BUCKET,
        Key=key,
        VersionId=version_id,
        Retention={"Mode": "GOVERNANCE", "RetainUntilDate": until},
        ChecksumAlgorithm="SHA256",
    )


def put_locked(key, body, until):
    result = _s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json",
        ObjectLockMode="GOVERNANCE",
        ObjectLockRetainUntilDate=until,
        ChecksumAlgorithm="SHA256",
    )
    return result["VersionId"]


def read_object(key, version_id):
    return _s3.get_object(Bucket=BUCKET, Key=key, VersionId=version_id)["Body"].read()


def lock_info(key, version_id):
    head = _s3.head_object(Bucket=BUCKET, Key=key, VersionId=version_id)
    if "ObjectLockRetainUntilDate" not in head:
        return None
    return {
        "mode": head.get("ObjectLockMode"),
        "retainUntil": head["ObjectLockRetainUntilDate"].strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---- Step Functions -------------------------------------------------------

def start_seal(record_id, name):
    _sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN, name=name, input=json.dumps({"recordId": record_id})
    )


def task_success(task_token, output):
    try:
        _sfn.send_task_success(taskToken=task_token, output=json.dumps(output))
    except ClientError as err:
        if err.response["Error"]["Code"] in ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken"):
            raise TaskGone() from err
        raise
