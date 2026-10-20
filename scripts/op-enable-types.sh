#!/bin/sh
# Zet alle werkpakkettypes (Fout, Episch, Gebruikersverhaal, ...) aan voor een project (of alle projecten).
# De OpenProject-API kan dit niet. Gebruik: scripts/op-enable-types.sh [project-identifier]
cd "$(dirname "$0")/.." || exit 1
ID="$1"
docker compose exec -T -e PID="$ID" openproject bash -c 'cd /app && RAILS_ENV=production bundle exec rails runner "
scope = ENV[%q(PID)].to_s.empty? ? Project.all : Project.where(identifier: ENV[%q(PID)])
scope.each { |p| Type.all.each { |t| ProjectType.find_or_create_by!(project_id: p.id, type_id: t.id) } }
puts %Q(types actief voor #{scope.count} project(en))"' 2>&1 | tail -1
