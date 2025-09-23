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

# --- Configuration ---
API_ENDPOINT = "http://localhost:8008/api/completion/request"
MODEL_IDS = [1]

# --- Authentication Tokens ---
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', 'efde6a22-2d6a-4ba3-8efa-889530dd70cc')
PROJECT_TOKEN = os.environ.get('PROJECT_TOKEN', '418c70dd-867f-4935-a5e0-ba2d7ad94f5b')
SESSION_TOKEN = os.environ.get('SESSION_TOKEN', 'c39799cc-9d25-4da5-b2b3-f67d46ae09d1')

# --- Benchmark Settings ---
DB_NAME = "benchmark_results.db"
CSV_NAME = "benchmark_results.csv"
NUM_WARMUP_RUNS = 10
NUM_BENCHMARK_RUNS = 1
BENCHMARK_SUBSET_SIZE = 1600


# --- Helper Functions ---

def setup_database(db_name="benchmark_results.db"):
    """Creates a SQLite database and a 'results' table with separate response fields."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('DROP TABLE IF EXISTS results')  # Recreate table with new schema
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS results (
            timestamp TEXT,
            model_ids TEXT,
            prefix TEXT,
            suffix TEXT,
            e2e_time_ms REAL,
            error TEXT,
            response_message TEXT,
            meta_query_id TEXT,
            completion_model_id INTEGER,
            completion_model_name TEXT,
            completion_text TEXT,
            generation_time_ms INTEGER,
            confidence REAL
        )
    ''')
    conn.commit()
    conn.close()
    print(f"Database '{db_name}' and new 'results' table are ready.")


def load_benchmark_data(subset_size=None):
    """Loads the SantaCoder FIM dataset from Hugging Face."""
    print("Downloading and preparing the santacoder-fim-task dataset (all languages)...")
    try:
        dataset = load_dataset("bigcode/santacoder-fim-task")['train']
        if subset_size and subset_size < len(dataset):
            print(f"Using a subset of {subset_size} samples.")
            return dataset.select(range(subset_size))
        print(f"Using the full dataset with {len(dataset)} samples.")
        return dataset
    except Exception as e:
        print(f"Error loading dataset: {e}")
        return None


def prepare_request_payload(benchmark_item, model_ids):
    """Formats a dataset item into the required JSON payload."""
    return {
        "model_ids": model_ids,
        "context": {
            "prefix": benchmark_item['prompt'],
            "suffix": benchmark_item['suffix'],
            "file_name": "main.py",
            "context_files": []
        },
        "store_context": True,
        "contextual_telemetry": {"language_id": 45, "trigger_type_id": 1, "version_id": 1,
                                 "document_char_length": len(benchmark_item['prompt']) + len(benchmark_item['suffix'])},
        "store_contextual_telemetry": True,
        "behavioral_telemetry": {"time_since_last_completion": 5000, "typing_speed": 300,
                                 "relative_document_position": 0.5},
        "store_behavioral_telemetry": True
    }


def send_request_and_measure_time(session, payload, cookies):
    """Sends a request and returns a dictionary with time, response, and error."""
    start_time = time.perf_counter()
    response_json, error_msg = None, None
    try:
        response = session.post(API_ENDPOINT, json=payload, cookies=cookies, timeout=30)
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
    print(f"Standard Deviation:      {stdev_val:.2f} ms")
    print(f"95th Percentile (P95):   {p95:.2f} ms")
    print(f"99th Percentile (P99):   {p99:.2f} ms")
    print(f"95% Confidence Interval: [{ci_lower:.2f} ms, {ci_upper:.2f} ms]")
    print("--------------------------\n")


# --- Main Execution ---
def main():
    """Main function to run the benchmark."""
    print("Starting End-to-End Response Time Benchmark...")
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

                # --- New Response Parsing Logic ---
                response_data = result['response_json']
                completion_data = {}
                message = None
                meta_query_id = None

                if response_data:
                    message = response_data.get('message')
                    data = response_data.get('data', {})
                    meta_query_id = data.get('meta_query_id')
                    completions = data.get('completions', [])
                    if completions:
                        completion_data = completions[0]  # Get the first completion

                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "model_ids": json.dumps(payload['model_ids']),
                    "prefix": payload['context']['prefix'],
                    "suffix": payload['context']['suffix'],
                    "e2e_time_ms": result['e2e_time_ms'],
                    "error": result['error'],
                    "response_message": message,
                    "meta_query_id": meta_query_id,
                    "completion_model_id": completion_data.get('model_id'),
                    "completion_model_name": completion_data.get('model_name'),
                    "completion_text": completion_data.get('completion'),
                    "generation_time_ms": completion_data.get('generation_time'),
                    "confidence": completion_data.get('confidence')
                }
                all_results_data.append(log_entry)

                if (j + 1) % 25 == 0:
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