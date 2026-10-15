#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# promote.sh <default|required>: release the CodePipeline approval(s) of THIS tag's execution
# Runs in the tag jobs promote:production / promote:production-gated on the
# digest-pinned public AWS CLI image, so it uses only `aws` with --query/--output text.
#
# Safety: it selects the execution whose sourceRevisions[0].revisionId equals $CI_COMMIT_SHA,
# releases an approval only when the approval stage's latestExecution is that execution, and
# never reads another execution's token. It exits at once if the execution's Staging stage
# failed, and it never follows Bake: it exits 0 once the Production deploy action succeeded.
#
# Required env: CI_COMMIT_SHA, CI_COMMIT_TAG, CI_JOB_URL, AWS_TOKEN (GitLab id_token),
# M11_PROMOTER_ROLE_ARN. Optional: M11_PIPELINE (m11-release) and the stage/action names below.
set -eu

LEVEL="${1:-default}"
case "$LEVEL" in default|required) ;; *) echo "usage: promote.sh <default|required>" >&2; exit 2 ;; esac
case "$CI_COMMIT_SHA" in *[!0-9a-f]*|"") echo "invalid CI_COMMIT_SHA" >&2; exit 2 ;; esac
[ "${#CI_COMMIT_SHA}" -eq 40 ] || { echo "invalid CI_COMMIT_SHA" >&2; exit 2; }

PIPELINE="${M11_PIPELINE:-m11-release}"
STAGING_STAGE="${M11_STAGING_STAGE:-Staging}"
APPROVAL_STAGE="${M11_APPROVAL_STAGE:-Approval}"
APPROVAL_ACTION="${M11_APPROVAL_ACTION:-ReleaseApproval}"
EXT_STAGE="${M11_EXT_STAGE:-ExtendedApproval}"
EXT_ACTION="${M11_EXT_ACTION:-ExtendedReleaseApproval}"
PROD_STAGE="${M11_PROD_STAGE:-Production}"
DEPLOY_ACTION="${M11_DEPLOY_ACTION:-Deploy}"
WAIT_EXECUTION_S="${M11_WAIT_EXECUTION_S:-2700}"   # 45 min for the execution to reach Approval
WAIT_DEPLOY_S="${M11_WAIT_DEPLOY_S:-600}"          # 10 min for the Production deploy action
POLL_S=10

# Web-identity credentials through the CLI's standard chain (the token never reaches stdout).
TOKEN_FILE="$(mktemp)"
trap 'rm -f "$TOKEN_FILE"' EXIT
printf '%s' "$AWS_TOKEN" > "$TOKEN_FILE"
export AWS_WEB_IDENTITY_TOKEN_FILE="$TOKEN_FILE"
export AWS_ROLE_ARN="$M11_PROMOTER_ROLE_ARN"
export AWS_ROLE_SESSION_NAME="m11-promote-${CI_JOB_ID:-job}"

now() { date +%s; }

execution_id() {
  aws codepipeline list-pipeline-executions --pipeline-name "$PIPELINE" --max-items 50 \
    --query "pipelineExecutionSummaries[?sourceRevisions[0].revisionId=='$CI_COMMIT_SHA'] | [0].pipelineExecutionId" \
    --output text
}

action_status() {  # <execution> <stage> <action>
  aws codepipeline list-action-executions --pipeline-name "$PIPELINE" --filter "pipelineExecutionId=$1" \
    --query "actionExecutionDetails[?stageName=='$2' && actionName=='$3'] | [0].status" --output text
}

staging_failed() {
  n="$(aws codepipeline list-action-executions --pipeline-name "$PIPELINE" --filter "pipelineExecutionId=$1" \
    --query "length(actionExecutionDetails[?stageName=='$STAGING_STAGE' && status=='Failed'])" --output text)"
  [ "$n" != "0" ]
}

release_approval() {  # <execution> <stage> <action>
  deadline=$(( $(now) + WAIT_EXECUTION_S ))
  while :; do
    if staging_failed "$1"; then
      echo "Staging failed for this execution; nothing to approve." >&2
      exit 1
    fi
    owner="$(aws codepipeline get-pipeline-state --name "$PIPELINE" \
      --query "stageStates[?stageName=='$2'] | [0].latestExecution.pipelineExecutionId" --output text)"
    if [ "$owner" = "$1" ]; then
      token="$(aws codepipeline get-pipeline-state --name "$PIPELINE" \
        --query "stageStates[?stageName=='$2'] | [0].actionStates[?actionName=='$3'] | [0].latestExecution.token" \
        --output text)"
      if [ -n "$token" ] && [ "$token" != "None" ]; then
        aws codepipeline put-approval-result --pipeline-name "$PIPELINE" --stage-name "$2" --action-name "$3" \
          --result "summary=Approved in GitLab by ${GITLAB_USER_LOGIN:-unknown} for $CI_COMMIT_TAG ($CI_JOB_URL),status=Approved" \
          --token "$token" --output text --query approvedAt >/dev/null
        echo "Released $2/$3 for execution $1."
        return 0
      fi
    fi
    [ "$(now)" -lt "$deadline" ] || { echo "Timed out waiting for $2/$3 of execution $1." >&2; exit 1; }
    sleep "$POLL_S"
  done
}

deadline=$(( $(now) + WAIT_EXECUTION_S ))
EXECUTION="None"
while [ "$EXECUTION" = "None" ] || [ -z "$EXECUTION" ]; do
  EXECUTION="$(execution_id)"
  if [ "$EXECUTION" = "None" ] || [ -z "$EXECUTION" ]; then
    [ "$(now)" -lt "$deadline" ] || { echo "No $PIPELINE execution for $CI_COMMIT_SHA." >&2; exit 1; }
    sleep "$POLL_S"
  fi
done
echo "Execution $EXECUTION for $CI_COMMIT_TAG (approval: $LEVEL)."

release_approval "$EXECUTION" "$APPROVAL_STAGE" "$APPROVAL_ACTION"
if [ "$LEVEL" = "required" ]; then
  release_approval "$EXECUTION" "$EXT_STAGE" "$EXT_ACTION"
fi

deadline=$(( $(now) + WAIT_DEPLOY_S ))
while :; do
  status="$(action_status "$EXECUTION" "$PROD_STAGE" "$DEPLOY_ACTION")"
  case "$status" in
    Succeeded) echo "Production deploy succeeded; the rollout is open. Bake follows in CodePipeline."; exit 0 ;;
    Failed|Abandoned) echo "Production deploy $status." >&2; exit 1 ;;
  esac
  [ "$(now)" -lt "$deadline" ] || { echo "Timed out waiting for the Production deploy." >&2; exit 1; }
  sleep "$POLL_S"
done
