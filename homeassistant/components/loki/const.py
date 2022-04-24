"""Constants for the loki integration."""

DOMAIN = "loki"

TIMEOUT = 10  # seconds
RETRY_DELAY = 10
QUEUE_BACKLOG_SECONDS = 30
RETRY_INTERVAL = 60  # seconds
BATCH_TIMEOUT = 1
BATCH_BUFFER_SIZE = 100

RETRY_MESSAGE = f"%s Retrying in {RETRY_INTERVAL} seconds."
CATCHING_UP_MESSAGE = "Catching up, dropped %d old events."
RESUMED_MESSAGE = "Resumed, lost %d events."
WROTE_MESSAGE = "Wrote %d events."
