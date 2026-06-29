"""Generate a sample input JSONL for Threshly.

Many requests share one system prompt (a common real-world batch shape), so the prefix-aware
scheduler has something to exploit. Usage: python examples/gen_sample.py [n] > examples/sample.jsonl
"""

from __future__ import annotations

import json
import sys

SYSTEM = (
    "You are a meticulous data-labeling assistant. Classify the sentiment of the user's text as "
    "exactly one of: positive, negative, neutral. Reply with only the label."
)

TEXTS = [
    "I absolutely loved this, best purchase of the year!",
    "It broke after two days and support ignored me.",
    "The package arrived on the scheduled date.",
    "Honestly the most underwhelming experience imaginable.",
    "Works as described, no complaints and no surprises.",
    "Incredible value, I told all my friends about it.",
]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    for i in range(n):
        text = TEXTS[i % len(TEXTS)]
        line = {
            "custom_id": f"request-{i}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "demo-model",
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 8,
                "temperature": 0.0,
            },
        }
        print(json.dumps(line))


if __name__ == "__main__":
    main()
