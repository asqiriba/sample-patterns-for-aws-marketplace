# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_module3_templates.py
===============================
Synthesizes every CDK template that module3/templates/cdk_patterns.py can
generate, so the sample code Module 3 hands to customers stays deployable.

Each generate_*_stack() output is exec'd, every Stack subclass it defines is
instantiated (stacks that take a `vpc` get one from Module 3's own VPC
template, which has the isolated subnets the RDS template selects), and the
app is synthesized into a temp directory. No AWS call is made.

Slow: 15 seconds to about 1 minute, and it needs Node.js for the jsii
runtime behind aws-cdk-lib. Skipped when aws-cdk-lib is not installed.

Run:
    pytest tests/test_module3_templates.py -v
"""
from __future__ import annotations

import inspect
import os

import pytest

cdk = pytest.importorskip("aws_cdk")
os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

from module3.templates import cdk_patterns  # noqa: E402

GENERATORS = {
    name: fn
    for name, fn in inspect.getmembers(cdk_patterns, inspect.isfunction)
    if name.startswith("generate_")
}
ENV = cdk.Environment(account="111122223333", region="us-east-1")


def _exec(code: str, name: str) -> dict:
    ns: dict = {}
    exec(compile(code, name, "exec"), ns)  # nosec B102 - runs the CDK code module3's generators return: what this test checks
    return ns


def _helper_vpc(app, stack_id: str):
    ns = _exec(cdk_patterns.generate_vpc_stack(), "helper_vpc")
    return ns["VpcStack"](app, stack_id, env=ENV).vpc


def _synth(code: str, name: str, outdir: str) -> None:
    ns = _exec(code, name)
    stack_classes = [
        v for v in ns.values()
        if isinstance(v, type) and issubclass(v, cdk.Stack) and v is not cdk.Stack
    ]
    assert stack_classes, f"{name} defines no Stack subclass"
    app = cdk.App(outdir=outdir)
    for cls in stack_classes:
        if "vpc" in inspect.signature(cls.__init__).parameters:
            cls(app, cls.__name__, vpc=_helper_vpc(app, f"{cls.__name__}Vpc"), env=ENV)
        else:
            cls(app, cls.__name__, env=ENV)
    app.synth()


@pytest.fixture
def workdir(tmp_path):
    # The Lambda template stages lambda_.Code.from_asset(code_path). The jsii
    # Node process keeps the cwd it started with, so the test passes an
    # absolute code_path instead of chdir'ing to a relative "lambda" dir.
    (tmp_path / "lambda").mkdir()
    (tmp_path / "lambda" / "index.py").write_text(
        "def handler(event, context):\n    return {}\n"
    )
    return tmp_path


def test_all_six_generators_present():
    assert sorted(GENERATORS) == [
        "generate_ecs_stack",
        "generate_elasticache_stack",
        "generate_lambda_stack",
        "generate_rds_stack",
        "generate_s3_stack",
        "generate_vpc_stack",
    ]


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_generated_template_synths(name, workdir):
    kwargs = {"code_path": str(workdir / "lambda")} if name == "generate_lambda_stack" else {}
    _synth(GENERATORS[name](**kwargs), name, str(workdir / f"cdk.out-{name}"))


def test_rds_defaults_to_postgres_17():
    code = cdk_patterns.generate_rds_stack()
    assert "rds.DatabaseInstanceEngine.postgres(" in code
    assert "PostgresEngineVersion.VER_17" in code


@pytest.mark.parametrize(
    "engine, method, version_class",
    [
        ("POSTGRES", "postgres", "PostgresEngineVersion"),
        ("postgres", "postgres", "PostgresEngineVersion"),
        ("MYSQL", "mysql", "MysqlEngineVersion"),
        ("MariaDB", "maria_db", "MariaDbEngineVersion"),
    ],
)
def test_rds_engine_mapping_is_case_insensitive(engine, method, version_class):
    code = cdk_patterns.generate_rds_stack(engine=engine, version="VER_X")
    assert f"rds.DatabaseInstanceEngine.{method}(" in code
    assert f"rds.{version_class}.VER_X" in code


def test_rds_mysql_synths(workdir):
    code = cdk_patterns.generate_rds_stack(engine="MYSQL", version="VER_8_0")
    _synth(code, "rds_mysql", str(workdir / "cdk.out-mysql"))


def test_rds_unknown_engine_raises():
    with pytest.raises(ValueError, match="Unsupported RDS engine"):
        cdk_patterns.generate_rds_stack(engine="ORACLE")


def test_s3_enforces_ssl():
    assert "enforce_ssl=True" in cdk_patterns.generate_s3_stack()


def test_lambda_defaults_to_python_3_13():
    assert "lambda_.Runtime.PYTHON_3_13" in cdk_patterns.generate_lambda_stack()


def test_elasticache_renders_python_booleans():
    code = cdk_patterns.generate_elasticache_stack()
    assert "automatic_failover_enabled=True" in code
    assert "multi_az_enabled=True" in code
