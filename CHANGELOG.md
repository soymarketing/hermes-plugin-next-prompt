# Changelog

## 0.3.0

- **Keyboard and palette.** Ctrl/⌘+Shift+Y uses the suggestion shown in the
  focused chat (rebindable in Settings → Keybinds); the command palette gets
  *Next Prompt: use suggestion* (with a preview) and *dismiss suggestion*.
- **Failures are visible.** When a suggestion can't be generated, a muted,
  dismissible note says why (credentials rejected, rate limit, timeout,
  other) with a hint, instead of failing silently. The provider's error text
  is never shown.
- **Better suggestions.**
  - Context is your recent requests plus the agent's final reply to each;
    tool traffic and intermediate notes are skipped, so tool-heavy turns no
    longer push your request out of the window. Long replies keep their
    ending, where conclusions and questions are.
  - The prompt now expects a next step on most turns instead of defaulting
    to `NULL`, never answers the agent's question for you, never claims you
    did something, and follows the language of your last message.
  - Lower temperature (0.3); the output cleaner also strips labels,
    markdown, smart quotes and `NULL` variants.
  - Measured on 14 cases × 3 runs: Opus 5.5 42/42, Sonnet 5 42/42,
    GPT-6-luna 40/42 (up from 6/10 on 0.2.x).
- README documents how to pick the model (`auxiliary.next_prompt`) with the
  measurements.
- Tests and CI in the repository: backend and Desktop suites, a check that
  every SDK import exists, and `hermes plugins validate` on every push.

## 0.2.1

- Desktop half is SDK-only (catalog rule 8): the pill uses
  `host.composer.insertText` when the host provides it (Desktop plugin SDK
  hook 1) and otherwise copies the suggestion to the clipboard with a notice.
  Removed the `[data-composer-target]` lookup and the internal
  `hermes:composer-*` window events.
- A clipboard failure is now reported instead of silently ignored.

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
