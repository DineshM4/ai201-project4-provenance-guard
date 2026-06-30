# Provenance Guard

A service that estimates whether a piece of text was **AI-generated** or
**human-written**, returns a **calibrated confidence score**, and renders a
**plain-language transparency label**. Every decision is **rate-limited** and
**audit-logged**, and any creator can **appeal** a verdict.

The design goal is *honest uncertainty*, not a confident binary. Detection is
inherently unreliable, so the system is built to say **"I'm not sure"** loudly
whenever its two independent signals disagree, and to give the human the last
word through appeals.

---

## Setup & running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Signal 2 (the Groq LLM judge) needs an API key. Put it in .env:
echo "GROQ_API_KEY=your_key_here" > .env

python app.py     # serves on http://127.0.0.1:5000
```

If `GROQ_API_KEY` is absent (or Groq times out / errors), the system degrades
gracefully to Signal 1 only — it still works, just with capped confidence.

> **macOS note:** AirPlay Receiver squats on port 5000. Disable it in System
> Settings → General → AirDrop & Handoff, or change the port in `app.py`.

### API surface

| Method & path        | Accepts                                          | Returns |
|----------------------|--------------------------------------------------|---------|
| `POST /submit`       | `{ "text": "<content>" }`                        | `{ content_id, result, confidence, label_text, label_variant, signals: { p_style, p_llm, p_final }, status }` |
| `POST /appeal`       | `{ "content_id": "<id>", "reasoning": "<why>" }` | `{ content_id, status: "under review", appeal_id, message }` |
| `GET /appeals`       | `?status=open`                                   | `{ appeals: [ { appeal_id, content_id, text_excerpt, result, confidence, label_variant, p_style, p_llm, rationale, reasoning, status, submitted_at } ] }` |
| `GET /log`           | optional `?content_id=`                          | `{ entries: [ { ts, event_type, content_id, details } ] }` |
| `GET /content/<id>`  | —                                                | `{ content_id, result, confidence, status, label_text }` |
| `GET /health`        | —                                                | `{ status: "ok" }` |

Evidence persists in `provenance.db` (SQLite): a `content` row per decision, an
`appeals` row per appeal, and an `audit_log` trail beside each (the audit
`submit` entry carries both signal scores plus the LLM rationale).

---

## Architecture overview — the path a submission takes

```
Client ──raw text──▶ POST /submit
                        │
                        ▼
                  Rate limiter (10/min, 100/day) ──▶ 429 if over budget
                        │
                        ▼
                  ┌─────────────── Detector ───────────────┐
                  │  Signal 1: stylometric heuristics       │ → p_style
                  │  Signal 2: Groq LLM judge (skippable)   │ → p_llm (or null)
                  └─────────────────┬───────────────────────┘
                        ▼
                  Confidence scoring  (combine → p_final, result, confidence)
                        │
                        ▼
                  Transparency label  (3 variants → label_text, label_variant)
                        │
                        ▼
                  Storage (SQLite)  content row (status="analyzed") + audit_log "submit"
                        │
                        ▼
        JSON ◀── { content_id, result, confidence, label_text, label_variant, signals, status }
```

A submission flows in one direction: **rate limiter → two independent detectors
→ blended score → label → persisted decision → JSON response.** The `content_id`
in that response is the user's ticket to look the decision back up
(`GET /content/<id>`, `GET /log`) or to appeal it.

The **appeal flow** re-enters at `POST /appeal` with a `content_id` + `reasoning`
and touches **storage only — no re-classification.** It writes an `appeals` row,
flips the content status `analyzed → under review`, and appends an `appeal`
audit entry beside the original decision. The original verdict is preserved but
no longer presented as final.

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

Code map: `detector.py` (Signal 1), `llm_judge.py` (Signal 2), `scoring.py`
(blend), `labels.py` (transparency text), `storage.py` (SQLite + audit),
`app.py` (Flask routes + rate limiting).

---

## Detection signals

The core design decision is to **deliberately pair two signals that fail
differently**, so their disagreement becomes information. One reads *form*
without understanding meaning; the other reads *meaning* without measuring form.
When they agree, that agreement is meaningful precisely because they arrived at
it through independent routes. When they disagree, the score collapses toward
"uncertain" — which is the honest answer.

### Signal 1 — Stylometric heuristics (`detector.py`)

Local, deterministic, no network. It measures three structural properties and
maps each to a probability-of-AI contribution, then averages them into
`p_style ∈ [0, 1]`:

| Sub-metric | What it measures | Why AI differs |
|------------|------------------|----------------|
| **Burstiness** | Coefficient of variation of sentence lengths (`stdev/mean`) | Humans alternate long winding sentences with terse ones (high variation); models are metronomic (low variation). |
| **Lexical diversity** | Length-normalized type/token ratio (MATTR, window 50) | AI reuses a smaller, safer vocabulary, so its diversity runs lower. |
| **Connective density** | Polished transitions/hedges per 100 words ("moreover", "furthermore", "it's important to note") | AI over-produces these connective-tissue phrases. |

**Why I chose it.** It is free, instant, fully reproducible, and — critically —
*explainable*. A reviewer can look at the three numbers and understand exactly
why the score came out the way it did. It also has no shared failure mode with
an LLM: it doesn't know what the text *means*, so it can't be talked out of a
verdict by clever framing the way a language model can.

**What it misses.** It is blind to meaning. It cannot tell a sonnet from a spam
email if both are tidy; it cannot detect a cliché or a generic "as an AI
assistant" tone. It is also unreliable on short text — burstiness needs several
sentences before its variance means anything (the code returns `None` for that
sub-metric below two sentences and re-weights the rest).

**What I'd change for real deployment.** The reference points (`_BURSTINESS_REF
= 0.80`, `_DIVERSITY_REF = 0.70`, `_CONNECTIVE_REF = 4.0`) and the equal 1/3
weights are hand-set for transparency, not fit to data. In production I'd
calibrate them against a labeled corpus (and per-genre, since academic prose and
casual chat have very different baselines), and I'd add a perplexity-style
feature from a small local model, which is a stronger statistical signal than
hand-counted connectives.

### Signal 2 — Groq LLM judge (`llm_judge.py`)

`llama-3.3-70b-versatile`, temperature 0, strict-JSON
`{ "p_ai": <float>, "rationale": "<one sentence>" }`. It reads what the
statistics can't: cliché density, generic "helpful assistant" framing, absence
of lived specificity, suspiciously even emotional tone.

**Why I chose it.** It captures semantic texture that no n-gram statistic sees,
and it produces a human-readable rationale that goes straight into the appeal
queue as evidence. Temperature 0 keeps it as reproducible as an LLM gets.

**What it misses.** It is gameable by framing — a "write this as a nervous diary
entry" prompt can talk it out of an AI verdict — and it can be overconfident on
formal human writing. It also depends on a paid external API, so it must be
*optional*: if the key is missing or the call fails, the signal is skipped
entirely (returns `(None, None)`) rather than crashing the request.

**What I'd change for real deployment.** I'd add a short timeout + retry with
backoff (currently a single 15s timeout), cache identical submissions, and
consider an ensemble of two differently-prompted judges to reduce the
single-prompt framing weakness.

---

## Confidence scoring

### How signals are combined (`scoring.py`)

```
p_final    = 0.45 * p_style + 0.55 * p_llm      # both signals present
p_final    = p_style                            # LLM unavailable (Signal 1 only)
result     = "AI-generated" if p_final > 0.5 else "Human-written"   # directional lean
confidence = max(p_final, 1 - p_final)          # distance from a coin flip, in [0.5, 1.0]
```

**Why this approach.** I deliberately used a **plain weighted average with no
special-casing**, because the averaging itself encodes uncertainty for free.
When the two signals disagree (say 0.9 vs 0.1), `p_final` lands near 0.5 and
`confidence = max(p, 1−p)` collapses toward 0.5 automatically — I never had to
write an "if the signals conflict, lower the confidence" rule, the arithmetic
does it. The LLM gets the slightly higher weight (0.55) because reading meaning
is generally more informative than counting sentence lengths.

`confidence` is defined as *distance from a coin flip*, which is the honest thing
to report: it answers "how far from 50/50 are we?" rather than pretending to be a
calibrated probability.

**The single-signal cap.** When the LLM is unavailable, `confidence` is capped at
**0.70**. A one-signal result can therefore *never* clear the 0.75 "high
confidence" line — we refuse to sound certain on half the evidence.

### How I validated it produces meaningful variation

I fed clearly-different inputs through `combine()` and the live `/submit`
endpoint and confirmed the score moves, plus verified the conflict case collapses
toward 0.5 and the single-signal cap engages at exactly 0.70. Two real results
from Milestone 4 testing (both signals live):

**High-confidence case — a terse, idiosyncratic human paragraph:**

> *"I woke up. The coffee was cold again, bitter, the kind of cold that makes you
> question every decision. Rain. My cat knocked the mug off the counter and just
> stared, a tiny furious god. I laughed, then didn't."*

| | value |
|---|---|
| `p_style` | **0.00** |
| `p_llm` | **0.10** |
| `p_final` | **0.055** |
| `result` | Human-written |
| **confidence** | **0.94** |
| `label_variant` | `high_confidence_human` |

Both signals agree strongly (bursty sentence lengths, vivid specificity), so
confidence pushes toward 1.0.

**Lower-confidence case — connective-heavy "corporate AI" text:**

> *"It is important to note that artificial intelligence has become increasingly
> prevalent across industries. Moreover, it offers numerous benefits.
> Furthermore, it streamlines workflows. Additionally, it unlocks new
> capabilities. In conclusion, AI represents a transformative technology."*

| | value |
|---|---|
| `p_style` | **0.47** |
| `p_llm` | **0.90** |
| `p_final` | **0.71** |
| `result` | AI-generated |
| **confidence** | **0.71** |
| `label_variant` | `uncertain` |

Here the LLM is sure it's AI (0.90) but Signal 1 reads the short, varied prose as
near-human (0.47). The disagreement drags `p_final` to 0.71 and the result lands
**uncertain** rather than a confident "AI" — exactly the honest-uncertainty
behavior the design is built around.

**0.94 vs 0.71 on adjacent inputs is the proof the score is not a constant** —
agreement and disagreement move it in opposite directions, with no hand-tuned
special cases.

**What I'd change for real deployment.** `confidence` is currently a *geometric*
distance from 0.5, not a true calibrated probability. Before trusting these
numbers I'd run a calibration study so that "80% confidence" actually means "wrong about 1 in 5
times." I'd also revisit the fixed 0.45/0.55 weights — they're a reasonable prior
but should be learned.

---

## Transparency label

Three variants, chosen by `(result, confidence)` against the **0.75** threshold
(`labels.py`). Every variant states it is an *estimate*, shows the numeric
confidence as a whole percent, and surfaces the appeal route on any result that
could harm a creator. `{confidence}` is interpolated as a whole percent and
`{result}` as the directional lean.

### `high_confidence_ai` — confidence ≥ 0.75, result = AI-generated

> Likely AI-generated. Our analysis indicates this text was probably produced by
> an AI system (confidence: {confidence}). This is an automated estimate, not
> proof. If you wrote this yourself, you can appeal this result.

### `high_confidence_human` — confidence ≥ 0.75, result = Human-written

> Likely human-written. Our analysis indicates this text was probably written by
> a person (confidence: {confidence}). This is an automated estimate, not a
> guarantee of authorship.

### `uncertain` — confidence < 0.75, either result

> Uncertain. Our signals disagree or are too weak to call this one (confidence:
> {confidence}, leaning {result}). Please do not treat this as a definitive
> judgment of authorship. You can appeal if needed.

*Example rendered (from the high-confidence case above):* "Likely human-written.
Our analysis indicates this text was probably written by a person (confidence:
94%). This is an automated estimate, not a guarantee of authorship."

---

## Rate limiting

**Limits:** `POST /submit` only — **10 requests/minute and 100 requests/day**,
keyed by client IP. No other route is limited.

**Reasoning for those specific values.** `/submit` is the only route that is both
*expensive* (it calls the paid Groq API on every request) and *mutating* (it
writes a new content row + audit entry); the read routes (`/log`, `/content`,
`/appeals`, `/health`) are cheap and idempotent, so rate-limiting them would only
hurt legitimate reviewers. 10/min is roughly one submission every six seconds —
comfortably above any honest interactive user pasting in passages to check, but
far below a script hammering the endpoint to run up Groq cost or flood storage.
100/day then caps the total daily cost and storage exposure per client even if
they pace themselves under the per-minute limit.

**What I'd change for real deployment.** Keying by IP is the weak part: IPs are
shared behind NAT/corporate proxies (one limit unfairly shared by many users) and
trivially rotated by an attacker. I'd key by authenticated API token / account
instead. I'd also move the limiter's storage from the current in-memory backend
(limits reset on restart and aren't shared across workers) to Redis, so the
limits actually hold in a multi-process deployment.

---

## Known limitations

**Formally written works are most likely to be incorrectly identified as AI-generated content, and not identified as human-generated**. All three factors included in our measurement of our neural network algorithm are indicators of orderliness. They are designed to measure "orderliness" through having low variation in sentence length, limited vocabulary, and a high number of connecting words (e.g., however, as a result, furthermore, etc.). Since formal writing is created to be orderly, as in the case of a legal brief, where every sentence has been constructed in a similar manner and contains many uses of connecting words (or connecting phrases such as, notwithstanding), or as in the case of an academic abstract that has been compressed and has a uniform tone, it would be viewed by Signal 1 as machine-produced due to the high number of similarities, and thus the `p_style` value reported from Signal 1 could be interpreted as being very high. As far as we are concerned, the objective of our second signal measurement, Signal 2, is to read and identify the underlying content of the formal writing — since the meaning of a dense legal clause does not have lived experience, it would not be reasonable to expect the LLM judge to find any human texture to rely on, causing the LLM judge to agree with the false positive from the Signal 1 measurement, rather than correcting it. Thus, the cumulative confidence values created by the two signals will lead to very high confidence in a false positive.

There's another issue with **very short textual materials** (tweets, one-line comments, haikus) where measurement for burstiness and lexical diversity doesn't get much variance until there are multiple sentences. So with one or two sentences, the reported p_style for the text will essentially have randomness and therefore, it will always provide a random value. The system loves to drop sub-measures that it can't measure for re-adjustment of weights, so the correct answer would be "too short to score," but the system still reports a value.

The appeals workflow and the reason each harmful label has an appeal process is that the system is built on the assumption that it will misclassify what appear to be confident posting of formal materials, allowing the final decision to be made by a human.

---
## Spec reflection

**Where the spec helped.** The spec's confidence section gave me an exact,
testable rule instead of a vague goal: `confidence = max(p_final, 1 − p_final)`,
a fixed 0.45/0.55 blend, and an explicit instruction that disagreement should
*automatically* drive the score toward 0.5 "with no special-casing." That turned
the fuzzy aspiration "be honest about uncertainty" into a single arithmetic line
I could implement and verify directly — my conflict test (`combine(0.9, 0.1)` →
confidence 0.54) confirmed the behavior fell out of the formula without any
conflict-detection branch. The spec also pinned down the single-signal 0.70 cap,
which made the "never sound certain on half the evidence" rule concrete.

**Where my implementation diverged, and why.** The spec contained an internal
tension about the `result` field. Section 2 describes result thresholds where the
middle band is "uncertain" (`result = "AI-generated" if p_final ≥ 0.75 …
in between is uncertain`), but the Appendix contract note states `result ∈
{ "AI-generated", "Human-written" }` and is *always* the directional lean, never
literally "uncertain," with uncertainty carried by `label_variant`/`confidence`.
I followed the **Appendix**: `result` is purely the lean (`p_final > 0.5 → AI`,
else Human) and the word "uncertain" lives only in `label_variant`. I diverged
from the literal §2 phrasing because a machine-readable API field should have a
clean closed set of values, and splitting "the lean" from "how sure we are" into
two fields is cleaner for any client than overloading one field with three
meanings.

A smaller divergence: the spec defines lexical diversity as "type/token ratio,
length-normalized" without saying how. Plain TTR isn't actually
length-normalized — it sinks as text grows — so I implemented **MATTR**
(moving-average TTR over a 50-token window) to deliver on the *intent* of the
phrase rather than its literal minimum.

---

## AI usage

I used an AI coding assistant (Claude) as a pair programmer, but I drove the
project: I wrote `planning.md` first, broke the build into milestones, and fed
the assistant one milestone at a time so I could verify each piece before moving
on. I treated its output as a draft to review, not an answer to accept. A few
specific instances where my direction or my corrections shaped the result:

**1. Keeping the first milestone scoped to Signal 1.** I asked it to build
`score_stylometric` and the `/submit` skeleton from the Signal 1 spec, and it
came back with a much larger slice — scoring, labels, and storage all wired in at
once. I pulled that back: I'd intentionally planned to ship Signal 1 alone first
so I could test the stylometric scores in isolation, so I told it to defer Signal
2 and appeals to later milestones. I also looked at its design calls — it chose
MATTR for length-normalization and equal 1/3 weights across the sub-metrics. I
kept MATTR because plain TTR isn't actually length-normalized, but I'm treating
the weights and the connective word-list as hand-set values I'd calibrate against
real data rather than numbers I trust.

**2. Making sure Signal 2 actually fails safely.** I asked it to write the Groq
judge with strict-JSON output at temperature 0 and a fallback when the API is
unavailable. When I went to test it, the first test script it wrote crashed on a
`find_dotenv` assertion because it loaded the `.env` from a heredoc; I had it fix
that with an explicit path. More importantly, I didn't take "it has a fallback"
on faith — I read the error handling myself to confirm that *any* failure
(timeout, bad JSON, missing key) returns `(None, None)` and degrades to
single-signal scoring instead of 500-ing the request, since that graceful
degradation is the whole reason Signal 2 is optional.

**3. Enforcing "no re-classification" on appeals.** I asked it to build
`POST /appeal` and `GET /appeals` as storage-only operations per §4. It produced
an atomic `create_appeal` and a reviewer queue, but I had to check one thing
carefully: the queue had to pull `p_style`, `p_llm`, and the LLM `rationale` out
of the stored audit log for the original submission, *not* re-run the detectors.
The spec is explicit that an appeal must preserve the original verdict, and I
wanted a reviewer judging against the exact numbers the system first produced — so
I verified the evidence was being read back from storage rather than recomputed.
