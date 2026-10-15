# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_module9_ingestion.py
================================
Module 9 ingestion pipeline tests: consume -> land -> chunk -> embed ->
load in mock mode, plus idempotent re-run behavior.

All tests run with AGENT_MOCK_PIPELINE=true, AGENT_MOCK_MEMORY=true, and
AGENT_MOCK_MODE=true: no Confluent, Databricks, Atlas, Neo4j, Auth0, or
AWS credentials are required.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["AGENT_MOCK_PIPELINE"] = "true"
os.environ["AGENT_MOCK_MEMORY"] = "true"
os.environ["AGENT_MOCK_MODE"] = "true"


def _reset_module9_state() -> None:
    from module9.ingestion import embed, kb_sink, lineage, load, quality
    from module9.mock import confluent_mock, databricks_mock
    from module9.tools import pipeline_tools

    confluent_mock.reset()
    databricks_mock.reset()
    lineage.reset_lineage()
    quality.reset_dedup_index()
    load.get_mongo_store.cache_clear()
    embed._get_embedding_service.cache_clear()
    pipeline_tools._get_stores.cache_clear()
    kb_sink.reset_sink()


@pytest.fixture(autouse=True)
def mock_pipeline_env(monkeypatch):
    monkeypatch.setenv("AGENT_MOCK_PIPELINE", "true")
    monkeypatch.setenv("AGENT_MOCK_MEMORY", "true")
    monkeypatch.setenv("AGENT_MOCK_MODE", "true")
    # Another module's tests may load agentic-ai/.env, which would point the
    # knowledge sink at a real Bedrock knowledge base. Keep these tests
    # hermetic regardless of suite ordering.
    monkeypatch.delenv("BEDROCK_KB_ID", raising=False)
    monkeypatch.delenv("BEDROCK_KB_DATA_SOURCE_ID", raising=False)
    _reset_module9_state()
    yield
    _reset_module9_state()


class TestProduceAndConsume:
    def test_produce_returns_delivery_report_with_offset(self):
        from module9.producers.devops_event_producer import produce_event

        report = produce_event()
        assert report["topic"] == "devops-events"
        assert report["partition"] == 0
        assert report["offset"] == 0
        assert report["event"]["event_type"] == "deployment"

    def test_offsets_are_monotonic(self):
        from module9.mock.sample_events import deployment_event, incident_event
        from module9.producers.devops_event_producer import produce_event

        first = produce_event(deployment_event())
        second = produce_event(incident_event())
        assert (first["offset"], second["offset"]) == (0, 1)

    def test_consumer_reads_events_with_provenance_anchor(self):
        from module9.ingestion.stream_consumer import create_stream_source
        from module9.producers.devops_event_producer import produce_event

        produce_event()
        source = create_stream_source()
        events = source.poll_events(max_records=5)
        assert len(events) == 1
        event = events[0]
        assert event.topic == "devops-events"
        assert event.offset == 0
        assert event.source_uri == "confluent://devops-events/partition-0/offset-0"
        assert event.event["service"] == "checkout-api"

    def test_consumer_groups_track_independent_offsets(self):
        from module9.config.settings import ConfluentSettings
        from module9.ingestion.stream_consumer import KafkaStreamSource
        from module9.producers.devops_event_producer import produce_event

        produce_event()
        ingest = KafkaStreamSource(ConfluentSettings())
        viewer_settings = ConfluentSettings()
        viewer_settings.group_id = "viewer-group"
        viewer = KafkaStreamSource(viewer_settings)

        assert len(ingest.poll_events(max_records=5)) == 1
        # The viewer group replays the same event independently.
        assert len(viewer.poll_events(max_records=5)) == 1
        # The ingestion group is already past it.
        assert ingest.poll_events(max_records=5) == []


class TestDeltaLanding:
    def test_land_event_increments_versions_and_captures_lineage(self):
        from module9.ingestion.delta_writer import create_lakehouse, land_event
        from module9.ingestion.stream_consumer import create_stream_source
        from module9.producers.devops_event_producer import produce_event

        produce_event()
        consumed = create_stream_source().poll_events(max_records=1)[0]
        result = land_event(consumed)

        assert result.bronze_table == "devops.bronze.raw_events"
        assert result.silver_table == "devops.silver.deployment_events"
        assert result.bronze_version == 1
        assert result.silver_version == 1
        assert result.unity_catalog_lineage_id.startswith("lin-")

        lakehouse = create_lakehouse()
        lineage = lakehouse.table_lineage("devops.silver.deployment_events")
        assert len(lineage) == 1
        assert lineage[0]["source_table_full_name"] == "devops.bronze.raw_events"

    def test_normalize_event_produces_flat_silver_row(self):
        from module9.ingestion.delta_writer import normalize_event
        from module9.mock.sample_events import deployment_event, incident_event

        row = normalize_event(deployment_event())
        assert row["event_type"] == "deployment"
        assert row["service"] == "checkout-api"
        assert row["status"] == "succeeded"
        assert "duration_minutes" in row["attributes"]

        incident_row = normalize_event(incident_event())
        assert incident_row["service"] == "checkout-api,payments-gateway"
        assert incident_row["status"] == "high"


class _FakeCursor:
    def __init__(self, log):
        self.log, self.description = log, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.log.append((statement, params))
        self.description = ("version",) if statement.startswith(("DESCRIBE", "SELECT")) else None

    def fetchall(self):
        return [(3,)]


class _FakeConnection:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _FakeCursor(self.log)


def _live_lakehouse(settings):
    """A DatabricksLakehouse over a fake connection (no connector, no network)."""
    from module9.ingestion.delta_writer import DatabricksLakehouse
    lakehouse = DatabricksLakehouse.__new__(DatabricksLakehouse)
    lakehouse._settings, lakehouse._tables_ready, lakehouse._connection = settings, False, _FakeConnection()
    return lakehouse


class TestDatabricksIdentifiers:
    def test_table_names_must_be_plain_three_part_identifiers(self):
        from module9.config.settings import databricks_table
        assert databricks_table("devops.bronze.raw_events") == "devops.bronze.raw_events"
        for bad in ("devops.bronze", "a.b.c.d", "devops.bronze.raw events", "devops.bronze.x; DROP TABLE y",
                    "devops.bronze.`x`", "devops.1bronze.x", "devops..x", "devops.bronze.x--", "devops.bronze.x\n"):
            with pytest.raises(ValueError, match="catalog.schema.table"):
                databricks_table(bad)

    def test_live_settings_reject_names_that_are_not_identifiers(self):
        from module9.config.settings import DatabricksSettings
        live = dict(host="https://dbc.example.cloud.databricks.com", token="t", http_path="/sql/1.0/warehouses/x")  # nosec B106 - dummy token for a settings check; nothing connects
        DatabricksSettings(**live).validate_for_live()
        with pytest.raises(ValueError, match="DATABRICKS_BRONZE_TABLE"):
            DatabricksSettings(**live, bronze_table="raw_events; DROP TABLE x").validate_for_live()
        with pytest.raises(ValueError, match="DATABRICKS_CATALOG"):
            DatabricksSettings(**live, catalog="my-catalog").validate_for_live()

    def test_live_writer_binds_values_and_checks_every_table_name(self):
        from module9.config.settings import DatabricksSettings
        lakehouse = _live_lakehouse(DatabricksSettings())
        payload = {"event_type": "deployment", "note": "x'); DROP TABLE devops.bronze.raw_events; --"}
        assert lakehouse.write_bronze(payload, "abc123") == 3
        assert lakehouse.write_silver("abc123") == 3
        log = lakehouse._connection.log
        inserts = [(s, p) for s, p in log if s.startswith("INSERT")]
        assert [p for _, p in inserts] == [{"ingest_id": "abc123", "payload": __import__("json").dumps(payload)},
                                           {"ingest_id": "abc123"}]
        assert all("DROP" not in s for s, _ in log)          # the payload travels as a bound parameter only
        assert inserts[0][0].startswith("INSERT INTO devops.bronze.raw_events ")
        assert "FROM devops.bronze.raw_events WHERE ingest_id = :ingest_id" in inserts[1][0]
        with pytest.raises(ValueError, match="catalog.schema.table"):
            lakehouse.read_table("devops.bronze.raw_events; DROP TABLE x")
        with pytest.raises(ValueError, match="catalog.schema.table"):
            lakehouse.table_version("devops.bronze.raw_events LIMIT 1; --")
        with pytest.raises(ValueError, match="last_n"):
            lakehouse.read_table("devops.bronze.raw_events", last_n=-1)
        lakehouse.read_table("devops.silver.deployment_events", last_n="2")
        assert log[-1] == ("SELECT * FROM devops.silver.deployment_events LIMIT 2", None)


class TestChunking:
    def test_history_corpus_is_one_fixed_chunk_per_record(self):
        from module9.ingestion.chunk import chunk_record
        from module9.mock.sample_events import deployment_event

        chunks = chunk_record("", "history", event=deployment_event())
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunking_strategy == "fixed"
        assert chunk.chunk_index == 0 and chunk.chunk_count == 1
        assert "checkout-api" in chunk.content
        assert chunk.extra_metadata["structured_record"]["event_type"] == "deployment"

    def test_deployment_rendering_reads_as_prose(self):
        from module9.ingestion.chunk import render_history_record
        from module9.mock.sample_events import deployment_event

        text = render_history_record(deployment_event())
        assert "service checkout-api" in text
        assert "deployed to production in us-east-1" in text
        assert "succeeded after 12 minutes" in text
        assert "CloudWatch alarm" in text

    def test_incident_rendering_includes_root_cause_and_resolution(self):
        from module9.ingestion.chunk import render_history_record
        from module9.mock.sample_events import incident_event

        text = render_history_record(incident_event())
        assert "INC-2043" in text
        assert "connection pool exhaustion" in text
        assert "circuit breaker" in text

    def test_service_corpus_is_hierarchical_with_parent_ids(self):
        from module9.ingestion.chunk import chunk_record
        from module9.mock.seed_corpora_data import SEED_DOCS

        service_doc = next(d for d in SEED_DOCS if d["corpus"] == "service")
        chunks = chunk_record(service_doc["content"], "service")
        strategies = [c.chunking_strategy for c in chunks]
        assert strategies[0] == "hierarchical-parent"
        assert all(s == "hierarchical-child" for s in strategies[1:])
        assert all(c.parent_chunk_id for c in chunks[1:])

    def test_unknown_corpus_raises(self):
        from module9.ingestion.chunk import chunk_record

        with pytest.raises(ValueError):
            chunk_record("text", "nonexistent")


class TestFullPipelineRun:
    def test_run_stores_chunk_and_reports_counts(self):
        from module9.ingestion.pipeline_run import run_pipeline
        from module9.producers.devops_event_producer import produce_event

        produce_event()
        result = run_pipeline(corpus="history", max_events=5)

        assert result.consumed == 1
        assert result.landed == 1
        assert result.chunks_stored == 1
        assert result.chunks_rejected == 0
        assert result.chunks_skipped == 0
        assert len(result.doc_ids) == 1
        assert result.doc_ids[0].startswith("kb-his-")
        assert result.run_id.startswith("run-")

    def test_stored_chunk_is_retrievable_with_corpus_filter(self):
        from module9.ingestion.pipeline_run import run_pipeline
        from module9.producers.devops_event_producer import produce_event
        from module9.tools.pipeline_tools import governed_recall

        produce_event()
        run_pipeline(corpus="history", max_events=5)

        recall = governed_recall(
            "latest checkout-api deployment", corpus="history", role="DeployObserve"
        )
        assert recall["access"] == "granted"
        assert recall["filter"] == {"corpus": "history", "agent_scope": "operations"}
        assert len(recall["results"]) == 1
        assert "checkout-api" in recall["results"][0]["content"]

    def test_rerun_is_idempotent_via_dedup(self):
        from module9.ingestion.pipeline_run import run_pipeline
        from module9.producers.devops_event_producer import produce_event
        from module9.tools.pipeline_tools import governed_recall

        produce_event()
        first = run_pipeline(corpus="history", max_events=5)
        produce_event()  # identical event content, new offset
        second = run_pipeline(corpus="history", max_events=5)

        assert first.chunks_stored == 1
        assert second.chunks_stored == 0
        assert second.chunks_skipped == 1

        recall = governed_recall(
            "checkout-api deployment", corpus="history", role="DeployObserve"
        )
        assert len(recall["results"]) == 1  # still exactly one document

    def test_run_emits_module8_audit_records(self):
        from module9.ingestion.pipeline_run import run_pipeline
        from module9.producers.devops_event_producer import produce_event

        produce_event()
        result = run_pipeline(corpus="history", max_events=5)

        assert len(result.audit_records) == 2
        auth = result.audit_records[0]
        assert auth["trace_point"] == "pipeline-authorization"
        assert auth["decision"] == "allow"
        assert auth["actor"] == "a2a://devops-companion/pipeline"
        assert auth["required_scope"] == "devops:pipeline:ingest"
        assert auth["token_fingerprint"].startswith("sha256:")

    def test_seed_corpora_load_is_idempotent(self):
        from module9.ingestion.load import load_seed_corpora

        first = load_seed_corpora()
        assert all(r.action == "stored" for r in first)
        second = load_seed_corpora()
        assert all(r.action == "skipped_duplicate" for r in second)
