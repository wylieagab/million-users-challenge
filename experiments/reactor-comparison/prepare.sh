#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ "${GITHUB_ACTIONS:-}" = true || "${ALLOW_LOCAL_DISPOSABLE_DB:-}" = 1 ]] || {
  echo 'This setup replaces feed_seed in the explicitly disposable PostgreSQL server.' >&2; exit 2;
}
: "${PG_CONTAINER:?Set the disposable PostgreSQL Docker container ID}"
mkdir -p reactor-results
exec > >(tee reactor-results/setup.log) 2>&1

# libpq on the app host and the database server are both PostgreSQL 18.
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl gnupg jq openssl clang make gcc zlib1g-dev libssl-dev libicu74
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o "$RUNNER_TEMP/pgdg.asc"
sudo install -m 0644 "$RUNNER_TEMP/pgdg.asc" /usr/share/keyrings/reactor-pgdg.asc
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/reactor-pgdg.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' | sudo tee /etc/apt/sources.list.d/reactor-pgdg.list
sudo apt-get update -qq
pq_version=$(apt-cache madison libpq5 | awk '$3 ~ /^18\./ {print $3; exit}')
[[ -n "$pq_version" ]] || { echo 'No PostgreSQL 18 libpq package available'; exit 1; }
sudo apt-get install -y -qq postgresql-client-18 "libpq5=$pq_version" "libpq-dev=$pq_version"
dpkg-query -W libpq5 libpq-dev postgresql-client-18

# One runtime/SDK for all .NET builds. This is the frozen Kestrel install script.
git show 3cff0a51d19018639af2614be2308762a738133e:submissions/csharp-kestrel-wylieagab/install.sh > "$RUNNER_TEMP/reactor-dotnet-install.sh"
sudo bash "$RUNNER_TEMP/reactor-dotnet-install.sh"
export PATH="/opt/million-users-dotnet-10.0.403:/usr/lib/postgresql/18/bin:$PATH"
echo /opt/million-users-dotnet-10.0.403 >> "$GITHUB_PATH"
echo /usr/lib/postgresql/18/bin >> "$GITHUB_PATH"
dotnet --info

# Exact toolchain requested by the unchanged official generator's go.mod.
curl -fsSL https://go.dev/dl/go1.27.1.linux-amd64.tar.gz -o "$RUNNER_TEMP/go.tar.gz"
printf '%s  %s\n' 63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445 "$RUNNER_TEMP/go.tar.gz" | sha256sum --check
mkdir -p "$RUNNER_TEMP/reactor-go"
tar -xzf "$RUNNER_TEMP/go.tar.gz" -C "$RUNNER_TEMP/reactor-go"
echo "$RUNNER_TEMP/reactor-go/go/bin" >> "$GITHUB_PATH"
"$RUNNER_TEMP/reactor-go/go/bin/go" version

# Apply the repository settings first, then explicitly documented diagnostic
# memory/WAL sizes for the smaller host shared by DB, app, and load generator.
{
  cat box/postgresql.conf
  printf '\n# Shared GitHub runner diagnostic overrides\n'
  printf "%s\n" "shared_buffers = '1GB'" "effective_cache_size = '4GB'" "maintenance_work_mem = '256MB'" "max_wal_size = '2GB'" "min_wal_size = '256MB'"
} > reactor-results/postgresql-applied.conf
docker exec -i -u postgres "$PG_CONTAINER" sh -c 'cat >> "$PGDATA/postgresql.conf"' < reactor-results/postgresql-applied.conf
docker restart "$PG_CONTAINER"
for attempt in $(seq 1 60); do pg_isready -q && break; sleep 1; done
pg_isready -q
psql -X -At -d postgres -c 'SELECT version()'
bash seed/make-seed.sh
