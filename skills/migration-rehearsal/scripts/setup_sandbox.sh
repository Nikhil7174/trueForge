#!/usr/bin/env bash
# Installs what rehearse.py needs: psycopg + pgserver (a bundled Postgres 16). No sudo.
# Idempotent; prints READY when done. Takes ~20-40 s on a fresh sandbox.
#
# pgserver ships wheels for CPython 3.9-3.12 only. When the sandbox's python3 is newer
# (Daytona images run 3.13), this builds a Python 3.12 venv with uv at $REHEARSAL_VENV and
# rehearse.py re-executes itself there, so the commands in SKILL.md don't change.
set -uo pipefail
PY="${PYTHON:-python3}"
VENV="${REHEARSAL_VENV:-/tmp/rehearsal-venv}"
PKGS=("psycopg[binary]>=3.1" "pgserver>=0.1.4")

ready() {
  "$1" - <<'PYEOF'
import psycopg, pgserver, pathlib, sys
bin_dir = pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin"
print(f"READY python {sys.version.split()[0]}; psycopg {psycopg.__version__}; bundled postgres at {bin_dir}")
PYEOF
}

for cand in "$PY" "$VENV/bin/python"; do
  if [ -x "$(command -v "$cand" 2>/dev/null)" ] && "$cand" -c 'import psycopg, pgserver' 2>/dev/null; then
    ready "$cand"; exit 0
  fi
done
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || { echo "need Python 3.9+"; exit 1; }

pip_install() { "$PY" -m pip install --quiet --disable-pip-version-check --no-input "$@"; }
pip_install_any() {
  pip_install "$@" 2>/dev/null \
    || pip_install --user "$@" 2>/dev/null \
    || pip_install --user --break-system-packages "$@"
}

if "$PY" -c 'import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)' && pip_install_any "${PKGS[@]}"; then
  ready "$PY"; exit 0
fi

echo "pgserver has no wheel for $("$PY" -V 2>&1); building a Python 3.12 venv at $VENV with uv"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  pip_install_any uv >/dev/null || { echo "pip install uv failed"; exit 1; }
  UV="$("$PY" -c 'import uv, sys; sys.stdout.write(uv.find_uv_bin())')"
fi
"$UV" venv --quiet --python 3.12 "$VENV" || { echo "uv could not create a Python 3.12 venv"; exit 1; }
"$UV" pip install --quiet --python "$VENV/bin/python" "${PKGS[@]}" || { echo "installing into $VENV failed"; exit 1; }
chmod -R a+rX "$VENV"  # pgserver runs postgres as a separate user when we are root
ready "$VENV/bin/python"
