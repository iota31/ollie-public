"""
Write (POST / PUT / DELETE) endpoint handlers for Mission Control.

Moved verbatim (logic-preserving) from the `_add_source` / `_put_source` /
`_delete_source` / `_put_interests` / `_queue_reorder` / `_put_queue_item` /
`_delete_queue_item` methods of the original `DashboardHandler`. Path params
(`{id}`, `{fp}`) are captured + guarded by the route registry, so the 400-on-
bad-param behavior the tests assert is preserved (the dispatcher emits 400
before the handler runs).
"""
import uuid

import research_dashboard as rd
from . import route

# Same guards the legacy do_PUT/do_DELETE applied inline.
_ID_GUARD = rd._ID_RE
_FP_GUARD = rd._FP_RE


# ── Sources ─────────────────────────────────────────────────────────────────
@route("POST", "/api/sources")
def add_source(handler):
    body, err = handler._read_body()
    if err:
        handler._err(400, err); return
    ok, verr = rd.validate_source(body)
    if not ok:
        handler._err(400, verr); return
    sources = rd.load_sources()
    new_id = body.get("id") or f"src_{uuid.uuid4().hex[:8]}"
    if any(s.get("id") == new_id for s in sources):
        handler._err(400, f"source id {new_id!r} already exists"); return
    src = {
        "id":          new_id,
        "type":        body["type"],
        "target":      body["target"].strip(),
        "domain_tags": list(body.get("domain_tags") or []),
        "weight":      float(body.get("weight", 1.0)),
        "enabled":     bool(body.get("enabled", True)),
        "recency_days": int(body.get("recency_days", 7)),
        "added_at":    body.get("added_at") or rd._utcnow(),
    }
    sources.append(src)
    rd.save_sources(sources)
    handler._json(201, src)


@route("PUT", "/api/sources/{id}", guards={"id": _ID_GUARD})
def put_source(handler, id):
    body, err = handler._read_body()
    if err:
        handler._err(400, err); return
    sources = rd.load_sources()
    idx = next((i for i, s in enumerate(sources) if s.get("id") == id), None)
    if idx is None:
        handler._err(404, "source not found"); return
    src = dict(sources[idx])
    for field in ("type", "target", "domain_tags", "weight", "enabled", "recency_days"):
        if field in body:
            src[field] = body[field]
    ok, verr = rd.validate_source(src)
    if not ok:
        handler._err(400, verr); return
    sources[idx] = src
    rd.save_sources(sources)
    handler._json(200, src)


@route("DELETE", "/api/sources/{id}", guards={"id": _ID_GUARD})
def delete_source(handler, id):
    sources = rd.load_sources()
    before = len(sources)
    sources = [s for s in sources if s.get("id") != id]
    if len(sources) == before:
        handler._err(404, "source not found"); return
    rd.save_sources(sources)
    handler._json(200, {"deleted": id})


# ── Interests ───────────────────────────────────────────────────────────────
@route("PUT", "/api/interests")
def put_interests(handler):
    body, err = handler._read_body()
    if err:
        handler._err(400, err); return
    ok, verr = rd.validate_interests(body)
    if not ok:
        handler._err(400, verr); return
    interests = rd.load_interests()
    for field in ("domains", "keywords_boost", "anti_interests"):
        if field in body:
            interests[field] = body[field]
    interests["updated_at"] = rd._utcnow()
    rd.save_interests(interests)
    handler._json(200, interests)


# ── Queue ───────────────────────────────────────────────────────────────────
@route("POST", "/api/queue/reorder")
def queue_reorder(handler):
    body, err = handler._read_body()
    if err:
        handler._err(400, err); return
    if not isinstance(body, list):
        handler._err(400, "body must be an ordered list of fingerprint strings"); return
    if not all(isinstance(fp, str) and _FP_GUARD.match(fp) for fp in body):
        handler._err(400, "each fingerprint must be a non-empty alphanumeric/dash/underscore string"); return
    queue = rd.load_queue()
    fp_set = set(body)
    fp_idx = {fp: i for i, fp in enumerate(body)}
    updated = 0
    for item in queue:
        fp = item.get("fingerprint")
        if fp in fp_idx:
            item["manual_priority"] = fp_idx[fp]
            updated += 1
        elif fp not in fp_set:
            item["manual_priority"] = None
    rd.save_queue(queue)
    handler._json(200, {"reordered": updated})


@route("PUT", "/api/queue/{fp}", guards={"fp": _FP_GUARD})
def put_queue_item(handler, fp):
    body, err = handler._read_body()
    if err:
        handler._err(400, err); return
    queue = rd.load_queue()
    item = next((i for i in queue if i.get("fingerprint") == fp), None)
    if item is None:
        handler._err(404, "queue item not found"); return
    for k in ("status", "manual_priority"):
        if k in body:
            item[k] = body[k]
    rd.save_queue(queue)
    handler._json(200, item)


@route("DELETE", "/api/queue/{fp}", guards={"fp": _FP_GUARD})
def delete_queue_item(handler, fp):
    queue = rd.load_queue()
    before = len(queue)
    queue = [it for it in queue if it.get("fingerprint") != fp]
    if len(queue) == before:
        handler._err(404, "queue item not found"); return
    rd.save_queue(queue)
    handler._json(200, {"deleted": fp})
