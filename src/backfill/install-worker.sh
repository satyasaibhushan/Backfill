#!/bin/sh
set -eu
server=${1:?Website address required}
code=${2:?Pairing code required}
case "$server" in https://*) ;; *) echo 'The website must use HTTPS' >&2; exit 1;; esac
[ "$(uname -s)" = Linux ] || { echo 'Run this command on your Linux machine' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 or newer required"'
root="$HOME/.local/share/backfill-worker"
umask 077
mkdir -p "$root"
python3 -m venv "$root/venv"
package="$root/backfill-0.3.0-py3-none-any.whl"
curl -fsSL "$server/worker.whl" -o "$package"
"$root/venv/bin/python" -m pip install --quiet "$package"
rm "$package"
if ! command -v codexbar >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64) arch=x86_64; checksum=784b6f9d9e3e4fd35e2d0ff6ee83fab04d1a32f3a1323b5fbee457a904c62cd0;;
    aarch64|arm64) arch=aarch64; checksum=bba08c509966ecd6c331851ab953b7025cf954d1d079bae463519437b057410c;;
    *) echo 'Unsupported CPU architecture' >&2; exit 1;;
  esac
  archive="$root/meter.tar.gz"
  curl -fsSL "https://github.com/steipete/CodexBar/releases/download/v0.56.7/CodexBarCLI-v0.56.7-linux-musl-$arch.tar.gz" -o "$archive"
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
  mkdir -p "$root/meter"
  tar -xzf "$archive" -C "$root/meter"
  meter="$root/meter/codexbar"
  [ -x "$meter" ] || { echo 'Quota reader missing from package' >&2; exit 1; }
  ln -sf "$meter" "$root/venv/bin/codexbar"
  rm "$archive"
fi
PATH="$root/venv/bin:$PATH"
export PATH
"$root/venv/bin/backfill" --data-dir "$root/data" connect --server "$server" --code "$code" --install
printf '\nConnected. Return to the website to see machine and provider status.\n'
