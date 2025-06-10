import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Union

from celery import chain, group
from fastapi import APIRouter, Cookie, Depends

import celery_app.tasks.db_tasks as db_tasks
import Queries
from App import App
from backend import completion
from backend.Responses import (
    ErrorResponse,
    GenerateChatCompletionsError,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)
from database import crud
from database.utils import create_uuid
from response_models import (
    ChatCompletionErrorItem,
    ChatCompletionItem,
    ChatHistoryItem,
    ChatHistoryResponse,
    ChatMessageItem,
    ChatMessageRole,
)

router = APIRouter()


@router.post(
    "",
    responses={
        "200": {"model": ChatHistoryResponse},
        "401": {
            "model": Union[
                InvalidOrExpiredAuthToken,
                InvalidOrExpiredSessionToken,
                InvalidOrExpiredProjectToken,
            ]
        },
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": GenerateChatCompletionsError},
    },
)
def request_chat_completion(
    chat_completion_request: Queries.RequestChatCompletion,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
    project_token: str = Cookie("project_token"),
) -> JsonResponseWithStatus:
    """
    Request chat completions based on provided messages.
    """
    overall_start = time.perf_counter()
    logging.info(f"Chat completion request: {chat_completion_request.dict()}")

    db_auth = app.get_db_session()
    redis_manager = app.get_redis_manager()
    completion_models = app.get_completion_models()
    config = app.get_config()

    try:
        # Authentication checks
        t0 = time.perf_counter()

        # Validate session token
        session_info = redis_manager.get("session_token", session_token)
        if session_token is None or session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Get user_id and auth_token from session info
        auth_token = session_info.get("auth_token")
        if not auth_token:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        auth_info = redis_manager.get("auth_token", auth_token)
        if auth_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredAuthToken()
            )
        user_id = auth_info.get("user_id")
        if not user_id:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Validate project token
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Verify project is linked to this session
        session_projects = session_info.get("project_tokens", [])
        if project_token not in session_projects:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        t1 = time.perf_counter()
        logging.info(f"Auth check took {(t1 - t0) * 1000:.2f}ms")

        # Process chat completion
        chat_completions = []
        add_generation_tasks = []

        def handle_model_completion(model_id, created_query_id_provided):
            local_t0 = time.perf_counter()
            model = crud.get_model_by_id(db_auth, model_id)
            if not model:
                return ChatCompletionErrorItem(model_name=f"Model ID: {model_id}")
            local_t1 = time.perf_counter()

            # Get chat completion model
            chat_completion_model = completion_models.get_model(
                model_name=str(model.model_name),
                config=app.get_config(),
            )

            if chat_completion_model is None:
                return ChatCompletionErrorItem(model_name=model.model_name)
            local_t2 = time.perf_counter()

            # Invoke the chat model with messages
            # ensure that the chat_completion_model is of type ChatCompletionModel
            if not isinstance(chat_completion_model, completion.ChatCompletionModel):
                return ChatCompletionErrorItem(
                    model_name=model.model_name,
                    message="Model is not a ChatCompletionModel",
                )

            completion_result = chat_completion_model.invoke(
                chat_completion_request.to_langchain_messages()
            )
            local_t3 = time.perf_counter()

            logging.info(
                f"Model {model_id} timing: "
                f"DB={((local_t1 - local_t0) * 1000):.2f}ms, "
                f"GetModel={((local_t2 - local_t1) * 1000):.2f}ms, "
                f"Invoke={((local_t3 - local_t2) * 1000):.2f}ms"
            )

            # Create generation task
            add_generation_task = db_tasks.add_generation_task.si(
                Queries.CreateGeneration(
                    meta_query_id=uuid.UUID(created_query_id_provided),
                    model_id=model_id,
                    completion=completion_result["completion"],
                    generation_time=completion_result["generation_time"],
                    shown_at=[datetime.now().isoformat()],
                    was_accepted=False,
                    confidence=completion_result.get("confidence", 0.0),
                    logprobs=completion_result.get("logprobs", []),
                ).dict(),
                created_query_id_provided,
            )
            add_generation_tasks.append(add_generation_task)

            return ChatCompletionItem(
                model_id=model_id,
                model_name=str(model.model_name),
                completion=completion_result["completion"],
                generation_time=completion_result["generation_time"],
                confidence=completion_result.get("confidence", 0.0),
                was_accepted=False,
            )

        # Process models in parallel
        t4 = time.perf_counter()

        # Create necessary IDs (this it put here to ensure the same ids are used in the generation tasks)
        created_query_id = create_uuid()
        created_context_id = create_uuid()
        created_contextual_telemetry_id = create_uuid()
        created_behavioral_telemetry_id = create_uuid()

        with ThreadPoolExecutor(max_workers=config.thread_pool_max_workers) as executor:
            future_to_model = {
                executor.submit(
                    handle_model_completion, model_id, created_query_id
                ): model_id
                for model_id in chat_completion_request.model_ids
            }
            for future in as_completed(future_to_model):
                result = future.result()
                if result is not None:
                    chat_completions.append(result)
                else:
                    chat_completions.append(
                        ChatCompletionErrorItem(
                            model_name=f"{future_to_model[future]}",
                            message="Model completion failed",
                        )
                    )
        t5 = time.perf_counter()
        logging.info(f"Model completion (threaded) took {(t5 - t4) * 1000:.2f}ms")

        # Celery task preparation
        t6 = time.perf_counter()

        # Create context task
        add_context_task = db_tasks.add_context_task.si(
            chat_completion_request.context.dict(), created_context_id
        )

        # Create telemetry task
        add_telemetry_task = db_tasks.add_telemetry_task.si(
            chat_completion_request.contextual_telemetry.dict(),
            chat_completion_request.behavioral_telemetry.dict(),
            created_contextual_telemetry_id,
            created_behavioral_telemetry_id,
        )

        # check if the content of any of the chat completions contains a [Title], [/Title] tag pair
        title_found = None
        for completion_item in chat_completions:
            if isinstance(completion_item, ChatCompletionItem):
                if (
                    "[Title]" in completion_item.completion
                    and "[/Title]" in completion_item.completion
                ):
                    start_index = completion_item.completion.index("[Title]") + len(
                        "[Title]"
                    )
                    end_index = completion_item.completion.index("[/Title]")
                    title_found = completion_item.completion[
                        start_index:end_index
                    ].strip()
                    completion_item.completion = (
                        completion_item.completion[: start_index - (len("[Title]"))]
                        + completion_item.completion[end_index + (len("[/Title]")) :]
                    )

        # Calculate total serving time
        total_serving_time = int((time.perf_counter() - overall_start) * 1000)

        add_chat_task = db_tasks.get_or_create_chat_task.si(
            Queries.CreateChat(
                user_id=uuid.UUID(str(user_id)),
                project_id=uuid.UUID(project_token),
                title=title_found or "Untitled Chat",
            ).dict(),
            chat_completion_request.chat_id,
        )

        # Create chat query task
        add_chat_query_task = db_tasks.add_chat_query_task.si(
            Queries.CreateChatQuery(
                user_id=uuid.UUID(str(user_id)),
                contextual_telemetry_id=uuid.UUID(created_contextual_telemetry_id),
                behavioral_telemetry_id=uuid.UUID(created_behavioral_telemetry_id),
                context_id=uuid.UUID(created_context_id),
                session_id=uuid.UUID(session_token),
                project_id=uuid.UUID(project_token),
                chat_id=chat_completion_request.chat_id,
                total_serving_time=total_serving_time,
                server_version_id=app.get_config().server_version_id,
                web_enabled=chat_completion_request.web_enabled,
            ).dict(),
            created_query_id,
        )

        # Chain tasks
        chain(
            group(add_context_task, add_telemetry_task),
            add_chat_task,
            add_chat_query_task,
            group(*add_generation_tasks),
        ).apply_async(queue="db")
        t7 = time.perf_counter()
        logging.info(f"Celery task prep and queuing took {(t7 - t6) * 1000:.2f}ms")

        overall_end = time.perf_counter()
        logging.info(
            f"TOTAL serving time: {(overall_end - overall_start) * 1000:.2f}ms"
        )

        return JsonResponseWithStatus(
            status_code=200,
            content=ChatHistoryResponse(
                chat_id=chat_completion_request.chat_id,
                # either include the found title or put the first 3 words of the first chat completion as title with ...
                title=(
                    title_found
                    or (
                        chat_completion_request.context.prefix.split(" ")[0:3] + ["..."]
                    )[0]
                    if chat_completions
                    else "Chat History"
                ),
                history=[
                    ChatHistoryItem(
                        user_message=ChatMessageItem(
                            role=ChatMessageRole.USER,
                            content=chat_completion_request.context.prefix,
                            timestamp=datetime.fromtimestamp(overall_start),
                            meta_query_id=uuid.UUID(created_query_id),
                        ),
                        assistant_responses=chat_completions,
                    )
                ],
            ),
        )

    except Exception as e:
        logging.error(f"Error processing chat completion request: {str(e)}")
        db_auth.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=GenerateChatCompletionsError(),
        )


def get_chat_history_response(db, chat_id, user_id):
    """Helper function to build chat history response"""
    # Get chat metadata
    chat = crud.get_chat_by_id(db, chat_id)
    if not chat:
        return None

    # Check if user has access to this chat
    if str(chat.user_id) != user_id:
        return None

    # Get chat history
    history_data = crud.get_chat_history(db, chat_id)

    # Build response
    history_items = []
    for meta_query, context, generations in history_data:
        # Extract user message from context
        # For chat, we store the user message in the prefix field
        user_message = ChatMessageItem(
            role=ChatMessageRole.USER,
            content=context.prefix,
            timestamp=meta_query.timestamp,
            meta_query_id=meta_query.meta_query_id,
        )

        # Process assistant responses
        assistant_responses = []
        for generation in generations:
            model = crud.get_model_by_id(db, int(str(generation.model_id)))

            if not model:
                assistant_responses.append(
                    ChatCompletionErrorItem(
                        model_name=f"Model ID: {generation.model_id}"
                    )
                )
            else:
                assistant_responses.append(
                    ChatCompletionItem(
                        model_id=int(str(generation.model_id)),
                        model_name=str(model.model_name),
                        completion=str(generation.completion),
                        generation_time=int(str(generation.generation_time)),
                        confidence=float(str(generation.confidence)),
                        was_accepted=bool(generation.was_accepted),
                    )
                )

        # Add to history
        history_items.append(
            ChatHistoryItem(
                user_message=user_message, assistant_responses=assistant_responses
            )
        )

    return ChatHistoryResponse(chat_id=chat_id, title=chat.title, history=history_items)
