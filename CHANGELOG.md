# Changelog

## 0.2.0

- Suggestions persist per chat (profile `plugin-data`) until used, dismissed,
  or superseded by the user's own message; they survive screen/chat switches
  and backend restarts, and expire after 7 days.
- Clicking the pill inserts the text into the composer instead of sending it.
- Desktop half talks to the backend through `ctx.rest` and the durable
  session id (`focusedStoredSessionId`).
- New `pre_llm_call` hook clears the pending suggestion when the user sends
  their own message; late generations for an older turn are discarded.
- Own auxiliary task (`auxiliary.next_prompt`) so the model can be changed.
- Messaging-platform turns are skipped (no LLM call nobody can see).
- Background work runs through `spawn_context_thread` (profile-scoped).

## 0.1.0 — Initial release

- `post_llm_call` hook generates follow-up suggestions via `ctx.llm`.
- Suggestion pill in the `composer.underside` area.
- Settings: enable/disable, context window size, cooldown.
