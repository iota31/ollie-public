# ollie-executive

Shadow-safe foundation for Ollie's canonical goals, commitments, work,
evidence, and value-event loop. It does not invoke workers, Hands, timers, or
notifications. SQLite is configured for WAL mode and every schema migration is
transactional.

```bash
cd ollie-executive
python3 -m ollie_executive --db /tmp/ollie-executive.db init
python3 -m ollie_executive --db /tmp/ollie-executive.db goal-add \
  --title "Reliable autonomy" --outcome "Ollie closes work without supervision"
python3 -m ollie_executive --db /tmp/ollie-executive.db status
```

Selection is deliberately inspectable. It first applies strict class priority:

1. founder commitments and promised follow-ups
2. blockers
3. active-goal work
4. maintenance
5. exploration

Only then does it use an integer score within the winning class:
`3×expected value + 2×urgency + confidence − effort − 2×risk`. Stable creation
time and ID ordering break ties. No eligible work is a valid result.

A commitment cannot enter `verified`, `failed`, or `cancelled` without an
evidence record linked to that same commitment. The database enforces this,
not merely the CLI.

