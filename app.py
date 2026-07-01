import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from signals import compute_signal1

app = Flask(__name__)

# In-memory for now -- content_id is the key the appeal endpoint (Milestone 5)
# will use to look submissions back up.
SUBMISSIONS = {}
AUDIT_LOG = []


@app.route("/submit", methods=["POST"])
def submit():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    creator_id = body.get("creator_id")

    if not text or not creator_id:
        return jsonify({"error": "text and creator_id are required"}), 400

    try:
        attribution = compute_signal1(text)
    except Exception as exc:
        return jsonify({"error": f"signal 1 failed: {exc}"}), 502

    content_id = str(uuid.uuid4())
    confidence = None  # placeholder -- real combine (planning.md §1.3) lands in M4
    label = "pending"  # placeholder -- real label copy (planning.md §3) lands in M4/M5
    created_at = datetime.now(timezone.utc).isoformat()

    SUBMISSIONS[content_id] = {
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "attribution": attribution,
        "confidence": confidence,
        "label": label,
        "status": "pending",
        "created_at": created_at,
    }

    AUDIT_LOG.append(
        {
            "event": "submission_created",
            "content_id": content_id,
            "creator_id": creator_id,
            "attribution": attribution,
            "confidence": confidence,
            "label": label,
            "timestamp": created_at,
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "attribution": attribution,
            "confidence": confidence,
            "label": label,
        }
    )


if __name__ == "__main__":
    app.run(debug=True)
