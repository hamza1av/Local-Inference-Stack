"""Example 04 - decision models: classify, gate (yes/no) and score, with automatic fallback.

Run:   just agents::example 4
       just agents::example 4 "The update broke my login and support never answered."

A decision model (uzu `d3-flash`, "System One" API at $GATEWAY_URL/decisions) answers *typed
questions* about a text in one forward pass and returns calibrated probabilities - no text
generation, no JSON parsing, fast and deterministic. Use it wherever an agent has to branch:

    choice  "which of these?"       routing (see stack_agents/graph.py), intent, topic
    noul    "yes or no?"            guards: "does this need a tool?", "is the answer grounded?"
    score   "how much, on a scale?" grading, urgency, quality checks

On 16 GB the decision model is usually off (gateway answers 503). `decide()` then asks the
`task` model each question with a strict-JSON prompt, validates the reply, and returns the
same shape with `"fallback": true`. The fallback's confidence is self-reported by a small model,
so treat it as a rough hint, not a calibrated probability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stack_agents.decide import DecisionClient, choice, score, yes_no

TEXT = "The headphones arrived a day early and sound amazing. I already recommended them to two friends."


def main(text: str) -> None:
    client = DecisionClient()  # URL/key from AGENTS_DECIDE_URL / gateway defaults
    d = client.decide(
        text,
        {
            # 1) classification: criteria = option name -> description (or None)
            "topic": choice(
                "What is this message about?",
                {"praise": "Compliments a product or service", "question": "Asks for information or help",
                 "complaint": "Reports a problem"},
            ),
            # 2) gate: P(yes). Optional criteria describe what yes/no mean
            "needs_reply": yes_no("Does this message need a reply from support?",
                                  true="the customer has a problem or question", false="nothing to do"),
            # 3) score: levels from low to high, answer = expected level index
            "urgency": score("How urgent is it?", ["none", "low", "medium", "high"]),
        },
        priority="background",  # tells the gateway this is not interactive work
    )

    print(f"model: {d.model}   fallback: {d.fallback}")
    topic, reply, urgency = d.answers["topic"], d.answers["needs_reply"], d.answers["urgency"]
    print(f"topic:       {topic.choice} (confidence {topic.confidence:.2f})")
    print(f"needs reply: {reply.yes} (p_yes {reply.noul:.2f})")
    print(f"urgency:     {urgency.level} (score {urgency.score:.2f} of {len(urgency.legend) - 1})")

    # How you'd branch on it in an agent:
    if topic.choice == "complaint" and reply.yes and urgency.score >= 2:
        print("-> escalate to a human")
    elif reply.yes:
        print("-> draft a reply with the chat model")
    else:
        print("-> archive")

    print("\nraw response:\n" + json.dumps(d.to_api(), indent=2))


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or TEXT)
