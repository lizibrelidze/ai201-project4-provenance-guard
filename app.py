import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from audit_log import append_log_entry, get_log
from signals import combine_scores, compute_signal1, compute_signal2

app = Flask(__name__)

# In-memory for now -- content_id is the key the appeal endpoint (Milestone 5)
# will use to look submissions back up. The audit log itself is persisted
# separately (audit_log.py), not kept in memory.
SUBMISSIONS = {}


def iso_timestamp():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@app.route("/submit", methods=["POST"])
def submit():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    creator_id = body.get("creator_id")

    if not text or not creator_id:
        return jsonify({"error": "text and creator_id are required"}), 400

    try:
        llm_score = compute_signal1(text)
    except Exception as exc:
        return jsonify({"error": f"signal 1 failed: {exc}"}), 502

    stylometric_score = compute_signal2(text)
    result = combine_scores(llm_score, stylometric_score)
    confidence = result["confidence_score"]
    attribution = result["band"]
    disagreement = result["disagreement"]
    low_coverage = result["low_coverage"]

    label = "pending"  # placeholder -- real label copy (planning.md §3) lands in M5
    content_id = str(uuid.uuid4())
    timestamp = iso_timestamp()

    SUBMISSIONS[content_id] = {
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "llm_score": llm_score,
        "stylometric_score": stylometric_score,
        "attribution": attribution,
        "confidence": confidence,
        "disagreement": disagreement,
        "low_coverage": low_coverage,
        "label": label,
        "status": "pending",  # appeal-workflow status, distinct from the log's "status"
        "created_at": timestamp,
    }

    append_log_entry(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "timestamp": timestamp,
            "attribution": attribution,
            "confidence": confidence,
            "llm_score": llm_score,
            "stylometric_score": stylometric_score,
            "disagreement": disagreement,
            "low_coverage": low_coverage,
            "status": "classified",
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "attribution": attribution,
            "confidence": confidence,
            "llm_score": llm_score,
            "stylometric_score": stylometric_score,
            "label": label,
        }
    )


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", type=int)
    return jsonify({"entries": get_log(limit)})


if __name__ == "__main__":
    app.run(debug=True)
