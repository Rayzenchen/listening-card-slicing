#!/bin/bash
# Install the two skills this workspace needs into ~/.agents/skills.
# Re-runnable. Existing skill directories are backed up, never silently replaced.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DIR="${DSH_SKILLS_DIR:-$HOME/.agents/skills}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SKILLS_DIR" "$SKILLS_DIR/.backup"

for name in listening-card-slicing markji-channel; do
  src="$ROOT/skills/$name"
  dst="$SKILLS_DIR/$name"
  if [ ! -d "$src" ]; then
    echo "ERROR: missing $src" >&2; exit 1
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    backup="$SKILLS_DIR/.backup/$name-$STAMP"
    echo "-- backing up existing $name -> $backup"
    mv "$dst" "$backup"
  fi
  mkdir -p "$dst"
  # .env is intentionally preserved/created below, never overwritten here.
  rsync -a --exclude '.env' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' "$src/" "$dst/"
  if [ "$name" = "markji-channel" ]; then
    if [ ! -f "$dst/.env" ]; then
      cp "$src/.env.example" "$dst/.env"
      chmod 600 "$dst/.env"
      echo "   -> created $dst/.env (fill MAIMEMO_TOKEN=... before E10)"
    else
      echo "   -> kept existing $dst/.env"
    fi
  fi
  echo "-- installed $name -> $dst"
done
echo "== skills installed under $SKILLS_DIR"
