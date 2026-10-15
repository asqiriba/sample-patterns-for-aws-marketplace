"""ECS startup: discover this task, create TLS, then publish readiness."""
import ipaddress
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import tempfile
import time

import boto3

from module10.urls import ECS_METADATA_HOST, open_url


def task_address(ecs, ec2, metadata):
    task_arn, cluster = metadata["TaskARN"], metadata["Cluster"]
    task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
    eni = next(d["value"] for a in task["attachments"] for d in a.get("details", [])
               if d["name"] == "networkInterfaceId")
    interface = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])["NetworkInterfaces"][0]
    return task_arn, str(ipaddress.IPv4Address(interface["Association"]["PublicIp"]))


def main():
    with open_url(os.environ["ECS_CONTAINER_METADATA_URI_V4"] + "/task", timeout=5,
                  http_hosts={ECS_METADATA_HOST}) as response:
        metadata = json.load(response)
    task_arn, address = task_address(boto3.client("ecs"), boto3.client("ec2"), metadata)
    with tempfile.TemporaryDirectory(prefix="portkey-tls-") as directory:
        key, cert = Path(directory) / "key.pem", Path(directory) / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", f"/CN={address}",
            "-addext", f"subjectAltName=IP:{address},IP:127.0.0.1"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        source = Path(__file__).parent.parent / ".cache" / "portkey"
        process = subprocess.Popen(["node", "--import", str(source / "node_modules/tsx/dist/loader.mjs"),
            str(Path(__file__).with_name("hosted.mjs")), str(source)],
            env={**os.environ, "PORTKEY_TLS_KEY": str(key), "PORTKEY_TLS_CERT": str(cert)})
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True
            process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            context = ssl.create_default_context(cafile=str(cert))
            for _ in range(120):
                if stopping or process.poll() is not None:
                    raise RuntimeError("Portkey stopped during startup")
                try:
                    with open_url("https://127.0.0.1:8443/ping", context=context, timeout=1):
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("Portkey startup timed out")
            boto3.client("ssm").put_parameter(Name=os.environ["PORTKEY_DISCOVERY_PARAMETER"],
                Type="String", Overwrite=True, Value=json.dumps({
                    "status": "ready", "url": f"https://{address}:8443",
                    "certificate": cert.read_text(), "task_arn": task_arn,
                    "published_at": int(time.time()),
                }))
            if process.wait() and not stopping:
                raise RuntimeError("Portkey exited unexpectedly")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=65)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
