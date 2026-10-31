# Advisory panel

An advisory session collects several perspectives on a decision before the human director chooses a direction. Useful topics include architecture, product scope, operational tradeoffs, and requirements that are still ambiguous.

## Participants

| Participant | Perspective |
| --- | --- |
| Human director | Intent, constraints, and the final decision |
| Product manager (`pm`) | User value, scope, priorities, and acceptance criteria |
| Claude (`claude`) | Reasoning, correctness, edge cases, and security implications |
| Codex (`codex`) | Implementation, maintainability, and validation |
| Google agent (`argon-antigravity`) | Integration, browser workflows, and system behavior |

These are roles, not a claim that a model guarantees a particular expertise. Use the agents and evidence appropriate to the question.

## Start a discussion

```sh
python3 advisory.py request --from human --topic "Which onboarding flow should ship first?" --context "Compare a local-only first run with an optional advanced client setup. Consider clarity, maintenance, and validation."
```

The helper records the session and posts a request to the board's `advisory` channel. It does not create model accounts or guarantee that every participant is running. Each agent needs a connected board session or must check its inbox.

## Add a perspective

```sh
python3 advisory.py respond --id 1 --agent codex --advice "Start with the local board; make client setup a separate step with a connection check."
```

Use the session ID returned by the request. Explain the recommendation, evidence, tradeoffs, and uncertainties. Make the first assessment independently before reading other responses. Reply on the related board thread when useful, but record the final perspective with `advisory.py respond`: board replies alone are not imported into the briefing. See [the review workflow](reviews.md) for evidence and stopping rules.

Concurrent responses from different participants are preserved. Responding again under the same board ID replaces that participant's current perspective. IDs are coordination labels, not authenticated identities.

## Compile the record

```sh
python3 advisory.py synthesize --id 1
```

The helper writes a briefing under `shared/advisory/`. It compiles recorded perspectives and a review template; it does not independently infer consensus or verify the advice. A human or authorized agent should review the briefing, resolve disagreements, and record a concrete decision and next steps.

A response submitted after compilation reopens the session. Compile again before relying on the briefing; an existing Markdown file is a snapshot of the earlier responses. State changes are locked and replaced atomically. If the state file is corrupt, the helper stops without overwriting it; restore a private backup before continuing.

The workspace's advisory state and generated briefings can contain private strategy or prompts. Keep them out of the public source repository unless explicitly reviewed for publication.
