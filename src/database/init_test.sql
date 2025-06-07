BEGIN TRANSACTION;

-- Config table
CREATE TABLE IF NOT EXISTS public.config
(
    config_id BIGSERIAL PRIMARY KEY,
    config_data TEXT NOT NULL
);

-- User table with config reference and preferences
CREATE TABLE IF NOT EXISTS public."user"
(
    user_id uuid NOT NULL PRIMARY KEY,
    joined_at timestamp with time zone NOT NULL,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR NOT NULL,
    password VARCHAR NOT NULL,
    is_oauth_signup BOOLEAN DEFAULT FALSE,
    verified BOOLEAN DEFAULT FALSE,
    config_id BIGINT NOT NULL,
    preference TEXT,
    auth_token uuid NULL
);

-- Model name table with instruction tuning flag
CREATE TABLE IF NOT EXISTS public.model_name
(
    model_id BIGSERIAL PRIMARY KEY,
    model_name text NOT NULL,
    is_instruction_tuned BOOLEAN NOT NULL DEFAULT FALSE
);

-- Plugin version table
CREATE TABLE IF NOT EXISTS public.plugin_version
(
    version_id BIGSERIAL PRIMARY KEY,
    version_name text NOT NULL,
    ide_type text NOT NULL,
    description text
);

-- Trigger type table
CREATE TABLE IF NOT EXISTS public.trigger_type
(
    trigger_type_id BIGSERIAL PRIMARY KEY,
    trigger_type_name text NOT NULL
);

-- Programming language table
CREATE TABLE IF NOT EXISTS public.programming_language
(
    language_id BIGSERIAL PRIMARY KEY,
    language_name text NOT NULL
);

-- Context table with selected text
CREATE TABLE IF NOT EXISTS public.context
(
    context_id uuid NOT NULL PRIMARY KEY,
    prefix text,
    suffix text,
    file_name text,
    selected_text text
);

-- Contextual telemetry table
CREATE TABLE IF NOT EXISTS public.contextual_telemetry
(
    contextual_telemetry_id uuid NOT NULL PRIMARY KEY,
    version_id BIGINT NOT NULL,
    trigger_type_id BIGINT NOT NULL,
    language_id BIGINT NOT NULL,
    file_path text,
    caret_line integer,
    document_char_length integer,
    relative_document_position double precision
);

-- Behavioral telemetry table
CREATE TABLE IF NOT EXISTS public.behavioral_telemetry
(
    behavioral_telemetry_id uuid NOT NULL PRIMARY KEY,
    time_since_last_shown BIGINT,
    time_since_last_accepted BIGINT,
    typing_speed double precision
);

-- Project table
CREATE TABLE IF NOT EXISTS public.project
(
    project_id uuid NOT NULL PRIMARY KEY,
    project_name VARCHAR NOT NULL,
    multi_file_contexts text DEFAULT '{}',
    multi_file_context_changes text DEFAULT '{}',
    created_at timestamp with time zone NOT NULL
);

-- Project users junction table
CREATE TABLE IF NOT EXISTS public.project_users
(
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    joined_at timestamp with time zone NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

-- Session table (individual user sessions within projects)
CREATE TABLE IF NOT EXISTS public.session
(
    session_id uuid NOT NULL PRIMARY KEY,
    user_id uuid NOT NULL,
    project_id uuid NOT NULL,
    start_time timestamp with time zone NOT NULL,
    end_time timestamp with time zone
);

-- Chat table
CREATE TABLE IF NOT EXISTS public.chat
(
    chat_id uuid NOT NULL PRIMARY KEY,
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    title VARCHAR NOT NULL,
    created_at timestamp with time zone NOT NULL
);

-- MetaQuery table (parent for chat and completion queries)
CREATE TABLE IF NOT EXISTS public.meta_query
(
    meta_query_id uuid NOT NULL PRIMARY KEY,
    user_id uuid,
    contextual_telemetry_id uuid NOT NULL,
    behavioral_telemetry_id uuid NOT NULL,
    context_id uuid NOT NULL,
    session_id uuid NOT NULL,
    project_id uuid NOT NULL,
    multi_file_context_changes_indexes text DEFAULT '{}',
    "timestamp" timestamp with time zone NOT NULL,
    total_serving_time integer,
    server_version_id BIGINT,
    query_type VARCHAR NOT NULL CHECK (query_type IN ('chat', 'completion'))
);

-- CompletionQuery table (inherits from meta_query)
CREATE TABLE IF NOT EXISTS public.completion_query
(
    meta_query_id uuid NOT NULL PRIMARY KEY
);

-- ChatQuery table (inherits from meta_query)
CREATE TABLE IF NOT EXISTS public.chat_query
(
    meta_query_id uuid NOT NULL PRIMARY KEY,
    chat_id uuid NOT NULL,
    web_enabled BOOLEAN NOT NULL DEFAULT FALSE
);

-- Had generation table (now references meta_query)
CREATE TABLE IF NOT EXISTS public.had_generation
(
    meta_query_id uuid NOT NULL,
    model_id BIGINT NOT NULL,
    completion text NOT NULL,
    generation_time integer NOT NULL,
    shown_at timestamp with time zone[] NOT NULL,
    was_accepted boolean NOT NULL,
    confidence double precision NOT NULL,
    logprobs double precision[] NOT NULL,
    PRIMARY KEY (meta_query_id, model_id)
);

-- Ground truth table (now references completion_query only)
CREATE TABLE IF NOT EXISTS public.ground_truth
(
    completion_query_id uuid NOT NULL,
    truth_timestamp timestamp with time zone NOT NULL,
    ground_truth text NOT NULL,
    PRIMARY KEY (completion_query_id, truth_timestamp)
);

-- Foreign Key Constraints
ALTER TABLE public."user"
    ADD CONSTRAINT fk_user_config FOREIGN KEY (config_id)
    REFERENCES public.config (config_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.contextual_telemetry
    ADD CONSTRAINT fk_ctxt_telemetry_version FOREIGN KEY (version_id)
    REFERENCES public.plugin_version (version_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.contextual_telemetry
    ADD CONSTRAINT fk_ctxt_telemetry_trigger_type FOREIGN KEY (trigger_type_id)
    REFERENCES public.trigger_type (trigger_type_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.contextual_telemetry
    ADD CONSTRAINT fk_ctxt_telemetry_language FOREIGN KEY (language_id)
    REFERENCES public.programming_language (language_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.project_users
    ADD CONSTRAINT fk_project_users_project FOREIGN KEY (project_id)
    REFERENCES public.project (project_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.project_users
    ADD CONSTRAINT fk_project_users_user FOREIGN KEY (user_id)
    REFERENCES public."user" (user_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.session
    ADD CONSTRAINT fk_session_user FOREIGN KEY (user_id)
    REFERENCES public."user" (user_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.session
    ADD CONSTRAINT fk_session_project FOREIGN KEY (project_id)
    REFERENCES public.project (project_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.chat
    ADD CONSTRAINT fk_chat_project FOREIGN KEY (project_id)
    REFERENCES public.project (project_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.chat
    ADD CONSTRAINT fk_chat_user FOREIGN KEY (user_id)
    REFERENCES public."user" (user_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.meta_query
    ADD CONSTRAINT fk_meta_query_user FOREIGN KEY (user_id)
    REFERENCES public."user" (user_id)
    ON UPDATE NO ACTION
    ON DELETE SET NULL;

ALTER TABLE public.meta_query
    ADD CONSTRAINT fk_meta_query_contextual_telemetry FOREIGN KEY (contextual_telemetry_id)
    REFERENCES public.contextual_telemetry (contextual_telemetry_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.meta_query
    ADD CONSTRAINT fk_meta_query_behavioral_telemetry FOREIGN KEY (behavioral_telemetry_id)
    REFERENCES public.behavioral_telemetry (behavioral_telemetry_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.meta_query
    ADD CONSTRAINT fk_meta_query_context FOREIGN KEY (context_id)
    REFERENCES public.context (context_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.meta_query
    ADD CONSTRAINT fk_meta_query_project FOREIGN KEY (project_id)
    REFERENCES public.project (project_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.meta_query
    ADD CONSTRAINT fk_meta_query_session FOREIGN KEY (session_id)
    REFERENCES public.session (session_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.completion_query
    ADD CONSTRAINT fk_completion_query_meta_query FOREIGN KEY (meta_query_id)
    REFERENCES public.meta_query (meta_query_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.chat_query
    ADD CONSTRAINT fk_chat_query_meta_query FOREIGN KEY (meta_query_id)
    REFERENCES public.meta_query (meta_query_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.chat_query
    ADD CONSTRAINT fk_chat_query_chat FOREIGN KEY (chat_id)
    REFERENCES public.chat (chat_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.had_generation
    ADD CONSTRAINT fk_had_generation_meta_query FOREIGN KEY (meta_query_id)
    REFERENCES public.meta_query (meta_query_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.had_generation
    ADD CONSTRAINT fk_had_generation_model FOREIGN KEY (model_id)
    REFERENCES public.model_name (model_id)
    ON UPDATE NO ACTION
    ON DELETE NO ACTION;

ALTER TABLE public.ground_truth
    ADD CONSTRAINT fk_ground_truth_completion_query FOREIGN KEY (completion_query_id)
    REFERENCES public.completion_query (meta_query_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_user_email ON public."user" (email);
CREATE INDEX IF NOT EXISTS idx_user_config_id ON public."user" (config_id);

CREATE INDEX IF NOT EXISTS idx_ctxt_telemetry_version_id ON public.contextual_telemetry (version_id);
CREATE INDEX IF NOT EXISTS idx_ctxt_telemetry_trigger_type_id ON public.contextual_telemetry (trigger_type_id);
CREATE INDEX IF NOT EXISTS idx_ctxt_telemetry_language_id ON public.contextual_telemetry (language_id);

CREATE INDEX IF NOT EXISTS idx_project_users_project_id ON public.project_users (project_id);
CREATE INDEX IF NOT EXISTS idx_project_users_user_id ON public.project_users (user_id);

CREATE INDEX IF NOT EXISTS idx_session_user_id ON public.session (user_id);
CREATE INDEX IF NOT EXISTS idx_session_project_id ON public.session (project_id);

CREATE INDEX IF NOT EXISTS idx_chat_project_id ON public.chat (project_id);
CREATE INDEX IF NOT EXISTS idx_chat_user_id ON public.chat (user_id);

CREATE INDEX IF NOT EXISTS idx_meta_query_user_id ON public.meta_query (user_id);
CREATE INDEX IF NOT EXISTS idx_meta_query_project_id ON public.meta_query (project_id);
CREATE INDEX IF NOT EXISTS idx_meta_query_session_id ON public.meta_query (session_id);
CREATE INDEX IF NOT EXISTS idx_meta_query_type ON public.meta_query (query_type);

CREATE INDEX IF NOT EXISTS idx_chat_query_chat_id ON public.chat_query (chat_id);

CREATE INDEX IF NOT EXISTS idx_had_generation_meta_query_model ON public.had_generation (meta_query_id, model_id);

CREATE INDEX IF NOT EXISTS idx_ground_truth_completion_query_timestamp ON public.ground_truth (completion_query_id, truth_timestamp);

-- Insert default data
INSERT INTO public.config (config_data) VALUES ('{"default": true, "version": "1.0"}');

INSERT INTO public.model_name (model_name, is_instruction_tuned) VALUES
    ('deepseek-ai/deepseek-coder-1.3b-base', FALSE),
    ('bigcode/starcoder2-3b', FALSE),
    ('gpt-4-turbo', TRUE),
    ('claude-3-sonnet', TRUE);

INSERT INTO public.programming_language (language_name) VALUES
    ('plaintext'), ('code-text-binary'), ('Log'), ('log'), ('scminput'), ('bat'),
    ('clojure'), ('coffeescript'), ('jsonc'), ('json'), ('c'), ('cpp'), ('cuda-cpp'),
    ('csharp'), ('css'), ('dart'), ('diff'), ('dockerfile'), ('ignore'), ('fsharp'),
    ('git-commit'), ('git-rebase'), ('go'), ('groovy'), ('handlebars'), ('hlsl'),
    ('html'), ('ini'), ('properties'), ('java'), ('javascriptreact'), ('javascript'),
    ('jsx-tags'), ('jsonl'), ('snippets'), ('julia'), ('juliamarkdown'), ('tex'),
    ('latex'), ('bibtex'), ('cpp_embedded_latex'), ('markdown_latex_combined'),
    ('less'), ('lua'), ('makefile'), ('markdown'), ('markdown-math'), ('wat'),
    ('objective-c'), ('objective-cpp'), ('perl'), ('raku'), ('php'), ('powershell'),
    ('jade'), ('python'), ('r'), ('razor'), ('restructuredtext'), ('ruby'), ('rust'),
    ('scss'), ('search-result'), ('shaderlab'), ('shellscript'), ('sql'), ('swift'),
    ('typescript'), ('typescriptreact'), ('vb'), ('xml'), ('xsl'), ('dockercompose'),
    ('yaml'), ('doctex'), ('bibtex-style'), ('latex-expl3'), ('pweave'), ('jlweave'),
    ('rsweave'), ('csv'), ('tsv'), ('jinja'), ('pip-requirements'), ('toml'), ('raw'),
    ('ssh_config'), ('Vimscript');

INSERT INTO public.trigger_type (trigger_type_name) VALUES ('manual'), ('auto'), ('idle');

INSERT INTO public.plugin_version (version_name, ide_type, description) VALUES
    ('0.0.1j', 'JetBrains', 'the mvp version of the plugin'),
    ('0.0.1v', 'VSCode', 'the mvp version of the plugin'),
    ('0.1.0j', 'JetBrains', 'enhanced version with chat support'),
    ('0.1.0v', 'VSCode', 'enhanced version with chat support');

COMMIT;