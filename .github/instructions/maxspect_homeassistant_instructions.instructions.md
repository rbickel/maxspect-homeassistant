---
description: Describe when these instructions should be loaded by the agent based on task context
# applyTo: 'Describe when these instructions should be loaded by the agent based on task context' # when provided, instructions will automatically be added to the request context when the pattern matches an attached file
---

<!-- Tip: Use /create-instructions in chat to generate content with agent assistance -->
Always put temporary/working/test files in a `_agent_workdir_` folder. This makes it easy to ignore them in `.gitignore` and ensures they won't be accidentally committed to the repo.
Always refer to `MAXSPECT_PROTOCOL.md` for the latest details on the Maxspect local protocol, which is crucial for implementing the `api.py` client correctly. Always update the Maxspect protocol documentation as you reverse-engineer or test the devices, so that the integration code and docs stay in sync.