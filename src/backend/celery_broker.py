import asyncio
import json
import logging
from typing import Dict, List, Union

import redis
import redis.exceptions
from fastapi import WebSocket

from database.utils import create_uuid


class CeleryBroker:
    def __init__(self, host: str, port: int):
        self.redis_client = redis.Redis(host=host, port=port, decode_responses=True)
        self.active_connections: Dict[str, WebSocket] = {}
        self.pubsub_tasks: List[asyncio.Task] = []
        self.pubsub_channels = [
            "completion_request_channel",
            "completion_feedback_channel",
            "multi_file_context_update_channel",
        ]
        self.pubsubs_registered = False
        try:
            self.redis_client.ping()
            logging.log(
                logging.INFO,
                f"Connected to Celery Redis server successfully on {host}:{port}.",
            )
        except redis.exceptions.ConnectionError:
            raise Exception(
                "Could not connect to Redis server. Please check your configuration."
            )

    def register_pubsubs(self):
        """
        Register pubsub handlers for all channels.
        This is now optimized to only register if not already registered.
        """
        if not self.pubsubs_registered:
            logging.info("Registering pubsub handlers")
            for channel in self.pubsub_channels:
                task = asyncio.create_task(self.__handle_pubsub(channel))
                self.pubsub_tasks.append(task)
            self.pubsubs_registered = True

    def register_new_connection(self, websocket: WebSocket) -> str:
        connection_id = str(create_uuid())
        self.active_connections[connection_id] = websocket

        # Lazily register pubsubs when the first connection is established
        if len(self.active_connections) == 1 and not self.pubsubs_registered:
            self.register_pubsubs()

        return connection_id

    def unregister_connection(self, connection_id: str) -> None:
        self.active_connections.pop(connection_id, None)

        # If no more active connections, cancel pubsub tasks to save resources
        if not self.active_connections and self.pubsubs_registered:
            self._cancel_pubsub_tasks()

    def _cancel_pubsub_tasks(self):
        """Cancel all pubsub tasks to free up resources."""
        logging.info("Cancelling pubsub tasks as there are no active connections")
        for task in self.pubsub_tasks:
            if not task.done() and not task.cancelled():
                task.cancel()
        self.pubsub_tasks = []
        self.pubsubs_registered = False

    def publish_message(self, channel: str, message: Union[str, dict]) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        self.redis_client.publish(channel, message)

    async def __handle_pubsub(self, channel: str):
        pubsub = self.redis_client.pubsub()
        pubsub.subscribe(channel)

        # Wait for confirmation of the subscription
        confirmation = pubsub.get_message(timeout=1)
        if confirmation and confirmation.get("type") == "subscribe":
            logging.info(
                f"Successfully subscribed to channel: {confirmation.get('channel')}"
            )
        else:
            logging.warning(f"Failed to confirm subscription to channel: {channel}")
            return  # Exit early if subscription not confirmed

        try:
            # Adaptive sleep interval - starts higher and adjusts based on activity
            sleep_interval = 0.5  # Start with a higher interval to reduce CPU usage
            consecutive_empty_polls = 0
            max_sleep_interval = 1.0  # Maximum sleep time
            min_sleep_interval = 0.2  # Minimum sleep time

            while True:
                # Check if the task has been cancelled
                current_task = asyncio.current_task()
                if current_task is None or current_task.cancelled():
                    logging.info(f"Pubsub handler for {channel} was cancelled")
                    break

                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

                if message:
                    logging.info(
                        f"Received message on channel {channel}: {message['data']}"
                    )
                    await self.__handle_message(message["data"])
                    # Message received, decrease sleep interval for responsiveness
                    consecutive_empty_polls = 0
                    sleep_interval = min_sleep_interval
                else:
                    # No message, gradually increase sleep interval to save resources
                    consecutive_empty_polls += 1
                    if consecutive_empty_polls > 5:  # After 5 empty polls
                        sleep_interval = min(sleep_interval * 1.2, max_sleep_interval)

                await asyncio.sleep(sleep_interval)

        except asyncio.CancelledError:
            # Handle task cancellation gracefully
            logging.info(f"Pubsub handler for {channel} was cancelled")
        finally:
            # Clean up the pubsub connection
            pubsub.unsubscribe(channel)
            pubsub.close()

    async def __handle_message(self, raw_data: str):
        try:
            data = json.loads(raw_data)
            connection_id = data["connection_id"]
            result = data.get("result")
            if not result:
                result = data.get("error")
            ws = self.active_connections.get(connection_id)
            if ws:
                await ws.send_json(result)
        except Exception as e:
            print(f"Error sending WebSocket message: {e}")

    def cleanup(self):
        # Cancel any active pubsub tasks before closing the Redis connection
        if self.pubsubs_registered:
            self._cancel_pubsub_tasks()
        self.redis_client.close()
