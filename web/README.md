# Console

One page that drives all eleven projects, for the consolidated demo. Each
project's CLI remains the canonical interface; this exists so a reviewer can
see everything without eleven terminals.

```bash
make dev      # from the repo root — FastAPI :8000 and this on :3000
```

Or separately:

```bash
uvicorn server.main:app --port 8000     # repo root
pnpm dev                                 # here
```

Next rewrites `/api/*` to the FastAPI app, so the browser stays on one origin
and there is no CORS preflight on every interaction during a demo.

## The design

**Organised by failure, not by feature.** Every project in this repo is defined
by the failure it survives, so the rail is a list of failure modes and each
panel opens with the one it handles.

**The verdict strip** is the one loud element. Every result leads with the
safety mechanism that engaged — `REFUSED`, `ESCALATED`, `LOOP DETECTED`,
`DUPLICATE SUPPRESSED`, `ROLLED BACK`, `KEPT THE BEST` — so you can read what
the system did about it without reading a single number. Colour encodes state
and nothing else.

**Mono-first.** IBM Plex Mono carries the interface and Plex Sans is used only
for real sentences, because nearly every element here is a measured value. It
is meant to read as a live test report, which is what the repo's own smoke
output is.

## Offline

The console shows `offline — recorded responses` when the API has no API key.
Panels that replay a cassette (P1, P2, P6, P7) show real model output; the
others show short, clearly-labelled placeholders, because their prompts depend
on accumulated conversation state and cannot be keyed deterministically. Set
`ANTHROPIC_API_KEY` and the badge switches to `live`.
