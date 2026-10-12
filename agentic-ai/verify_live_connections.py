#!/usr/bin/env python3
"""
verify_live_connections.py
===========================
Verify all four live connections before running the demo.

Usage:
    python verify_live_connections.py
"""
import json
import os
import sys

# Load .env
def _load_dotenv(env_path=None) -> None:
    """Load agentic-ai/.env if present; values already in the shell win."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
        return
    # Fallback parser: handles `export KEY=value` and quoted values.
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v


_load_dotenv()

all_ok = True
_INSECURE_TLS = os.getenv("MODULE7_INSECURE_TLS", "").lower() == "true"

# Secure default: point OpenSSL at certifi's CA bundle so the Atlas/Aura
# certificate chains verify on Python builds with an empty system trust
# store (common on macOS). Matches the behavior of MongoStore/Neo4jStore.
_CA_FILE = None
if not _INSECURE_TLS:
    try:
        from module7.config.tls import ensure_secure_ca
        _CA_FILE = ensure_secure_ca()
    except ImportError:
        pass

# 1. Bedrock Titan Embeddings
print("1. Bedrock Titan Embeddings v2 ... ", end="", flush=True)
try:
    import boto3
    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    resp = client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": "connection test"}),
        contentType="application/json", accept="application/json",
    )
    vec = json.loads(resp["body"].read())["embedding"]
    print(f"OK ({len(vec)} dims)")
except Exception as e:
    print(f"FAILED\n   {e}")
    all_ok = False

# 2. Redis Cloud
print("2. Redis Cloud (session memory) ... ", end="", flush=True)
try:
    import redis as redis_lib
    r = redis_lib.Redis(
        host=os.getenv("REDIS_HOST"), port=int(os.getenv("REDIS_PORT", "6379")),
        username=os.getenv("REDIS_USERNAME", "default"),
        password=os.getenv("REDIS_PASSWORD", ""),
        decode_responses=True, socket_connect_timeout=8,
    )
    r.set("_verify", "ok", ex=10)
    assert r.get("_verify") == "ok"
    r.delete("_verify")
    print("OK")
except Exception as e:
    print(f"FAILED\n   {e}")
    all_ok = False

# 3. MongoDB Atlas Vector Search
print("3. MongoDB Atlas Vector Search ... ", end="", flush=True)
try:
    from pymongo import MongoClient
    from pymongo.server_api import ServerApi
    _mongo_kwargs = {"server_api": ServerApi("1"), "serverSelectionTimeoutMS": 8000}
    if _INSECURE_TLS:
        _mongo_kwargs["tlsInsecure"] = True
    elif _CA_FILE:
        _mongo_kwargs["tlsCAFile"] = _CA_FILE
    client = MongoClient(os.getenv("MONGODB_URI"), **_mongo_kwargs)
    client.admin.command("ping")
    db = client[os.getenv("MONGODB_DATABASE", "agent_memory")]
    col = db[os.getenv("MONGODB_COLLECTION", "memories")]
    indexes = list(col.list_search_indexes())
    idx = next((i for i in indexes if i["name"] == os.getenv("MONGODB_VECTOR_INDEX", "vector_index")), None)
    if not idx:
        print(f"FAILED — vector_index not found. Existing: {[i['name'] for i in indexes]}")
        all_ok = False
    elif idx["status"] != "READY":
        print(f"NOT READY — status={idx['status']} (wait ~2 min and retry)")
        all_ok = False
    else:
        print(f"OK — vector_index READY ({col.count_documents({})} docs)")
    client.close()
except Exception as e:
    print(f"FAILED\n   {e}")
    all_ok = False

# 4. Neo4j AuraDB
print("4. Neo4j AuraDB (graph memory) ... ", end="", flush=True)
try:
    from neo4j import GraphDatabase
    _uri = os.getenv("NEO4J_URI", "")
    if _uri.startswith("neo4j+s://") and _INSECURE_TLS:
        _uri = _uri.replace("neo4j+s://", "neo4j+ssc://", 1)
    driver = GraphDatabase.driver(
        _uri, auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
    )
    with driver.session() as s:
        assert s.run("RETURN 1 AS n").single()["n"] == 1
    driver.close()
    print("OK")
except Exception as e:
    print(f"FAILED\n   {e}")
    all_ok = False

print()
if all_ok:
    print("✅ All connections OK. Run the demo:")
    print("   python demos/module7_demo.py --section 3")
    print("   python demos/module7_demo.py")
else:
    print("❌ Fix the issues above, then re-run this script.")
    sys.exit(1)
