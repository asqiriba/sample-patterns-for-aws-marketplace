# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""EventBridge bus `m11-releases`, SSM `/m11/devops-companion/freeze`, the freeze-check Lambda and the
`m11-gitlab-notes` broker. Built behind
`enablePipeline=true`, with the corpus handler and its DLQ.

Both Lambdas are Python 3.13, published (P2), with a pre-created 30-day log group (P5) and a role
trusted by lambda.amazonaws.com for this account only (P6). Cost notes: the freeze check runs once
per release at Approval (and about once a minute while a planned freeze holds); the broker runs a
few times per MR pipeline. Both stay within the Lambda free tier for the demo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pulumi
import pulumi_aws as aws

from module11.infrastructure.platform import pipeline_roles as PR
from module11.infrastructure.platform import roles as R
from module11.release import naming

MODULE11 = Path(__file__).resolve().parents[2]
LOG_RETENTION_DAYS = 30


def bus(tags: dict) -> aws.cloudwatch.EventBus:
    return aws.cloudwatch.EventBus("release-bus", name=PR.EVENT_BUS, tags=tags)


def freeze_parameter(tags: dict) -> aws.ssm.Parameter:
    """Created as `none`; the value is owned by operators (`module11 freeze`) and the shadow experiment."""
    return aws.ssm.Parameter("freeze", name=naming.freeze_param(), type="String", tier="Standard",
                             value=json.dumps({"state": "none"}), tags=tags,
                             opts=pulumi.ResourceOptions(ignore_changes=["value"]))


def _function(name: str, *, account: str, code: dict[str, Path], policy: pulumi.Input[str], env: dict,
              tags: dict, timeout: int) -> aws.lambda_.Function:
    logs = aws.cloudwatch.LogGroup(f"{name}-logs", name=f"/aws/lambda/{name}", retention_in_days=LOG_RETENTION_DAYS,
                                   tags=tags)
    role = aws.iam.Role(f"{name}-role", name=name, tags={**tags, "M11Role": "lambda"},
                        assume_role_policy=json.dumps(R.service_trust("lambda.amazonaws.com", account)))
    inline = aws.iam.RolePolicy(f"{name}-policy", role=role.id, policy=policy)
    archive = pulumi.AssetArchive({target: pulumi.FileAsset(str(src)) for target, src in code.items()})
    return aws.lambda_.Function(name, name=name, role=role.arn, runtime="python3.13", architectures=["arm64"],
                                handler="handler.handler", code=archive, timeout=timeout, memory_size=256,
                                publish=True, environment={"variables": env} if env else None,
                                logging_config={"log_format": "Text", "log_group": logs.name},
                                tags=tags, opts=pulumi.ResourceOptions(depends_on=[inline, logs]))


def freeze_check(account: str, policy: pulumi.Input[str], tags: dict) -> aws.lambda_.Function:
    return _function(PR.FREEZE_FUNCTION, account=account, policy=policy, tags=tags, timeout=30,
                     code={"handler.py": MODULE11 / "lambdas/freeze_check/handler.py"},
                     env={"FREEZE_PARAMETER": PR.FREEZE_PARAMETER})


CORPUS_FUNCTION = "m11-corpus-handler"
CORPUS_DLQ = "m11-corpus-dlq"
CORPUS_RULE = "m11-corpus-snapshot-published"
DLQ_RETENTION_SECONDS = 14 * 86400


def corpus_dlq(tags: dict) -> aws.sqs.Queue:
    """`m11-corpus-dlq` (14 days): the handler's fatal events, its async on-failure records, and delivery failures."""
    return aws.sqs.Queue("corpus-dlq", name=CORPUS_DLQ, message_retention_seconds=DLQ_RETENTION_SECONDS,
                         sqs_managed_sse_enabled=True, tags=tags)


def corpus_handler(account: str, policy: pulumi.Input[str], *, bus: aws.cloudwatch.EventBus, dlq: aws.sqs.Queue,
                   project_id: str, secret_arn: pulumi.Input[str], release_bucket: pulumi.Input[str],
                   tags: dict) -> aws.lambda_.Function:
    """The corpus-event handler: rule `source=devops-companion.module9`,
    `detail-type=CorpusSnapshotPublished` on `m11-releases`; Lambda async config 2 retries within 1 h, on-failure
    destination the DLQ; the target's retry policy (3 attempts, 1 h) and dead-letter queue for delivery failures;
    the queue policy for EventBridge (aws:SourceArn = the rule); an alarm on visible messages."""
    fn = _function(CORPUS_FUNCTION, account=account, policy=policy, tags=tags, timeout=120,
                   code={"handler.py": MODULE11 / "lambdas/corpus_handler/handler.py",
                         "gitlab_api.py": MODULE11 / "aws/gitlab_api.py",
                         "bot_changes.py": MODULE11 / "aws/bot_changes.py",
                         "pin.py": MODULE11 / "corpus/pin.py"},
                   env={"M11_GITLAB_PROJECT_ID": project_id, "GITLAB_TOKEN_SECRET": secret_arn,
                        "RELEASE_BUCKET": release_bucket, "DLQ_URL": dlq.url})
    aws.lambda_.FunctionEventInvokeConfig("corpus-handler-async", function_name=fn.name, qualifier=fn.version,
                                          maximum_retry_attempts=2, maximum_event_age_in_seconds=3600,
                                          destination_config={"on_failure": {"destination": dlq.arn}})
    rule = aws.cloudwatch.EventRule("corpus-rule", name=CORPUS_RULE, event_bus_name=bus.name,
                                    description="Module 9 policy snapshots become corpus change requests (4.4.13)",
                                    event_pattern=json.dumps({"source": ["devops-companion.module9"],
                                                              "detail-type": ["CorpusSnapshotPublished"]}),
                                    tags=tags)
    permission = aws.lambda_.Permission("corpus-handler-events", action="lambda:InvokeFunction", function=fn.name,
                                        qualifier=fn.version, principal="events.amazonaws.com", source_arn=rule.arn,
                                        source_account=account)
    aws.sqs.QueuePolicy("corpus-dlq-policy", queue_url=dlq.url, policy=pulumi.Output.all(dlq.arn, rule.arn).apply(
        lambda v: json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "RuleDeliveryFailures", "Effect": "Allow", "Principal": {"Service": "events.amazonaws.com"},
            "Action": "sqs:SendMessage", "Resource": v[0], "Condition": {"ArnEquals": {"aws:SourceArn": v[1]}}}]})))
    aws.cloudwatch.EventTarget("corpus-target", rule=rule.name, event_bus_name=bus.name, arn=fn.qualified_arn,
                               retry_policy={"maximum_retry_attempts": 3, "maximum_event_age_in_seconds": 3600},
                               dead_letter_config={"arn": dlq.arn}, opts=pulumi.ResourceOptions(depends_on=[permission]))
    aws.cloudwatch.MetricAlarm("corpus-dlq-alarm", name=CORPUS_DLQ, namespace="AWS/SQS",
                               metric_name="ApproximateNumberOfMessagesVisible", dimensions={"QueueName": CORPUS_DLQ},
                               statistic="Maximum", period=300, evaluation_periods=1, threshold=0,
                               comparison_operator="GreaterThanThreshold", treat_missing_data="notBreaching",
                               alarm_description="m11: a corpus event did not become a change request (4.4.13, M15)",
                               tags=tags)
    return fn


def notes_broker(account: str, policy: pulumi.Input[str], *, project_id: str, secret_arn: pulumi.Input[str],
                 release_bucket: pulumi.Input[str], tags: dict, group: str = "") -> aws.lambda_.Function:
    return _function(PR.NOTES_FUNCTION, account=account, policy=policy, tags=tags, timeout=120,
                     code={"handler.py": MODULE11 / "lambdas/gitlab_notes/handler.py",
                           "gitlab_api.py": MODULE11 / "aws/gitlab_api.py"},
                     env={"M11_GITLAB_PROJECT_ID": project_id, "NOTES_CREDENTIAL_SECRET": secret_arn,
                          "RELEASE_BUCKET": release_bucket, "M11_GITLAB_GROUP": group})
