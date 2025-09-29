import requests
import time
import os
import statistics
import math
from datasets import load_dataset
import sqlite3
import json
from datetime import datetime
import pandas as pd
import uuid

# --- Configuration ---
API_ENDPOINT = "http://localhost:8008/api/chat/request"
MODEL_IDS = [3]
PROMPT_PREFIX = "Please explain the different components of the solution for the following problem: "

# --- Authentication Tokens ---
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '242dfc32-73b8-4a33-88dc-9279dd63b349')
PROJECT_TOKEN = os.environ.get('PROJECT_TOKEN', 'de68bfe7-55fc-4f7c-83cc-5a5901d3416e')
SESSION_TOKEN = os.environ.get('SESSION_TOKEN', '896e8387-bedf-4f2b-9f2c-6057ea37833d')

# --- Benchmark Settings ---
DB_NAME = "chat_benchmark_results.db"
CSV_NAME = "chat_benchmark_results.csv"
NUM_WARMUP_RUNS = 5
NUM_BENCHMARK_RUNS = 1
BENCHMARK_SUBSET_SIZE = 100  # Use a subset for faster testing


# --- Helper Functions ---

def setup_database(db_name="chat_benchmark_results.db"):
    """Creates a SQLite database and a 'results' table for chat benchmarks."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('DROP TABLE IF EXISTS results')  # Recreate table with new schema
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS results (
            timestamp TEXT,
            model_ids TEXT,
            user_prompt TEXT,
            e2e_time_ms REAL,
            error TEXT,
            chat_id TEXT,
            response_title TEXT,
            assistant_completion TEXT,
            generation_time_ms INTEGER,
            confidence REAL
        )
    ''')
    conn.commit()
    conn.close()
    print(f"Database '{db_name}' and new 'results' table are ready.")


def load_benchmark_data(subset_size=None):
    """Loads the MBPP dataset from Hugging Face."""
    print("Downloading and preparing the MBPP dataset...")
    try:
        dataset = load_dataset("mbpp", "full")['test']
        if subset_size and subset_size < len(dataset):
            print(f"Using a subset of {subset_size} samples.")
            return dataset.select(range(subset_size))
        print(f"Using the full dataset with {len(dataset)} samples.")
        return dataset
    except Exception as e:
        print(f"Error loading dataset: {e}")
        return None


def prepare_request_payload(benchmark_item, model_ids):
    """Formats a dataset item into the required chat JSON payload."""
    user_message = f"{PROMPT_PREFIX}{benchmark_item['text']}"
    return {
        "model_ids": model_ids,
        "chat_id": str(uuid.uuid4()),
        "messages": [
            ["system", "You are a helpful python assistant"],
            ["user", user_message]
        ],
        # CORRECTED: Added all required fields with placeholder data
        "context": {
            "prefix": "",
            "suffix": "",
            "file_name": "benchmark.py",
            "selected_text": "",
            "context_files": []
        },
        "contextual_telemetry": {
            "version_id": 1,
            "trigger_type_id": 1,
            "language_id": 1,
            "file_path": "string",
            "caret_line": 0,
            "document_char_length": len(user_message),
            "relative_document_position": 1
        },
        "behavioral_telemetry": {
            "time_since_last_shown": 0,
            "time_since_last_accepted": 0,
            "typing_speed": 0
        },
        "web_enabled": False
    }

def send_request_and_measure_time(session, payload, cookies):
    """Sends a request and returns a dictionary with time, response, and error."""
    start_time = time.perf_counter()
    response_json, error_msg = None, None
    try:
        response = session.post(API_ENDPOINT, json=payload, cookies=cookies, timeout=60)  # Increased timeout for chat
        response.raise_for_status()
        response_json = response.json()
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        print(f"Request failed: {error_msg}")
    finally:
        end_time = time.perf_counter()

    duration_ms = (end_time - start_time) * 1000

    return {
        "e2e_time_ms": duration_ms,
        "response_json": response_json,
        "error": error_msg
    }


def analyze_results(timings):
    """Calculates and prints key statistics from the collected timings."""
    if not timings:
        print("No successful requests to analyze.")
        return
    # This function remains the same as before
    n = len(timings)
    mean_val = statistics.mean(timings)
    median_val = statistics.median(timings)
    stdev_val = statistics.stdev(timings) if n > 1 else 0
    p95 = sorted(timings)[int(n * 0.95)] if n > 19 else median_val
    p99 = sorted(timings)[int(n * 0.99)] if n > 99 else median_val
    ci_margin = 1.96 * (stdev_val / math.sqrt(n)) if n > 1 else 0
    ci_lower = mean_val - ci_margin
    ci_upper = mean_val + ci_margin

    print("\n--- Performance Analysis ---")
    print(f"Total Successful Requests: {n}")
    print(f"Mean Response Time:      {mean_val:.2f} ms")
    print(f"Median Response Time (P50):{median_val:.2f} ms")
    print(f"95th Percentile (P95):   {p95:.2f} ms")
    print("--------------------------\n")


# --- Main Execution ---
def main():
    """Main function to run the benchmark."""
    print("Starting Chat End-to-End Response Time Benchmark...")
    setup_database(DB_NAME)

    cookies = {
        'auth_token': AUTH_TOKEN,
        'project_token': PROJECT_TOKEN,
        'session_token': SESSION_TOKEN
    }

    dataset = load_benchmark_data(subset_size=BENCHMARK_SUBSET_SIZE)
    if not dataset: return

    with requests.Session() as session:
        print(f"Performing {NUM_WARMUP_RUNS} warm-up requests...")
        for i in range(NUM_WARMUP_RUNS):
            payload = prepare_request_payload(dataset[i % len(dataset)], MODEL_IDS)
            send_request_and_measure_time(session, payload, cookies)

        all_results_data = []
        print(f"\nStarting {NUM_BENCHMARK_RUNS} benchmark run(s) across {len(dataset)} samples each...")
        for i in range(NUM_BENCHMARK_RUNS):
            print(f"--- Starting Run {i + 1}/{NUM_BENCHMARK_RUNS} ---")
            for j, item in enumerate(dataset):
                payload = prepare_request_payload(item, MODEL_IDS)
                result = send_request_and_measure_time(session, payload, cookies)

                # --- New Response Parsing Logic for Chat ---
                response_data = result['response_json']
                assistant_response = {}
                chat_id, title = None, None

                if response_data:
                    chat_id = response_data.get('chat_id')
                    title = response_data.get('title')
                    history = response_data.get('history', [])
                    if history:
                        assistant_responses = history[-1].get('assistant_responses', [])
                        if assistant_responses:
                            assistant_response = assistant_responses[0]  # Get the first assistant response

                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "model_ids": json.dumps(payload['model_ids']),
                    "user_prompt": payload['messages'][-1][1],  # Get the user message content
                    "e2e_time_ms": result['e2e_time_ms'],
                    "error": result['error'],
                    "chat_id": chat_id,
                    "response_title": title,
                    "assistant_completion": assistant_response.get('completion'),
                    "generation_time_ms": assistant_response.get('generation_time'),
                    "confidence": assistant_response.get('confidence')
                }
                all_results_data.append(log_entry)

                if (j + 1) % 10 == 0:
                    print(f"  ...completed {j + 1}/{len(dataset)} requests.")

    if not all_results_data:
        print("No data collected. Exiting.")
        return

    df = pd.DataFrame(all_results_data)

    with sqlite3.connect(DB_NAME) as conn:
        df.to_sql('results', conn, if_exists='append', index=False)
        print(f"Successfully saved {len(df)} records to '{DB_NAME}' in the 'results' table.")

    df.to_csv(CSV_NAME, index=False)
    print(f"Successfully saved {len(df)} records to '{CSV_NAME}'.")

    successful_timings = df[df['error'].isnull()]['e2e_time_ms'].tolist()
    analyze_results(successful_timings)


if __name__ == "__main__":
    main()