# Cold-agent distribution log

Give each agent only `https://github.com/ctmmit/chorus` (or, before the push,
the checkout path restricted to README.md, SKILL.md, skills/, llms.txt,
AGENTS.md). Record whether it can obtain credentials, install a distribution
surface, build a soul, submit a digest, poll it to a terminal state, and
explain any failure without human help.

| Date | Agent | Attempt | Token | Install/connect | Soul | Submit/poll | Result | Notes |
|---|---|---:|---|---|---|---|---|---|
| 21 Sep 2026 | Claude (Sonnet) | 1 | given | HTTP only (curl) | seed | done on first poll; 4 highlights × 4 eps, 1 refused | PASS with friction | Local mock instance. Stalled on episode enumeration (`GET /shows` was undocumented), guessed `POST /personas` body from `/openapi.json`, `.txt` placeholder audio had no warning, `episode_title` null, agent card `url` was the localhost default. All four fixed the same day: SKILL.md §0 + persona body, placeholder warning, catalog title enrichment, card URL derived from the request. |
| TBD | Codex | 1 | pending | pending | pending | pending | pending | |
| TBD | Hermes | 1 | pending | pending | pending | pending | pending | |
| TBD | OpenClaw | 1 | pending | pending | pending | pending | pending | |
