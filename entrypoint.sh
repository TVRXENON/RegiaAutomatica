#!/bin/sh
set -e
mkdir -p "$DATA_DIR"
python -c "from app import db; db.init()"

# motore di messa in onda: se si ferma per un errore imprevisto riparte da solo
(
  while true; do
    python -m app.engine || true
    echo "Motore di messa in onda terminato: riavvio tra 3 secondi"
    sleep 3
  done
) &

# pagina di gestione
exec gunicorn --preload --bind "0.0.0.0:${PORT:-8080}" --workers 2 --threads 8 \
  --timeout 120 --graceful-timeout 10 --error-logfile - app.web:app
