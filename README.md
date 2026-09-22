# Next Prompt — Hermes Plugin

AI-generated follow-up prompt suggestions for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

After each completed turn, Hermes quietly generates a contextual follow-up
suggestion and shows it as a subtle pill below the composer. Click to send it,
or just start typing to dismiss.

![Next Prompt suggestion pill](docs/screenshot.png)

## Install

```bash
hermes plugins install soymarketing/hermes-plugin-next-prompt
```

Then enable it in **Settings → Plugins → Next Prompt**.

## How it works

1. **Python backend** hooks into `post_llm_call` — when a turn finishes (no
   tool calls), it uses `ctx.llm` to ask a fast auxiliary model whether there's
   a natural follow-up.
2. **Desktop UI** polls the backend and renders a pill in the
   `composer.underside` area — the floating zone below the composer.
3. **Click** the pill to send the suggestion. **Start typing** or click **×** to
   dismiss. The pill auto-clears when the agent starts a new turn.

The suggestion is generated with the host's auxiliary model (fast and cheap) and
never touches the main conversation context until you accept it.

## Configuration

In `config.yaml`:

```yaml
plugins:
  entries:
    next-prompt:
      settings:
        enabled: true            # toggle suggestions on/off
        max_context_messages: 4  # how many recent messages to consider (2-10)
        cooldown_seconds: 3      # minimum gap between suggestions
```

## Requirements

- Hermes Agent ≥ 0.21
- Hermes Desktop app (the suggestion pill is a desktop surface)
- An LLM provider configured (uses the auxiliary model for generation)

## Design decisions

- **Pill, not ghost text.** The plugin SDK doesn't expose the compositor's
  internal input field, so we use a pill in `composer.underside` — a first-class
  extension point. If Hermes core later exposes a ghost-text hook, we'll upgrade.
- **Poll, not push.** The Python backend generates suggestions in background
  threads; the desktop half polls every 2s. This avoids adding a custom websocket
  channel and works with remote/cloud backends where `ctx.socket` is a no-op.
- **Click sends.** Since `requestComposerInsert` isn't in the plugin SDK, clicking
  the pill sends the suggestion directly via `prompt.submit`. A future version
  could insert-to-edit if the SDK surface grows.
- **No suggestion is better than a bad one.** The generator returns `NULL` when
  the task is done, the agent asked a question, or there's no useful follow-up.
  The pill simply doesn't appear.

## Development

```bash
# Clone and link for local development
git clone https://github.com/soymarketing/hermes-plugin-next-prompt
cd hermes-plugin-next-prompt

# Install as a local directory plugin
hermes plugins install ./

# Validate
hermes plugins doctor .

# The desktop half hot-reloads — edit plugin.js and it updates live.
```

## License

MIT
