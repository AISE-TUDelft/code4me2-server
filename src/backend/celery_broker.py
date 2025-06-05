import asyncio
import json
import logging
from typing import Dict, Union

import redis
from fastapi import WebSocket

from database.utils import create_uuid


class CeleryBroker:
    def __init__(self, host: str, port: int):
        self.redis_client = redis.Redis(host=host, port=port, decode_responses=True)
        self.active_connections: Dict[str, WebSocket] = {}
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
        asyncio.create_task(self.__handle_pubsub("completion_request_channel"))
        asyncio.create_task(self.__handle_pubsub("completion_feedback_channel"))
        asyncio.create_task(self.__handle_pubsub("multi_file_context_update_channel"))

    def register_new_connection(self, websocket: WebSocket) -> str:
        connection_id = str(create_uuid())
        self.active_connections[connection_id] = websocket
        return connection_id

    def unregister_connection(self, connection_id: str) -> None:
        self.active_connections.pop(connection_id, None)

    def publish_message(self, channel: str, message: Union[str, dict]) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        self.redis_client.publish(channel, message)

    def get_key(self, key: str) -> str:
        return self.redis_client.get(key)

    def set_key(self, key: str, value: str, expire: int = None) -> None:
        if expire:
            self.redis_client.setex(key, expire, value)
        else:
            self.redis_client.set(key, value)

    def delete_key(self, key: str) -> None:
        self.redis_client.delete(key)

    async def __handle_pubsub(self, channel: str):
        pubsub = self.redis_client.pubsub()
        pubsub.subscribe(channel)

        # Wait for confirmation of the subscription
        confirmation = pubsub.get_message(timeout=1.0)
        if confirmation and confirmation.get("type") == "subscribe":
            logging.info(
                f"Successfully subscribed to channel: {confirmation.get('channel')}"
            )
        else:
            logging.warning(f"Failed to confirm subscription to channel: {channel}")
            return  # Exit early if subscription not confirmed

        async def listener():
            while True:
                message = pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message:
                    logging.info(
                        f"Received message on channel {channel}: {message['data']}"
                    )
                    await self.__handle_message(message["data"])
                await asyncio.sleep(0.1)  # prevent blocking the loop

        # Run the listener in the current event loop
        await listener()

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
        self.redis_client.close()
