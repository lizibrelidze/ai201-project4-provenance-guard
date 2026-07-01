import json
import math
import os
import re
from statistics import mean, stdev

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


# Signal 2 -- stylometric composite (planning.md §1.2): two metrics combined
# into one score. Originally speced as burstiness alone; type-token ratio was
# tried as a second metric and measured on the four M4 test paragraphs, but
# at short (~40-55 word) paragraph lengths TTR barely varies (0.86-0.90
# across a clearly-AI, clearly-human, and two borderline samples) -- there
# just isn't enough text for repetition patterns to show up. Filler/casual-
# word density measured on the same four samples actually discriminated
# (0.091 on the casual sample vs 0.000 on all three formal-register ones),
# so that replaced TTR as metric 2.
MIN_SENTENCES = 3
COV_MID = 0.45
COV_SCALE = 0.15
FILLER_MID = 0.02
FILLER_SCALE = 0.02
BURSTINESS_WEIGHT = 0.65
FILLER_WEIGHT = 0.35

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-zA-Z']+")

FILLER_WORDS = {
    "like", "so", "honestly", "probably", "guess", "ok", "okay", "lol",
    "kinda", "gonna", "yeah", "well", "just", "really", "actually",
}


def sentence_tokenize(text):
    stripped = text.strip()
    if not stripped:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(stripped) if s.strip()]


def burstiness_component(text, sentences):
    lengths = [len(s.split()) for s in sentences]
    cov = stdev(lengths) / mean(lengths)
    return cov, 1 / (1 + math.exp((cov - COV_MID) / COV_SCALE))


def filler_density_component(text):
    words = _WORD_RE.findall(text.lower())
    if not words:
        return 0.0, 0.5
    density = sum(1 for w in words if w in FILLER_WORDS) / len(words)
    # higher filler density -> more casual -> lower AI-likelihood
    return density, 1 / (1 + math.exp((density - FILLER_MID) / FILLER_SCALE))


def compute_signal2(text):
    sentences = sentence_tokenize(text)
    if len(sentences) < MIN_SENTENCES:
        return None

    _, burst = burstiness_component(text, sentences)
    _, filler = filler_density_component(text)
    return BURSTINESS_WEIGHT * burst + FILLER_WEIGHT * filler


def compute_signal2_debug(text):
    """Exposes the raw sub-metrics and components, for diagnosing which part
    of the composite is driving (or misbehaving in) a given score."""
    sentences = sentence_tokenize(text)
    if len(sentences) < MIN_SENTENCES:
        return {"signal2": None, "reason": "fewer than MIN_SENTENCES sentences"}

    cov, burst = burstiness_component(text, sentences)
    density, filler = filler_density_component(text)
    return {
        "signal2": BURSTINESS_WEIGHT * burst + FILLER_WEIGHT * filler,
        "cov": cov,
        "burst_component": burst,
        "filler_density": density,
        "filler_component": filler,
    }


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
        disagreement = round(abs(s1 - s2), 4)
        low_coverage = False

    confidence_score = 0.5 + (raw_combined - 0.5) * (1 - disagreement)
    if low_coverage:
        confidence_score = 0.5 + (confidence_score - 0.5) * LOW_COVERAGE_DAMPING
    confidence_score = round(confidence_score, 4)

    return {
        "confidence_score": confidence_score,
        "band": band_for_score(confidence_score),
        "disagreement": disagreement,
        "low_coverage": low_coverage,
    }
