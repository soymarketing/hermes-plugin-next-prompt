# Next Prompt — Hermes Plugin

AI-generated follow-up prompt suggestions for [Hermes Agent](https://github.com/NousResearch/hermes-agent) Desktop.

After each completed turn, Hermes quietly asks the model whether there is a
natural next step and, if there is, shows it as a small pill under the
composer. Click it to drop the text into the composer (it is **not** sent —
edit it or press Enter yourself). Click **×** to dismiss it.

## Install

```bash
hermes plugins install next-prompt        # once listed in the plugin catalog
# or straight from GitHub:
hermes plugins install soymarketing/hermes-plugin-next-prompt
hermes plugins enable next-prompt
```

Then restart Hermes Desktop and turn **Next Prompt** on in
**Settings → Plugins** (desktop plugins are opt-in).

## How it works

1. **`post_llm_call` hook** — when a turn finishes in a Desktop/TUI session,
   a background worker asks `ctx.llm` for one short follow-up (or `NULL` when
   there is nothing useful to suggest).
2. **Stays until you act on it** — the suggestion is kept per chat and saved
   under the profile's `plugin-data/next-prompt/`, so switching screens or
   chats, or a backend restart, does not lose it. It goes away when you use
   it, dismiss it, or send your own next message (`pre_llm_call`). Stale
   suggestions expire after 7 days.
3. **Desktop half** — reads the suggestion through the plugin's REST route
   (`ctx.rest` → `/api/plugins/next-prompt/suggestion`) and renders it in the
   `composer.underside` area.

Messaging platforms (Telegram, Discord, …) are skipped: nobody would see the
pill there, so no LLM call is spent on them.

## Configuration

Suggestions run on your main model by default. To use a cheaper or faster
model, point the plugin's auxiliary task at it in `config.yaml`:

```yaml
auxiliary:
  next_prompt:
    provider: openrouter
    model: vendor/small-fast-model
```

Plugin settings:

```yaml
plugins:
  entries:
    next-prompt:
      settings:
        enabled: true            # toggle suggestions on/off
        max_context_messages: 4  # recent messages sent for context (2-10)
        cooldown_seconds: 3      # minimum gap between suggestions per chat
```

## Privacy and cost

- The last few messages of the chat (each truncated to 500 characters) are
  sent to the configured model once per completed turn.
- One short completion per turn (`max_tokens=100`), on the model you choose.
- No telemetry, no network access other than the host's own `ctx.llm` call.

## Requirements

- Hermes Agent ≥ 0.21 with the Desktop app.

## Design notes

- **Pill, not ghost text.** The plugin SDK does not expose the composer's
  input, so the suggestion lives in `composer.underside`, a first-class
  extension area.
- **Insert, don't send.** Clicking uses the composer's insert event, so you
  always review the text before it goes out. If no composer is visible, the
  text is copied to the clipboard instead.
- **Poll, not push.** `ctx.socket` is a no-op on OAuth remote backends, so the
  desktop half polls (every 2 s right after a turn, every 20 s otherwise).
- **No suggestion beats a bad one.** The model is told to answer `NULL` when
  the task is done or the agent asked you a question.

## Development

```bash
git clone https://github.com/soymarketing/hermes-plugin-next-prompt
cp -r hermes-plugin-next-prompt "$HERMES_HOME/plugins/next-prompt"
hermes plugins validate "$HERMES_HOME/plugins/next-prompt"
hermes plugins enable next-prompt
```

Python changes need a Desktop restart; `desktop/plugin.js` hot-reloads.

## License

MIT
