"""heg-testapp — stub container app for F4 sandbox runtime tests.

Prints three JSONL event lines to stdout (so ``docker logs`` / transcript
capture has content), then sleeps so the container stays alive for
``docker exec`` probes. This is a test fixture, NOT the miniloop harness.
"""

import json
import time

EVENTS = [
    {"type": "test_start", "app": "heg-testapp"},
    {"type": "llm_request", "model": "stub", "note": "fixture only — no real call"},
    {"type": "agent_done", "note": "stub finished emitting; idling for exec probes"},
]

SLEEP_SECONDS = 300


def main() -> None:
    for event in EVENTS:
        print(json.dumps(event), flush=True)
    try:
        time.sleep(SLEEP_SECONDS)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
