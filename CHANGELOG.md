# saom-bot — bugfix + upgrade pass

Applied to `saom_deploy.py` from `Rishavsingh26/saom-bot@master`.

## Critical (fatal) bug

- **Lines 30 and 853 used `//` for comments instead of `#`.** That's JS syntax,
  not Python — `//` is the floor-division operator with no left operand there,
  so the file was a hard `SyntaxError`. **The bot could not start at all.**
  This also meant `AUTH_FILE` was undefined and `_load_auth()` was never
  called at startup.

## Other bugs fixed

1. **Authorized-users list lived only on local disk** (`authorized.json`)
   while every other piece of state (bans, conversations, profiles) persists
   through a pinned Telegram message. Render's disk is ephemeral, so every
   redeploy silently wiped who was allowed to use the bot. `authorized_users`
   is now folded into the same pinned-message persistence (`_build_state`,
   `_restore_state`, `_save_state` on every `/auth add|remove`); the local
   file is kept only as a secondary/offline fallback.
2. **Unbounded memory growth** — `message_log` was only trimmed to 1000
   entries at *save* time but kept growing without limit between saves
   (every 10 messages). Long-running deploys would slowly leak memory.
   Now capped to 2000 in-memory entries on every append.
3. **Long or malformed responses could silently vanish** — `sendMessage` had
   no length guard (Telegram's hard cap is 4096 chars) and no fallback if
   Markdown parsing failed (Telegram 400s on malformed Markdown, dropping
   the whole reply). Added a `send_message()` helper that truncates long
   text and retries as plain text if the Markdown send fails.
4. **One bad update could stall the whole poll loop** — nothing scoped
   per-update, so an exception while handling one message aborted the rest
   of that batch and triggered a blanket 5s sleep. Each update is now
   processed inside its own `try/except`.
5. **SSRF exposure** — `/fetch`, `/get`, and `/webhook` let any authorized
   user (not just the owner) make the bot request arbitrary URLs, including
   internal/private IPs and cloud metadata endpoints (`169.254.169.254`).
   Added `_is_safe_url()`, which resolves the hostname and blocks
   private/loopback/link-local/reserved/multicast addresses and non-http(s)
   schemes.
6. Two invalid `\(` escape sequences in vision prompts (SyntaxWarning
   under Python 3.12) cleaned up.

## Features added

- `/ping` — quick liveness check.
- `/uptime` — time since process start.
- Basic per-chat rate limiting (2s cooldown) on LLM calls, to prevent
  spam from burning through the Groq quota/bill. Returns a friendly
  "slow down" message instead of hammering the API.
- Graceful shutdown: `SIGTERM`/`SIGINT` now trigger `_save_state()`
  before exit, so a Render redeploy doesn't lose the last (up to 9)
  unsaved messages of conversation/profile state.
- `/help` text updated to list the new commands.

## Notes / things I deliberately left alone

- The owner-only `exec`/`run` shell command is a legitimate admin/debug
  console gated to `OM_CHAT_ID`, so I kept it — but it's worth knowing
  it exists: if the owner's Telegram account or `OM_CHAT_ID` env var is
  ever compromised, it's full remote code execution on the host.
- `user_profiles` is keyed by `chat_id`, which conflates "the group" and
  "whichever user last spoke in it" for group chats with multiple admins.
  It's harmless for private chats (chat_id == user_id there) and is only
  a display quirk (profile info like name/msg_count can get overwritten
  by whichever admin spoke most recently in a shared group), so I left it
  as-is rather than risk a wider refactor — flagging it here in case you
  want it fixed properly later.

## Verification performed

- `ast.parse()` — file parses with zero errors and zero warnings.
- `pyflakes` — no undefined names; only pre-existing cosmetic lint notes.
- Loaded the module directly (no real Telegram/Groq calls, since those
  hosts aren't reachable from this sandbox) and smoke-tested offline-safe
  paths: `/health`, `/ping`, `/uptime`, `/version`, `/whoami`, the SSRF
  guard (blocks private/loopback/metadata, allows public URLs), the rate
  limiter, `strip_latex()`, and `clean_name()`. All behaved as expected.

See `saom_deploy.diff` for the full unified diff against the original.
