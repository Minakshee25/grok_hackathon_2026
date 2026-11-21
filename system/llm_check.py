#!/usr/bin/env python3
"""Check that the OpenRouter key and Grok model work.

Usage:  OPENROUTER_API_KEY=sk-or-... python3 system/llm_check.py
        (optional) TM_LLM_MODEL=x-ai/<model-id>

Sends one made-up test document (no case data) and prints the label.
Does not use or write the cache.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import tmllm  # noqa: E402

if not os.environ.get("OPENROUTER_API_KEY"):
    print("OPENROUTER_API_KEY is not set.")
    sys.exit(1)

r = tmllm.Reader()
sample = ("Staff note: the customer asked whether keeping each cash deposit just under "
          "the reporting threshold would mean no forms have to be filled in.")
label = r._call(tmllm.PROMPT % sample)
print("model:", tmllm.MODEL)
if label:
    print("OK, label returned:", label, "(expected: structuring)")
    sys.exit(0)
print("FAILED:", r.last_error)

# Show which Grok models OpenRouter offers, so the right ID can be set.
import json
import urllib.request
try:
    req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                 headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        models = json.loads(resp.read().decode("utf-8")).get("data", [])
    grok = sorted(m["id"] for m in models if "grok" in m.get("id", "").lower() or m.get("id", "").startswith("x-ai/"))
    if grok:
        print("\nGrok models available on OpenRouter:")
        for g in grok:
            print("   ", g)
        print("\nPick one and set it, e.g.:  export TM_LLM_MODEL=%s" % grok[0])
    else:
        print("\nNo Grok models found in OpenRouter's model list (%d models listed)." % len(models))
except Exception as e:
    print("\nCould not fetch the model list:", e)
sys.exit(1)
