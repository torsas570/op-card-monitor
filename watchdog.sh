#!/usr/bin/env bash
# Vigilante cruzado de los 4 bots TCG.
#
# El bucle continuo se relanza a sí mismo al acabar cada bloque. Si GitHub no
# consigue arrancar ese siguiente run ("The job was not acquired by Runner",
# pasó el 05/10/2026 durante un incidente de Actions), nadie lo relanza: el cron
# de rescate GitHub lo retrasa horas. Por eso cada bot mira a los otros tres y
# relanza el bucle que se haya quedado sin run activo ni en cola.
#
# Necesita el secret WATCHDOG_TOKEN: un token fine-grained con permiso
# "Actions: Read and write" sobre los 4 repos (GITHUB_TOKEN no sirve entre repos).
# Sin él, no hace nada.
set -u
[ -n "${WATCHDOG_TOKEN:-}" ] || exit 0
export GH_TOKEN="$WATCHDOG_TOKEN"
BUCLES="
torsas570/op-card-monitor monitor-continuo.yml
torsas570/pokemon-tcg-monitor monitor-high.yml
torsas570/naruto-tcg-monitor monitor-continuo.yml
torsas570/dragonball-tcg-monitor monitor-continuo.yml
"
echo "$BUCLES" | while read -r repo wf; do
  [ -n "$repo" ] || continue
  [ "$repo" = "${GITHUB_REPOSITORY:-}" ] && continue   # el propio bucle está vivo: es este
  activos=$(gh run list -R "$repo" --workflow "$wf" --limit 5 \
            --json status -q '[.[] | select(.status != "completed")] | length' 2>/dev/null) || continue
  if [ "$activos" = "0" ]; then
    if gh workflow run -R "$repo" "$wf" --ref master; then
      echo "watchdog: $repo sin bucle activo -> relanzado"
    fi
  fi
done
exit 0
