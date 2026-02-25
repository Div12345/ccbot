# ccbot Concierge Rebuild — Progress Tracker

## Verified Working
- [x] discovery.py — scans backends, models, MCPs, sessions
- [x] state.py — project-name-first unified state
- [x] concierge.py — intent recognition, proposals, buttons
- [x] bot.py wiring — /status, /c, /threads registered + cc: callbacks
- [x] /status from main area → concierge project list with action buttons
- [x] [Resume arterial] button → sends Enter to tmux
- [x] /threads → lists all projects with thread binding status
- [x] Privacy fix — bot receives messages inside forum topic threads
- [x] Bot can send replies inside threads (message_thread_id works)
- [x] Switched to Opencode_job_bot (Duncad_bot rate-limited ~10h)
- [x] Session monitor rate limiting — sliding window 20 msg/60s in message_queue.py
- [x] Removed DEBUG_UPDATE handler (was temporary diagnostic)
- [x] Thread auto-binding — [Resume] inside thread binds it to project
- [x] In-thread text routing — already existed, now auto-bound via [Resume]
- [x] /c natural text — main area text already routes to concierge
- [x] AIORateLimiter capped — max_retries=2, overall_max_rate=20/60s
- [x] Session monitor batching — combine events per poll cycle (~10-50x reduction)
- [x] Commit + push all new modules

## Next Up
- [ ] Thread cleanup — close orphan threads via /threads buttons
- [ ] Update .env with Opencode_job_bot token (hook blocks edit)

## Future (after basics work)
- [ ] Agent-assisted setup (concierge uses cheap model for intelligence)
- [ ] Subscription/quota tracking in /status
- [ ] MCP health in /status
- [ ] Dynamic model/backend switching per session
- [ ] Swap back to Duncad_bot after rate limit expires (~6 AM Feb 25)

## Known Issues
- agent-browser: can't target in-thread reply input (types in "New Thread" area)
- Duncad_bot: rate-limited until ~6 AM Feb 25 (session monitor flooding)
