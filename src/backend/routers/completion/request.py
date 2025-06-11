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
    CompletionPostResponse,
    ErrorResponse,
    GenerateCompletionsError,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)
from database import crud
from response_models import (
    CompletionErrorItem,
    ResponseCompletionItem,
    ResponseCompletionResponseData,
)
from utils import create_uuid, extract_secrets, redact_secrets

router = APIRouter()


@router.post(
    "",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "401": {
            "model": Union[
                InvalidOrExpiredAuthToken,
                InvalidOrExpiredSessionToken,
                InvalidOrExpiredProjectToken,
            ]
        },
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": GenerateCompletionsError},
    },
)
def request_completion(
    completion_request: Queries.RequestCompletion,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
    project_token: str = Cookie("project_token"),
) -> JsonResponseWithStatus:
    """
    Request code completions based on provided context.
    """
    overall_start = time.perf_counter()
    logging.info(f"Completion request: {completion_request.dict()}")

    db_auth = app.get_db_session()
    redis_manager = app.get_redis_manager()
    completion_models = app.get_completion_models()
    config = app.get_config()

    try:
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
        # Redact secrets from context
        secrets = extract_secrets(
            completion_request.context.prefix
            + "\n"
            + completion_request.context.suffix,
            file_name=str(completion_request.context.file_name),
        )
        completion_request.context.prefix = redact_secrets(
            completion_request.context.prefix, secrets
        )
        completion_request.context.suffix = redact_secrets(
            completion_request.context.suffix, secrets
        )
        t2 = time.perf_counter()
        logging.info(f"Context secret redaction took {(t2 - t1) * 1000:.2f}ms")

        # Prepare tasks based on user preferences
        created_context_id = None
        add_context_task = None
        if completion_request.store_context:
            created_context_id = create_uuid()
            add_context_task = db_tasks.add_context_task.si(
                completion_request.context.dict(), created_context_id
            )
        created_contextual_telemetry_id = None
        add_contextual_telemetry_task = None
        if completion_request.store_contextual_telemetry:
            created_contextual_telemetry_id = create_uuid()
            add_contextual_telemetry_task = db_tasks.add_contextual_telemetry_task.si(
                completion_request.contextual_telemetry.dict(),
                created_contextual_telemetry_id,
            )
        created_behavioral_telemetry_id = None
        add_behavioral_telemetry_task = None
        if completion_request.store_behavioral_telemetry:
            created_behavioral_telemetry_id = create_uuid()
            add_behavioral_telemetry_task = db_tasks.add_behavioral_telemetry_task.si(
                completion_request.behavioral_telemetry.dict(),
                created_behavioral_telemetry_id,
            )

        t3 = time.perf_counter()
        logging.info(
            f"Preparing celery tasks based on user preferences took {(t3 - t2) * 1000:.2f}ms"
        )

        multi_file_contexts = project_info.get("multi_file_contexts", {})
        multi_file_context_changes = project_info.get("multi_file_context_changes", {})

        created_query_id = create_uuid()

        if multi_file_contexts:
            other_files_context = []
            for file_name, context in multi_file_contexts.items():
                if file_name != completion_request.context.file_name and (
                    completion_request.context.context_files == ["*"]
                    or file_name in completion_request.context.context_files
                ):
                    joined_context = "\n".join(context).strip()
                    other_files_context.append(f"#{file_name}\n{joined_context}")

            if other_files_context:
                completion_request.context.prefix = (
                    "Other files context:\n"
                    + "\n".join(other_files_context)
                    + "\n\n"
                    + (completion_request.context.prefix or "")
                )
        multi_file_context_changes_indexes = {}
        if multi_file_context_changes:
            multi_file_context_changes_indexes = {
                file_name: len(changes)
                for file_name, changes in multi_file_context_changes.items()
            }
        t4 = time.perf_counter()
        logging.info(f"Multi-file context processing took {(t4 - t3) * 1000:.2f}ms")
        completions = []
        add_generation_tasks = []

        def handle_model_completion(model_id):
            local_t0 = time.perf_counter()
            model = crud.get_model_by_id(db_auth, model_id)
            if not model:
                return CompletionErrorItem(model_name=f"Model ID: {model_id}")
            local_t1 = time.perf_counter()

            completion_model = completion_models.get_model(
                model_name=str(model.model_name),
                prompt_template=completion.Template.PREFIX_SUFFIX,
            )

            if completion_model is None:
                return CompletionErrorItem(model_name=model)
            local_t2 = time.perf_counter()
            completion_result = completion_model.invoke(
                {
                    "prefix": completion_request.context.prefix,
                    "suffix": completion_request.context.suffix,
                }
            )
            local_t3 = time.perf_counter()

            logging.info(
                f"Model {model_id} timing: "
                f"DB={((local_t1 - local_t0) * 1000):.2f}ms, "
                f"GetModel={((local_t2 - local_t1) * 1000):.2f}ms, "
                f"Invoke={((local_t3 - local_t2) * 1000):.2f}ms"
            )
            completion_result["completion"] = redact_secrets(
                completion_request["completion"],
                extract_secrets(completion_request["completion"], file_name),
            )

            # Used for test mode
            # completion_result = {"completion": "test", "generation_time": 100, "confidence": 0.8, "logprobs": [0.2, 0.5]}

            add_generation_task = db_tasks.add_generation_task.si(
                Queries.CreateGeneration(
                    model_id=model_id,
                    completion=completion_result["completion"],
                    generation_time=completion_result["generation_time"],
                    shown_at=[datetime.now().isoformat()],
                    was_accepted=False,
                    confidence=completion_result["confidence"],
                    logprobs=completion_result["logprobs"],
                ).dict(),
                created_query_id,
            )
            add_generation_tasks.append(add_generation_task)

            return ResponseCompletionItem(
                model_id=model_id,
                model_name=str(model.model_name),
                completion=completion_result["completion"],
                generation_time=completion_result["generation_time"],
                confidence=completion_result["confidence"],
            )

        with ThreadPoolExecutor(max_workers=config.thread_pool_max_workers) as executor:
            future_to_model = {
                executor.submit(handle_model_completion, model_id): model_id
                for model_id in completion_request.model_ids
            }
            for future in as_completed(future_to_model):
                result = future.result()
                if result is not None:
                    completions.append(result)
        t5 = time.perf_counter()
        logging.info(f"Model completion (threaded) took {(t5 - t4) * 1000:.2f}ms")

        # Celery task prep
        add_query_task = db_tasks.add_completion_query_task.si(
            Queries.CreateCompletionQuery(
                user_id=uuid.UUID(str(user_id)),
                contextual_telemetry_id=(
                    uuid.UUID(created_contextual_telemetry_id)
                    if created_contextual_telemetry_id
                    else None
                ),
                behavioral_telemetry_id=(
                    uuid.UUID(created_behavioral_telemetry_id)
                    if created_behavioral_telemetry_id
                    else None
                ),
                context_id=(
                    uuid.UUID(created_context_id) if created_context_id else None
                ),
                session_id=uuid.UUID(session_token),
                project_id=uuid.UUID(project_token),
                multi_file_context_changes_indexes=multi_file_context_changes_indexes,
                total_serving_time=int((time.perf_counter() - overall_start) * 1000),
                server_version_id=app.get_config().server_version_id,
            ).dict(),
            created_query_id,
        )

        # Filter out None tasks
        pre_query_tasks = list(
            filter(
                None,
                [
                    add_context_task,
                    add_contextual_telemetry_task,
                    add_behavioral_telemetry_task,
                ],
            )
        )
        chain(
            group(*pre_query_tasks) if pre_query_tasks else None,
            add_query_task,
            group(*add_generation_tasks),
        ).apply_async(queue="db")
        t6 = time.perf_counter()
        logging.info(f"Celery task prep and queuing took {(t6 - t5) * 1000:.2f}ms")

        overall_end = time.perf_counter()
        logging.info(
            f"TOTAL serving time: {(overall_end - overall_start) * 1000:.2f}ms"
        )

        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionPostResponse(
                data=ResponseCompletionResponseData(
                    meta_query_id=uuid.UUID(created_query_id), completions=completions
                ),
            ),
        )

    except Exception as e:
        logging.error(f"Error processing completion request: {str(e)}")
        db_auth.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=GenerateCompletionsError(),
        )
