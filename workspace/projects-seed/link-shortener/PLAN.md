# PLAN — link-shortener

## Phase 1: skeleton
- [ ] Init repo (git, `ollie/link-shortener` branch), pick stack
      (suggest: Python 3.12 + FastAPI + SQLite via stdlib `sqlite3`,
      uv-managed venv — but your call, record it in Decisions)
- [ ] Project layout + health endpoint + first passing test

## Phase 2: core
- [ ] POST /shorten (validation, idempotency) + tests
- [ ] GET /<code> 301 + 404 + tests
- [ ] GET /stats/<code> + hit counting + tests
- [ ] Restart-durability test

## Phase 3: delivery
- [ ] README (quickstart + API examples)
- [ ] Run on the box; curl transcript of all endpoints into journal
- [ ] Repo home decision resolved; PR opened
- [ ] DONE → Tushar review

## Later (out of scope v1)
- custom aliases, expiry, auth, Docker
