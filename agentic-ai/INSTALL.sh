#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Installation script for AI Agent Learning Series
#
# Usage: ./INSTALL.sh [core|module4|module7|module10]     (default: core)
#
# Each family gets its own virtual environment, because the modules pin
# different dependency sets:
#   core      Modules 1-3, 5, 6, 8, 9, routing agent, evaluation, orchestration
#   module4   Module 4 multi-agent orchestration
#   module7   Module 7 agent memory (exact pins)
#   module10  Module 10 (Python 3.13)
#
# Overrides (environment variables):
#   VENV_DIR  where to create the venv (relative paths are relative to agentic-ai/)
#   PYTHON    interpreter used to create the venv

set -euo pipefail

# Always run from agentic-ai/ so the relative requirement paths resolve.
cd "$(dirname "$0")"

FAMILY="${1:-core}"

case "$FAMILY" in
    core)
        DEFAULT_VENV=".venv"
        TESTED_PYTHON="3.12"
        REQUIREMENTS=(-r requirements.txt -r module8/requirements.txt -r module9/requirements.txt)
        ;;
    module4)
        DEFAULT_VENV="module4/.venv"
        TESTED_PYTHON="3.12"
        REQUIREMENTS=(-r module4/requirements.txt)
        ;;
    module7)
        DEFAULT_VENV="module7/.venv"
        TESTED_PYTHON="3.12"
        REQUIREMENTS=(-r module7/requirements.txt)
        ;;
    module10)
        DEFAULT_VENV=".venv"
        TESTED_PYTHON="3.13"
        REQUIREMENTS=(-r module10/requirements-review.txt -r module10/requirements-telemetry.txt 'boto3==1.43.6')
        ;;
    -h|--help)
        sed -n '5,18p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "✗ Unknown family: $FAMILY"
        echo "  Usage: ./INSTALL.sh [core|module4|module7|module10]"
        exit 2
        ;;
esac

VENV_DIR="${VENV_DIR:-$DEFAULT_VENV}"
PYTHON="${PYTHON:-python$TESTED_PYTHON}"

echo "=========================================="
echo "AI Agent Learning Series - Installation"
echo "=========================================="
echo ""
echo "Family:      $FAMILY"
echo "Virtual env: $VENV_DIR"
echo ""

# Check the interpreter
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "✗ $PYTHON not found. Install Python $TESTED_PYTHON or set PYTHON=/path/to/python."
    exit 1
fi
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PY_MINOR="${PY_VERSION%.*}"
echo "✓ Using $PYTHON (Python $PY_VERSION)"
if [ "$PY_MINOR" != "$TESTED_PYTHON" ]; then
    echo "! Warning: the $FAMILY family is tested with Python $TESTED_PYTHON; continuing with $PY_MINOR"
fi
echo ""

# Create the virtual environment only if it is absent
if [ -d "$VENV_DIR" ]; then
    if [ "$FAMILY" = "module10" ] && [ -x "$VENV_DIR/bin/python" ]; then
        VENV_MINOR="$("$VENV_DIR/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        if [ "$VENV_MINOR" != "$PY_MINOR" ]; then
            echo "✗ $VENV_DIR already exists with Python $VENV_MINOR (Module 10 needs $PY_MINOR)."
            echo "  use VENV_DIR=.venv-m10 and replace .venv/bin/python with .venv-m10/bin/python in the Module 10 commands"
            exit 1
        fi
    fi
    echo "✓ Virtual environment already exists"
else
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo "✓ Virtual environment created"
fi
VENV_PYTHON="$VENV_DIR/bin/python"

echo ""
echo "Installing dependencies..."
"$VENV_PYTHON" -m pip install --upgrade pip -q
"$VENV_PYTHON" -m pip install "${REQUIREMENTS[@]}"

echo ""
echo "=========================================="
echo "Installation Complete!"
echo "=========================================="
echo ""
echo "To activate the virtual environment, run (from agentic-ai/):"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "Check the installation:"
if [ "$FAMILY" = "core" ]; then
    echo "  python verify_installation.py"
else
    echo "  python verify_installation.py --family $FAMILY"
fi
echo ""

case "$FAMILY" in
    core)
        echo "Then try running the demos (mock mode):"
        echo "  AGENT_MOCK_AWS=true python demos/module1_demo.py"
        echo "  AGENT_MOCK_REPO=true python demos/module2_demo.py"
        echo "  AGENT_MOCK_REPO=true python demos/module3_demo.py"
        echo "  AGENT_MOCK_DOMAIN=true python demos/module5_demo.py"
        echo "  AGENT_MOCK_MODE=true python demos/module6_demo.py"
        echo "  AGENT_MOCK_MODE=true python demos/module8_demo.py"
        echo "  python demos/module9_demo.py --mock"
        echo ""
        echo "Run the tests:"
        echo "  AGENT_MOCK_AWS=true pytest tests/test_tools.py -v"
        echo "  AGENT_MOCK_REPO=true pytest tests/test_repo_tools.py -v"
        echo "  AGENT_MOCK_REPO=true pytest tests/test_module3_tools.py -v"
        echo "  pytest tests/test_module6_tools.py -v"
        echo "  AGENT_MOCK_MODE=true pytest tests/test_module8_identity.py -v"
        echo "  AGENT_MOCK_PIPELINE=true AGENT_MOCK_MEMORY=true pytest tests/test_module9_*.py -v"
        echo ""
        echo "Module 4, 7 and 10 use their own environments:"
        echo "  ./INSTALL.sh module4 | ./INSTALL.sh module7 | ./INSTALL.sh module10"
        ;;
    module4)
        echo "Then try running the demo (mock mode):"
        echo "  AGENT_MOCK_MODE=true python demos/module4_demo.py"
        echo "  AGENT_MOCK_MODE=true python demos/module4_demo.py --section 3"
        ;;
    module7)
        echo "Then try running the demo (mock mode):"
        echo "  AGENT_MOCK_MEMORY=true python demos/module7_demo.py"
        echo ""
        echo "Run the tests:"
        echo "  AGENT_MOCK_MEMORY=true pytest tests/test_module7_*.py -v"
        ;;
    module10)
        echo "One-time setup, then run the demo (see module10/README.md):"
        echo "  $VENV_PYTHON module10/gateway/setup.py"
        echo "  $VENV_PYTHON demos/module10_demo.py --mode local"
        ;;
esac
echo ""
echo "See README.md for the full quick start."
echo ""
