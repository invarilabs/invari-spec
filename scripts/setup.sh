#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
TLA_JAR_URL=${TLA_JAR_URL:-https://github.com/tlaplus/tlaplus/releases/latest/download/tla2tools.jar}
TLA_JAR_PATH=${TLA_JAR_PATH:-"$ROOT/third_party/tla2tools.jar"}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "error: python interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi

if ! command -v java >/dev/null 2>&1; then
    echo "error: Java is required but was not found on PATH" >&2
    exit 1
fi

if command -v curl >/dev/null 2>&1; then
    DOWNLOAD_TOOL="curl"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD_TOOL="wget"
else
    echo "error: curl or wget is required to download tla2tools.jar" >&2
    exit 1
fi

mkdir -p "$(dirname "$TLA_JAR_PATH")"

echo "Installing Speclens into the current Python environment..."
"$PYTHON_BIN" -m pip install -e "$ROOT"

echo "Downloading tla2tools.jar to $TLA_JAR_PATH..."
if [ "$DOWNLOAD_TOOL" = "curl" ]; then
    curl -fL "$TLA_JAR_URL" -o "$TLA_JAR_PATH"
else
    wget -O "$TLA_JAR_PATH" "$TLA_JAR_URL"
fi

echo "Setup complete. Default TLC jar: $TLA_JAR_PATH"
echo "You can now run: speclens check --file <path>"
