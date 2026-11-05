#!/bin/sh
# Download a pinned copy of mermaid.js for the docs portal (diagrams in Markdown ```mermaid blocks).
# The portal serves it itself (/docs/_assets/mermaid.js), so readers' browsers never contact a third-party CDN.
# Then rebuild the glue image (tenantctl digest/enable-* rebuilds it, or: docker compose build glue).
set -eu
VERSION="${MERMAID_VERSION:-11.4.0}"
DEST="$(dirname "$0")/../glue/app/static/mermaid.min.js"
mkdir -p "$(dirname "$DEST")"
curl -fsSL "https://cdn.jsdelivr.net/npm/mermaid@${VERSION}/dist/mermaid.min.js" -o "$DEST"
echo "mermaid ${VERSION} saved to ${DEST} ($(wc -c < "$DEST") bytes, sha256 $(shasum -a 256 "$DEST" | cut -d' ' -f1))"
