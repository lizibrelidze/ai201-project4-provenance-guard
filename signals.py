import json
import os

from groq import Groq

DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

SIGNAL1_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "ai_likelihood_judgment",
        "schema": {
            "type": "object",
            "properties": {
                "ai_likelihood": {
                    "type": "number",
                    "description": "0.0 (clearly human-written) to 1.0 (clearly AI-generated)",
                }
            },
            "required": ["ai_likelihood"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

SIGNAL1_SYSTEM_PROMPT = (
    "You judge whether a piece of text was likely written by an AI language "
    "model or by a human. Base your judgment on writing style only -- "
    "predictability of word choice, uniformity of sentence structure, generic "
    "or formulaic phrasing, hedge-word patterns -- not on whether the content "
    "is true, on topic, or well-argued. Respond with a single number from 0.0 "
    "(clearly human-written) to 1.0 (clearly AI-generated)."
)


def build_signal1_prompt(text):
    return [
        {"role": "system", "content": SIGNAL1_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]


def parse_signal1_response(raw_content):
    data = json.loads(raw_content)
    score = float(data["ai_likelihood"])
    return max(0.0, min(1.0, score))


def compute_signal1(text, client=None, model=None):
    client = client or Groq()
    model = model or DEFAULT_MODEL

    response = client.chat.completions.create(
        model=model,
        messages=build_signal1_prompt(text),
        response_format=SIGNAL1_SCHEMA,
        temperature=0,
    )
    raw_content = response.choices[0].message.content
    return parse_signal1_response(raw_content)
