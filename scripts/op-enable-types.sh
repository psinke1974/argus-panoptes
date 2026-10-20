#!/bin/sh
# Enable all work package types (Bug, Epic, User story, ...) for a project (or all projects).
# The OpenProject API cannot do this. Usage: scripts/op-enable-types.sh [project-identifier]
# For a tenant stack: COMPOSE_FILE=deploy/tenant/docker-compose.yml COMPOSE_ENV_FILE=tenants/<name>/.env scripts/op-enable-types.sh [project]
cd "$(dirname "$0")/.." || exit 1
ID="$1"
docker compose ${COMPOSE_FILE:+-f "$COMPOSE_FILE"} ${COMPOSE_ENV_FILE:+--env-file "$COMPOSE_ENV_FILE"} exec -T -e PID="$ID" openproject bash -c 'cd /app && RAILS_ENV=production bundle exec rails runner "
scope = ENV[%q(PID)].to_s.empty? ? Project.all : Project.where(identifier: ENV[%q(PID)])
scope.each { |p| Type.all.each { |t| ProjectType.find_or_create_by!(project_id: p.id, type_id: t.id) } }
puts %Q(types enabled for #{scope.count} project(s))"' 2>&1 | tail -1
