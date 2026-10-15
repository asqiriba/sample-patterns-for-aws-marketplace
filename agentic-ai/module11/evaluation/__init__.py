# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The evaluation runner, calibration and evaluator-set procedures.

`runner.run_suite` runs a scenario suite against an endpoint as the eval-runner role and scores every
session; `calibrate.run` repeats it to write a baseline and noise floor; `evalset.rebaseline` is the
evaluator-set change procedure; `records` holds the run record and its S3 layout; `cost` projects and
measures spend. The pass/fail rules themselves are the pure engine in `module11/release/gate.py`.
"""
