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
CREATE TABLE IF NOT EXISTS public.session (
    session_id uuid PRIMARY KEY,
    user_id uuid NULL,
    start_time timestamp with time zone NOT NULL,
    end_time timestamp with time zone
);

CREATE TABLE IF NOT EXISTS public.session_projects (
    session_id uuid NOT NULL,
    project_id uuid NOT NULL,
    PRIMARY KEY (session_id, project_id)
);

-- Chat table
CREATE TABLE IF NOT EXISTS public.chat
(
    chat_id uuid NOT NULL PRIMARY KEY,
    project_id uuid NOT NULL,
    user_id uuid NULL,
    title VARCHAR NOT NULL,
    created_at timestamp with time zone NOT NULL
);

-- MetaQuery table (parent for chat and completion queries)
CREATE TABLE IF NOT EXISTS public.meta_query
(
    meta_query_id uuid NOT NULL PRIMARY KEY,
    user_id uuid,
    contextual_telemetry_id uuid NULL,
    behavioral_telemetry_id uuid NULL,
    context_id uuid NULL,
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
    ON DELETE SET NULL;

ALTER TABLE public.chat
    ADD CONSTRAINT fk_chat_project FOREIGN KEY (project_id)
    REFERENCES public.project (project_id)
    ON UPDATE NO ACTION
    ON DELETE CASCADE;

ALTER TABLE public.chat
    ADD CONSTRAINT fk_chat_user FOREIGN KEY (user_id)
    REFERENCES public."user" (user_id)
    ON UPDATE NO ACTION
    ON DELETE SET NULL;

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
    
ALTER TABLE public.session_projects
    ADD CONSTRAINT fk_session
    FOREIGN KEY (session_id)
    REFERENCES public.session(session_id)
    ON DELETE CASCADE;

ALTER TABLE public.session_projects
    ADD CONSTRAINT fk_project
    FOREIGN KEY (project_id)
    REFERENCES public.project(project_id)
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
CREATE INDEX IF NOT EXISTS idx_session_projects_session_id ON public.session_projects (session_id);
CREATE INDEX IF NOT EXISTS idx_session_projects_project_id ON public.session_projects (project_id);

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
INSERT INTO public.config (config_data) VALUES ('config {
  // module configuration
  modules {
    // List of available modules
    available = [
      {
        id = "BehavioralTelemetryAggregator"
        class = "me.code4me.services.modules.aggregators.BaseBehavioralTelemetryAggregator"
        name = "Behavioral Telemetry Aggregator"
        type = "aggregator"
        description = "Module for aggregating behavioral telemetry data"
        enabled = true
        // Example of submodules
        submodules = [
          {
            id = "TimeSinceLastAcceptedCompletion"
            class = "me.code4me.services.modules.telemetry.behavioral.TimeSinceLastAcceptedCompletion"
            name = "Time Since Last Accepted Completion"
            type = "telemetry"
            description = "Calculates the time since the last accepted completion"
            enabled = true
          },
          {
            id = "TimeSinceLastShownCompletion"
            class = "me.code4me.services.modules.telemetry.behavioral.TimeSinceLastShownCompletion"
            name = "Time Since Last Shown Completion"
            type = "telemetry"
            description = "Calculates the time since the last shown completion"
            enabled = true
          },
          {
            id = "TypingSpeed"
            class = "me.code4me.services.modules.telemetry.behavioral.TypingSpeed"
            name = "Typing Speed"
            type = "telemetry"
            description = "Calculates the typing speed of the user"
            enabled = true
          }
        ]
        // Example of dependencies
        dependencies = [
          {
            moduleId = "TimeSinceLastAcceptedCompletion"
            isHard = false
          },
          {
            moduleId = "TimeSinceLastShownCompletion"
            isHard = false
          },
          {
            moduleId = "TypingSpeed"
            isHard = false
          }
        ]
      },

      {
          id = "ContextualTelemetryAggregator"
          class = "me.code4me.services.modules.aggregators.BaseContextualTelemetryAggregator"
          name = "Contextual Telemetry Aggregator"
          type = "aggregator"
          description = "Module for aggregating contextual telemetry data"
          enabled = true
          // Example of submodules
          submodules = [
          {
              id = "EditorContextRetrievalModule"
              class = "me.code4me.services.modules.telemetry.contextual.EditorContextRetrievalModule"
              name = "Editor Context Retrieval Module"
              type = "telemetry"
              description = "Retrieves context from the editor"
              enabled = true
          }
          ]
          // Example of dependencies
          dependencies = [
          {
              moduleId = "EditorContextRetrievalModule"
              isHard = false
          }
          ]
      },

      {
        id = "contextAggregator"
        class = "me.code4me.services.modules.aggregators.BaseContextAggregator"
        name = "Context Aggregator"
        type = "aggregator"
        description = "Module for aggregating context data"
        enabled = true
        // Example of submodules
        submodules = [
          {
            id = "FileContextRetrievalModule"
            class = "me.code4me.services.modules.context.FileContextRetrievalModule"
            name = "File Context Retrieval Module"
            type = "context"
            description = "Retrieves context from the file"
            enabled = true
          },
          {
            id = "MultiFileContextRetrievalModule"
            class = "me.code4me.services.modules.context.MultiFileContextRetrievalModule"
            name = "Multi File Context Retrieval Module"
            type = "context"
            description = "Retrieves context from multiple files"
            enabled = true
          }
        ]
        // Example of dependencies
        dependencies = [
          {
            moduleId = "FileContextRetrievalModule"
            isHard = true  // This is a hard dependency
          },
          {
            moduleId = "MultiFileContextRetrievalModule"
            isHard = false
          }
        ]
      },

      {
        id = "BaseModelAggregator"
        class = "me.code4me.services.modules.aggregators.BaseModelAggregator"
        name = "Model Aggregator"
        type = "aggregator"
        description = "Module for aggregating model data"
        enabled = true
        submodules = [
          {
            id = "ChatModel"
            class = "me.code4me.services.modules.model.ChatModel"
            name = "Model Selection and Settings for Chat"
            type = "model"
            description = "Module for selecting the appropriate model for chat interactions"
            enabled = true
          }
          {
            id = "CompletionModel"
            class = "me.code4me.services.modules.model.CompletionModel"
            name = "Model Selection and Settings for Code Completion"
            type = "model"
            description = "Module for selecting the appropriate model for code generation"
            enabled = true
          }
        ]

        dependencies = [
          {
            moduleId = "ChatModel"
            isHard = true
          },
          {
            moduleId = "CompletionModel"
            isHard = true
          }
        ]
      },

      {
        id = "AfterInsertionAggregator"
        class = "me.code4me.services.modules.aggregators.BaseAfterInsertionAggregator"
        name = "After Insertion Aggregator"
        type = "aggregator"
        description = "Module for aggregating after code insertion activities"
        enabled = true
        submodules = [
          {
            id = "GroundTruth"
            class = "me.code4me.services.modules.afterInsertion.GroundTruth"
            name = "Ground Truth Module"
            type = "afterInsertion"
            description = "Module for handling actions after code insertion"
            enabled = true
          }
          {
            id = "AcceptanceFeedback"
            class = "me.code4me.services.modules.afterInsertion.AcceptanceFeedback"
            name = "Code Insertion Acceptance Feedback Module"
            type = "afterInsertion"
            description = "Module for sending acceptance feedback after code insertion"
            enabled = true
          }
        ]
        dependencies = [
          {
            moduleId = "GroundTruth"
            isHard = false
          }
          {
            moduleId = "AcceptanceFeedback"
            isHard = false
          }
        ]
      }

    ]

    // Module categories
    categories = {
      behavioralTelemetry = {
        path = "me.code4me.services.modules.telemetry.behavioral"
        description = "Modules for behavioral telemetry collection"
      }
      contextualTelemetry = {
        path = "me.code4me.services.modules.telemetry.contextual"
        description = "Modules for contextual telemetry collection"
      }
      context = {
        path = "me.code4me.services.modules.context"
        description = "Modules for context retrieval"
      }
      aggregators = {
        path = "me.code4me.services.modules.aggregators"
        description = "Modules for data aggregation"
      }
      models = {
        path = "me.code4me.services.modules.model"
        description = "Modules for model selection and settings"
      }
      afterInsertion = {
        path = "me.code4me.services.modules.afterInsertion"
        description = "Modules for actions after code insertion"
      }
    }
  }
  // Server Settings
  server {
    host = "http://127.0.0.1"
    port = 8008
    contextPath = ""
    timeout = 5000
  }
  // Authentication Settings
  auth {
    google {
      clientId = "67736337656-un249ihklv5i93n033i1v46q12bfv5g2.apps.googleusercontent.com"
    }
  }
  // model configuration
  models {
    available = [
      {
        id = 1
        name = "deepseek-coder-1.3b"
        isChatModel = false
        isDefault = true
      }
      {
        id = 2,
        name = "starcoder2-3b"
        isChatModel = false
        isDefault = false
      }
      {
        id = 3
        name = "Ministral-8B-Instruct"
        isChatModel = true
        isDefault = true
      }
    ]
    systemPrompt = "You are a helpful assistant that provides information and answers questions to the best of your ability. Please respond in a clear and concise manner."
  }

  languages {
    "Oracle NetSuite" = 1,
    "GoPlusBuild" = 2,
    "HelmJSON" = 3,
    "USS" = 4,
    "UnityYaml" = 5,
    "Blade" = 6,
    "Xaml" = 7,
    "Meson" = 8,
    "Angular SVG template" = 9,
    "Pug (ex-Jade)" = 10,
    "Java" = 11,
    "DTD" = 12,
    "SQL" = 13,
    "LinkerScript" = 14,
    "Asp" = 15,
    "ClickHouse" = 16,
    "CMakeCache" = 17,
    "Chameleon template" = 18,
    "VB" = 19,
    ".ignore (IgnoreLang)" = 20,
    "Vue template" = 21,
    "UastContextLanguage" = 22,
    "EditorConfig" = 23,
    "XPath" = 24,
    "MariaDB" = 25,
    "MySQL based" = 26,
    "MongoJS" = 27,
    "Angular SVG template (17+)" = 28,
    "IBM Db2 LUW" = 29,
    "SCSS" = 30,
    "DotEnv" = 31,
    "Kotlin/Native Def" = 32,
    "JSONPath" = 33,
    "Microsoft SQL Server" = 34,
    "Flow JS" = 35,
    "Devicetree" = 36,
    "JSON5" = 37,
    "protobase" = 38,
    "Angular HTML template (17+)" = 39,
    "BladeHTML" = 40,
    "TypeScript" = 41,
    "Metadata JSON" = 42,
    "Greenplum" = 43,
    "Handlebars" = 44,
    "Oracle SQL*Plus" = 45,
    "JVM languages" = 46,
    "Python" = 47,
    "GoBuild" = 48,
    "VueTS" = 49,
    "JSON" = 50,
    "ASM" = 51,
    "VueExpr" = 52,
    "DynamoDB" = 53,
    "MSBuild" = 54,
    "C#" = 55,
    "Exasol" = 56,
    "Sybase ASE" = 57,
    "XML" = 58,
    ".gitignore (GitIgnore)" = 59,
    "vgo" = 60,
    "prototext" = 61,
    "Groovy" = 62,
    "ECMAScript 6" = 63,
    "TypeScript JSX" = 64,
    "CSS" = 65,
    "Config" = 66,
    "DjangoUrlPath" = 67,
    "Composer Log" = 68,
    "Jinja2" = 69,
    "Injectable PHP" = 70,
    "HelmTEXT" = 71,
    "PythonStub" = 72,
    "GoTag" = 73,
    "JSON Lines" = 74,
    "PyTypeHint" = 75,
    "Angular HTML template (18.1+)" = 76,
    "JSUnicodeRegexp" = 77,
    "Razor" = 78,
    "CMake" = 79,
    "HTML" = 80,
    "HtmlCompatible" = 81,
    "Go" = 82,
    "Angular2" = 83,
    "XHTML" = 84,
    "VueJS" = 85,
    "Apache Cassandra" = 86,
    "IBM Db2 iSeries" = 87,
    "SQL2016" = 88,
    "exclude (GitExclude)" = 89,
    "ShaderLab" = 90,
    ".hgignore (HgIgnore)" = 91,
    "Angular HTML template" = 92,
    "YouTrack" = 93,
    "Markdown" = 94,
    "WerfYAML" = 95,
    "RELAX-NG" = 96,
    "UXML" = 97,
    "Kotlin" = 98,
    "GDB" = 99,
    "Redis" = 100,
    "XPath2" = 101,
    "PostCSS" = 102,
    "Jupyter" = 103,
    "Twig" = 104,
    "JShell Snippet" = 105,
    "Go Template" = 106,
    "SQLite" = 107,
    "PyFunctionTypeComment" = 108,
    "Angular SVG template (18.1+)" = 109,
    "HTTP Request" = 110,
    "Scmp" = 111,
    "Plain text" = 112,
    "H2" = 113,
    "Gradle Declarative Configuration" = 114,
    "RuleSet" = 115,
    "plan9_x86" = 116,
    "WebSymbolsEnabledLanguage" = 117,
    "Properties" = 118,
    "Asxx" = 119,
    "Vertica" = 120,
    "JVM" = 121,
    "ModuleMap" = 122,
    "C/C++" = 123,
    "Resx" = 124,
    "Blazor" = 125,
    "HSQLDB" = 126,
    "Cookie" = 127,
    "Generic SQL" = 128,
    "XsdRegExp" = 129,
    "SlnLaunchLanguage" = 130,
    "Dockerfile" = 131,
    "MySQL" = 132,
    "IL" = 133,
    "HelmYAML" = 134,
    "Terminal Prompt" = 135,
    "Requirements" = 136,
    "Apache Spark" = 137,
    "HttpClientHandlerJavaScriptDialect" = 138,
    "textmate" = 139,
    "QML" = 140,
    "GoTime" = 141,
    "F#" = 142,
    "PostgreSQL" = 143,
    "Rest language" = 144,
    "protobuf" = 145,
    "Oracle" = 146,
    "Cgo" = 147,
    ".dockerignore (DockerIgnore)" = 148,
    "JQL" = 149,
    "Manifest" = 150,
    "LLDB" = 151,
    "Makefile" = 152,
    "RegExp" = 153,
    "GoFuzzCorpus" = 154,
    "Cython" = 155,
    "Sass" = 156,
    "TOML" = 157,
    "Shell Script" = 158,
    "Ini" = 159,
    "Less" = 160,
    "CockroachDB" = 161,
    "Apache Derby" = 162,
    "GoDebug" = 163,
    "SolutionFile" = 164,
    "YAML" = 165,
    "ActionScript" = 166,
    "JSRegexp" = 167,
    "T4" = 168,
    "JavaScript" = 169,
    "Gherkin" = 170,
    "TerminalOutput" = 171,
    "SVG" = 172,
    "SPI" = 173,
    "Doxygen" = 174,
    "Notebook" = 175,
    "Apache Hive" = 176,
    "GithubExpressionLanguage" = 177,
    "PythonRegExp" = 178,
    "unknown" = 179,
  }
}');

INSERT INTO public.model_name (model_name, is_instruction_tuned) VALUES
    ('deepseek-ai/deepseek-coder-1.3b-base', FALSE),
    ('bigcode/starcoder2-3b', FALSE),
    ('mistralai/Ministral-8B-Instruct-2410', TRUE);

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