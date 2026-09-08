# P9 — Multi-Agent Debate System

**The failure this survives:** four agents agreeing because they were primed to,
producing a confident answer with no more information in it than a single call
would have had, at four times the price.

Status: working end to end. 20 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p09_debate.cli run "Should we move document ingestion from nightly batch to streaming?"
python -m p09_debate.cli personas
python ../scripts/smoke.py p09 && python -m pytest tests -q
```

## The only reason to run several agents is independence

Several correlated opinions are one opinion that costs five times as much.
Everything here protects that independence and then reports honestly on how much
the agents actually agreed.

Proposers never see each other's work. The moment one proposal lands in the
prompt, the next is anchored to it and the debate collapses into agreement with
whoever went first. There's a test asserting no proposal text reaches another
proposer's prompt.

Personas are structural rather than flavour text. Four agents given the same
prompt produce four samples from one distribution. These four have genuinely
different objectives (delivery, risk, cost, the end user) so they disagree for
reasons rather than by chance. Each one also has to state the strongest argument
against its own recommendation, since an advisor who can't name their own
downside hasn't thought about it.

## Five verdicts, because "the answer" isn't always one of them

| | When | Winner |
|---|---|---|
| `CONSENSUS` | ≥75% of the vote, no fatal flaw | yes |
| `MAJORITY` | >50%, with a real minority | yes |
| `CONTESTED` | tied, or below majority | none |
| `BLOCKED` | the vote winner has a disqualifying flaw | none |
| `FAILED` | fewer than two proposals, or no valid votes | none |

A tie is a result. Splitting 2–2 tells you the question is genuinely contested
and a human should see it. Breaking ties by arbitrary ordering manufactures a
decision nobody made.

Blocking doesn't promote the runner-up either. If the critic disqualifies the
proposal that won, the answer is "no recommendation, here's the flaw" rather than
the option nobody voted for. Promoting it would invent a mandate.

## Confidence is measured, not asserted

```
confidence = 0.6 × measured agreement
           + 0.4 × mean proposer self-confidence
           − 0.05 × non-fatal flaws in the winner
```

Unanimity reports 0.92 and a 2–1–1 split reports materially lower. Half the room
disagreeing shouldn't read as confidently as unanimity, which is the whole point
of counting the vote instead of just reporting the winner.

## Guarding the guards

A critic that marks everything fatal is treated as uninformative rather than as a
blocking verdict, since flagging every proposal blocks the decision without
informing it.

A vote for a proposal that doesn't exist gets discarded and noted. Guessing what
the voter meant would invent a result.

One proposer failing shrinks the debate rather than ending it. Three advisors is
still a debate, and it goes in `notes` so the reader knows the sample was
smaller.

If synthesis fails, the winning proposal's own text comes back rather than
nothing.

## Cost shape

Proposals and votes run on Sonnet 5. Only the critique and the final synthesis
use Opus 5. Four proposals plus four votes on the top tier is where a debate's
cost runs away, and aggregation is where the capability actually matters. There's
a test pinning this.

## Where the code lives

`debate.py` holds the personas, the four stages, the verdict rules and the
confidence formula.
