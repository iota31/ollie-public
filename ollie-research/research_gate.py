#!/usr/bin/env python3
"""Ollie Curiosity Engine — the SEMANTIC GATE (the token-saver).

Sits between mechanical pollers and the (expensive) research/POC job pipeline.
Drops candidates that are STALE or OFF-INTEREST *before* any costly research
runs. This is explicitly NOT a keyword filter (keyword-only relevance is
forbidden, ISC-A5): relevance is measured by EMBEDDING cosine similarity to an
interests centroid, so a piece about "ONNX quantization for edge inference"
scores high against the interest "on-device AI" even with zero shared words.

Two cheap-to-expensive stages:
  1. recency HARD filter  — drop older than the source's recency_days (no model)
  2. embedding relevance  — cosine vs interests centroid, penalized by anti-interests
                            borderline band routed to an optional fast-LLM judge

Pluggability (so tests run OFFLINE and the deterministic path needs no network):
  - gate.EMBED_FN : embed_fn(texts)->[vector]. Default = lazy fastembed
    (BAAI/bge-small-en-v1.5, ONNX, no torch) from the shared venv. If fastembed
    is unavailable we DEGRADE to a clearly-marked lexical-overlap BACKSTOP and
    log it — never crash. Tests assign gate.EMBED_FN = <stub>.
  - gate.JUDGE_FN : judge_fn(borderline_candidates, interests)->[bool]. Optional
    fast-LLM tiebreak (e.g. make_groq_judge()). Default None -> borderline kept.
    Tests assign gate.JUDGE_FN = <stub>.

Contracts (match EXACTLY):
  CANDIDATE = {source_id, source_type, url, title, text(<=1500), ts(ISO|None),
               domain_tags[str], fingerprint}
  SCORED    = CANDIDATE + {relevance(0..1), recency_factor(0..1), gate_reason}
  Rejects are DROPPED from the returned list (and logged with a reason).

Public API:
  recency_factor(ts, recency_days, now_ts=None) -> float
  relevance_score(candidate, interests) -> float
  score_and_filter(candidates, interests, now_ts=None, sources_by_id=None)
      -> [scored survivors]
"""
import json
import math
import os
import re
import time

# --- runtime paths (computed at import; tests reassign these module globals) --
HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
LOGS = f"{HOME}/.openclaw/logs"
WA_SECRETS = f"{HOME}/.openclaw/secrets/whatsapp-cloud.json"  # groqApiKey lives here

DAY_S = 24 * 3600
MAX_TEXT = 1500

# Relevance band (cosine-derived, mapped to 0..1):
#   relevance <  LOW_THRESHOLD            -> DROP "off-interest"
#   LOW_THRESHOLD <= r < BORDERLINE_HIGH  -> BORDERLINE (route to judge if set)
#   relevance >= BORDERLINE_HIGH          -> KEEP
LOW_THRESHOLD = 0.30
BORDERLINE_HIGH = 0.50
# How hard an anti-interest match pulls relevance down (subtracted from sim).
ANTI_PENALTY = 0.60
# Recency decay floor at the window edge (1.0 now -> RECENCY_FLOOR at edge).
RECENCY_FLOOR = 0.30

# Pluggable hooks (assigned by integrator/tests; None -> default behavior).
EMBED_FN = None
JUDGE_FN = None

# Lazy fastembed resolution cache.
_LAZY_EMBED = None
_LAZY_TRIED = False


def _paths():
    return {"log": f"{LOGS}/research-gate.log"}


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        path = _paths()["log"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------- recency --------
_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_iso(ts):
    """Best-effort ISO8601 -> epoch seconds. None on failure. Tolerates a
    trailing 'Z' and a timezone offset like +02:00 (offset is dropped — the box
    runs everything in one local zone, exact tz drift is < the day-grain we
    gate on)."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    # strip a +HH:MM / -HH:MM offset (but not the leading date dashes)
    s = re.sub(r"([+-]\d{2}:?\d{2})$", "", s).strip()
    for fmt in _ISO_FORMATS:
        try:
            return time.mktime(time.strptime(s, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def recency_factor(ts, recency_days, now_ts=None):
    """Freshness multiplier in [0,1].

      ts is None/unparseable -> 0.5 (unknown age: neither boosted nor dropped)
      age > recency_days      -> 0.0 (caller DROPS, gate_reason "stale Xd>Yd")
      otherwise               -> linear decay 1.0 (now) -> RECENCY_FLOOR (edge)

    now_ts is injectable for deterministic tests. Monotonically non-increasing
    in age."""
    if now_ts is None:
        now_ts = time.time()
    if not ts:
        return 0.5
    t = _parse_iso(ts)
    if t is None:
        return 0.5
    try:
        window = max(1, int(recency_days)) * DAY_S
    except (TypeError, ValueError):
        window = 14 * DAY_S
    age = now_ts - t
    if age > window:
        return 0.0
    if age < 0:  # future-dated item: treat as now
        age = 0.0
    frac = age / window  # 0..1 across the window
    return round(1.0 - (1.0 - RECENCY_FLOOR) * frac, 4)


# ---------------------------------------------------------- embeddings --------
def _get_embedder():
    """Return the active embed_fn or None (None => use lexical backstop).

    Priority: explicit gate.EMBED_FN > lazily-imported fastembed > None."""
    if EMBED_FN is not None:
        return EMBED_FN
    global _LAZY_EMBED, _LAZY_TRIED
    if not _LAZY_TRIED:
        _LAZY_TRIED = True
        try:
            from fastembed import TextEmbedding  # type: ignore

            _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

            def _fn(texts):
                return [list(v) for v in _model.embed(list(texts))]

            _LAZY_EMBED = _fn
            _log("embedder: fastembed BAAI/bge-small-en-v1.5 ready")
        except Exception as e:  # noqa: BLE001 — ImportError or model fetch fail
            _LAZY_EMBED = None
            _log(f"embedder: fastembed UNAVAILABLE ({e}) -> lexical backstop")
    return _LAZY_EMBED


def _candidate_text(candidate):
    title = (candidate.get("title") or "").strip()
    text = (candidate.get("text") or "").strip()[:MAX_TEXT]
    tags = " ".join(candidate.get("domain_tags") or [])
    return f"{title}\n{text}\n{tags}".strip() or title or "(empty)"


def _interest_phrases(interests):
    phrases = list(interests.get("domains") or [])
    phrases += list(interests.get("keywords_boost") or [])
    return [p for p in phrases if p] or ["(no interests configured)"]


def _cosine(a, b):
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sa = sb = 0.0
    for i in range(n):
        x, y = a[i], b[i]
        dot += x * y
        sa += x * x
        sb += y * y
    if sa <= 0 or sb <= 0:
        return 0.0
    return dot / (math.sqrt(sa) * math.sqrt(sb))


def _mean(vectors):
    vectors = [v for v in vectors if v]
    if not vectors:
        return []
    dim = min(len(v) for v in vectors)
    out = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            out[i] += v[i]
    return [x / len(vectors) for x in out]


def _clamp01(x):
    # float() coercion is load-bearing: embedder cosines are numpy.float32, and
    # round()/arithmetic preserve numpy types -> json.dump(queue.json) then raises
    # "Object of type float32 is not JSON serializable". Coerce to native float
    # here so every relevance/recency value the engine emits is JSON-safe.
    return float(0.0 if x < 0 else (1.0 if x > 1 else round(x, 4)))


def _relevance_from_vectors(cand_v, int_centroid, anti_centroid):
    sim_int = _cosine(cand_v, int_centroid)
    sim_anti = _cosine(cand_v, anti_centroid) if anti_centroid else 0.0
    return _clamp01(sim_int - ANTI_PENALTY * max(0.0, sim_anti))


# --- lexical backstop (FALLBACK ONLY — used iff no embedder is available) -----
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s):
    return set(_WORD.findall((s or "").lower()))


def _lexical_relevance(cand_text, interests):
    """Documented degraded path: token-overlap relevance when embeddings are
    unavailable. This is a keyword backstop ONLY — it exists so the engine keeps
    running (degraded) rather than crashing, and every use is logged. It is NOT
    the sanctioned relevance path."""
    cand = _tokens(cand_text)
    if not cand:
        return 0.0
    interest_toks = _tokens(" ".join(_interest_phrases(interests)))
    anti_toks = _tokens(" ".join(interests.get("anti_interests") or []))
    if not interest_toks:
        return 0.0
    hit = len(cand & interest_toks)
    anti_hit = len(cand & anti_toks)
    # normalize by the smaller of the two vocabularies so a short candidate that
    # hits several interest tokens still scores meaningfully.
    denom = max(1, min(len(cand), len(interest_toks)))
    rel = hit / denom - ANTI_PENALTY * (anti_hit / max(1, len(cand)))
    return _clamp01(rel)


def relevance_score(candidate, interests):
    """Embedding cosine relevance of a single candidate vs the interests
    centroid, penalized by anti-interest similarity. 0..1. Never raises:
    embedding failure -> lexical backstop."""
    cand_text = _candidate_text(candidate)
    embed = _get_embedder()
    if embed is None:
        return _lexical_relevance(cand_text, interests)
    try:
        phrases = _interest_phrases(interests)
        anti = list(interests.get("anti_interests") or [])
        texts = [cand_text] + phrases + anti
        vecs = embed(texts)
        cand_v = vecs[0]
        int_vs = vecs[1:1 + len(phrases)]
        anti_vs = vecs[1 + len(phrases):]
        int_centroid = _mean(int_vs)
        anti_centroid = _mean(anti_vs) if anti_vs else []
        return _relevance_from_vectors(cand_v, int_centroid, anti_centroid)
    except Exception as e:  # noqa: BLE001 — any embedder hiccup -> safe fallback
        _log(f"relevance embedding failed ({e}) -> lexical backstop")
        return _lexical_relevance(cand_text, interests)


def _batch_relevances(candidates, interests):
    """Compute relevance for many candidates with ONE embed call (centroid +
    all candidate texts). Returns list[float] aligned to candidates. Falls back
    to per-candidate lexical scoring if no embedder is available or embedding
    raises."""
    embed = _get_embedder()
    cand_texts = [_candidate_text(c) for c in candidates]
    if embed is None:
        return [_lexical_relevance(t, interests) for t in cand_texts]
    try:
        phrases = _interest_phrases(interests)
        anti = list(interests.get("anti_interests") or [])
        texts = phrases + anti + cand_texts
        vecs = embed(texts)
        int_vs = vecs[:len(phrases)]
        anti_vs = vecs[len(phrases):len(phrases) + len(anti)]
        cand_vs = vecs[len(phrases) + len(anti):]
        int_centroid = _mean(int_vs)
        anti_centroid = _mean(anti_vs) if anti_vs else []
        return [
            _relevance_from_vectors(cv, int_centroid, anti_centroid)
            for cv in cand_vs
        ]
    except Exception as e:  # noqa: BLE001
        _log(f"batch embedding failed ({e}) -> lexical backstop")
        return [_lexical_relevance(t, interests) for t in cand_texts]


# ------------------------------------------------------------- the gate -------
def score_and_filter(candidates, interests, now_ts=None, sources_by_id=None):
    """Recency hard-filter then embedding relevance. Returns the SURVIVORS only,
    each enriched with relevance / recency_factor / gate_reason. Every drop is
    logged with its reason. Never raises."""
    if now_ts is None:
        now_ts = time.time()
    sources_by_id = sources_by_id or {}
    candidates = list(candidates or [])

    # Stage 1: recency hard filter (cheapest, no model).
    recency_survivors = []
    rfacts = []
    for c in candidates:
        src = sources_by_id.get(c.get("source_id")) or {}
        try:
            recency_days = int(src.get("recency_days", 14))
        except (TypeError, ValueError):
            recency_days = 14
        rf = recency_factor(c.get("ts"), recency_days, now_ts=now_ts)
        if rf == 0.0 and c.get("ts"):
            t = _parse_iso(c.get("ts"))
            age_d = int((now_ts - t) // DAY_S) if t is not None else -1
            reason = f"stale {age_d}d>{recency_days}d"
            _log(f"DROP recency [{c.get('source_id')}] {c.get('url')} :: {reason}")
            continue
        recency_survivors.append(c)
        rfacts.append(rf)

    if not recency_survivors:
        return []

    # Stage 2: embedding relevance (batched).
    rels = _batch_relevances(recency_survivors, interests)

    kept = []          # (candidate, relevance, recency_factor, reason)
    borderline = []    # (index_into_kept_pending, candidate, rel, rf)
    pending = []       # candidates whose verdict awaits the judge
    for c, rel, rf in zip(recency_survivors, rels, rfacts):
        if rel < LOW_THRESHOLD:
            _log(f"DROP off-interest [{c.get('source_id')}] {c.get('url')} "
                 f":: rel={rel} < {LOW_THRESHOLD}")
            continue
        if rel >= BORDERLINE_HIGH:
            kept.append((c, rel, rf, f"on-interest rel={rel}"))
        else:
            pending.append((c, rel, rf))

    # Stage 3: borderline band -> optional fast-LLM judge.
    if pending:
        if JUDGE_FN is not None:
            verdicts = None
            try:
                verdicts = JUDGE_FN([c for c, _, _ in pending], interests)
            except Exception as e:  # noqa: BLE001 — judge failure -> keep all
                _log(f"judge failed ({e}) -> keeping {len(pending)} borderline")
            if verdicts is None:
                for c, rel, rf in pending:
                    kept.append((c, rel, rf, f"borderline judge-error-keep rel={rel}"))
            else:
                for (c, rel, rf), keep in zip(pending, verdicts):
                    if keep:
                        kept.append((c, rel, rf, f"borderline judge-keep rel={rel}"))
                    else:
                        _log(f"DROP judge-reject [{c.get('source_id')}] "
                             f"{c.get('url')} :: rel={rel}")
        else:
            for c, rel, rf in pending:
                kept.append((c, rel, rf, f"borderline kept-no-judge rel={rel}"))

    survivors = []
    for c, rel, rf, reason in kept:
        scored = dict(c)
        scored["relevance"] = rel
        scored["recency_factor"] = rf
        scored["gate_reason"] = reason
        survivors.append(scored)
    _log(f"gate: {len(candidates)} in -> {len(survivors)} survivors")
    return survivors


# ------------------------------------------------- optional Groq judge --------
def make_groq_judge(model="llama-3.1-8b-instant", timeout=20):
    """Factory for a PLUGGABLE borderline judge backed by Groq's fast tier
    (key in whatsapp-cloud.json groqApiKey, same source as reel_understand.py).
    OPTIONAL: the deterministic embedding path works without it. Assign the
    result to gate.JUDGE_FN to enable. Returns a list[bool] aligned to inputs;
    on any failure returns all-True (keep) so the gate degrades safely.

    Not exercised by the offline tests (they inject a stub JUDGE_FN)."""
    def judge(candidates, interests):  # pragma: no cover — network path
        try:
            key = json.load(open(WA_SECRETS)).get("groqApiKey", "")
        except Exception:  # noqa: BLE001
            key = ""
        if not key:
            _log("groq judge: no key -> keep all borderline")
            return [True] * len(candidates)
        import urllib.request

        out = []
        domains = ", ".join(interests.get("domains") or []) or "(none)"
        anti = ", ".join(interests.get("anti_interests") or []) or "(none)"
        for c in candidates:
            prompt = (
                "You gate a research feed. Reply with exactly one token: YES or "
                "NO.\nKeep (YES) only if the item is genuinely relevant to these "
                f"interests: {domains}.\nReject (NO) if it is off-topic or matches "
                f"anti-interests: {anti}.\n\nTITLE: {c.get('title','')}\n"
                f"TEXT: {(c.get('text') or '')[:600]}\n"
            )
            verdict = True
            try:
                body = json.dumps({
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 2,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode()
                req = urllib.request.Request(
                    "https://api.groq.com/openai/v1/chat/completions",
                    data=body,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json",
                             "User-Agent": "ollie-research-gate/1.0"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                ans = data["choices"][0]["message"]["content"].strip().upper()
                verdict = not ans.startswith("NO")
            except Exception as e:  # noqa: BLE001
                _log(f"groq judge error ({e}) -> keep")
                verdict = True
            out.append(verdict)
        return out

    return judge


if __name__ == "__main__":  # pragma: no cover — operational smoke
    emb = _get_embedder()
    print("embedder:", "fastembed/custom" if emb else "lexical-backstop")
