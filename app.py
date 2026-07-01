import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from audit_log import append_log_entry, get_log, update_log_entry
from labels import label_for
from signals import combine_scores, compute_signal1, compute_signal2

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# In-memory for now -- content_id is the key the appeal endpoint uses to look
# submissions back up. The audit log itself is persisted separately
# (audit_log.py), not kept in memory.
SUBMISSIONS = {}


def iso_timestamp():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
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

    label = label_for(attribution, confidence)
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


@app.route("/appeal", methods=["POST"])
def appeal():
    body = request.get_json(silent=True) or {}
    content_id = body.get("content_id")
    creator_reasoning = body.get("creator_reasoning")

    if not content_id or not creator_reasoning:
        return jsonify({"error": "content_id and creator_reasoning are required"}), 400

    submission = SUBMISSIONS.get(content_id)
    if submission is None:
        return jsonify({"error": "unknown content_id"}), 404

    if submission["status"] == "under_review":
        return jsonify({"error": "an appeal is already under review for this content_id"}), 409

    submission["status"] = "under_review"
    submission["appeal_reasoning"] = creator_reasoning

    # Merge into the existing audit-log entry rather than appending a
    # disconnected one -- planning.md §4.3: the appeal sits alongside the
    # original classification fields (attribution, confidence, signal
    # scores), not as a separate unlinked record.
    found = update_log_entry(
        content_id,
        {
            "status": "under_review",
            "appeal_reasoning": creator_reasoning,
            "appeal_timestamp": iso_timestamp(),
        },
    )
    if not found:
        return jsonify({"error": "no audit log entry found for content_id"}), 404

    return jsonify(
        {
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received and logged for review.",
        }
    )


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", type=int)
    return jsonify({"entries": get_log(limit)})


if __name__ == "__main__":
    app.run(debug=True)
