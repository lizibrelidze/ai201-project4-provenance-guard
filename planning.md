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

### 1.1 Signal 1 — LLM judgment score (model-based, via Groq)

- **Measures:** a reference LLM's own stylistic judgment of how AI-like the
  text reads — predictability of word choice, uniformity of sentence
  structure, generic/formulaic phrasing — reported directly as a 0–1 number.
  (Originally specced as raw perplexity via per-token `logprobs`; switched
  because Groq does not currently support `logprobs`/`echo` on any hosted
  model, so token-level probabilities of arbitrary input text aren't
  obtainable through the API. Same role in the pipeline, same output shape,
  different mechanism for getting there.)
- **Computation:** send the text to Groq chat completions with a system
  prompt instructing the model to judge writing style only (not content
  correctness) and return a single number 0.0–1.0, enforced via Groq's
  `json_schema` structured-output mode (`{"ai_likelihood": <float>}`) so the
  response always parses. `temperature=0` for determinism. See
  `signals.py::compute_signal1` (prompt in `build_signal1_prompt`, parsing/
  clamping in `parse_signal1_response`, both testable without hitting the
  API; the live call is isolated in `compute_signal1` itself).
- **Output:** float 0–1, clamped. The model's raw JSON reply is logged
  alongside it.
- **Calibration note:** there's no `PPL_MID`/`PPL_SCALE`-style constant here
  — calibration lives in the prompt wording instead. Verified informally
  against two hand-picked samples (a clearly-formulaic AI paragraph scored
  `0.8`, a clearly-casual human paragraph scored `0.1`); this is a smoke test
  showing the signal discriminates in the expected direction, not a
  calibrated accuracy measurement.

### 1.2 Signal 2 — Stylometric composite (structural, local, no API call)

- **Measures:** two independent stylometric properties, combined into one
  score:
  1. **Burstiness** — coefficient of variation (CoV) of sentence length
     across the document, i.e. how uniform vs. bursty the rhythm is.
  2. **Filler/casual-word density** — fraction of words drawn from a small
     set of casual/hedge markers (`like`, `honestly`, `kinda`, `lol`, `just`,
     ...).
  (Originally speced as burstiness alone. Type-token ratio was tried as a
  second metric and measured on the four M4 test paragraphs, but at
  ~40–55-word paragraph lengths TTR barely varied — 0.86–0.90 across a
  clearly-AI, clearly-human, and two borderline samples — there isn't enough
  text at that length for repetition patterns to show up. Filler-word density
  measured on the same four samples actually discriminated (0.091 on the
  casual sample vs. 0.000 on all three formal-register ones), so it replaced
  TTR.)
- **Computation:**
  ```
  sentences = sentence_tokenize(text)
  if len(sentences) < MIN_SENTENCES:      # MIN_SENTENCES = 3
      s2 = None                            # insufficient data, see §5.2
  else:
      lengths        = [word_count(s) for s in sentences]
      cov            = stdev(lengths) / mean(lengths)
      burst          = 1 / (1 + exp((cov - COV_MID) / COV_SCALE))

      density        = filler_word_count(text) / word_count(text)
      filler         = 1 / (1 + exp((density - FILLER_MID) / FILLER_SCALE))

      s2 = 0.65 * burst + 0.35 * filler   # burstiness weighted higher: it's
                                           # the better-understood metric;
                                           # filler density is a secondary
                                           # casualness catch
  ```
  Calibration constants: `COV_MID = 0.45`, `COV_SCALE = 0.15`,
  `FILLER_MID = 0.02`, `FILLER_SCALE = 0.02` (all placeholders pending a
  labeled-set calibration pass, same caveat as §2.2).
  `MIN_SENTENCES` was lowered from an initial placeholder of 4 to 3 after
  testing on the four M4 samples showed 3 of them (2–3 sentences each) are
  realistic paragraph lengths, not the "very short" edge case §5.2 is about
  — gating Signal 2 out for ordinary 3-sentence submissions defeated the
  point of having two signals. A 2-sentence input still falls back to
  single-signal mode, which is correct.
- **Output:** float 0–1, or `None` when the document is too short to produce
  a meaningful variance estimate. `signals.compute_signal2_debug` exposes the
  raw `cov`, `burst_component`, `filler_density`, and `filler_component`
  separately, for diagnosing which sub-metric is driving a given score.

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
`s1`, `s2`, disagreement, and `low_coverage` are all written to the audit
log alongside it, along with Signal 1's raw model reply and Signal 2's raw
`cov` value.

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

The two signals are calibrated differently now that Signal 1 isn't a math
transform of a raw statistic:

- **Signal 2** still works the way §1.2 describes: the sigmoid normalization
  maps the unbounded coefficient-of-variation statistic onto 0–1, centered on
  `COV_MID`. That midpoint is a **placeholder** for launch; before relying on
  it in production it needs to be fit against a small labeled set (e.g. 50
  known-human + 50 known-AI samples) by picking the midpoint that best
  separates the two groups' CoV distributions.
- **Signal 1** has no equivalent constant to tune — the model returns a 0–1
  judgment directly, so "calibration" means tuning the prompt wording in
  `signals.SIGNAL1_SYSTEM_PROMPT` against the same labeled set, not fitting a
  midpoint. This was smoke-tested (not calibrated) against two hand-picked
  samples — see §1.1 — and is flagged as a known limitation until a real
  labeled-set pass happens.

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

Whoever holds the `content_id` (an unguessable UUID issued at submission
time — it functions as a capability token, no separate account system
required for this milestone). Any band can be appealed, though in practice
almost all appeals will target `likely-ai-assisted`.

### 4.2 What they submit

`POST /appeal` with `{ content_id, creator_reasoning }` (renamed from the
original `reason` -- `creator_reasoning` is the field name the M5 spec
actually asks for). Both required, free text — the creator explains why they
believe the label is wrong (e.g., "I wrote this myself, English is my second
language and I follow a fixed essay template").

Constraints: one open appeal per submission at a time — a second `POST
/appeal` on a submission whose status is already `under_review` is rejected
with `409 Conflict`.

### 4.3 What happens on receipt

M5 implements a simplified version of this (no separate `appeal_id`, no
`evidence` field, no reviewer queue yet — those stay future work, §4.4/§4.5):

1. Validate the `content_id` exists and has no open appeal.
2. Update the submission's status to `under_review` (renamed from
   `appealed`) — separate from its `band`, which is left untouched (the
   original algorithmic result stays visible).
3. Update that submission's existing audit-log entry **in place** — merge in
   `status: "under_review"` and `appeal_reasoning: <creator_reasoning>`
   rather than appending a disconnected new entry, so `GET /log` shows the
   appeal *alongside* the original classification fields (`attribution`,
   `confidence`, `llm_score`, `stylometric_score`) in one entry, not two.
4. Return `{ content_id, status: "under_review", message: ... }`.

No signal is re-run at this step. Re-scoring with the same two heuristics
would reproduce whatever mistake triggered the appeal.

### 4.4 Reviewer queue

`GET /appeals?status=pending` returns a list, newest-first, of everything a
reviewer needs to decide **without re-running anything**:

```json
{
  "appeal_id": "...",
  "content_id": "...",
  "reason": "...",
  "evidence": "...",
  "submitted_at": "...",
  "original_submission": {
    "text_preview": "first ~300 chars, full text available on click-through",
    "band": "likely-ai-assisted",
    "confidence_score": 0.78,
    "signals": { "s1_llm_judgment": 0.82, "s2_stylometric": 0.71, "disagreement": 0.11 },
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
sentences to compute a meaningful variance for Signal 2 — `MIN_SENTENCES = 3`
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
    | LLM judgment score  |              | stylometric composite  |
    | (Groq chat call,    |              | (local, burstiness +   |
    |  json_schema output)|              |  filler density)       |
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
   | { content_id, label, confidence_score,|
   |   band, signals, created_at }            |
   +----------------------------------------+
```

### Appeal flow

```
              POST /appeal
              { content_id, reason, evidence? }
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
  for the stylometric composite (including the "too short to score" rule
  from §1.2), and a
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
