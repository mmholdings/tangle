---
name: ask-user-question
description: Present a structured question and wait when a Tangle workflow requests AskUserQuestion but the current Claude environment has no native question tool
argument-hint: "question and 2-4 concrete choices"
---

# Ask User Question

Use this only when a Tangle skill requires the `AskUserQuestion` tool and the current environment does not provide it natively.

Present the question in Markdown and stop until the user replies:

```markdown
**Short header**: Question text

1. **Recommended choice** — concise impact or tradeoff
2. **Alternative** — concise impact or tradeoff
3. *(Other — type your own answer)*
```

Keep the header under 12 characters. Provide two to four mutually exclusive choices plus `Other`. For a multi-select question, say that multiple choices are allowed. Do not continue the gated workflow until the user answers.
