# Provenance Guard — Planning

This is the implementation contract for Provenance Guard. `README.md` covers
the *rationale* for the two detection signals and their blind spots; this
document specifies the exact formulas, thresholds, label copy, and workflow
behavior that the code has to implement — nothing here should require a
judgment call at build time.

## 1. Detection signals

Two signals, both normalized to a `0.0–1.0` float where **higher = more
AI-like**. Neither is a binary flag — binary flags throw away the margin
information the confidence scoring in §2 depends on.

### 1.1 Signal 1 — Perplexity (model-based, via Groq)

- **Measures:** how predictable the text's tokens are under a reference LLM,
  queried through the Groq API with per-token `logprobs`.
- **Computation:**
  ```
  avg_logprob = mean(token_logprob for token in text)
  raw_ppl     = exp(-avg_logprob)          # stored as-is in the audit log
  s1 = 1 / (1 + exp((raw_ppl - PPL_MID) / PPL_SCALE))
  ```
  Initial calibration constants: `PPL_MID = 20`, `PPL_SCALE = 6` (placeholders
  until tuned against a labeled calibration set — see §2.2).
- **Output:** float 0–1. `raw_ppl` is also logged so recalibration doesn't
  require re-querying Groq.

### 1.2 Signal 2 — Burstiness (structural, local, no API call)

- **Measures:** coefficient of variation (CoV) of sentence length across the
  document — how uniform vs. bursty the rhythm is.
- **Computation:**
  ```
  sentences = sentence_tokenize(text)
  if len(sentences) < MIN_SENTENCES:      # MIN_SENTENCES = 4
      s2 = None                            # insufficient data, see §5.2
  else:
      lengths  = [word_count(s) for s in sentences]
      cov      = stdev(lengths) / mean(lengths)
      s2 = 1 / (1 + exp((cov - COV_MID) / COV_SCALE))
  ```
  Initial calibration constants: `COV_MID = 0.45`, `COV_SCALE = 0.15`.
- **Output:** float 0–1, or `None` when the document is too short to produce
  a meaningful variance estimate.

### 1.3 Combining into one confidence score

```
if s2 is None:                            # only one signal available
    raw_combined   = s1
    disagreement   = 0
    low_coverage   = True
else:
    raw_combined   = 0.6 * s1 + 0.4 * s2   # s1 weighted higher: it's a
                                            # direct model signal, s2 is a
                                            # weaker structural proxy
    disagreement   = abs(s1 - s2)
    low_coverage   = False

confidence_score = 0.5 + (raw_combined - 0.5) * (1 - disagreement)
if low_coverage:
    confidence_score = 0.5 + (confidence_score - 0.5) * 0.7   # extra damping
```

The `(1 - disagreement)` term is the key move: when the two signals disagree,
the combined score is pulled *toward 0.5*, not just averaged. Two signals
shouting different answers should produce **less** certainty, not a
confident-looking midpoint.

`confidence_score` is the single number stored, returned, and banded in §2.
`s1`, `s2`, `raw_ppl`, `disagreement`, and `low_coverage` are all written to
the audit log alongside it.

## 2. Uncertainty representation

### 2.1 What a score means

`confidence_score` is read as **P(this text involved AI generation)** as
estimated by the two signals above — not a probability calibrated against
ground truth (that requires a labeled dataset this project doesn't have yet),
but an internally consistent 0–1 scale where 0.5 is maximal uncertainty and
distance from 0.5 reflects both signal strength *and* signal agreement.

**A score of 0.6 means:** the signals lean toward AI involvement, but not
past the 0.70 bar (§2.3) — it lands in the `uncertain` band. The system makes
**no assertion** at 0.6. This is deliberate: 0.6 is exactly the range where a
formulaic-but-human document (§5.1) and a genuinely AI-assisted document are
hardest to tell apart, so the system should say "inconclusive," not "likely
AI, but only a little."

### 2.2 Mapping raw signal output to a calibrated score

The sigmoid normalization in §1.1/§1.2 *is* the calibration step — it maps an
unbounded raw statistic (perplexity, coefficient of variation) onto a 0–1
scale centered at a midpoint chosen from expected typical values. These
midpoints (`PPL_MID`, `COV_MID`) are **placeholders** for launch; before
relying on this in production, they need to be fit against a small labeled
set (e.g. 50 known-human + 50 known-AI samples) by picking the midpoint that
best separates the two groups' raw statistic distributions. Until that
calibration pass happens, this is flagged as a known limitation, not treated
as ground truth.

### 2.3 Thresholds / bands

| `confidence_score` | Band |
|---|---|
| `>= 0.70` | `likely-ai-assisted` |
| `0.30 – 0.70` (exclusive) | `uncertain` |
| `<= 0.30` | `likely-human` |

Bands are wide (a 0.40-point uncertain zone) on purpose — given the shared
false-positive population described in `README.md` §1, a narrow "confident"
zone would assert far more certainty than two cheap heuristics can support.

## 3. Transparency label design

Exact copy, parameterized only by the numeric score. No other free text is
generated per-request — hedging language is baked into the template, not
improvised, so it can't accidentally drift into an assertion of fact.

**`likely-ai-assisted`** (score ≥ 0.70):
> "Our automated signals indicate a high likelihood of AI involvement in this
> text (AI-likelihood score: {score:.2f}). This is a probabilistic signal,
> not a factual determination — the creator may appeal this label."

**`uncertain`** (0.30 < score < 0.70):
> "Our automated signals were inconclusive about AI involvement in this text
> (AI-likelihood score: {score:.2f}). No determination has been made, and no
> restriction is applied based on this score alone."

**`likely-human`** (score ≤ 0.30):
> "Our automated signals did not find strong indicators of AI involvement in
> this text (AI-likelihood score: {score:.2f})."

Note the asymmetry: only the `likely-ai-assisted` label mentions the appeal
path. Appealing a `likely-human` or `uncertain` result is still technically
allowed (§4.1) but isn't surfaced as an expected action, since those labels
don't impose anything on the creator to contest.

## 4. Appeals workflow

### 4.1 Who can appeal

Whoever holds the `submission_id` (an unguessable UUID issued at submission
time — it functions as a capability token, no separate account system
required for this milestone). Any band can be appealed, though in practice
almost all appeals will target `likely-ai-assisted`.

### 4.2 What they submit

`POST /appeal` with `{ submission_id, reason }`. `reason` is required,
minimum 20 characters (rejects empty/placeholder appeals), free text — the
creator explains why they believe the label is wrong (e.g., "I wrote this
myself, English is my second language and I follow a fixed essay template").
An optional `evidence` field (free text or a link) lets them point to drafts,
revision history, etc.

Constraints: one open appeal per submission at a time — a second `POST
/appeal` on a submission that already has a `pending` or `appealed` appeal
is rejected with `409 Conflict`. `/appeal` is rate-limited via
`flask-limiter` to prevent spam.

### 4.3 What happens on receipt

1. Validate the submission exists and has no open appeal.
2. Create an appeal record: `{ appeal_id, submission_id, reason, evidence,
   status: "pending", submitted_at }`.
3. Update the submission's status to `appealed` (separate from its `band`,
   which is left untouched — the original algorithmic result stays visible).
4. Write an audit log entry: `{ event: "appeal_filed", appeal_id,
   submission_id, reason, timestamp }`.
5. Return `{ appeal_id, submission_id, status: "pending", submitted_at }`.

No signal is re-run at this step. Re-scoring with the same two heuristics
would reproduce whatever mistake triggered the appeal.

### 4.4 Reviewer queue

`GET /appeals?status=pending` returns a list, newest-first, of everything a
reviewer needs to decide **without re-running anything**:

```json
{
  "appeal_id": "...",
  "submission_id": "...",
  "reason": "...",
  "evidence": "...",
  "submitted_at": "...",
  "original_submission": {
    "text_preview": "first ~300 chars, full text available on click-through",
    "band": "likely-ai-assisted",
    "confidence_score": 0.78,
    "signals": { "s1_perplexity": 0.82, "s2_burstiness": 0.71, "disagreement": 0.11 },
    "model_version": "groq/<model-id>@<date>",
    "submitted_at": "..."
  }
}
```

Surfacing `disagreement` and both raw signal scores directly in the queue
matters: a case where both signals agree strongly is a different kind of
appeal than one where they disagreed and got unlucky-averaged — the reviewer
should be able to tell those apart at a glance.

### 4.5 Resolution

`POST /appeals/<id>/resolve` with `{ reviewer_id, decision: "upheld" |
"overturned", reviewer_notes }`.

- Appeal status → `resolved`.
- If `overturned`: the submission's **displayed label** (not its stored
  `confidence_score` or `band`) is overridden to a fourth, reviewer-only
  state — `label_override: "human-confirmed"` — which takes precedence over
  the algorithmic band whenever the submission is rendered. The original
  score/band stay in the record for audit purposes; they are never deleted
  or rewritten.
- If `upheld`: submission status returns to its prior band-derived state,
  `label_override` remains unset.
- Audit log entry: `{ event: "appeal_resolved", appeal_id, reviewer_id,
  decision, reviewer_notes, resolved_at }`.
- Response: `{ appeal_id, status: "resolved", decision, resolved_at }`.

## 5. Anticipated edge cases

Specific failure modes tied to how Signals 1 and 2 are actually computed —
not generic "the model might be wrong."

### 5.1 Repetition-heavy, simple-vocabulary creative writing

A nursery rhyme, a villanelle, or a children's picture book manuscript that
leans on refrains and anaphora ("I will not eat them here, I will not eat
them there...") will drive **both** signals toward AI-like for reasons that
have nothing to do with authorship: repeated exact phrases make each token
highly predictable (Signal 1 → low perplexity → high `s1`), and deliberate
uniform meter/line length makes sentence length nearly constant (Signal 2 →
low CoV → high `s2`). Because both signals agree, the disagreement-damping
in §1.3 does *not* protect this case — it will confidently land in
`likely-ai-assisted`, even though the repetition is the deliberate craft of
the piece, not a symptom of machine generation.

### 5.2 Very short submissions (statistical floor, not a bias)

A one-sentence product review, a tweet, or a haiku doesn't contain enough
sentences to compute a meaningful variance for Signal 2 — `MIN_SENTENCES = 4`
already guards against a nonsense CoV value, but that guard means short
text is scored on Signal 1 *alone*, with damped confidence (`low_coverage`
in §1.3). The system will never confidently call a short text either way; it
degrades to `uncertain` for almost all very short input regardless of actual
authorship, which is the correct failure mode but worth naming explicitly:
Provenance Guard is structurally unable to make a confident call below
`MIN_SENTENCES`, independent of content quality.

### 5.3 Quotation-heavy or boilerplate legal/academic text

A legal brief quoting a statute verbatim, or a literature review quoting
long passages from cited sources, mixes the *submitter's* writing with
someone/something else's exact words. Both signals score the document as a
whole — they can't distinguish "the submitter wrote this" from "the
submitter copied this from elsewhere." If the quoted material happens to be
formal, low-perplexity legal or technical language, the score reflects the
quoted source's style, not the submitter's authorship, and there's currently
no attribution boundary in the design that separates quoted spans from
original prose.

### 5.4 Non-English or code-mixed text

Signal 1 depends on Groq's reference model's fluency in whatever language
the text is in; if the model is materially weaker in that language, its
token probabilities are noisier and less meaningful, so `raw_ppl` stops
correlating with "predictability" the way it does in English and the
calibration constants in §1.1 (tuned against English text) will be wrong in
either direction. Signal 2 is more language-agnostic but still assumes
punctuation-delimited sentences of roughly English-like length distribution,
which breaks down for languages with different sentence-terminal
conventions. Neither signal has a language-detection step today, so
non-English submissions get scored with English-calibrated thresholds with
no warning to the caller that the score is out of the calibration domain.

## 6. Architecture

### Submission flow

```
                        POST /submit
                        { text, creator_id?, metadata? }
                                |
                                v
              +-----------------+-----------------+
              |                                   |
              v                                   v
    +-------------------+              +----------------------+
    | Signal 1:          |              | Signal 2:             |
    | perplexity          |              | burstiness             |
    | (Groq API, per-     |              | (local, sentence-      |
    |  token logprobs)    |              |  length variance)      |
    +-------------------+              +----------------------+
              |                                   |
              | s1 (0-1)                          | s2 (0-1) or None
              +-----------------+-----------------+
                                |
                                v
                  +--------------------------+
                  | Confidence scoring        |
                  | combine (0.6*s1+0.4*s2)   |
                  | damp toward 0.5 on         |
                  | disagreement (see §1.3)   |
                  +--------------------------+
                                |
                  combined confidence_score + band
                                |
              +-----------------+-----------------+
              |                                   |
              v                                   v
   +----------------------+           +--------------------------+
   | Transparency label    |           | Audit log                 |
   | (hedged text, §3,     |           | s1, s2, raw_ppl,           |
   |  keyed off band)      |           | disagreement, combined     |
   +----------------------+           | score, model version,       |
              |                        | timestamp                    |
              |                        +--------------------------+
              v                                   :
   +----------------------------------------+     :
   | Response to caller                       |<....:
   | { submission_id, label, confidence_score,|
   |   band, signals, created_at }            |
   +----------------------------------------+
```

### Appeal flow

```
              POST /appeal
              { submission_id, reason, evidence? }
                        |
                        v
           +--------------------------+
           | Status update             |
           | pending -> appealed       |
           +--------------------------+
                        |
                        | appeal record
                        v
                +----------------+
                | Audit log      |
                +----------------+
                        |
                        v
           +--------------------------+
           | Human reviewer             |
           | GET /appeals?status=pending|
           | sees text preview, band,   |
           | confidence_score, s1, s2,  |
           | disagreement (§4.4)         |
           +--------------------------+
                        |
                        | POST /appeals/<id>/resolve
                        | { decision, reviewer_notes }
                        v
           +--------------------------+
           | Status update             |
           | appealed -> resolved      |
           +--------------------------+
              |                    |
   decision, resolved_at   response to caller
              |             { appeal_id, status,
              v               decision, resolved_at }
        +----------------+
        | Audit log      |
        +----------------+
```

### Narrative

A submission fans out to both signals in parallel; their raw scores meet at
the confidence-scoring step, which produces one `confidence_score` and band
that simultaneously drives the transparency label shown to the caller and an
audit-log entry carrying every intermediate number needed to reconstruct the
decision later. An appeal never re-enters the signal path — it moves the
submission through its own `pending → appealed → resolved` status machine,
and the only thing that can close it is a human reviewer's decision, which is
logged with the same rigor as the original scoring and, if the appeal is
upheld, overrides what's displayed without erasing the original score.

This is the reference diagram for Milestones 3–5: any code-generation prompt
for `/submit`, `/appeal`, or `/appeals/<id>/resolve` should point back to the
matching flow above rather than re-deriving the shape of the pipeline.

## 7. AI Tool Plan

This is my plan for using an AI coding tool for M3–M5: what spec I give it,
what I ask it to build, and how I check the result before trusting it. My
rule for all three milestones: I never plug generated code straight into the
endpoint. I test each piece by itself first, on a few inputs I pick myself.
A function can look fine and still be wrong (like using the wrong number in
a math formula), and that's much easier to catch on its own than after it's
buried inside a route.

### M3 — Submission endpoint + first signal

- **Spec I'll give it:** §1.1 (the perplexity signal — the formula and the
  numbers it uses) and the submission diagram in §6. I will not give it
  §1.2 or §1.3 yet, since M3 only needs one signal. I don't want it adding a
  second signal or a combine step early.
- **What I'll ask for:** a basic Flask app with a `POST /submit` route that
  takes `{ text, creator_id?, metadata? }`, plus a separate function
  `compute_signal1(text) -> float` that follows the §1.1 formula step by
  step (get logprobs from Groq → average them → turn that into a score).
  I'll ask for the Groq call and the math as two separate pieces, so I can
  test the math without needing to call the API every time.
- **How I'll check it before wiring it in:** I'll run `compute_signal1` by
  itself on a few texts I already have a guess about: something I wrote
  myself, something copied straight from an AI chat, and one very short
  text (from §5.2) just to make sure it doesn't crash. If the AI text
  doesn't score clearly higher than my own writing, the formula or the
  numbers are wrong, and I fix that before it ever touches `/submit`.

### M4 — Second signal + confidence scoring

- **Spec I'll give it:** all of §1 (both signals plus the combine step in
  §1.3), §2 (how the score should be read, plus the exact bands in §2.3),
  and the submission diagram in §6. I'm pasting the combine formula
  in directly instead of describing it in my own words, so the tool builds
  exactly that math and not something close to it.
- **What I'll ask for:** a function `compute_signal2(text) -> float | None`
  for burstiness (including the "too short to score" rule from §1.2), and a
  function `combine_scores(s1, s2) -> (confidence_score, band)` that does
  the combine-and-damp math from §1.3 and picks a band using §2.3.
- **What I'll check:** I'll pick a few texts I'm sure are AI-written and a
  few I'm sure are human-written (old emails, notes I wrote years ago), and
  run them all through both signals and `combine_scores`. What I'm looking
  for is a gap: the AI ones should land near `likely-ai-assisted` and the
  human ones near `likely-human`. If everything comes back near 0.5 no
  matter what I feed it, the signals aren't actually telling anything apart,
  and I go back and fix the numbers before touching M5.

### M5 — Production layer (labels + appeals)

- **Spec I'll give it:** §3 (the three label sentences, copied in exactly as
  written, so it doesn't write its own version) and all of §4 (who can
  appeal, the `pending → appealed → resolved` steps, what the reviewer sees,
  what happens on a decision), plus the appeal diagram in §6.
- **What I'll ask for:** a function that picks the right label sentence from
  §3 based on the band and fills in the score, plus the three routes —
  `POST /appeal`, `GET /appeals?status=pending`, and `POST
  /appeals/<id>/resolve` — built exactly to §4 (only one open appeal at a
  time, the fields the reviewer's list shows, and what changes when an
  appeal is overturned).
- **How I'll check it:** for labels, I'll set a submission's score to 0.85,
  then 0.5, then 0.1 by hand and confirm each one gives back the matching
  exact sentence from §3 — and specifically check the numbers right at 0.30
  and 0.70, since those edges are where a small mistake would quietly swap
  two labels. For appeals, I'll walk through the whole thing by hand: submit
  something, file an appeal, check the status is now `appealed` and that a
  second appeal on the same submission gets rejected, then resolve it as
  overturned and check the status becomes `resolved`, the override is set,
  and both log entries (appeal filed, appeal resolved) actually got written.
  If any one of those steps is missing, something in the appeal flow is
  silently losing data.
