"""mini-swe-agent model class for OpenCode (Zen/Go) endpoints.

OpenCode rejects requests without ``x-opencode-session`` and asks for a stable id
per conversation. mini-swe-agent builds one model per task instance, so a fresh
id per instance gives one session per task. Loaded by ``mini-extra`` through
``--model-class opencode_model.OpenCodeModel`` with this folder on PYTHONPATH;
it imports minisweagent, so it only runs in the baseline's own venv.
"""

import uuid

from minisweagent.models.litellm_model import LitellmModel


class OpenCodeModel(LitellmModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        headers = dict(self.config.model_kwargs.get("extra_headers") or {})
        headers["x-opencode-session"] = f"msa-{uuid.uuid4().hex}"
        self.config.model_kwargs = {**self.config.model_kwargs, "extra_headers": headers}
