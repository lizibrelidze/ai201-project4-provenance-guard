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


# Bands from planning.md §2.3
AI_BAND_THRESHOLD = 0.70
HUMAN_BAND_THRESHOLD = 0.30
# Extra damping applied when only one signal is available (planning.md §1.3)
LOW_COVERAGE_DAMPING = 0.7


def band_for_score(confidence_score):
    if confidence_score >= AI_BAND_THRESHOLD:
        return "likely-ai-assisted"
    if confidence_score <= HUMAN_BAND_THRESHOLD:
        return "likely-human"
    return "uncertain"


def combine_scores(s1, s2=None):
    """planning.md §1.3 -- combine signal scores into one confidence_score + band."""
    if s2 is None:
        raw_combined = s1
        disagreement = 0
        low_coverage = True
    else:
        raw_combined = 0.6 * s1 + 0.4 * s2
        disagreement = abs(s1 - s2)
        low_coverage = False

    confidence_score = 0.5 + (raw_combined - 0.5) * (1 - disagreement)
    if low_coverage:
        confidence_score = 0.5 + (confidence_score - 0.5) * LOW_COVERAGE_DAMPING
    confidence_score = round(confidence_score, 4)

    return confidence_score, band_for_score(confidence_score)
