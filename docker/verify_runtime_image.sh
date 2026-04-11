#!/usr/bin/env sh
set -eu

cd /app

unexpected_paths="
tests
docs
README.md
AGENTS.md
LICENSE
.gitignore
.env.example
"

failed=0
for path in $unexpected_paths; do
  if [ -e "$path" ]; then
    echo "[FAIL] unexpected runtime file exists: /app/$path"
    failed=1
  else
    echo "[OK] not present: /app/$path"
  fi
done

exit "$failed"
