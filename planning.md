# Provenance Guard — Planning

A service that estimates whether a piece of text was **AI-generated** or
**human-written**, returns a **calibrated confidence score**, and shows a
**plain-language transparency label**. Every
decision is **rate-limited** and **audit-logged**.

The design goal is *honest uncertainty*, not a confident binary. Detection is
inherently unreliable, so the system is built to say **"I'm not sure"** loudly
whenever its two independent signals disagree, and to give the human the last
word through appeals.

---

## 1. Detection signals

We deliberately pair **one model-free statistical signal** with **one semantic
LLM signal** so they can *disagree*. This leads to one side focusing semantically,
and the other side focusing structurally. Disagreement is not a bug but rather is the
mechanism that drives the final score toward "uncertain."

### Signal 1 — Stylometric heuristics (local, deterministic)

**What it measures (form, not meaning):**

| Sub-metric | Definition |
|------------|------------|
| Burstiness | Coefficient of variation of sentence lengths (`stdev / mean`). Humans alternate long, winding sentences with terse ones which results in high burstiness. Models are metronomic which leads to low burstiness. |
| Lexical diversity | Type/token ratio (unique words ÷ total), length-normalized. AI tends to reuse a safer, smaller vocabulary leading to lower diversity. |
| Connective/hedging density | Rate of polished transitions per 100 words ("moreover", "furthermore", "it's important to note", "in conclusion"). AI over-produces these. |

**Output:** a single score in range of `[0, 1]`which represents the estimated probability that the text is
AI-generated. The higher the score, the more likely content is AI generated. Format is fully deterministic and reproducible. This result will be stored to `p_style`

### Signal 2 — Groq LLM judge (`llama-3.3-70b-versatile`, temperature 0)

**What it measures (meaning and texture):** cliché density, generic "helpful
assistant" framing, absence of lived specificity, suspiciously even emotional
tone — semantic cues the statistics are blind to.

**Output:** strict-JSON `{ "p_ai": <float 0–1>, "rationale": "<one sentence>" }`. If Groq is unavailable (no key, timeout, error), this signal is **skipped** and the system degrades gracefully to Signal 1 only. The result of `p_ai` will be stored to `p_llm`

### Combining into one score

Both signals output a probability-of-AI in [0, 1]. We blend both results with a fixed weighted
average the LLM having slightly higher weight due to its nature of reading the meaning not just the shape:

```
p_final    = 0.45 * p_style + 0.55 * p_llm      # both signals present
p_final    = p_style                            # LLM unavailable (Signal 1 only)
```

However, in certain cases the averaging itself encodes uncertainty. For example,  if the signals disagree (0.9 vs 0.1),
`p_final ≈ 0.5` with no special-casing. Strong agreement (0.9 & 0.85) yields a score near the extremes while the opposite stands true as well.

---

## 2. Uncertainty representation

**What does confidence = 0.6 mean?** It means the system's blended estimate sits
only modestly off the coin-flip line. The two signals are leaning the same way
but weakly, *or* leaning opposite ways and partly cancelling. As such, a 0.6 is an explicit
"I lean this way but I am not sure," and it will always render as the **uncertain**
label, never as a verdict the reader should trust as fact.

**Mapping raw signal outputs to a calibrated score.**

```
p_final    = 0.45 * p_style + 0.55 * p_llm       # in [0, 1]
result     = "AI-generated"  if p_final >= 0.75,  "Human-written" if p_final <= 0.25, anything in between is uncertain but below or equal 0.50 is leaning human while above 0.50 is leaning AI
confidence = max(p_final, 1 - p_final)           # in [0.5, 1.0], distance from coin-flip
```

`confidence` is the distance of the blended estimate from 0.5, rescaled to
[0.5, 1.0]. It answers "how far from a coin flip are we?". Agreement pushes it
toward 1.0, disagreement collapses it toward 0.5 automatically.

**Single-signal cap.** When the LLM is unavailable, `p_final = p_style` and
**confidence is capped at 0.70**. A one-signal result can therefore never present
as high-confidence — we refuse to sound certain on half the evidence.

**Thresholds — the three regions:**

| Confidence band | Region | Meaning |
|-----------------|--------|---------|
| `0.50 – 0.74`   | **Uncertain** | Signals weak or in conflict. No trustworthy verdict. |
| `0.75 – 1.00`, result = AI    | **Likely AI** | Both signals agree the text is machine-written. |
| `0.75 – 1.00`, result = Human | **Likely human** | Both signals agree the text is human-written. |

So **0.75 is the line** between "uncertain" and "likely." Everything below 0.75
is presented as a tentative lean, not a verdict.

---

## 3. Transparency label design

There are three variants, chosen by `(result, confidence)` per the thresholds above. The
exact reader-facing text:

### High-confidence AI (confidence ≥ 0.75, result = AI-generated)

> **Likely AI-generated.** Our analysis indicates this text was probably
> produced by an AI system (confidence: {confidence:.0%}). This is an automated
> estimate, not proof. If you wrote this yourself, you can appeal this result.

### High-confidence human (confidence ≥ 0.75, result = Human-written)

> **Likely human-written.** Our analysis indicates this text was probably
> written by a person (confidence: {confidence:.0%}). This is an automated
> estimate, not a guarantee of authorship.

### Uncertain (confidence < 0.75, either result)

> **Uncertain.** Our signals disagree or are too weak to call this one
> (confidence: {confidence:.0%}, leaning {result}). Please do **not** treat this
> as a definitive judgment of authorship. You can appeal if needed.

Every variant states it is an *estimate*, shows the numeric confidence, and — for
any result that could harm a creator — surfaces the appeal route.


---

## 4. Appeals workflow

**Who can submit.** Anyone holding a `content_id` from a prior `/submit` response
aka, the creator of the analyzed text.

**What they provide.** `POST /appeal` with:
```json
{ "content_id": "<id from /submit>", "reasoning": "<free-text explanation>" }
```
The `reasoning` is required and might include "this is my original work, here is
my draft history."

**What the system does on receipt (Storage only — no re-classification):**
1. Validate the `content_id` exists (else `400`).
2. Insert a row into the `appeals` table: `appeal_id`, `content_id`, `reasoning`,
   `submitted_at`, `status = "open"`.
3. Flip the content row's status: `analyzed → under review`.
4. Append an `audit_log` entry (`event_type = "appeal"`) **beside the original
   decision**, capturing the appeal_id, content_id, and timestamp.
5. Return `{ content_id, status: "under review", appeal_id, message }`.

The original verdict is preserved (never overwritten) but is no longer presented
as final once status is `under review`.

**What a human reviewer sees in the appeal queue** (`GET /appeals?status=open`,
one row per open appeal):

| Field shown | Source |
|-------------|--------|
| `appeal_id`, `submitted_at` | appeals table |
| Original `text` excerpt | content row |
| Original `result` + `confidence` + `label_variant` | content row / audit log |
| Both signal scores `p_style`, `p_llm` and the LLM `rationale` | audit log |
| Appellant's `reasoning` | appeals table |
| Current `status` | content row |

The reviewer has everything needed to judge the appeal — the original evidence
*and* the human's rebuttal — in one view, without re-running detection.

---

## 5. Anticipated edge cases

Content this system will handle poorly, with the specific failure mode:

1. **Formal/structured human writing (sonnets, legal, academic, technical docs).**
   These are *naturally* uniform in sentence length and connective-heavy, so
   Signal 1 misreads their tidiness as machine-like → high `p_style` false
   positive. (We Mitigate this through Signal 2. It  reads genuine imagery/specificity and pulls
   `p_final` back toward 0.5; the result lands "uncertain" rather than a wrong
   confident "AI.")

2. **Very short text (a haiku, a tweet, a one-line comment).** Burstiness and
   lexical diversity need several sentences to estimate. On 1–2 sentences the
   variance is noise and `p_style` is essentially random. The score will be
   unstable and should not be trusted — ideally flagged as too-short to score.

3. **Lightly AI-edited human text (grammar-tool / "polish this" passes).**
   Genuine human authorship that's been smoothed drifts into the AI statistical
   region without being AI-*written*. Provenance is genuinely ambiguous here, and
   neither signal can cleanly separate "assisted" from "generated."

4. **Adversarially disguised AI ("write erratically, add typos and slang").**
   A model explicitly prompted to mimic human burstiness can fool Signal 1, and a
   fake-diary framing can talk Signal 2 out of an AI verdict. Both signals are
   gameable by a motivated adversary; the honest outcome is low confidence.

---

## Architecture

```
 ── SUBMISSION FLOW ───────────────────────────────────────────────
 ┌────────┐   raw text (JSON)    ┌──────────────────┐
 │ Client │ ───────────────────▶ │  Flask  /submit   │
 └────────┘                      └─────────┬─────────┘
      ▲                                    │ raw text
      │                          ┌─────────▼─────────┐
      │                          │   Rate limiter     │ 429 if over budget
      │                          │ (10/min, 100/day)  │
      │                          └─────────┬─────────┘
      │                                    │ raw text
      │                          ┌─────────▼──────────────────────────┐
      │                          │            Detector                 │
      │                          │  ┌───────────────┐  ┌────────────┐  │
      │                          │  │ Signal 1:     │  │ Signal 2:  │  │
      │                          │  │ Stylometric   │  │ Groq LLM   │  │
      │                          │  │ heuristics    │  │ judge      │  │
      │                          │  └──────┬────────┘  └─────┬──────┘  │
      │                          │  p_style│           p_llm │         │
      │                          │         ▼                 ▼         │
      │                          │     ┌───────────────────────┐       │
      │                          │     │  Confidence scoring    │       │
      │                          │     │  p_final, confidence   │       │
      │                          │     └───────────┬───────────┘       │
      │                          └─────────────────┼───────────────────┘
      │                            label+confidence │
      │                          ┌─────────────────▼─────────┐
      │                          │   Transparency label       │
      │                          │   (3 variants → label_text)│
      │                          └─────────────────┬─────────┘
      │                            decision record  │
      │                          ┌─────────────────▼──────────┐
      │                          │   Storage (SQLite)          │
      │                          │   content row (status)      │
      │                          │   + audit_log entry         │
      │                          └─────────────────┬──────────┘
      │  JSON: content_id, result,                 │ content_id
      │  confidence, label_text, signals           │
      └────────────────────────────────────────────┘

 ── APPEAL FLOW ───────────────────────────────────────────────────
 ┌────────┐  {content_id, reasoning}  ┌──────────────────┐
 │ Client │ ────────────────────────▶ │  Flask  /appeal   │
 └────────┘                           └─────────┬────────┘
      ▲                                          │ content_id, reasoning
      │                               ┌──────────▼───────────────────┐
      │                               │      Storage (SQLite)          │
      │                               │  1. insert appeals row         │
      │                               │  2. status → "under review"    │
      │                               │  3. append audit_log entry     │
      │                               │     (beside original decision) │
      │                               └──────────┬───────────────────┘
      │  JSON: {content_id,                       │ appeal_id, new status
      │  status:"under review", appeal_id}        │
      └───────────────────────────────────────────┘
```

**Submission flow.** Text arrives at `POST /submit`, clears the rate limiter, and
is scored by two independent detectors (local stylometrics + Groq LLM judge) whose
outputs are blended into `p_final`/`confidence`; the transparency module turns that
into one of three labels, and the full decision is persisted (content row +
audit-log entry) before the JSON response — including the `content_id` — returns to
the client.

**Appeal flow.** A creator re-enters at `POST /appeal` with their `content_id` and
reasoning; this touches Storage only — it writes an `appeals` row, flips the
content's status to `under review`, and appends an `appeal` audit entry beside the
original decision. No re-classification runs; the verdict is preserved but no
longer presented as final.

---

## AI Tool Plan

I have Three implementation milestones. For each: which spec sections feed the AI tool,
what I ask it to generate, and how I verify before moving on.

### M3 — Submission endpoint + first signal

- **Spec provided to the tool:** S1 *Detection signals* (Signal 1 sub-metrics) +
  the **Architecture** diagram (submission flow).
- **Ask it to generate:** a Flask app skeleton (`app.py` with `POST /submit` and
  `GET /health`) plus the first signal function `score_stylometric(text) ->
  p_style` in `detector.py`, computing burstiness, lexical diversity, and
  connective density.
- **Verify:** call `score_stylometric` directly on a handful of inputs (a known
  AI paragraph, a bursty human paragraph, an empty/short string) and confirm the
  scores fall in [0,1] and trend the right way **before** wiring it into the
  endpoint. Then hit `/submit` with curl and confirm it returns the score.

### M4 — Second signal + confidence scoring

- **Spec provided to the tool:** S1 *Detection signals* (Signal 2) + S2
  *Uncertainty representation* + the **Architecture** diagram.
- **Ask it to generate:** the second signal function `score_llm(text) -> (p_llm,
  rationale)` calling Groq with strict-JSON output and graceful fallback, plus the
  `combine(p_style, p_llm) -> {p_final, result, confidence}` scoring logic (weights,
  `max(p, 1-p)`, single-signal 0.70 cap).
- **Verify:** feed clearly-AI text and clearly-human text and confirm scores vary
  meaningfully (high vs low `p_final`); feed a conflicting case and confirm
  confidence collapses toward 0.5; kill the API key and confirm the cap at 0.70
  engages.

### M5 — Production layer (labels + appeals)

- **Spec provided to the tool:** S3 *Transparency label design* (the three exact
  variants) + S4 *Appeals workflow* + the **Architecture** diagram (appeal flow).
- **Ask it to generate:** the label-generation logic `make_label(result,
  confidence) -> {label_text, label_variant}` in `labels.py`, and the
  `POST /appeal` endpoint with its SQLite mutations (insert appeal, flip status,
  audit entry) plus `GET /appeals` for the reviewer queue.
- **Verify:** craft inputs that land in each confidence band and confirm **all
  three label variants are reachable**; submit an appeal for a real `content_id`
  and confirm the content status changes `analyzed → under review`, an `appeals`
  row is written, and an `appeal` audit entry appears beside the original decision.

---

## Appendix — API surface (the contract)

| Method & path     | Accepts                                   | Returns |
|-------------------|-------------------------------------------|---------|
| `POST /submit`    | `{ "text": "<content>" }`                 | `{ content_id, result, confidence, label_text, label_variant, signals: { p_style, p_llm, p_final }, status }` |
| `POST /appeal`    | `{ "content_id": "<id>", "reasoning": "<why>" }` | `{ content_id, status: "under review", appeal_id, message }` |
| `GET /appeals`    | `?status=open` (reviewer queue)           | `{ appeals: [ { appeal_id, content_id, text_excerpt, result, confidence, label_variant, p_style, p_llm, rationale, reasoning, status, submitted_at } ] }` |
| `GET /log`        | optional `?content_id=`                   | `{ entries: [ { ts, event_type, content_id, details } ] }` |
| `GET /content/<id>` | —                                       | `{ content_id, result, confidence, status, label_text }` |
| `GET /health`     | —                                         | `{ status: "ok" }` |

Contract notes:
- `result` ∈ `{ "AI-generated", "Human-written" }` — always the directional lean
  (never literally "uncertain"); uncertainty is carried by `label_variant`/`confidence`.
- `confidence` ∈ [0.5, 1.0], returned to 2 d.p.; rendered as a whole percent inside `label_text`.
- `label_variant` ∈ `{ high_confidence_ai, high_confidence_human, uncertain }`, chosen by
  `(result, confidence)` against the 0.75 threshold (S2/S3).
- `signals.p_llm` is `null` when the Groq judge is skipped (no key / timeout / error); then
  `p_final = p_style` and `confidence` is capped at 0.70 (single-signal, per S2).
- Errors: `400` on missing/empty `text` or unknown `content_id`; `429` when rate-limited.
- `/submit` is the only rate-limited route (10 requests/min, 100/day).

### Decisions / trade-offs

- **SQLite (stdlib) over a flat JSON file:** appeals must mutate a content row's status
  by `content_id` (`analyzed → under review`), which relational rows handle cleanly while
  keeping the original decision and its audit-log entry intact beside it.
- **Graceful LLM degradation:** demo/tests run with no network, so the Groq judge is skipped
  and the system falls back to Signal 1 only — confidence capped at 0.70 so a one-signal
  result never presents as high-confidence.
- **Statistical signal is intentionally simple and transparent** — the goal is
  honest uncertainty, not a state-of-the-art detector.
