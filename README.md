# Next Prompt — Hermes Plugin

AI-generated follow-up prompt suggestions for [Hermes Agent](https://github.com/NousResearch/hermes-agent) Desktop.

After each completed turn, Hermes quietly asks a model for the natural next
step and, if there is one, shows it as a small pill under the composer. Click
it — or press **Ctrl/⌘+Shift+Y** — to use the text; it is **never** sent for
you. Click **×** to dismiss it.

On Hermes Desktop builds with the plugin SDK's composer API
(`host.composer.insertText`), using a suggestion puts the text straight into
the composer. On earlier builds it copies the text to the clipboard and tells
you so; paste it with Ctrl/⌘+V.

## Install

```bash
hermes plugins install next-prompt        # once listed in the plugin catalog
# or straight from GitHub:
hermes plugins install soymarketing/hermes-plugin-next-prompt
hermes plugins enable next-prompt
```

Then restart Hermes Desktop and turn **Next Prompt** on in
**Settings → Plugins** (desktop plugins are opt-in).

## Using it

- **Click the pill** or press **Ctrl/⌘+Shift+Y** to use the suggestion.
  The shortcut only acts on the suggestion shown in the chat you are looking
  at, and does nothing while that chat is working. Change it in
  **Settings → Keybinds**.
- **Command palette** (Ctrl/⌘+K): *Next Prompt: use suggestion* (previews the
  text) and *Next Prompt: dismiss suggestion*.
- **When a suggestion can't be generated**, a muted note takes the pill's
  place and says why — the model rejected its credentials, the provider is
  rate-limiting, or the model took too long — with a hint on hover. It never
  shows the provider's error text. Dismiss it with **×**; it also clears when
  you send your next message.

## How it works

1. **`post_llm_call` hook** — when a turn finishes in a Desktop/TUI session,
   a background worker asks `ctx.llm` for one short follow-up, or `NULL` when
   the agent is waiting for your answer or there is nothing to act on.
2. **Stays until you act on it** — the suggestion is kept per chat in the
   plugin's data directory (`ctx.state.data_dir`, under the profile's
   `plugin-data/`), so switching screens or chats, or a backend restart, does
   not lose it. It goes away when you use it, dismiss it, or send your own
   next message (`pre_llm_call`). Stale suggestions expire after 7 days.
3. **Desktop half** — reads the suggestion through the plugin's REST route
   (`ctx.rest` → `/api/plugins/next-prompt/suggestion`) and renders it in the
   `composer.underside` area.

Messaging platforms (Telegram, Discord, …) are skipped: nobody would see the
pill there, so no LLM call is spent on them.

## Choosing the model

Suggestions run on your **main model** unless you give the plugin's
auxiliary task its own. A small, fast model is plenty and far cheaper than an
Opus-class main model:

```bash
hermes config set auxiliary.next_prompt.provider anthropic
hermes config set auxiliary.next_prompt.model claude-sonnet-5
```

`hermes config set` warns that the key is not recognized — plugin tasks are
registered at runtime, and the value is still read. You can also pick it
interactively with `hermes model` → **Auxiliary** → **Next Prompt**. (Desktop's
Settings → Models → Auxiliary lists only Hermes' built-in tasks.) The change
applies from the next suggestion; no restart needed.

Measured with this version's prompt on 14 conversations (English and Spanish;
8 where a follow-up fits, 6 where the right answer is silence), each run three
times:

| Model | Correct | Median latency | Notes |
|---|---|---|---|
| `claude-opus-5-5` | 42/42 | 1.7 s | Most useful suggestions; the most expensive |
| `claude-sonnet-5` | 42/42 | 1.3 s | Recommended balance |
| `gpt-6-luna` (reasoning off) | 40/42 | 1.6 s | ~20× cheaper than Sonnet; sometimes follows the topic's language instead of yours |

"Correct" means it suggested when it should and stayed silent when it
should, wrote in the user's language, and did not claim the user had done
something.

Plugin settings:

```yaml
plugins:
  entries:
    next-prompt:
      settings:
        enabled: true            # toggle suggestions on/off
        max_context_messages: 4  # context size in messages: your last N/2 requests + replies (2-10)
        cooldown_seconds: 3      # minimum gap between suggestions per chat
```

## Privacy and cost

- Per completed turn, the model receives your last few requests and the
  agent's **final** reply to each (2 of each by default). Each is clipped to
  about 1,200 characters, keeping the start and the end. Tool calls, tool
  output and the agent's intermediate notes are never sent.
- One short completion per turn (`max_tokens=100`, about 900 input tokens
  with Anthropic models), on the model you choose.
- No telemetry, no network access other than the host's own `ctx.llm` call.

## Requirements

- Hermes Agent ≥ 0.21 with the Desktop app.

## Design notes

- **Pill, not ghost text.** The plugin SDK does not expose the composer's
  input, so the suggestion lives in `composer.underside`, a first-class
  extension area.
- **Insert, don't send.** Using a suggestion only ever places the text for
  you to review. The bundle stays inside the plugin SDK: it uses
  `host.composer.insertText` when the host has it and falls back to the
  clipboard otherwise — it never touches the app's composer DOM or internal
  events, so composer changes can't break it silently.
- **Poll, not push.** `ctx.socket` is a no-op on OAuth remote backends, so the
  desktop half polls (every 2 s right after a turn, every 20 s otherwise).
- **Suggest, never answer for you.** Most finished turns have a useful next
  step (verify it, apply it elsewhere, dig into a detail), so the model is
  asked to find one. It stays silent when the agent asked you a question,
  offered a choice or needs information only you have, and when the exchange
  is just a thank-you. It never reports actions you haven't taken.
- **Your language.** The suggestion follows the language of your last
  message, not the topic's.

## Development

```bash
git clone https://github.com/soymarketing/hermes-plugin-next-prompt
cd hermes-plugin-next-prompt
hermes plugins validate .

# tests (need hermes-agent importable, e.g. its venv, and Node 22+)
python -m unittest discover -s tests -v
node --test "tests/frontend/*.test.mjs"
node tests/check_sdk_exports.mjs /path/to/hermes-agent

# try it
cp -r . "$HERMES_HOME/plugins/next-prompt"
hermes plugins enable next-prompt
```

The same checks run in GitHub Actions on every push and pull request. Python
changes need a full Desktop restart (quit it from the system tray too);
`desktop/plugin.js` hot-reloads.

## License

MIT
