# Changelog

## Unreleased

- **"Generate suggestion" works on a dashboard backend.** Desktop connected
  to a `hermes dashboard` (no gateway session authority) has no plugin RPC,
  and the dashboard runs a profile's plugin API in a plugin host, apart from
  the process that runs the hooks, so the palette action answered "Regeneration
  is unavailable…". The API now queues the request in the plugin's SQLite
  store, and every process that runs the hooks checks for one each second
  and generates it. The note now only appears when no such process has been
  running for 15 seconds.
- **Updates take effect without a restart.** A process that reloaded the
  plugin after `hermes plugins install` or `update` kept the previous
  version's `store.py`, so new code could call functions it did not have.
  The store module is now keyed by its content as well as its path.

## 0.3.6

- **One pill per option when the agent offers a choice.** When the agent's
  last message names options and asks which one ("staging first, or straight
  to production?", "which of these three first?"), you get up to three
  pills, the one the conversation favours or the agent recommends first. Each
  is a complete reply; the shortcut and the palette use the first, and using
  any of them clears the row. Yes/no offers and single questions still get
  one pill. Measured on 11 conversations (Claude Sonnet 5.5 and GPT-6.1
  Terra): every choice got 2–3 options; every yes/no offer, finished
  task and no-suggestion case still got one pill or none.
- **Combine options into one message.** Clicking an option adds it to the
  composer and keeps the row, so you can add another ("do the images and the
  cache"); each lands as its own paragraph. The row goes away when you send,
  when every option is in, or with its ×. ⌘/Ctrl-click still sends one
  option at once. Without the composer API (older Desktop) a click copies
  that option, as before.
- **Several questions, every one answered.** When the agent asks several
  separate questions, the pill answers each in order, a short sentence
  apiece (up to about 50 characters per question instead of 80 for the
  whole reply). When the agent numbered its questions, the answers come
  numbered the same way, one per line, so you can change one in place.
  Numbered lines are one reply, never separate options.
- **API:** the suggestion payload adds `options` (every reply to show, `text`
  first). `text` is unchanged, so one-pill clients keep working. Stores from
  0.3.5 gain the column in place.

## 0.3.5

- **"Next Prompt: generate suggestion" now works on a standard install.** In
  0.3.4 it went through the dashboard API, which by default runs in the
  plugin host, a different process from the agent's hooks, so it always
  answered "unavailable". It now goes over the gateway's plugin RPC
  (`plugin.next-prompt.generate`), which runs next to the hooks; the REST
  route stays as the fallback on hosts without plugin RPC.
- **It also works for chats from before a restart.** The last turn's trimmed
  context (not the transcript) of the 50 most recent chats is kept for 7 days
  in the plugin's SQLite store, so you no longer need to send a message first.

## 0.3.4

- **Background turns no longer wipe the pill.** Turns Hermes injects on its
  own (a background process that matched a watch pattern or finished, a
  `[System: …]` note) neither clear nor replace the pending suggestion. A
  real message from any platform, Telegram included, still clears it.
- **"Next Prompt: generate suggestion" in the Command Palette.** Asks for a
  new suggestion from the chat's last turn when none showed up or it didn't
  fit. Where the plugin API runs in a different process from the agent's
  hooks (a secondary profile in a multi-profile gateway), it says
  regeneration is unavailable there.
- **Fewer valid suggestions dropped.** Up to 220 characters (was 160), and
  code in backticks such as `arr[0]` or `List<str>` is no longer mistaken
  for a blank to fill in. Placeholders are still filtered, backticks or not.
- **Each dropped suggestion is logged with its reason** (`empty`,
  `null_answer`, `fill_in_placeholder`, `too_short`, `too_long`).

Thanks to @madiajijah11 (#4, #5).

## 0.3.3

- **A clear note when the model's provider is unavailable.** When the
  provider pinned in `auxiliary.next_prompt` has no usable credentials (a
  subscription limit, a missing login) and no fallback took over, the note
  under the composer now says so and points to `fallback_chain`, instead of
  the generic "could not generate a suggestion".
- **README: "Add a fallback".** How to give the plugin's auxiliary task a
  backup provider; the main model's `fallback_providers` does not cover it.

## 0.3.2

- **⌘/Ctrl-click the pill to send the suggestion immediately** through
  `host.composer.submit`, skipping the review step. A plain click, the keybind
  and the palette command are unchanged and still never send. If the host has
  no `submit`, or refuses, it falls back to filling the composer.

## 0.3.1

- **Fix: no suggestions on a gateway that serves several profiles.** Since
  hermes-agent runs a secondary profile's dashboard plugin API in that
  profile's plugin host (NousResearch/hermes-agent#139689), the REST route
  and the hooks of a non-launch profile live in different processes. 0.3.0
  handed suggestions over in memory, so Desktop connected to such a gateway
  (e.g. a remote dashboard started with `-p default` while you chat in
  another profile) always got `null`. Suggestions and failure notes now live
  in a SQLite file under the profile's `plugin-data/next-prompt/` (via
  `plugin_db`, WAL), which both processes read and write.
- Pending suggestions from 0.3.0's `suggestions.json` move over on first load;
  the old file is renamed `suggestions.json.migrated`.
- Failure notes now cross processes too, and expire after 6 hours.

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
    to `NULL`, and follows the language of your last message.
  - **When the agent asks you something, the suggestion is your likely
    answer**, ready to send or tweak, committed to one option: the one the
    conversation or the agent favours, or a yes to a proposal that matches
    your request. Asked to try something and report back, it confirms you
    did it with the most likely outcome. Never a list of options or a blank
    to fill in (the output cleaner drops any that slip through), never a
    password. `NULL` is left for closed exchanges (thanks, goodbye) and for
    answers only you know, such as an email address.
  - Lower temperature (0.3); the output cleaner also strips labels,
    markdown, smart quotes and `NULL` variants.
  - Measured on 16 cases (4 real exchanges scored against the reply actually
    sent): Opus 5.5 30/32, Sonnet 5 43/48, GPT-6-luna 25/32 (up from 6/10
    on 0.2.x, where questions from the agent got no suggestion at all).
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
