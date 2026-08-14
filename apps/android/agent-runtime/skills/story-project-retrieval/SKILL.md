---
name: story-project-retrieval
description: Retrieve evidence from a Storydex story project with bounded depth and explicit provenance. Use when writing or analyzing a turn requires chapters, characters, worldbook, wiki, presets, scripts, memory, or story-time facts.
---

# Story Project Retrieval

1. Read `.storydex/project.json` and the relevant indexes before scanning content.
2. Load active constraints and player state first, then recent full fragments, structured memory, and only the older sources needed to resolve a variable.
3. Scale retrieval depth with the configured reasoning level. Never load all prose by default.
4. Keep each conclusion traceable to a project-relative source. Treat locked memory and project files as stronger than conversational recall.
5. Never use `.storydex/usage/` as story evidence. Story and Narrator modes are read-only; Agent mode may write only after explicit user authorization.
