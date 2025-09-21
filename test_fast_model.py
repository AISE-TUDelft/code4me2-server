#!/usr/bin/env python3
"""
Simple test script for FastTemplateCompletionModel.
"""

import json
import time
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

def test_fast_model():
    """Test the FastTemplateCompletionModel."""
    try:
        import sys
        import os
        sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))
        from backend.completion.FastTemplateCompletionModel import FastTemplateCompletionModel
        
        template = '{"fim_template":{"multi_file_template":"{multi_file_context}#{file_name}\\n{prefix}","single_file_template":"{prefix}"},"file_separator":"#{file_name}\\n","stop_tokens":["\\n\\n"]}'
        prompt_templates = json.loads(template)
        
        print("Initializing FastTemplateCompletionModel...")
        model = FastTemplateCompletionModel(
            model_name="deepseek-ai/deepseek-coder-1.3b-base",
            prompt_templates=prompt_templates,
            model_warmup=True,
            model_use_compile=True,
            do_sample=False,
            load_in_8bit=True,
            use_flash_attention=True,
        )
        
        print("Testing generation...")
        t0 = time.perf_counter()
        response = model.invoke({"prefix": "\n\ndef hello_world():", "suffix": ""})
        t1 = time.perf_counter()
        
        print(f"Time taken: {t1 - t0:.3f} seconds")
        print(f"Generation time: {response.get('generation_time', 0)}ms")
        print(f"Completion: {response.get('completion', '')}")
        print(f"Confidence: {response.get('confidence', 'N/A')}")
        
        return True
        
    except Exception as e:
        print(f"Error testing FastTemplateCompletionModel: {e}")
        return False

if __name__ == "__main__":
    success = test_fast_model()
    if success:
        print("✅ FastTemplateCompletionModel test passed!")
    else:
        print("❌ FastTemplateCompletionModel test failed!")
