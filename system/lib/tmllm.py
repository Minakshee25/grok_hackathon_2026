"""Optional language-model reader for free-text documents.

Where it sits: only free-text documents (chats, staff notes, emails, letters)
that the keyword rules do not recognise are sent here. The model labels them
with a fixed set of red-flag labels. It never chooses a verdict, and its
labels can only make a decision more cautious (block a close or escalate),
never close a case.

Determinism: temperature 0, a fixed seed, a fixed label set, and every answer
is cached on disk in system/.cache/llm_cache.json keyed by a hash of the
prompt, so repeated runs reuse exactly the same answer.

If OPENROUTER_API_KEY is not set, or the call fails, the reader returns None
and the rules treat the document as "not explaining anything".
"""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# xAI Grok via OpenRouter. Override with TM_LLM_MODEL if your OpenRouter
# account lists a different Grok model ID (see openrouter.ai/models, search "grok").
MODEL = os.environ.get("TM_LLM_MODEL", "x-ai/grok-4.3")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(HERE, ".cache", "llm_cache.json")

LABELS = [
    "none",                 # routine or irrelevant, no concern
    "structuring",          # asks about thresholds / splitting to avoid checks
    "third_party_control",  # someone else uses or controls the account
    "mule",                 # receives and forwards others' money for a 'job'
    "evasive",              # refuses or cannot explain, or story does not fit
    "high_risk_routing",    # unexplained routing to a higher-risk country
    "unverified_claim",     # gives an innocent explanation with no document
]

PROMPT = """You review one document from a bank's file on a customer during an
anti-money-laundering alert review. Label the document with exactly one label:

none                - routine or unrelated to the payments (e.g. card lost, address update)
structuring         - the customer asks about thresholds, aggregation, or splitting amounts to avoid checks
third_party_control - someone other than the customer uses, funds or controls the account
mule                - the customer receives money and passes it on for someone else (e.g. a 'payments job')
evasive             - the customer refuses, cannot explain, or gives an explanation that does not fit the activity
high_risk_routing   - money is routed to a higher-risk country without a reason
unverified_claim    - the customer gives an innocent explanation but nothing documents it

Reply with JSON only: {"label": "<one label>"}

Document:
<<<
%s
>>>"""


class Reader:
    def __init__(self):
        self.key = os.environ.get("OPENROUTER_API_KEY")
        self.lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.last_error = None
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                self.cache = json.load(fh)
        except (OSError, ValueError):
            self.cache = {}

    def label(self, text):
        prompt = PROMPT % text[:4000]
        k = hashlib.sha256((MODEL + "\n" + prompt).encode("utf-8")).hexdigest()
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        if not self.key:
            return None
        lab = self._call(prompt)
        if lab is not None:
            with self.lock:
                self.cache[k] = lab
        return lab

    def _call(self, prompt):
        body = json.dumps({
            "model": MODEL,
            "temperature": 0,
            "seed": 7,
            # Grok models may reason before answering; leave room for that
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(API_URL, data=body, headers={
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "X-Title": "TM alert triage",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = json.loads(r.read().decode("utf-8"))
                txt = (out["choices"][0]["message"].get("content") or "")
                self.calls += 1
                for lab in LABELS:
                    if '"%s"' % lab in txt:
                        return lab
                self.last_error = "unrecognised reply: %r" % txt[:80]
                return None
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8")[:200]
                except Exception:
                    detail = ""
                self.last_error = "HTTP %s %s" % (e.code, detail)
                if e.code in (400, 401, 402, 403, 404):   # bad key / model / credits: retrying won't help
                    self.key = None
                    break
            except Exception as e:
                self.last_error = repr(e)[:200]
        self.failures += 1
        return None

    def save(self):
        if not self.cache:
            return
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, sort_keys=True, indent=0)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass
