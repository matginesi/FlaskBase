#!/usr/bin/env bash
set -e

export LC_NUMERIC=C

ZIP_NAME="codebase.zip"
TMP_LIST=$(mktemp)

echo "Building file list..."

find . -type f \
  ! -path "*/.venv/*" \
  ! -path "./.venv/*" \
  ! -path "*/__pycache__/*" \
  ! -path "./instance/*" \
  ! -path "*/instance/*" \
  ! -path "*/rag_data/*" \
  ! -path "*/.chroma/*" \
  ! -path "*/uploads/*" \
  ! -name ".env" \
  ! -name "*.db" \
  ! -name "*.sqlite" \
  ! -name "*.sqlite3" \
  ! -name "*.sql" \
  ! -name "*.log" \
  ! -name "*.zip" \
  > "$TMP_LIST"

TOTAL_FILES=$(wc -l < "$TMP_LIST")
echo "Files to compress: $TOTAL_FILES"

rm -f "$ZIP_NAME"

echo "Compressing..."

pv -l -s "$TOTAL_FILES" "$TMP_LIST" | zip -q -@ "$ZIP_NAME"

rm "$TMP_LIST"

SIZE_BYTES=$(stat -c%s "$ZIP_NAME")

# 5 cifre: le divide per 1024, lo 0 non lo stampa
human_size() {
  awk -v b="$SIZE_BYTES" '
  function human(x) {
    s="B KB MB GB TB"
    split(s,unit)
    for(i=1; x>=1024 && i<5; i++) x/=1024
    return sprintf("%.2f %s", x, unit[i])
  }
  BEGIN { print human(b) }'
}

echo "Archive created: $ZIP_NAME"
echo "Final size: $(human_size)"
