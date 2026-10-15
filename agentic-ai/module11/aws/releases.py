# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The release bucket `m11-release-<account>-<region>` and the operator seal.

Layout: handlers/ layers/ builds/ releases/<mv>/ release-inputs/<stage>/ evidence/ snapshots/
tooling/ evaluations/ baselines/ previews/ ledger/ captures/. Objects are content-addressed and
written once: an existing key is never overwritten (`put_absent`), so a re-seal of the same bytes
is a no-op and a sealed object can be trusted by its key.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable

from module11.release import naming
from module11.release.hashing import sha256_bytes
from module11.release.manifest import ReleaseInputsCorrupt

IMAGE_POLL_SECONDS = 10
IMAGE_DEADLINE_SECONDS = 1800


def bucket_name(account: str, region: str) -> str:
    return naming.RELEASE_BUCKET.format(account=account, region=region)


def split(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri}")
    bucket, key = uri[5:].split("/", 1)
    return bucket, key


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:
        if _code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def put_absent(s3: Any, bucket: str, key: str, body: bytes, *, metadata: dict | None = None,
               content_type: str = "application/octet-stream") -> bool:
    """Write once: returns False when the key already exists (content-addressed keys)."""
    if exists(s3, bucket, key):
        return False
    kwargs = {"Bucket": bucket, "Key": key, "Body": body, "ContentType": content_type}
    if metadata:
        kwargs["Metadata"] = metadata
    s3.put_object(**kwargs)
    return True


def upload(s3: Any, bucket: str, objects: Iterable[Any]) -> dict[str, int]:
    counts = {"written": 0, "present": 0}
    for obj in objects:
        written = put_absent(s3, bucket, obj.key, obj.body, metadata=obj.metadata or None, content_type=obj.content_type)
        counts["written" if written else "present"] += 1
    return counts


def get(s3: Any, uri: str) -> bytes:
    bucket, key = split(uri)
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def get_verified(s3: Any, uri: str, sha256: str, what: str) -> bytes:
    data = get(s3, uri)
    if sha256_bytes(data) != sha256:
        raise ReleaseInputsCorrupt(f"{what}: sha256 of {uri} differs from the record")
    return data


def put_json(s3: Any, bucket: str, key: str, doc: dict) -> tuple[str, str]:
    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return f"s3://{bucket}/{key}", sha256_bytes(body)


def seal_record_uri(bucket: str, manifest_version: str) -> str:
    return f"s3://{bucket}/releases/{manifest_version}/seal.json"


def read_seal(s3: Any, bucket: str, manifest_version: str, *, experiment: bool = False) -> dict:
    uri = seal_record_uri(bucket, manifest_version)
    if experiment:
        uri = uri.replace("/releases/", "/releases/experiments/", 1)
    return json.loads(get(s3, uri))


def latest_seal(s3: Any, bucket: str) -> str | None:
    """The newest sealed manifestVersion (keys sort by timestamp)."""
    best, token = None, None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": "releases/", **({"ContinuationToken": token} if token else {})}
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/seal.json") and "/experiments/" not in obj["Key"]:
                mv = obj["Key"][len("releases/"):-len("/seal.json")]
                best = mv if best is None or mv > best else best
        if not page.get("IsTruncated"):
            return best
        token = page.get("NextContinuationToken")


# ---------------------------------------------------------------- the image (m11-image)
def image_digest(ecr: Any, repository: str, tag: str) -> str | None:
    try:
        images = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])["imageDetails"]
    except Exception as exc:
        if _code(exc) == "ImageNotFoundException":
            return None
        raise
    return images[0]["imageDigest"] if images else None


def build_image(codebuild: Any, ecr: Any, *, project: str, repository: str, context_hex: str, tag: str,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                log: Callable[[str], None] = print, buildspec: str | None = None,
                extra_env: dict[str, str] | None = None) -> str:
    """Start m11-image for a sealed context unless the tag exists; wait; return the digest.

    `buildspec`: run with a NO_SOURCE override and this buildspec (the project's own text, read from the
    checkout), which an operator needs once m11-image's source type is CODEPIPELINE."""
    digest = image_digest(ecr, repository, tag)
    if digest:
        log(f"image {repository}:{tag[:16]}… already exists ({digest[:19]}…)")
        return digest
    env = {"CONTEXT_HASH": context_hex, "IMAGE_TAG": tag, **(extra_env or {})}
    request: dict[str, Any] = {"projectName": project, "environmentVariablesOverride": [
        {"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()]}
    if buildspec:
        request.update(sourceTypeOverride="NO_SOURCE", buildspecOverride=buildspec,
                       artifactsOverride={"type": "NO_ARTIFACTS"})
    build = codebuild.start_build(**request)["build"]
    log(f"image build {build['id']} started")
    end = clock() + IMAGE_DEADLINE_SECONDS
    while True:
        status = codebuild.batch_get_builds(ids=[build["id"]])["builds"][0]
        if status["buildStatus"] != "IN_PROGRESS":
            break
        if clock() >= end:
            raise RuntimeError(f"image build {build['id']} did not finish in {IMAGE_DEADLINE_SECONDS} s")
        sleep(IMAGE_POLL_SECONDS)
    if status["buildStatus"] != "SUCCEEDED":
        raise RuntimeError(f"image build {build['id']} ended {status['buildStatus']}")
    digest = image_digest(ecr, repository, tag)
    if not digest:
        raise RuntimeError(f"image build {build['id']} succeeded but {repository}:{tag} is missing")
    seconds = (status["endTime"] - status["startTime"]).total_seconds() if status.get("endTime") else None
    log(f"image build {build['id']} SUCCEEDED" + (f" in {int(seconds)} s" if seconds else ""))
    return digest
