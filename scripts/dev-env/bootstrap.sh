#!/usr/bin/env bash
set -euo pipefail

# Rebuild the loopback-only development services on this Linux ARM64 host.
# Persistent state stays under .runtime/ and is ignored by Git.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime="$repo_root/.runtime"
downloads="$runtime/downloads"
bin_dir="$runtime/bin"
user_bin="$HOME/.local/bin"
postgres_version="16.15-0ubuntu0.24.04.1"
minio_version="RELEASE.2025-09-07T16-13-09Z"
uv_version="0.13.5"

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "aarch64" ]]; then
  echo "This bootstrap is pinned to the current Linux ARM64 development host." >&2
  exit 1
fi

for command in apt-get curl dpkg-deb openssl sha256sum tar; do
  command -v "$command" >/dev/null || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done

mkdir -p "$downloads" "$bin_dir" "$user_bin" "$runtime/data" "$runtime/run" "$runtime/logs" \
  "$runtime/config"
chmod 0700 "$runtime/data" "$runtime/run" "$runtime/logs" "$runtime/config"

if [[ ! -x "$bin_dir/uv" ]] || [[ "$("$bin_dir/uv" --version | awk '{print $2}')" != "$uv_version" ]]; then
  uv_archive="uv-aarch64-unknown-linux-gnu.tar.gz"
  curl -fL --retry 3 --max-time 180 \
    "https://github.com/astral-sh/uv/releases/download/$uv_version/$uv_archive" \
    -o "$downloads/$uv_archive"
  curl -fL --retry 3 --max-time 30 \
    "https://github.com/astral-sh/uv/releases/download/$uv_version/$uv_archive.sha256" \
    -o "$downloads/$uv_archive.sha256"
  expected="$(cut -d' ' -f1 "$downloads/$uv_archive.sha256")"
  actual="$(sha256sum "$downloads/$uv_archive" | cut -d' ' -f1)"
  [[ "$expected" == "$actual" ]] || {
    echo "uv checksum mismatch" >&2
    exit 1
  }
  tar -xzf "$downloads/$uv_archive" -C "$bin_dir" --strip-components=1 \
    "uv-aarch64-unknown-linux-gnu/uv" "uv-aarch64-unknown-linux-gnu/uvx"
fi
ln -sfn "$bin_dir/uv" "$user_bin/uv"
ln -sfn "$bin_dir/uvx" "$user_bin/uvx"

if ! command -v pnpm >/dev/null || [[ "$(pnpm --version)" != 10.* ]]; then
  corepack enable --install-directory "$user_bin" pnpm
  corepack prepare pnpm@10 --activate
fi

postgres_root="$runtime/postgres"
postgres_bin="$postgres_root/usr/lib/postgresql/16/bin"
postgres_lib="$postgres_root/usr/lib/aarch64-linux-gnu:$postgres_root/usr/lib/postgresql/16/lib"
if [[ ! -x "$postgres_bin/postgres" ]]; then
  (
    cd "$downloads"
    for package in postgresql-16 postgresql-client-16 postgresql-common libpq5; do
      apt-get download "$package"
    done
  )
  mkdir -p "$postgres_root"
  for package in "$downloads"/postgresql-16_"$postgres_version"_arm64.deb \
    "$downloads"/postgresql-client-16_"$postgres_version"_arm64.deb \
    "$downloads"/postgresql-common_*_all.deb "$downloads"/libpq5_"$postgres_version"_arm64.deb; do
    dpkg-deb -x "$package" "$postgres_root"
  done
fi

postgres_data="$runtime/data/postgres"
mkdir -p "$postgres_data"
chmod 0700 "$postgres_data"
if [[ ! -f "$postgres_data/PG_VERSION" ]]; then
  LD_LIBRARY_PATH="$postgres_lib" "$postgres_bin/initdb" \
    -D "$postgres_data" --username=speechnexus --encoding=UTF8 --locale=C.UTF-8 \
    --auth-local=trust --auth-host=trust >/dev/null
  cat >> "$postgres_data/postgresql.conf" <<EOF

# SpeechNexus local development only
listen_addresses = '127.0.0.1'
port = 55432
unix_socket_directories = '$runtime/run'
shared_buffers = 512MB
max_connections = 100
timezone = 'UTC'
log_timezone = 'UTC'
lc_messages = 'C'
EOF
  cat > "$postgres_data/pg_hba.conf" <<'EOF'
# Loopback development access only.
local all all trust
host all all 127.0.0.1/32 trust
host all all ::1/128 trust
EOF
fi

minio_asset="minio.linux-arm64.$minio_version"
if [[ ! -x "$bin_dir/minio" ]]; then
  curl -fL --retry 3 --max-time 300 \
    "https://github.com/minio/minio/releases/download/$minio_version/$minio_asset" \
    -o "$downloads/$minio_asset"
  curl -fL --retry 3 --max-time 30 \
    "https://github.com/minio/minio/releases/download/$minio_version/$minio_asset.sha256sum" \
    -o "$downloads/$minio_asset.sha256sum"
  expected="$(cut -d' ' -f1 "$downloads/$minio_asset.sha256sum")"
  actual="$(sha256sum "$downloads/$minio_asset" | cut -d' ' -f1)"
  [[ "$expected" == "$actual" ]] || {
    echo "MinIO checksum mismatch" >&2
    exit 1
  }
  install -m 0755 "$downloads/$minio_asset" "$bin_dir/minio"
fi

minio_data="$runtime/data/minio"
mkdir -p "$minio_data"
chmod 0700 "$minio_data"
if [[ ! -f "$runtime/config/minio.env" ]]; then
  password="$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-40)"
  umask 077
  printf 'MINIO_ROOT_USER=speechnexus\nMINIO_ROOT_PASSWORD=%s\nMINIO_BROWSER=off\n' \
    "$password" > "$runtime/config/minio.env"
fi
chmod 0600 "$runtime/config/minio.env"

systemd_dir="$HOME/.config/systemd/user"
mkdir -p "$systemd_dir"
cat > "$systemd_dir/speechnexus-postgres.service" <<EOF
[Unit]
Description=SpeechNexus development PostgreSQL
After=network.target

[Service]
Type=simple
Environment=LD_LIBRARY_PATH=$postgres_lib
ExecStart=$postgres_bin/postgres -D $postgres_data
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
MemoryHigh=1G
MemoryMax=4G
CPUQuota=200%
TasksMax=512
LimitNOFILE=65536

[Install]
WantedBy=default.target
EOF
cat > "$systemd_dir/speechnexus-minio.service" <<EOF
[Unit]
Description=SpeechNexus development object storage
After=network.target

[Service]
Type=simple
EnvironmentFile=$runtime/config/minio.env
ExecStart=$bin_dir/minio server $minio_data --address 127.0.0.1:59000 --console-address 127.0.0.1:59001
Restart=on-failure
MemoryHigh=1G
MemoryMax=4G
CPUQuota=200%
TasksMax=512
LimitNOFILE=65536

[Install]
WantedBy=default.target
EOF
chmod 0644 "$systemd_dir/speechnexus-postgres.service" \
  "$systemd_dir/speechnexus-minio.service"

systemctl --user stop speechnexus-minio.service speechnexus-postgres.service 2>/dev/null || true
if LD_LIBRARY_PATH="$postgres_lib" "$postgres_bin/pg_ctl" -D "$postgres_data" status \
  >/dev/null 2>&1; then
  LD_LIBRARY_PATH="$postgres_lib" "$postgres_bin/pg_ctl" -D "$postgres_data" -m fast -w stop
fi
if [[ -f "$runtime/run/minio.pid" ]]; then
  kill "$(cat "$runtime/run/minio.pid")" 2>/dev/null || true
  rm -f "$runtime/run/minio.pid"
fi

systemctl --user daemon-reload
systemctl --user enable --now speechnexus-postgres.service speechnexus-minio.service
if [[ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]]; then
  loginctl enable-linger "$USER"
fi

for _ in $(seq 1 80); do
  if LD_LIBRARY_PATH="$postgres_lib" "$postgres_bin/pg_isready" \
    -h 127.0.0.1 -p 55432 -U speechnexus -d postgres \
    >/dev/null 2>&1 && curl -fsS --max-time 1 http://127.0.0.1:59000/minio/health/live \
    >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

for database in speechnexus_dev speechnexus_test; do
  if ! LD_LIBRARY_PATH="$postgres_lib" "$postgres_bin/psql" \
    -h 127.0.0.1 -p 55432 -U speechnexus -d postgres -Atqc \
    "select 1 from pg_database where datname='$database'" | grep -q 1; then
    LD_LIBRARY_PATH="$postgres_lib" "$postgres_bin/createdb" \
      -h 127.0.0.1 -p 55432 -U speechnexus "$database"
  fi
done

cd "$repo_root"
uv sync --all-groups
uv run python - <<'PY'
import os
from pathlib import Path

from minio import Minio

env = {}
for line in Path(".runtime/config/minio.env").read_text().splitlines():
    key, value = line.split("=", 1)
    env[key] = value
client = Minio(
    "127.0.0.1:59000",
    access_key=env["MINIO_ROOT_USER"],
    secret_key=env["MINIO_ROOT_PASSWORD"],
    secure=False,
)
for bucket in ("speechnexus-dev", "speechnexus-test"):
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
PY
ffmpeg_path="$(uv run python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
ln -sfn "$ffmpeg_path" "$bin_dir/ffmpeg"
ln -sfn "$ffmpeg_path" "$user_bin/ffmpeg"

cat > "$runtime/config/dev.env" <<'EOF'
DATABASE_URL_DEV=postgresql://speechnexus@127.0.0.1:55432/speechnexus_dev
DATABASE_URL_TEST=postgresql://speechnexus@127.0.0.1:55432/speechnexus_test
S3_ENDPOINT=http://127.0.0.1:59000
S3_DEV_BUCKET=speechnexus-dev
S3_TEST_BUCKET=speechnexus-test
EOF
chmod 0600 "$runtime/config/dev.env"

if [[ -f pnpm-lock.yaml ]]; then
  pnpm install --frozen-lockfile
else
  pnpm install
fi
pnpm exec playwright install chromium
"$repo_root/scripts/dev-env/check.sh"
