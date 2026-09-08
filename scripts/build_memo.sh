#!/usr/bin/env bash
# Rebuild the Part 1 scoping note PDF from its markdown source.
#
# The PDF is the deliverable, but the markdown is the source of truth — so the
# PDF stays reproducible rather than being a binary someone edited by hand.
# Requires pandoc and Google Chrome (headless, for printing).
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=docs/scoping-note-p2-rag.md
OUT=docs/scoping-note-p2-rag.pdf
CSS=scripts/memo.css
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

printf '<title>Scoping Note — Grounded Document Q&amp;A</title>\n' > /tmp/memo-head.html
pandoc "$SRC" -o /tmp/memo.html --standalone --css="$PWD/$CSS" -H /tmp/memo-head.html
"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=/tmp/memo.pdf --virtual-time-budget=3000 file:///tmp/memo.html 2>/dev/null
cp /tmp/memo.pdf "$OUT"

pages=$(python3 -c "import re,sys;print(len(re.findall(rb'/Type\s*/Page[^s]', open('$OUT','rb').read())))")
echo "wrote $OUT ($pages page(s))"
# The brief caps the memo at one page. Fail loudly rather than shipping two.
[ "$pages" -eq 1 ] || { echo "ERROR: the brief allows one page only"; exit 1; }
