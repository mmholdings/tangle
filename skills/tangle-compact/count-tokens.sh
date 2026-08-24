#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <file> [file2 ...]" >&2
  exit 64
fi

python3 - "$@" <<'PY'
import pathlib
import re
import sys

failed = False
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    if not path.is_file():
        print(f"Error: {raw!r} not found", file=sys.stderr)
        failed = True
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    letters = sum(char.isalpha() for char in text)
    digits = sum(char.isdigit() for char in text)
    spaces = sum(char in " \t" for char in text)
    newlines = text.count("\n")
    punctuation = len(text) - letters - digits - spaces - newlines
    tokens = round(
        letters / 4.8
        + digits / 2.5
        + punctuation / 2.8
        + spaces / 6.0
        + newlines * 0.75
    )
    words = len(re.findall(r"\S+", text))
    print(
        f"{raw:<40}  {newlines:5d} lines  {words:6d} words  "
        f"{len(text):7d} chars  ~{tokens:6d} tokens"
    )
raise SystemExit(1 if failed else 0)
PY
