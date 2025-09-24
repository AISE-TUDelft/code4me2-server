#!/usr/bin/env python3
"""
Database Test Data Populator for Code4Me Analytics Platform

This script populates the database with realistic test data to demonstrate
the analytics and visualization features including:
- Users (admins and regular users)
- Multiple configurations for A/B testing  
- Realistic usage patterns over time
- Model performance with varying acceptance rates
- Programming language distribution
- Studies and configuration assignments
- Telemetry data

Run this script to generate test data for the analytics dashboard.
"""

import os
import sys
import random
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json

# Add src to path so we can import database modules
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

try:
    import psycopg2
    from faker import Faker
    import numpy as np
    from psycopg2.extras import RealDictCursor
except ImportError as e:
    print(f"Missing required packages. Please install with:")
    print("pip install psycopg2-binary faker numpy")
    sys.exit(1)

# Configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', 5432),
    'database': os.getenv('DB_NAME', 'code4meV2'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', 'postgres')
}

# Initialize faker for generating realistic data
fake = Faker()

# Test data configurations
NUM_USERS = 50  # Total users to create
NUM_ADMIN_USERS = 3  # Number of admin users
NUM_PROJECTS = 15  # Projects to create
NUM_SESSIONS = 200  # User sessions
NUM_QUERIES = 2000  # Meta queries (chat + completion)
DAYS_OF_DATA = 30  # Generate data for last N days

# Programming languages with realistic usage distribution
PROGRAMMING_LANGUAGES = [
    ("Python", 0.25), ("JavaScript", 0.20), ("TypeScript", 0.15),
    ("Java", 0.12), ("C++", 0.08), ("Go", 0.06), ("Rust", 0.04),
    ("C#", 0.03), ("PHP", 0.03), ("Ruby", 0.02), ("Kotlin", 0.02)
]

# IDE/Plugin versions
PLUGIN_VERSIONS = [
    ("1.0.0", "VSCode", "Latest stable release"),
    ("1.0.1", "VSCode", "Bug fix update"),
    ("1.0.0", "JetBrains", "IntelliJ IDEA support"),
    ("0.9.5", "VSCode", "Beta version"),
]

# Trigger types
TRIGGER_TYPES = ["manual", "auto", "idle"]

# Model configurations with different performance characteristics
MODEL_CONFIGS = [
    {
        "name": "deepseek-ai/deepseek-coder-1.3b-base",
        "is_instruction_tuned": False,
        "base_acceptance_rate": 0.65,
        "avg_generation_time": 150,
        "confidence_bias": 0.1  # Slightly overconfident
    },
    {
        "name": "bigcode/starcoder2-3b", 
        "is_instruction_tuned": False,
        "base_acceptance_rate": 0.72,
        "avg_generation_time": 200,
        "confidence_bias": -0.05  # Slightly underconfident
    },
    {
        "name": "mistralai/Ministral-8B-Instruct-2410",
        "is_instruction_tuned": True,
        "base_acceptance_rate": 0.58,
        "avg_generation_time": 350,
        "confidence_bias": 0.0  # Well calibrated
    },
    {
        "name": "JetBrains/Mellum-4b-base",
        "is_instruction_tuned": False,
        "base_acceptance_rate": 0.78,
        "avg_generation_time": 180,
        "confidence_bias": -0.1  # Conservative
    }
]

# Different configurations for A/B testing
CONFIGS_FOR_TESTING = [
    {
        "name": "Default Configuration",
        "config_data": json.dumps({
            "model_selection_strategy": "balanced",
            "max_suggestions": 3,
            "auto_trigger_delay": 500,
            "context_window": 1000
        })
    },
    {
        "name": "Fast Response Config", 
        "config_data": json.dumps({
            "model_selection_strategy": "speed_optimized",
            "max_suggestions": 2,
            "auto_trigger_delay": 200,
            "context_window": 500
        })
    },
    {
        "name": "High Quality Config",
        "config_data": json.dumps({
            "model_selection_strategy": "quality_optimized", 
            "max_suggestions": 5,
            "auto_trigger_delay": 1000,
            "context_window": 2000
        })
    },
    {
        "name": "Experimental Config",
        "config_data": json.dumps({
            "model_selection_strategy": "experimental",
            "max_suggestions": 4,
            "auto_trigger_delay": 750,
            "context_window": 1500,
            "enable_multi_model": True
        })
    }
]


class DatabasePopulator:
    def __init__(self):
        self.conn = None
        self.cursor = None
        self.created_data = {
            'users': [],
            'configs': [],
            'models': [],
            'languages': [],
            'trigger_types': [],
            'plugin_versions': [],
            'projects': [],
            'sessions': [],
            'studies': []
        }

    def connect(self):
        """Connect to the database"""
        try:
            self.conn = psycopg2.connect(**DB_CONFIG)
            self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            print(f"✅ Connected to database: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"❌ Failed to connect to database: {e}")
            sys.exit(1)

    def close(self):
        """Close database connection"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

    def clear_existing_data(self):
        """Clear existing test data (optional)"""
        print("🧹 Clearing existing test data...")
        
        # Delete in reverse dependency order
        tables_to_clear = [
            'ground_truth',
            'had_generation', 
            'chat_query',
            'completion_query',
            'meta_query',
            'config_assignment_history',
            'study',
            'session_projects',
            'session',
            'project_users',
            'project',
            'contextual_telemetry',
            'behavioral_telemetry',
            'context',
            # Keep reference data: model_name, programming_language, trigger_type, plugin_version
            # Only clear user-generated data
        ]
        
        for table in tables_to_clear:
            try:
                self.cursor.execute(f'TRUNCATE TABLE "{table}" CASCADE;')
                print(f"  Cleared {table}")
            except Exception as e:
                print(f"  ⚠️  Could not clear {table}: {e}")
        
        # Clear test users (keep config_id = 1 which is the default)
        self.cursor.execute('DELETE FROM "user" WHERE config_id > 1;')
        self.cursor.execute('DELETE FROM config WHERE config_id > 1;')
        
        self.conn.commit()
        print("✅ Existing test data cleared")

    def create_configs(self):
        """Create test configurations"""
        print("📋 Creating test configurations...")
        
        for config in CONFIGS_FOR_TESTING:
            self.cursor.execute(
                'INSERT INTO config (config_data) VALUES (%s) RETURNING config_id;',
                (config['config_data'],)
            )
            config_id = self.cursor.fetchone()['config_id']
            
            config['config_id'] = config_id
            self.created_data['configs'].append(config)
            print(f"  Created config {config_id}: {config['name']}")
        
        self.conn.commit()

    def create_users(self):
        """Create test users with admin and regular roles"""
        print("👥 Creating test users...")
        
        # Create admin users
        for i in range(NUM_ADMIN_USERS):
            user_id = str(uuid.uuid4())
            email = f"admin{i+1}@code4me.dev"
            name = f"Admin User {i+1}"
            
            # Assign random config for admins
            config = random.choice(self.created_data['configs'])
            
            self.cursor.execute('''
                INSERT INTO "user" (user_id, joined_at, email, name, password, 
                                   is_oauth_signup, verified, config_id, is_admin)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                user_id,
                fake.date_time_between(start_date='-90d', end_date='-30d'),
                email,
                name,
                fake.password(),
                False,
                True,
                config['config_id'],
                True  # Admin user
            ))
            
            self.created_data['users'].append({
                'user_id': user_id,
                'email': email, 
                'name': name,
                'is_admin': True,
                'config_id': config['config_id']
            })
            print(f"  Created admin: {email}")

        # Create regular users
        for i in range(NUM_USERS - NUM_ADMIN_USERS):
            user_id = str(uuid.uuid4())
            email = fake.email()
            name = fake.name()
            
            # Assign random config
            config = random.choice(self.created_data['configs'])
            
            self.cursor.execute('''
                INSERT INTO "user" (user_id, joined_at, email, name, password, 
                                   is_oauth_signup, verified, config_id, is_admin)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                user_id,
                fake.date_time_between(start_date='-60d', end_date='-1d'),
                email,
                name,
                fake.password(),
                random.choice([True, False]),
                random.choice([True, False]),  # Some unverified users
                config['config_id'],
                False  # Regular user
            ))
            
            self.created_data['users'].append({
                'user_id': user_id,
                'email': email,
                'name': name, 
                'is_admin': False,
                'config_id': config['config_id']
            })

        print(f"✅ Created {NUM_USERS} users ({NUM_ADMIN_USERS} admins)")
        self.conn.commit()

    def get_reference_data(self):
        """Get existing reference data from database"""
        print("📚 Loading reference data...")
        
        # Get models
        self.cursor.execute('SELECT * FROM model_name ORDER BY model_id;')
        self.created_data['models'] = list(self.cursor.fetchall())
        print(f"  Found {len(self.created_data['models'])} models")
        
        # Get programming languages  
        self.cursor.execute('SELECT * FROM programming_language ORDER BY language_id;')
        self.created_data['languages'] = list(self.cursor.fetchall())
        print(f"  Found {len(self.created_data['languages'])} languages")
        
        # Get trigger types
        self.cursor.execute('SELECT * FROM trigger_type ORDER BY trigger_type_id;')
        self.created_data['trigger_types'] = list(self.cursor.fetchall())
        print(f"  Found {len(self.created_data['trigger_types'])} trigger types")
        
        # Get plugin versions
        self.cursor.execute('SELECT * FROM plugin_version ORDER BY version_id;')
        self.created_data['plugin_versions'] = list(self.cursor.fetchall())
        print(f"  Found {len(self.created_data['plugin_versions'])} plugin versions")

    def create_projects(self):
        """Create test projects"""
        print("📁 Creating test projects...")
        
        for i in range(NUM_PROJECTS):
            project_id = str(uuid.uuid4())
            project_name = f"{fake.word().title()} {fake.word().title()}"
            
            self.cursor.execute('''
                INSERT INTO project (project_id, project_name, created_at)
                VALUES (%s, %s, %s)
            ''', (
                project_id,
                project_name,
                fake.date_time_between(start_date='-120d', end_date='-1d')
            ))
            
            # Add random users to project
            num_users_in_project = random.randint(1, min(8, len(self.created_data['users'])))
            project_users = random.sample(self.created_data['users'], num_users_in_project)
            
            for user in project_users:
                self.cursor.execute('''
                    INSERT INTO project_users (project_id, user_id, joined_at)
                    VALUES (%s, %s, %s)
                ''', (
                    project_id,
                    user['user_id'],
                    fake.date_time_between(start_date='-90d', end_date='-1d')
                ))
            
            self.created_data['projects'].append({
                'project_id': project_id,
                'project_name': project_name,
                'users': project_users
            })

        print(f"✅ Created {NUM_PROJECTS} projects")
        self.conn.commit()

    def create_sessions(self):
        """Create user sessions"""
        print("🔄 Creating user sessions...")
        
        for i in range(NUM_SESSIONS):
            session_id = str(uuid.uuid4())
            user = random.choice(self.created_data['users'])
            
            # Sessions should be within the last 30 days for recent activity
            start_time = fake.date_time_between(start_date='-30d', end_date='now')
            end_time = start_time + timedelta(minutes=random.randint(5, 180))
            
            self.cursor.execute('''
                INSERT INTO session (session_id, user_id, start_time, end_time)
                VALUES (%s, %s, %s, %s)
            ''', (session_id, user['user_id'], start_time, end_time))
            
            # Associate session with random projects that the user is part of
            user_projects = [p for p in self.created_data['projects'] 
                           if any(u['user_id'] == user['user_id'] for u in p['users'])]
            
            if user_projects:
                project = random.choice(user_projects)
                self.cursor.execute('''
                    INSERT INTO session_projects (session_id, project_id)
                    VALUES (%s, %s)
                ''', (session_id, project['project_id']))
            
            self.created_data['sessions'].append({
                'session_id': session_id,
                'user_id': user['user_id'],
                'start_time': start_time,
                'end_time': end_time
            })

        print(f"✅ Created {NUM_SESSIONS} sessions")
        self.conn.commit()

    def create_telemetry_and_queries(self):
        """Create telemetry data and meta queries with realistic patterns"""
        print("📊 Creating telemetry and query data...")

        # Weight languages by popularity
        language_weights = [weight for _, weight in PROGRAMMING_LANGUAGES]
        languages = [lang for lang, _ in PROGRAMMING_LANGUAGES]

        for i in range(NUM_QUERIES):
            # Select random session
            session = random.choice(self.created_data['sessions'])
            user = next(u for u in self.created_data['users'] if u['user_id'] == session['user_id'])

            # Find project for this session
            self.cursor.execute('''
                                SELECT project_id
                                FROM session_projects
                                WHERE session_id = %s LIMIT 1
                                ''', (session['session_id'],))
            project_result = self.cursor.fetchone()

            if not project_result:
                continue

            project_id = project_result['project_id']

            # Create contextual telemetry
            contextual_telemetry_id = str(uuid.uuid4())
            plugin_version = random.choice(self.created_data['plugin_versions'])
            trigger_type = random.choice(self.created_data['trigger_types'])

            # Choose language with realistic distribution
            language_name = np.random.choice(languages, p=language_weights)
            # Find language by name with fallback
            try:
                language = next(l for l in self.created_data['languages']
                                if l['language_name'] == language_name)
            except StopIteration:
                # Fallback to first language if not found
                # get a random langauge from the languages list
                language = random.choice(self.created_data['languages'])
                print(
                    f"⚠️  Warning: Language '{language_name}' not found in database, using {language['language_name']}")

            # Create realistic contextual data
            document_length = random.randint(100, 10000)
            caret_line = random.randint(1, max(10, document_length // 50))
            relative_position = random.uniform(0.1, 0.9)

            self.cursor.execute('''
                                INSERT INTO contextual_telemetry
                                (contextual_telemetry_id, version_id, trigger_type_id, language_id,
                                 file_path, caret_line, document_char_length, relative_document_position)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                ''', (
                                    contextual_telemetry_id,
                                    plugin_version['version_id'],
                                    trigger_type['trigger_type_id'],
                                    language['language_id'],
                                    f"/project/{fake.file_name(extension=self._get_file_extension(language_name))}",
                                    caret_line,
                                    document_length,
                                    relative_position
                                ))

            # Create behavioral telemetry
            behavioral_telemetry_id = str(uuid.uuid4())

            self.cursor.execute('''
                                INSERT INTO behavioral_telemetry
                                (behavioral_telemetry_id, time_since_last_shown, time_since_last_accepted, typing_speed)
                                VALUES (%s, %s, %s, %s)
                                ''', (
                                    behavioral_telemetry_id,
                                    random.randint(1000, 30000),  # 1-30 seconds
                                    random.randint(5000, 300000),  # 5s-5min
                                    random.uniform(20, 120)  # WPM
                                ))

            # Create context
            context_id = str(uuid.uuid4())
            self.cursor.execute('''
                                INSERT INTO context (context_id, prefix, suffix, file_name, selected_text)
                                VALUES (%s, %s, %s, %s, %s)
                                ''', (
                                    context_id,
                                    self._generate_code_context(language_name, "prefix"),
                                    self._generate_code_context(language_name, "suffix"),
                                    fake.file_name(extension=self._get_file_extension(language_name)),
                                    None  # Most don't have selected text
                                ))

            # Create meta query
            meta_query_id = str(uuid.uuid4())
            query_type = random.choices(['completion', 'chat'], weights=[0.7, 0.3])[0]

            # Query timestamp within session time
            query_timestamp = fake.date_time_between(
                start_date=session['start_time'],
                end_date=session['end_time'] or datetime.now()
            )

            total_serving_time = random.randint(100, 2000)  # 100ms - 2s

            self.cursor.execute('''
                                INSERT INTO meta_query
                                (meta_query_id, user_id, contextual_telemetry_id, behavioral_telemetry_id,
                                 context_id, session_id, project_id, timestamp, total_serving_time, query_type)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ''', (
                                    meta_query_id, user['user_id'], contextual_telemetry_id, behavioral_telemetry_id,
                                    context_id, session['session_id'], project_id, query_timestamp,
                                    total_serving_time, query_type
                                ))

            # Create specific query type
            if query_type == 'completion':
                self.cursor.execute('''
                                    INSERT INTO completion_query (meta_query_id)
                                    VALUES (%s)
                                    ''', (meta_query_id,))

                # Create generation data for completions
                self._create_generations(meta_query_id, language_name, user['config_id'], is_completion=True)

            else:  # chat
                # Find a chat or create one
                self.cursor.execute('''
                                    SELECT chat_id
                                    FROM chat
                                    WHERE project_id = %s
                                      AND user_id = %s LIMIT 1
                                    ''', (project_id, user['user_id']))

                chat_result = self.cursor.fetchone()
                if not chat_result:
                    # Create a chat
                    chat_id = str(uuid.uuid4())
                    self.cursor.execute('''
                                        INSERT INTO chat (chat_id, project_id, user_id, title, created_at)
                                        VALUES (%s, %s, %s, %s, %s)
                                        ''', (chat_id, project_id, user['user_id'],
                                              f"Chat about {language_name}", query_timestamp))
                else:
                    chat_id = chat_result['chat_id']

                self.cursor.execute('''
                                    INSERT INTO chat_query (meta_query_id, chat_id, web_enabled)
                                    VALUES (%s, %s, %s)
                                    ''', (meta_query_id, chat_id, random.choice([True, False])))

                # Create generation data for chat (no ground truth rows here)
                self._create_generations(meta_query_id, language_name, user['config_id'], is_completion=False)

            if i % 200 == 0:
                print(f"  Created {i}/{NUM_QUERIES} queries...")
                self.conn.commit()

        print(f"✅ Created {NUM_QUERIES} queries with telemetry")
        self.conn.commit()

    def _create_generations(self, meta_query_id: str, language_name: str, config_id: int, is_completion: bool):
        """Create realistic generation data for a query"""
        # Usually 1 generation, sometimes 2-3 for comparison
        num_generations = random.choices([1, 2, 3], weights=[0.7, 0.2, 0.1])[0]

        selected_models = random.sample(self.created_data['models'],
                                        min(num_generations, len(self.created_data['models'])))

        for model in selected_models:
            # Get model config for realistic performance
            model_config = next((m for m in MODEL_CONFIGS
                                 if m['name'] == model['model_name']), MODEL_CONFIGS[0])

            # Acceptance rate varies by language and model
            language_modifier = self._get_language_acceptance_modifier(language_name)
            config_modifier = self._get_config_acceptance_modifier(config_id)

            base_acceptance = model_config['base_acceptance_rate']
            final_acceptance_rate = float(np.clip(
                base_acceptance + language_modifier + config_modifier, 0.1, 0.95
            ))

            was_accepted = bool(random.random() < final_acceptance_rate)

            # Generation time varies by model and complexity
            base_time = model_config['avg_generation_time']
            generation_time = max(50, int(np.random.normal(base_time, base_time * 0.3)))

            # Confidence should correlate with acceptance but with model bias
            confidence_bias = model_config['confidence_bias']
            if was_accepted:
                confidence = np.clip(np.random.normal(0.75 + confidence_bias, 0.15), 0.1, 1.0)
            else:
                confidence = np.clip(np.random.normal(0.45 + confidence_bias, 0.20), 0.1, 1.0)

            # Realistic logprobs (negative values, more negative = less probable)
            logprobs = [-random.uniform(0.1, 3.0) for _ in range(random.randint(5, 20))]

            shown_at = [datetime.now()]  # When shown to user

            completion_text = self._generate_code_completion(language_name)

            self.cursor.execute('''
                                INSERT INTO had_generation
                                (meta_query_id, model_id, completion, generation_time, shown_at,
                                 was_accepted, confidence, logprobs)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                ''', (
                                    meta_query_id, int(model['model_id']), completion_text, generation_time,
                                    shown_at, was_accepted, float(confidence), logprobs
                                ))

            # Sometimes add ground truth for evaluation
            # Only valid for completion queries because ground_truth.completion_query_id
            # references completion_query.meta_query_id
            if is_completion and random.random() < 0.1:  # 10% of completions have ground truth
                self.cursor.execute('''
                                    INSERT INTO ground_truth (completion_query_id, truth_timestamp, ground_truth)
                                    VALUES (%s, %s, %s)
                                    ''', (
                                        meta_query_id,
                                        datetime.now() + timedelta(minutes=random.randint(1, 60)),
                                        self._generate_ground_truth(language_name)
                                    ))

    def _get_language_acceptance_modifier(self, language_name: str) -> float:
        """Get acceptance rate modifier based on language difficulty"""
        modifiers = {
            'Python': 0.1,  # Easy language, higher acceptance
            'JavaScript': 0.05,
            'TypeScript': 0.0,
            'Java': -0.05,  # More verbose, slightly lower
            'C++': -0.1,    # Complex language, lower acceptance
            'Rust': -0.15,  # Hard language, much lower acceptance
            'Go': 0.05,     # Simple language
        }
        return modifiers.get(language_name, 0.0)

    def _get_config_acceptance_modifier(self, config_id: int) -> float:
        """Get acceptance rate modifier based on configuration"""
        # Different configs should show different performance
        if config_id == 1:  # Default
            return 0.0
        elif config_id == 2:  # Fast response (lower quality)
            return -0.05
        elif config_id == 3:  # High quality
            return 0.08
        else:  # Experimental
            return random.uniform(-0.1, 0.1)

    def _get_file_extension(self, language_name: str) -> str:
        """Get file extension for language"""
        extensions = {
            'Python': 'py', 'JavaScript': 'js', 'TypeScript': 'ts',
            'Java': 'java', 'C++': 'cpp', 'Go': 'go', 'Rust': 'rs',
            'C#': 'cs', 'PHP': 'php', 'Ruby': 'rb', 'Kotlin': 'kt'
        }
        return extensions.get(language_name, 'txt')

    def _generate_code_context(self, language_name: str, context_type: str) -> str:
        """Generate realistic code context"""
        if language_name == 'Python':
            if context_type == 'prefix':
                return random.choice([
                    "def process_data(data):\n    result = []",
                    "class DataProcessor:\n    def __init__(self):",
                    "import pandas as pd\n\ndf = pd.read_csv('data.csv')\n"
                ])
            else:
                return random.choice([
                    "\n    return result",
                    "\n        self.data = data",
                    "\nprint(df.head())"
                ])
        elif language_name == 'JavaScript':
            if context_type == 'prefix':
                return random.choice([
                    "function processData(data) {\n    const result = [];",
                    "const apiCall = async () => {\n    try {",
                    "import React from 'react';\n\nfunction Component() {"
                ])
            else:
                return random.choice([
                    "\n    return result;\n}",
                    "\n    } catch (error) {\n        console.log(error);\n    }\n}",
                    "\n    return <div>Hello</div>;\n}"
                ])
        else:
            return f"// {context_type} context for {language_name}"

    def _generate_code_completion(self, language_name: str) -> str:
        """Generate realistic code completion"""
        completions = {
            'Python': [
                "for item in data:\n        result.append(process_item(item))",
                "self.data = data\n        self.initialized = True",
                "df_filtered = df[df['column'] > threshold]"
            ],
            'JavaScript': [
                "data.map(item => ({ ...item, processed: true }))",
                "const response = await fetch('/api/data')",
                "useState(null)"
            ],
            'TypeScript': [
                "interface DataItem {\n    id: number;\n    name: string;\n}",
                "const processData = (data: DataItem[]): ProcessedItem[] =>",
                "export type ApiResponse<T> = {\n    data: T;\n    status: number;\n}"
            ]
        }
        
        lang_completions = completions.get(language_name, [f"// TODO: implement {language_name} logic"])
        return random.choice(lang_completions)

    def _generate_ground_truth(self, language_name: str) -> str:
        """Generate ground truth data"""
        return f"// Correct implementation for {language_name}\n" + self._generate_code_completion(language_name)

    def create_studies(self):
        """Create test studies for A/B testing"""
        print("🔬 Creating test studies...")
        
        # Create a few studies to demonstrate the feature
        studies_to_create = [
            {
                "name": "Model Performance Comparison",
                "description": "Testing different model selection strategies",
                "config_ids": [1, 2, 3],  # Use first 3 configs
                "is_active": True,
                "days_ago_started": 7
            },
            {
                "name": "Response Time Optimization", 
                "description": "Comparing fast vs quality-optimized responses",
                "config_ids": [2, 3],  # Fast vs High Quality
                "is_active": False,
                "days_ago_started": 20,
                "days_ago_ended": 10
            }
        ]
        
        for study_info in studies_to_create:
            study_id = str(uuid.uuid4())
            admin_user = random.choice([u for u in self.created_data['users'] if u['is_admin']])
            
            starts_at = datetime.now() - timedelta(days=study_info['days_ago_started'])
            ends_at = None
            if 'days_ago_ended' in study_info:
                ends_at = datetime.now() - timedelta(days=study_info['days_ago_ended'])
            
            # Use first config as default
            default_config_id = self.created_data['configs'][0]['config_id']
            
            self.cursor.execute('''
                INSERT INTO study (study_id, name, description, created_by, starts_at, 
                                 ends_at, is_active, default_config_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                study_id, study_info['name'], study_info['description'],
                admin_user['user_id'], starts_at, ends_at, study_info['is_active'],
                default_config_id, starts_at
            ))
            
            # Assign users to configs for this study
            study_configs = [c for c in self.created_data['configs'] 
                           if c['config_id'] in study_info['config_ids']]
            
            for user in self.created_data['users']:
                if not user['is_admin']:  # Only assign regular users to studies
                    assigned_config = random.choice(study_configs) if study_configs else random.choice(self.created_data['configs'])
                    self.cursor.execute('''
                        INSERT INTO config_assignment_history 
                        (user_id, study_id, assigned_config_id, assigned_at)
                        VALUES (%s, %s, %s, %s)
                    ''', (
                        user['user_id'], study_id, assigned_config['config_id'], starts_at
                    ))
            
            self.created_data['studies'].append({
                'study_id': study_id,
                'name': study_info['name'],
                'is_active': study_info['is_active']
            })
            
            print(f"  Created study: {study_info['name']}")

        print(f"✅ Created {len(studies_to_create)} studies")
        self.conn.commit()

    def print_summary(self):
        """Print summary of created data"""
        print("\n🎉 Test Data Population Complete!")
        print("=" * 50)
        print(f"📊 Data Summary:")
        print(f"  • Users: {len(self.created_data['users'])} ({NUM_ADMIN_USERS} admins)")
        print(f"  • Configurations: {len(self.created_data['configs'])}")
        print(f"  • Projects: {len(self.created_data['projects'])}")
        print(f"  • Sessions: {len(self.created_data['sessions'])}")
        print(f"  • Queries: {NUM_QUERIES}")
        print(f"  • Studies: {len(self.created_data['studies'])}")
        print(f"  • Models: {len(self.created_data['models'])}")
        print(f"  • Languages: {len(self.created_data['languages'])}")
        
        print(f"\n🔐 Admin Users:")
        for user in self.created_data['users']:
            if user['is_admin']:
                print(f"  • {user['email']} (Config ID: {user['config_id']})")
        
        print(f"\n🔬 Active Studies:")
        for study in self.created_data['studies']:
            if study['is_active']:
                print(f"  • {study['name']}")

        print(f"\n🌐 You can now:")
        print(f"  • Login with any admin user to see all analytics")
        print(f"  • Login with regular users to see personal analytics") 
        print(f"  • Test the dashboard time windows and filters")
        print(f"  • View A/B test results in admin panel")
        print(f"  • Explore model performance comparisons")


def main():
    """Main execution function"""
    print("🚀 Code4Me Analytics Test Data Populator")
    print("=" * 50)
    
    populator = DatabasePopulator()
    
    try:
        # Connect to database
        populator.connect()
        
        # Clear existing data (optional - comment out to keep existing data)
        populator.clear_existing_data()
        
        # Create test data
        populator.create_configs()
        populator.create_users()
        populator.get_reference_data()
        populator.create_projects()
        populator.create_sessions()
        populator.create_telemetry_and_queries()
        populator.create_studies()
        
        # Print summary
        populator.print_summary()
        
    except KeyboardInterrupt:
        print("\n⚠️  Population cancelled by user")
    except Exception as e:
        print(f"❌ Error during population: {e}")
        import traceback
        traceback.print_exc()
    finally:
        populator.close()


if __name__ == "__main__":
    main()

