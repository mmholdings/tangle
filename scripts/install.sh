#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 [--force] [project-directory]" >&2
}

force=0
if [ "${1:-}" = "--force" ]; then
  force=1
  shift
fi
if [ "$#" -gt 1 ]; then
  usage
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_root="$(cd "$script_dir/.." && pwd)"
target="${1:-$PWD}"
if [ ! -d "$target" ]; then
  echo "error: project directory does not exist: $target" >&2
  exit 2
fi
target="$(cd "$target" && pwd)"

skills_target="$target/.claude/skills"
runtime_target="$target/.claude/tangle"
backup_target=""

if [ "$force" -eq 0 ]; then
  for skill in "$source_root"/skills/*; do
    name="$(basename "$skill")"
    if [ -e "$skills_target/$name" ]; then
      echo "error: $skills_target/$name already exists; rerun with --force to update Tangle" >&2
      exit 2
    fi
  done
fi

mkdir -p "$skills_target" "$runtime_target"
if [ "$force" -eq 1 ]; then
  backup_target="$runtime_target/backups/$(date -u '+%Y%m%dT%H%M%SZ')-$$"
  for skill in "$source_root"/skills/*; do
    name="$(basename "$skill")"
    if [ -e "$skills_target/$name" ]; then
      mkdir -p "$backup_target/skills/$name"
      cp -R "$skills_target/$name/." "$backup_target/skills/$name/"
    fi
  done
  if [ -e "$runtime_target/tangle_orchestrator.py" ]; then
    mkdir -p "$backup_target/runtime"
    cp "$runtime_target/tangle_orchestrator.py" "$backup_target/runtime/"
  fi
fi

for skill in "$source_root"/skills/*; do
  name="$(basename "$skill")"
  mkdir -p "$skills_target/$name"
  cp -R "$skill/." "$skills_target/$name/"
done
install -m 0755 "$source_root/scripts/tangle_orchestrator.py" \
  "$runtime_target/tangle_orchestrator.py"

if [ ! -e "$target/tangle.json" ]; then
  cp "$source_root/tangle.example.json" "$target/tangle.json"
fi

echo "Tangle installed in $target"
if [ -n "$backup_target" ]; then
  echo "Existing Tangle files backed up to $backup_target"
fi
echo "Next: python3 .claude/tangle/tangle_orchestrator.py doctor --config tangle.json"
