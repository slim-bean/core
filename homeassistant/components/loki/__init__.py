"""The loki integration."""
from __future__ import annotations

from concurrent.futures import thread
from contextlib import suppress
from http import HTTPStatus
import json
import logging
import queue
import threading
import time
from typing import Optional

import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant, callback

from .const import (
    BATCH_BUFFER_SIZE,
    BATCH_TIMEOUT,
    CATCHING_UP_MESSAGE,
    DOMAIN,
    QUEUE_BACKLOG_SECONDS,
    RESUMED_MESSAGE,
    RETRY_DELAY,
    RETRY_MESSAGE,
    TIMEOUT,
    WROTE_MESSAGE,
)

_LOGGER = logging.getLogger(__name__)

# TODO List the platforms that you want to support.
# For your initial PR, limit it to 1 platform.
# PLATFORMS: list[Platform] = [Platform.]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up loki from a config entry."""

    instance = hass.data[DOMAIN] = LokiThread(hass, 5)
    instance.start()

    # hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    def shutdown(event):
        """Shut down the thread."""
        instance.queue.put(None)
        instance.join()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)

    return True


# async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
#     """Unload a config entry."""
#     if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
#         instance = hass.data[DOMAIN].pop(entry.entry_id)
#         instance.queue.put(None)
#         instance.join()

#     return unload_ok


class LokiThread(threading.Thread):
    """A threaded event handler class."""

    def __init__(self, hass, max_tries):
        """Initialize the listener."""
        threading.Thread.__init__(self, name=DOMAIN)
        self.queue = queue.Queue()
        self.max_tries = max_tries
        self.write_errors = 0
        self.shutdown = False
        hass.bus.async_listen(EVENT_STATE_CHANGED, self._event_listener)
        self._session: requests.Session | None = None

    @property
    def session(self) -> requests.Session:
        """Create HTTP session."""
        if self._session is None:
            self._session = requests.Session
            # self._session.auth = self.auth or None
        return self._session

    @callback
    def _event_listener(self, event):
        """Listen for new messages on the bus and queue them for Loki."""
        item = (time.time_ns(), event)
        self.queue.put(item)

    @staticmethod
    def batch_timeout():
        """Return number of seconds to wait for more events."""
        return BATCH_TIMEOUT

    def event_to_json(self, event: dict) -> str:
        """Convert event to json"""
        state = event.data.get("new_state")
        if state is None or state.state in (STATE_UNKNOWN, "", STATE_UNAVAILABLE):
            return
        return json.dumps(state.as_dict())

    def get_events_json(self):
        """Return a batch of events formatted for writing."""
        queue_seconds = (
            QUEUE_BACKLOG_SECONDS + self.max_tries * RETRY_DELAY * 1000 * 1000 * 1000
        )

        count = 0
        json = []

        dropped = 0

        with suppress(queue.Empty):
            while len(json) < BATCH_BUFFER_SIZE and not self.shutdown:
                timeout = None if count == 0 else self.batch_timeout()
                item = self.queue.get(timeout=timeout)
                count += 1

                if item is None:
                    self.shutdown = True
                else:
                    timestamp, event = item
                    age = time.time_ns() - timestamp

                    if age < queue_seconds:
                        event_json = self.event_to_json(event)
                        if event_json:
                            json.append([str(timestamp), event_json])
                    else:
                        dropped += 1

        if dropped:
            _LOGGER.warning(CATCHING_UP_MESSAGE, dropped)

        return count, json

    def write_to_loki(self, jsn):
        """Write preprocessed events to loki, with retry."""
        for retry in range(self.max_tries + 1):
            try:

                stream = {
                    "streams": [
                        {
                            "stream": {"job": "homeassistant"},
                            "values": jsn,
                        }
                    ]
                }
                url = "https://loki.edjusted.com/loki/api/v1/push"
                resp = requests.post(
                    url,
                    data=json.dumps(stream),
                    headers={"Content-Type": "application/json"},
                    timeout=TIMEOUT,
                )
                _LOGGER.debug(json.dumps(stream))
                if resp.status_code == HTTPStatus.NO_CONTENT:
                    break
                _LOGGER.error(resp.json())
            except ValueError as err:
                _LOGGER.error(err)
                break
            except ConnectionError as err:
                if retry < self.max_tries:
                    time.sleep(RETRY_DELAY)
                else:
                    if not self.write_errors:
                        _LOGGER.error(err)

    def run(self):
        """Process incoming events."""
        while not self.shutdown:
            count, json = self.get_events_json()
            if json:
                self.write_to_loki(json)
            for _ in range(count):
                self.queue.task_done()

    def block_till_done(self):
        """Block till all events processed."""
        self.queue.join()
        if self._session is not None:
            self._session.close()
            self._session = None
