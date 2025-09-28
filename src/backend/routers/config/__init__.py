from typing import Any, Optional, Union, List
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from App import App
import database.crud as crud
from backend.routers.analytics.auth_utils import require_admin, AuthenticatedUser
from backend.Responses import JsonResponseWithStatus

router = APIRouter()


class ConfigCreate(BaseModel):
    config_data: Union[dict, str]


class ConfigUpdate(BaseModel):
    config_data: Union[dict, str]


def _ensure_json_string(data: Union[dict, str]) -> str:
    if isinstance(data, str):
        # If it's already a string, try to normalize JSON, otherwise keep as-is
        try:
            parsed = json.loads(data)
            return json.dumps(parsed)
        except Exception:
            return data
    return json.dumps(data)


def _maybe_parse_json(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s


@router.get("/", summary="List all configs")
def list_configs(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        rows = crud.get_all_configs(db)
        items = [
            {"config_id": int(r.config_id), "config_data": _maybe_parse_json(r.config_data)}
            for r in rows
        ]
        return JsonResponseWithStatus(status_code=200, content={"configs": items})
    finally:
        db.close()


@router.get("/{config_id}", summary="Get a config by id")
def get_config(
    config_id: int,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        row = crud.get_config_by_id(db, config_id)
        if not row:
            raise HTTPException(status_code=404, detail="Config not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"config_id": int(row.config_id), "config_data": _maybe_parse_json(row.config_data)},
        )
    finally:
        db.close()


@router.post("/", summary="Create a new config")
def create_config(
    payload: ConfigCreate,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        json_str = _ensure_json_string(payload.config_data)
        created = crud.create_config(db, crud.Queries.CreateConfig(config_data=json_str))
        return JsonResponseWithStatus(
            status_code=201,
            content={"config_id": int(created.config_id), "config_data": _maybe_parse_json(created.config_data)},
        )
    finally:
        db.close()


@router.put("/{config_id}", summary="Update a config")
def update_config(
    config_id: int,
    payload: ConfigUpdate,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        json_str = _ensure_json_string(payload.config_data)
        updated = crud.update_config(db, config_id, json_str)
        if not updated:
            raise HTTPException(status_code=404, detail="Config not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"config_id": int(updated.config_id), "config_data": _maybe_parse_json(updated.config_data)},
        )
    finally:
        db.close()


@router.delete("/{config_id}", summary="Delete a config")
def delete_config(
    config_id: int,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        ok = crud.delete_config(db, config_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Config not found")
        return JsonResponseWithStatus(status_code=200, content={"deleted": True, "config_id": int(config_id)})
    except Exception as e:
        return JsonResponseWithStatus(status_code=500, content={"error": str(e)})
    finally:
        db.close()


@router.get("/languages", summary="List supported programming languages")
def list_languages(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        langs = crud.get_all_programming_languages(db)
        items = [{"language_id": int(l.language_id), "language_name": l.language_name} for l in langs]
        # Also provide as a mapping name -> id for convenience
        mapping = {l["language_name"]: l["language_id"] for l in items}
        return JsonResponseWithStatus(status_code=200, content={"languages": items, "mapping": mapping})
    finally:
        db.close()


@router.get("/models", summary="List available models in database")
def list_models(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        models = crud.get_all_model_names(db)
        items = [{"model_id": int(m.model_id), "model_name": m.model_name} for m in models]
        return JsonResponseWithStatus(status_code=200, content={"models": items})
    finally:
        db.close()


@router.get("/models/validate", summary="Validate a Hugging Face model name")
def validate_hf_model(
    name: str = Query(..., description="Hugging Face repo id, e.g. org/model"),
    current_user: AuthenticatedUser = Depends(require_admin),
):
    if not name or "/" not in name:
        # HF models are generally in org/model format; allow single segment too but warn
        candidate = name.strip()
    else:
        candidate = name.strip()

    # Use httpx to check if the page exists
    url = f"https://huggingface.co/{candidate}"
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=5.0)
        exists = resp.status_code == 200
        return JsonResponseWithStatus(status_code=200, content={"name": candidate, "exists": exists, "status_code": resp.status_code})
    except Exception as e:
        # If network error, return a soft failure so UI can warn but not block
        return JsonResponseWithStatus(status_code=200, content={"name": candidate, "exists": False, "error": str(e)})
