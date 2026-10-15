"""Start, extend, inspect and stop the on-demand ECS gateway."""
import argparse
from datetime import datetime, timezone
import json
import time

import boto3

from module10.deployment.paths import deployment_path
from module10.portkey_remote import discovery_context
from module10.urls import open_url


class Lifecycle:
    def __init__(self, outputs, *, ecs=None, ssm=None, scheduler=None, secrets=None):
        self.outputs = outputs
        self.ecs = ecs or boto3.client("ecs")
        self.ssm = ssm or boto3.client("ssm")
        self.scheduler = scheduler or boto3.client("scheduler")
        self.secrets = secrets or boto3.client("secretsmanager")
        self.parameter = outputs["discoveryParameter"]
        self.cluster, self.service = outputs["clusterArn"], outputs["serviceName"]

    def write(self, name, value):
        self.ssm.put_parameter(Name=name, Type="String", Overwrite=True, Value=json.dumps(value))

    def read(self, name):
        return json.loads(self.ssm.get_parameter(Name=name)["Parameter"]["Value"])

    def status(self):
        service = self.ecs.describe_services(cluster=self.cluster, services=[self.service])["services"][0]
        session = self.read(self.parameter + "/session")
        endpoint = self.read(self.parameter)
        return {"desired_count": service["desiredCount"], "running_count": service["runningCount"],
            "pending_count": service["pendingCount"], "session": session,
            "console_url": endpoint.get("url", "") + "/public/logs" if endpoint.get("status") == "ready"
                and session.get("expires_at", 0) > time.time() and service["desiredCount"] else None}

    def schedule(self, expires_at):
        expression = datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        args = {"Name": "stop", "GroupName": self.outputs["scheduleGroup"],
            "ScheduleExpression": f"at({expression})", "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"}, "ActionAfterCompletion": "DELETE",
            "State": "ENABLED", "Target": {
                "Arn": "arn:aws:scheduler:::aws-sdk:ecs:updateService",
                "RoleArn": self.outputs["schedulerRoleArn"],
                "Input": json.dumps({"Cluster": self.cluster, "Service": self.service, "DesiredCount": 0}),
                "RetryPolicy": {"MaximumEventAgeInSeconds": 300, "MaximumRetryAttempts": 3},
            }}
        try:
            self.scheduler.get_schedule(Name="stop", GroupName=self.outputs["scheduleGroup"])
        except self.scheduler.exceptions.ResourceNotFoundException:
            self.scheduler.create_schedule(**args)
        else:
            self.scheduler.update_schedule(**args)

    def start(self, hours=2, *, timeout=600):
        expires_at = int(time.time() + hours * 3600)
        # Establish a shutdown before launching paid compute. If scheduling fails,
        # leave an already running session untouched.
        self.schedule(expires_at)
        try:
            self.write(self.parameter + "/session", {"status": "active", "expires_at": expires_at})
            self.ecs.update_service(cluster=self.cluster, service=self.service, desiredCount=1)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                task_arns = self.ecs.list_tasks(cluster=self.cluster, serviceName=self.service,
                    desiredStatus="RUNNING")["taskArns"]
                tasks = self.ecs.describe_tasks(cluster=self.cluster, tasks=task_arns)["tasks"] if task_arns else []
                endpoint = self.read(self.parameter)
                if endpoint.get("task_arn") in {t["taskArn"] for t in tasks if t["lastStatus"] == "RUNNING"}:
                    try:
                        url, context = discovery_context(endpoint)
                        with open_url(url + "/ping", context=context, timeout=3) as response:
                            if json.load(response).get("status") == "ok":
                                return {"status": "ready", "url": url, "console_url": url + "/public/logs",
                                    "expires_at": expires_at,
                                    "console_login": "Use console-login with the same --outputs file. Accept the browser certificate warning."}
                    except (OSError, ValueError):
                        pass
                time.sleep(5)
            raise TimeoutError("Portkey did not become ready within the startup deadline")
        except BaseException:
            self.stop()
            raise

    def extend(self, hours=2):
        if not self.status()["desired_count"]:
            raise RuntimeError("Portkey is stopped; use start")
        expires_at = int(time.time() + hours * 3600)
        self.schedule(expires_at)
        self.write(self.parameter + "/session", {"status": "active", "expires_at": expires_at})
        return {"status": "extended", "expires_at": expires_at}

    def stop(self):
        # Scale down even when discovery updates fail; never remove the safety
        # schedule unless ECS accepted the stop.
        self.ecs.update_service(cluster=self.cluster, service=self.service, desiredCount=0)
        self.write(self.parameter + "/session", {"status": "stopped", "expires_at": 0})
        self.write(self.parameter, {"status": "stopped"})
        try:
            self.scheduler.delete_schedule(Name="stop", GroupName=self.outputs["scheduleGroup"])
        except self.scheduler.exceptions.ResourceNotFoundException:
            pass
        return {"status": "stopping"}

    def wait_stopped(self, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task_arns = set()
            # Desired STOPPED includes tasks still draining and deprovisioning;
            # desired RUNNING alone can falsely report an immediate shutdown.
            for desired in ("RUNNING", "STOPPED"):
                token = None
                while True:
                    page = self.ecs.list_tasks(cluster=self.cluster, serviceName=self.service,
                        desiredStatus=desired, **({"nextToken": token} if token else {}))
                    task_arns.update(page["taskArns"])
                    token = page.get("nextToken")
                    if not token:
                        break
            tasks = []
            task_arns = list(task_arns)
            for offset in range(0, len(task_arns), 100):
                tasks.extend(self.ecs.describe_tasks(cluster=self.cluster,
                    tasks=task_arns[offset:offset + 100])["tasks"])
            if all(task["lastStatus"] == "STOPPED" for task in tasks):
                return {"status": "stopped"}
            time.sleep(3)
        raise TimeoutError("ECS is still draining tasks; the stop request remains active")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop", "extend", "status", "console-login"])
    parser.add_argument("--outputs", type=deployment_path, required=True)
    parser.add_argument("--hours", type=float, default=2)
    args = parser.parse_args()
    if not 0.1 <= args.hours <= 12:
        parser.error("--hours must be between 0.1 and 12")
    lifecycle = Lifecycle(json.loads(args.outputs.read_text()))
    if args.command == "console-login":
        # Deliberate interactive disclosure of only the console credential.
        print(lifecycle.secrets.get_secret_value(
            SecretId=lifecycle.outputs["consoleSecretArn"])["SecretString"])
        return
    if args.command == "start":
        result = lifecycle.start(args.hours)
    elif args.command == "extend":
        result = lifecycle.extend(args.hours)
    elif args.command == "stop":
        lifecycle.stop()
        result = lifecycle.wait_stopped()
    else:
        result = lifecycle.status()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
