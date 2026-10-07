#!/usr/bin/env bash
# Un tramo del bucle continuo: pasadas de monitor.py durante $1 minutos.
# El workflow encadena 5 tramos de 66 min (5 h 30 min) y guarda el state en la
# caché después de cada uno: si GitHub pierde la máquina a mitad de bloque, se
# pierde como mucho una hora de state, no cinco y media.
set -u
END=$(( $(date +%s) + ${1:-66} * 60 ))
i=0
while [ "$(date +%s)" -lt "$END" ]; do
  i=$((i+1))
  echo "== Pasada $i =="
  python3 monitor.py || echo "Pasada $i fallo, continuo con la siguiente"
  # Cada ~15 min, comprobar que los bucles de los otros 3 bots siguen vivos.
  [ $((i % 10)) -eq 1 ] && { ./watchdog.sh || true; }
  # Cada ~15 min, traer de GitHub el motor y el config nuevos: un arreglo entra en
  # la pasada siguiente en vez de esperar al relevo del bloque (hasta 5 h 30 min).
  # loop.sh NO se actualiza (bash lo va leyendo mientras corre). Si se colara un
  # error, check.yml lo marca en el push y monitor.py avisa al primer fallo.
  if [ $((i % 10)) -eq 0 ]; then
    antes=$(cat monitor.py config.json | md5sum)
    if git fetch -q --depth=1 origin master 2>/dev/null; then
      git checkout -q FETCH_HEAD -- monitor.py config.json heartbeat.py watchdog.sh 2>/dev/null || true
      [ "$antes" != "$(cat monitor.py config.json | md5sum)" ] && \
        echo "Código actualizado desde master ($(git rev-parse --short FETCH_HEAD))"
    fi
  fi
  # Jitter 60-105 s: cadencia ~1-2 min por tienda e IRREGULAR (menos "cara de
  # bot" que un intervalo fijo) -> menor riesgo de bloqueo de IP.
  sleep $((60 + RANDOM % 46))
done
echo "Tramo terminado tras $i pasadas."
