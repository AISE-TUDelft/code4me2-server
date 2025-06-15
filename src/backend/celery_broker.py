import asyncio
import json
import logging
from typing import Dict, List, Union

import redis
import redis.exceptions
from fastapi import WebSocket

from utils import create_uuid


class CeleryBroker:
    """
    A broker that manages Redis pub/sub channels for communication between
    Celery workers and WebSocket connections.

    It maintains active WebSocket connections, subscribes to Redis channels,
    listens for messages, and forwards them to the appropriate WebSocket clients.
    """

    def __init__(self, host: str, port: int):
        """
        Initialize the Redis client and prepare internal state.

        Args:
            host (str): Redis server host.
            port (int): Redis server port.
        """
        self.redis_client = redis.Redis(host=host, port=port, decode_responses=True)
        self.active_connections: Dict[str, WebSocket] = (
            {}
        )  # Maps connection IDs to WebSocket instances
        self.pubsub_tasks: List[asyncio.Task] = []  # Tasks listening to pubsub channels
        self.pubsub_channels = [
            "completion_request_channel",
            "completion_feedback_channel",
            "multi_file_context_update_channel",
        ]
        self.pubsubs_registered = False

        # Test Redis connection on initialization
        try:
            self.redis_client.ping()
            logging.info(
                f"Connected to Celery Redis server successfully on {host}:{port}."
            )
        except redis.exceptions.ConnectionError:
            raise Exception(
                "Could not connect to Redis server. Please check your configuration."
            )

    def register_pubsubs(self):
        """
        Register pubsub handlers for all configured channels.

        Starts asyncio tasks to listen on each channel only if not already registered.
        """
        if not self.pubsubs_registered:
            logging.info("Registering pubsub handlers")
            for channel in self.pubsub_channels:
                task = asyncio.create_task(self.__handle_pubsub(channel))
                self.pubsub_tasks.append(task)
            self.pubsubs_registered = True

    def register_new_connection(self, websocket: WebSocket) -> str:
        """
        Register a new active WebSocket connection.

        Args:
            websocket (WebSocket): The WebSocket connection instance.

        Returns:
            str: A unique connection ID assigned to this WebSocket.
        """
        connection_id = str(create_uuid())
        self.active_connections[connection_id] = websocket

        # Lazily register pubsub listeners on first connection
        if len(self.active_connections) == 1 and not self.pubsubs_registered:
            self.register_pubsubs()

        return connection_id

    def unregister_connection(self, connection_id: str) -> None:
        """
        Remove an active connection by its ID.

        If no connections remain, cancel pubsub tasks to save resources.

        Args:
            connection_id (str): The ID of the connection to remove.
        """
        self.active_connections.pop(connection_id, None)

        if not self.active_connections and self.pubsubs_registered:
            self._cancel_pubsub_tasks()

    def _cancel_pubsub_tasks(self):
        """
        Cancel all active pubsub listener tasks to free up resources.

        This is typically called when no active WebSocket connections remain.
        """
        logging.info("Cancelling pubsub tasks as there are no active connections")
        for task in self.pubsub_tasks:
            if not task.done() and not task.cancelled():
                task.cancel()
        self.pubsub_tasks = []
        self.pubsubs_registered = False

    def publish_message(self, channel: str, message: Union[str, dict]) -> None:
        """
        Publish a message to a Redis channel.

        Args:
            channel (str): The Redis channel name.
            message (Union[str, dict]): The message to publish. If dict, it will be JSON serialized.
        """
        if isinstance(message, dict):
            message = json.dumps(message)
        self.redis_client.publish(channel, message)

    async def __handle_pubsub(self, channel: str):
        """
        Internal coroutine to listen for messages on a specific Redis pubsub channel.

        Forwards messages to the appropriate WebSocket connections asynchronously.

        Args:
            channel (str): The Redis channel to subscribe to.
        """
        pubsub = self.redis_client.pubsub()
        pubsub.subscribe(channel)

        # Wait for confirmation of subscription
        confirmation = pubsub.get_message(timeout=1)
        if confirmation and confirmation.get("type") == "subscribe":
            logging.info(
                f"Successfully subscribed to channel: {confirmation.get('channel')}"
            )
        else:
            logging.warning(f"Failed to confirm subscription to channel: {channel}")
            return  # Exit early if subscription not confirmed

        try:
            # Adaptive sleep interval to balance responsiveness and CPU usage
            sleep_interval = 0.5  # Initial sleep time in seconds
            consecutive_empty_polls = 0
            max_sleep_interval = 1.0
            min_sleep_interval = 0.2

            while True:
                current_task = asyncio.current_task()
                if current_task is None or current_task.cancelled():
                    logging.info(f"Pubsub handler for {channel} was cancelled")
                    break

                # Retrieve message, ignoring subscription confirmation messages
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

                if message:
                    logging.info(
                        f"Received message on channel {channel}: {message['data']}"
                    )
                    await self.__handle_message(message["data"])
                    # Reset sleep interval for prompt processing
                    consecutive_empty_polls = 0
                    sleep_interval = min_sleep_interval
                else:
                    # Increase sleep interval gradually when no messages arrive
                    consecutive_empty_polls += 1
                    if consecutive_empty_polls > 5:
                        sleep_interval = min(sleep_interval * 1.2, max_sleep_interval)

                await asyncio.sleep(sleep_interval)

        except asyncio.CancelledError:
            # Expected on task cancellation, no action needed
            logging.info(f"Pubsub handler for {channel} was cancelled")
        finally:
            # Unsubscribe and close the pubsub connection
            pubsub.unsubscribe(channel)
            pubsub.close()

    async def __handle_message(self, raw_data: str):
        """
        Internal method to process a received pubsub message.

        Parses the JSON data and sends the relevant result back to the WebSocket
        corresponding to the connection ID.

        Args:
            raw_data (str): The raw JSON message data.
        """
        try:
            data = json.loads(raw_data)
            connection_id = data["connection_id"]
            # Prefer 'result', fallback to 'error'
            result = data.get("result") or data.get("error")
            ws = self.active_connections.get(connection_id)
            if ws:
                await ws.send_json(result)
        except Exception as e:
            logging.error(f"Error sending WebSocket message: {e}")

    def cleanup(self):
        """
        Clean up resources by cancelling pubsub tasks and closing Redis connection.

        Should be called on application shutdown or when broker is no longer needed.
        """
        if self.pubsubs_registered:
            self._cancel_pubsub_tasks()
        self.redis_client.close()
