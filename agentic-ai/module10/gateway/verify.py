"""Verify deployed HTTPS, separate credentials, console feed, and optional Bedrock routing."""
import argparse
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from uuid import uuid4

import boto3

from module10.deployment.paths import deployment_path
from module10.portkey_remote import RemotePortkeyReviewer, discovery_context
from module10.urls import open_url


def verify(outputs, *, model=False):
    ssm, secrets = boto3.client("ssm"), boto3.client("secretsmanager")
    discovery = json.loads(ssm.get_parameter(Name=outputs["discoveryParameter"])["Parameter"]["Value"])
    url, context = discovery_context(discovery)
    service = secrets.get_secret_value(SecretId=outputs["serviceSecretArn"])["SecretString"]
    console_token = secrets.get_secret_value(SecretId=outputs["consoleSecretArn"])["SecretString"]
    checks = {}

    def request(path, body=None, token=None):
        req = urllib.request.Request(url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                **({"Authorization": "Bearer " + token} if token else {})})
        try:
            return open_url(req, context=context, timeout=10)
        except urllib.error.HTTPError as error:
            return error

    with request("/ping") as response:
        assert response.status == 200
        checks["https_health"] = 200
    try:
        open_url(url + "/ping", timeout=5)
    except urllib.error.URLError as error:
        assert isinstance(error.reason, ssl.SSLCertVerificationError)
        checks["untrusted_certificate_rejected"] = True
    else:
        raise AssertionError("Untrusted TLS certificate unexpectedly accepted")
    with request("/v1/openai/v1/responses", {}, console_token) as response:
        assert response.status == 401
        checks["console_cannot_invoke_models"] = response.status
    with request("/log/stream", token=service) as response:
        assert response.status == 401
        checks["service_cannot_read_console"] = response.status
    with request("/public/auth", {"admin_token": service}) as response:
        assert response.status == 401
    with request("/public/auth", {"admin_token": console_token}) as response:
        assert response.status == 200 and "Secure" in response.headers["Set-Cookie"]
        checks["console_login"] = response.status
    with request("/public/logs") as response:
        assert response.status == 200 and b"Portkey" in response.read()
        checks["hosted_console_page"] = response.status
    with request("/__demo/drill", {"armed": True}, service) as response:
        assert response.status == 404
        checks["global_drill_disabled"] = response.status

    if model:
        os.environ.update(MODULE10_PORTKEY_DISCOVERY=outputs["discoveryParameter"],
            MODULE10_PORTKEY_SECRET=outputs["serviceSecretArn"],
            MODULE10_PORTKEY_EVIDENCE_BUCKET=outputs["evidenceBucket"])
        request_id = "gateway-verify-" + uuid4().hex
        reviewer = RemotePortkeyReviewer.from_env({
            "team": "platform", "principal": "gateway-verifier", "request_id": request_id})
        record = {}
        try:
            reviewer.set_drill(True)
            response = reviewer.create_response(agent="Recovery Planner", evidence_ready=True,
                record=record, deadline=time.monotonic() + 60,
                input="Reply with exactly: gateway ready", max_output_tokens=100,
                reasoning={"effort": "none"}, store=False)
            assert response.status == "completed"
            assert record["fallback_used"] and record["attempts"][0]["status_code"] == 429
            assert record["attempts"][1]["source"] == "bedrock"
            assert reviewer.console_evidence()
            checks["real_bedrock_fallback"] = True
            checks["request_id"] = request_id
            checks["call_id"] = record["call_id"]
            with request("/log/stream", token=console_token) as stream:
                found = False
                for _ in range(500):
                    line = stream.readline()
                    if request_id.encode() in line:
                        found = True
                        break
                assert found, "Model request missing from the hosted console feed"
                checks["live_console_request"] = True
        finally:
            reviewer.close()
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", type=deployment_path, required=True)
    parser.add_argument("--report", type=deployment_path, required=True)
    parser.add_argument("--model", action="store_true", help="Make one paid Bedrock fallback call")
    args = parser.parse_args()
    checks = verify(json.loads(args.outputs.read_text()), model=args.model)
    args.report.write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
