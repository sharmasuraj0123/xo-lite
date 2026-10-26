# xo-ui

**The front end, cut down to what this server can actually answer.**

It is [xo-coworker](https://github.com/sharmasuraj0123/xo-coworker) with the
parts that need a model taken out. Everything left is backed by a real route in
`../xolite`, so nothing in it is a button that cannot work.

```sh
cd ..  && python run.py        # the backend, :5002
cd xo-ui && python serve.py    # this,        :5180
```

Open <http://localhost:5180>. There is nothing to configure: `serve.py` proxies
`/api/*` to `XO_API`, which already defaults to `http://127.0.0.1:5002`.

| var | default | meaning |
|---|---|---|
| `PORT` | `5180` | where this serves |
| `XO_API` | `http://127.0.0.1:5002` | the backend it proxies to |

## Why there is a `serve.py` at all

The page is static — no build, no bundler, no dependencies. `serve.py` exists for
two things a `file://` page cannot do: put the API on the same origin, and keep
an SSE stream open while it does. It is stdlib only, and it refuses the routes a
browser has no business reaching.

## What was taken out

Each of these was a call the client made that this server has no honest answer
for. Removing the control is the honest version; leaving it to fail is not.

| gone | why |
|---|---|
| the **Chats** view | `POST /chat/prompt` — one agent in one folder, prompted directly. There is no agent here to prompt. |
| **Settings → Routing** | `POST`/`DELETE /chat/router/models` — connecting a provider and an API key. One engine, no models, no keys. |
| **demo mode** | `serve.py --demo` answered from `sample_data/` with no backend. This UI ships with its backend. |

Nothing else changed. The remaining files are byte-for-byte upstream, so a diff
against xo-coworker shows exactly these three cuts and no more.

## What was added to the backend instead

The other gaps were worth closing rather than cutting, because the control was
right and only the route was missing:

| route | what it makes work |
|---|---|
| `POST`/`DELETE /chat/rules` | **Settings → Rules**, and "Allow for this thread" on a decision |
| `POST /chat/abort` | stopping a session from the composer |
| `POST /chat/runs/{id}/stop`, `.../runs/stop` | stopping a run |
| `PATCH /chat/sessions/{id}` | the agent/model/effort pin |
| `GET /chat/chats/{chat}/memory/{path}` | reading a project's markdown |

The first one is the one that matters. A rule made from a decision is that
decision's own sentence, word for word, and it only ever covers that sentence —
so allowing *"Drop the old index"* once does not quietly allow *"Drop the users
table"* later. `tests/test_rules.py` upstairs is mostly about holding that line.

## The one thing to know before changing it

**The client never polls.** `watch()` and `watchThread()` in `js/storm.js` are
one-shot loads, not pollers, despite what the upstream README says. Every change
reaches the page as a stream frame or not at all, so a backend write that
broadcasts nothing is real, stored, and invisible until a reload.

## Tests

`tests/` is upstream's, unchanged, and covers `api.js`, `sse.js`, `storm.js`,
`markdown.js` and friends — none of the files cut above.

```sh
node tests/storm.test.mjs
node tests/logic.test.mjs
node tests/attach.test.mjs
```

## Fonts

`fonts/geist/` is [Geist](https://github.com/vercel/geist-font), SIL OFL 1.1.
`fonts/geist/LICENSE.txt` is the licence as shipped.
