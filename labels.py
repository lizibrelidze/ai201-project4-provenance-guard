# Exact copy from planning.md §3 -- do not improvise wording per-request.
LABEL_TEMPLATES = {
    "likely-ai-assisted": (
        "Our automated signals indicate a high likelihood of AI involvement in this "
        "text (AI-likelihood score: {score:.2f}). This is a probabilistic signal, "
        "not a factual determination -- the creator may appeal this label."
    ),
    "uncertain": (
        "Our automated signals were inconclusive about AI involvement in this text "
        "(AI-likelihood score: {score:.2f}). No determination has been made, and no "
        "restriction is applied based on this score alone."
    ),
    "likely-human": (
        "Our automated signals did not find strong indicators of AI involvement in "
        "this text (AI-likelihood score: {score:.2f})."
    ),
}


def label_for(band, confidence_score):
    return LABEL_TEMPLATES[band].format(score=confidence_score)
