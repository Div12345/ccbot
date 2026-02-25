# Concierge v1 — Setup Agent Plan

## Goal
Type what you want in Telegram → get a working session in one exchange.

## Architecture
```
bot.py (Telegram) → concierge.py (logic) → discovery.py (scanning) + state.py (truth)
```

## Stages (each gated — must pass before next)

### Stage 1: discovery.py
Extract from bot.py + enhance.

**Builds:**
- `scan_backends()` → `[{name, cli, installed, version}]`
- `scan_models(backend)` → `[{id, provider, name}]`
- `scan_mcps()` → `[{name, healthy, config_path}]`
- `scan_sessions()` → live tmux windows + their health

**Test:** `python -m ccbot.discovery` prints JSON of everything found.
**Gate:** Output matches reality (check against `tmux ls`, `which claude`, `which opencode`).

### Stage 2: state.py
Refactor state.json — project name is primary key, IDs are internal.

**Builds:**
- `get(project)` → `{window_id, thread_id, session_id, backend, model, context_pct, last_active, mcps}`
- `set(project, ...)` → updates state
- `list()` → all projects with status
- Migrates existing state.json on first load

**Test:** `state.get("arterial")` returns full info. `state.list()` shows all 3 projects.
**Gate:** Existing thread bindings + window mappings preserved. Round-trip: write → read → identical.

### Stage 3: concierge.py
Pure logic — no Telegram imports.

**Builds:**
- `handle_intent(text, state, discovery)` → `{type: "proposal"|"question"|"action", ...}`
- Intent recognition: "work on X" / "new session" / "fix X" / "status"
- Proposal: "resume arterial on opus, context 45%, paper-search MCP down — fix? [Resume] [Fresh] [Fix MCP]"
- Action execution: create tmux window, set model, install/restart MCPs

**Test cases:**
1. "work on arterial" with existing session → propose resume
2. "work on arterial" with dead session → propose fresh
3. "new opencode session" → propose backend + model + dir
4. "fix paper-search" → check MCP, restart, report

**Gate:** All 4 test cases return correct proposals. No Telegram imports in module.

### Stage 4: bot.py integration
Wire concierge into message handler.

**Builds:**
- Main thread messages → concierge.handle_intent()
- Render proposals as inline keyboard messages
- Button taps → concierge action execution
- Thread auto-binding on session launch

**Test (from phone):**
1. Type "work on arterial" in main thread → see proposal with buttons
2. Tap [Resume] → session wakes, thread binds, you're in
3. Type "status" → see all projects with health
4. Type "new session for brain" → walks through setup

**Gate:** All 4 work from Telegram on phone. No errors in bot logs.

## Model Strategy
- Dev/test: Claude (smart, catches issues)
- Prod: GPT-4.1 via Copilot/OpenCode (free)
- Concierge itself: cheapest available (config work, not deep thinking)

## Not in v1 (later)
- YAML harness files (research pending)
- Voice interface
- SSH interface
- Auto-compact suggestions
- Fork/agent session types
