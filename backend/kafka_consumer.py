"""
Baze — Kafka consumer

Reads sensor events from the `baze.sensor-events` topic and hands each one
to the backend's ingest function (save as PENDING, then run Gemini).

main.py starts this in a background thread when KAFKA_BOOTSTRAP_SERVERS is
set, so the FastAPI process is both the HTTP API and the Kafka consumer.

Delivery is at-least-once: the offset is committed only after the event has
been saved to MongoDB. If the backend crashes before the commit, Kafka sends
the event again, and the backend's upsert on event_id stops a duplicate.
"""

import json
import os
import threading
import time
from typing import Callable

from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC     = os.environ.get("KAFKA_TOPIC", "baze.sensor-events")
GROUP_ID  = os.environ.get("KAFKA_GROUP_ID", "baze-backend")


def consume_forever(handle_event: Callable[[dict], None],
                    stop: threading.Event) -> None:
    consumer = Consumer({
        "bootstrap.servers":  BOOTSTRAP,
        "group.id":           GROUP_ID,
        # A new consumer group starts from the oldest event still in the topic,
        # so events produced while the backend was down are not skipped.
        "auto.offset.reset":  "earliest",
        # We commit ourselves, after the event is safely in MongoDB.
        "enable.auto.commit": False,
    })
    consumer.subscribe([TOPIC])
    print(f"[kafka] consuming {TOPIC} from {BOOTSTRAP} as group '{GROUP_ID}'")

    try:
        while not stop.is_set():
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            device = msg.key().decode() if msg.key() else "?"
            try:
                event = json.loads(msg.value())
                handle_event(event)
            except ValueError as e:
                # Bad JSON or a failed schema check. Retrying would fail
                # forever, so log it and move past it.
                print(f"[kafka] skipping bad event from {device} "
                      f"(partition {msg.partition()}, offset {msg.offset()}): {e}")
            except Exception as e:
                # Anything else (e.g. MongoDB unreachable) may be temporary.
                # Don't commit; rewind to this offset and try again shortly.
                print(f"[kafka] ingest failed, retrying in 2s: {e}")
                time.sleep(2)
                consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
                continue

            consumer.commit(message=msg, asynchronous=False)
            print(f"[kafka] {device} → partition {msg.partition()} "
                  f"offset {msg.offset()} committed")
    finally:
        consumer.close()
        print("[kafka] consumer closed")


def start_in_background(handle_event: Callable[[dict], None]) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(
        target=consume_forever, args=(handle_event, stop),
        name="kafka-consumer", daemon=True,
    )
    thread.start()
    return thread, stop
