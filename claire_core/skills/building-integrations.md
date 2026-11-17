---
name: building-integrations
description: Writing a new integration for a service Claire has no connector for -- research the API, write the manifest, server and tests with the kit, check, submit for sir's approval, and get it connected.
when: a task is to connect, integrate or build a connector, integration or MCP server for a service or API; or to change an integration Claire built earlier
requires: coding, researching-apis, testing, uat
kinds: agent
steps: 60
---
# Building an integration

You are writing a small program that lets Claire use a service. It runs in a
sandbox, reaches only the hosts it declares, sees only the credentials it
declares, and is installed only after sir approves exactly what it does. The
`coding` skill applies to all of it: read before you write, change little at a
time, and let the tests decide.

Use `build_integration` for the integration lifecycle and the file tools for edits.
The kit is supplied and replaced by check; use the API reference in this skill.
Read its implementation only to answer a specific unresolved kit-contract question,
not as routine discovery: it adds thousands of irrelevant tokens to the task.
Work only in this task's folder. Before coding, keep a short WORKLOG.md there:
requested actions, acceptance cases, the documented API contract and sources,
checks run and what remains. Update it after each milestone. Read it after a
context compaction instead of reconstructing endpoint details from memory.

## The shape of one

    <name>/
      integration.json   what it is and does -- sir approves this
      server.py          the actions, written with claire_kit
      test.py            proves every action, against recorded answers, offline
      fixtures/          recorded answers (JSON), if they are long
      claire_kit.py      the kit (replaced with the real one at check; do not edit)

## Step 1: research before writing anything

Every endpoint, parameter and response field you use must come from a page
you fetched in this task -- not from memory, and not from the brief you were
given (a brief may guess). An integration written from guesses passes its own
tests, because you write those too, and then fails the moment sir enters his
credentials. If you cannot find the documentation, stop and report that.


Use web_search and fetch_url on the service's official API documentation. You
need, written down before you start:

- **The API host**, e.g. `www.googleapis.com`. Only hosts you list can be called.
  If the service lives at an address of sir's own -- a team or self-hosted
  instance such as YouTrack, Jira, GitLab, a Mastodon server -- there is no
  host to list: declare the address as a credential of kind `"url"` instead
  (step 3), and he enters it with the token.
- **How it authenticates.** What you can build:
  - an API key, personal access token, or bot token sent in a header or a
    query parameter -- sir creates it once and pastes it in;
  - no auth, for public data.
  What you cannot build yet: a flow where sir signs in through a browser
  (OAuth) for a non-Google service. If a token cannot do what he asked, stop
  and report that clearly, with what the service would need. For Google
  services, prefer an API key where the data allows it (public YouTube data
  does); his private Google data needs his Google account sign-in, which
  you cannot add to yet -- say so.
- **The endpoints** for exactly what sir asked for, with their parameters and
  the shape of their responses. Copy a real example response from the docs:
  it becomes your fixture.
- **Limits**: quotas, page sizes, rate limits.
- **What the response really looks like**, copied from the docs. Your
  fixtures are those examples, not shapes you imagine. If the API returns only
  ids unless you ask for fields (many do), the docs say so: ask for them.

Build only what was asked for. Three actions that work beat ten that might.

## Step 2: start it

    <build_integration>
    <action>new</action>
    <name>youtube</name>
    <title>YouTube</title>
    <host>www.googleapis.com</host>
    <description>Search YouTube and read video and channel details.</description>
    </build_integration>

The scaffold passes its own check, so run `check` once now: any later failure
is about your change.

## Step 3: integration.json

    {
      "name": "youtube",
      "title": "YouTube",
      "version": 1,
      "description": "Search YouTube and read video and channel details.",
      "hosts": ["www.googleapis.com"],
      "docs": ["https://developers.google.com/youtube/v3/docs/search/list"],
      "credentials": [
        {"key": "YOUTUBE_API_KEY",
         "label": "YouTube Data API key",
         "how": "Google Cloud Console → APIs & Services → Library → enable YouTube Data API v3; then Credentials → Create credentials → API key, and copy it."}
      ],
      "actions": [
        {"name": "search_videos", "effect": "read",
         "description": "Search videos. Returns title, channel, date, id and link for each.",
         "params": {"query": "what to search for", "count": "how many, 1-25 (default 5)"},
         "required": ["query"]}
      ],
      "check": {"action": "search_videos", "args": {"query": "test", "count": "1"}}
    }

Field by field:
- `name`: lowercase_with_underscores; `title`: what sir sees.
- `hosts`: host names only -- no `https://`, no paths. As few as possible.
- `docs`: the documentation pages you fetched and read for these endpoints.
  Sir sees them on the approval card. Every endpoint, parameter and response
  field in server.py must come from one of them.
- `credentials`: each `key` is UPPER_CASE; `how` is the exact steps sir
  follows to get it, specific enough that he never has to search. This text is
  shown to him next to the field he pastes into. `"kind"` is `"secret"` (a key
  or token -- the default) or `"url"` (the address of his own instance). The
  integration may call the host of a `"url"` he enters, so `hosts` can be
  empty for such a service:

      "hosts": [],
      "credentials": [
        {"key": "YOUTRACK_URL", "kind": "url", "label": "Your YouTrack address",
         "how": "The address you open YouTrack at, e.g. https://yourteam.youtrack.cloud"},
        {"key": "YOUTRACK_TOKEN", "label": "YouTrack permanent token",
         "how": "YouTrack → your avatar → Profile → Account Security → Tokens → New token, scope YouTrack, and copy it."}
      ]

  In server.py, `secret("YOUTRACK_URL")` is the address without a trailing
  slash: `http("GET", secret("YOUTRACK_URL") + "/api/issues", ...)`. In tests,
  give a made-up address in `secrets=` and key the fixtures by it.
- `actions`: every action, with an honest `effect`. **`"change"` for anything
  that creates, sends, posts, edits, deletes, subscribes, likes, buys or
  uploads** -- sir is asked before each one. `"read"` only for actions that
  look and leave everything as it was. `params` are all strings; say the
  format and default in each description. `required` lists the ones that must
  be given.
- `check`: one read action whose success proves the credentials work. It runs
  when sir saves them.

## Step 4: server.py

    from claire_kit import action, http, secret, Fail, run

    API = "https://www.googleapis.com/youtube/v3"


    @action("search_videos")
    def search_videos(query, count="5"):
        try:
            n = max(1, min(int(count), 25))
        except ValueError:
            raise Fail("count is a number from 1 to 25")
        data = http("GET", f"{API}/search", params={
            "part": "snippet", "type": "video", "q": query, "maxResults": n,
            "key": secret("YOUTUBE_API_KEY")}).json()
        return [{"title": i["snippet"]["title"], "channel": i["snippet"]["channelTitle"],
                 "date": i["snippet"]["publishedAt"][:10], "id": i["id"]["videoId"],
                 "link": "https://youtu.be/" + i["id"]["videoId"]}
                for i in data.get("items", [])]


    if __name__ == "__main__":
        run()

The kit, all of it:
- `@action("name")` -- one function per action in integration.json, same names.
  Arguments arrive as strings; convert and validate them yourself.
- `http(method, url, params=, headers=, json_body=, form=, allow=())` -- the
  only way to call the service. Returns a response with `.status`, `.text`,
  `.json()`. A status of 400 or more raises Fail with the service's own
  message, unless the status is in `allow`.
- `secret("KEY")` -- a credential declared in integration.json.
- `raise Fail("what went wrong and what fixes it")` -- Claire is shown this.
- `run()` -- serves the actions. Last lines of the file, as above.

Rules for the code:
- **Standard library and the kit only.** There is no pip in the sandbox.
  `import requests`, `urllib`, `socket`, `subprocess`, `eval` and friends are
  refused by `check`.
- **Return what is useful, compactly.** Pick the fields that answer the
  question; drop raw blobs; cap lists. A result fills Claire's context.
- **Never print.** Standard output carries the protocol; a stray print breaks it.
- **Do not name a parameter after a Python built-in** (`max`, `min`, `id`,
  `type`, `list`, `filter`): inside the function it hides the built-in. Use
  `count`, `video_id`, `kind`.
- **Errors say what to do.** "the video id is 11 characters, from the link" beats
  "invalid input".
- **No credentials anywhere in the code, tests or fixtures.** Tests use fake
  values like "test-key".

## Step 5: test.py

    from claire_kit import testing, call, Fail
    import server

    SEARCH = "GET https://www.googleapis.com/youtube/v3/search"
    found = testing.load("search")            # fixtures/search.json: a real example response

    with testing.fixtures({SEARCH: found}, secrets={"YOUTUBE_API_KEY": "test-key"}) as sent:
        videos = call("search_videos", query="lofi", count="2")
        assert videos[0]["link"].startswith("https://youtu.be/"), videos
        assert sent[0].params["q"] == "lofi" and sent[0].params["maxResults"] == "2"

    with testing.fixtures({SEARCH: testing.Reply(403, {"error": {"message": "quota exceeded"}})},
                          secrets={"YOUTUBE_API_KEY": "test-key"}):
        try:
            call("search_videos", query="x")
            raise AssertionError("a 403 must fail")
        except Fail as exc:
            assert "quota" in str(exc)

    print("all tests passed")

- Fixtures are keyed `"METHOD https://host/path"` (no query string). A value is
  the JSON body of a 200 answer, or `testing.Reply(status, body)`.
- `sent` records each request: `.method`, `.url`, `.params`, `.headers`, `.body`.
  Assert on what you sent, not only on what came back.
- Test every action through `call()` successfully, and at least one failure.
  Check enforces action coverage and function/manifest argument compatibility. Tests run offline: there is no
  network in the check, so a test that needs one fails.

## Step 6: check, and fix what it says

    <build_integration>
    <action>check</action>
    <name>youtube</name>
    </build_integration>

Read every line of the report. Fix the cause it names -- the manifest, the
code, or the test -- and check again. If the same failure comes back three
times, re-read the docs and your fixture: the shape you assumed is probably
wrong. After six failed checks, stop and report what blocks you.

## Step 7: submit

    <build_integration>
    <action>submit</action>
    <name>youtube</name>
    </build_integration>

Sir is shown what it reaches, what it needs from him, and which actions change
things, and approves or not. If he does not approve, do not resubmit it
unchanged. Once installed, it waits for the credentials it declares; sir has
been told where. Your report must give him the `how` steps, numbered.

## Step 8: when it is connected, use it once

Approved integrations are hot-loaded; do not restart Claire or edit her engine.
Offline tests passing, installed, connected, and live accepted are separate states.
Use the UAT skill to report each accurately.

If it is connected by the time you finish (no credentials needed, or sir
entered them while you worked), call one read action through use_connector and
report the result you saw. Never say it works on the strength of the tests
alone: they prove the code, not the account.

## Changing one that is installed

    <build_integration>
    <action>edit</action>
    <name>youtube</name>
    </build_integration>

copies the installed version here. Change it, check, submit. Sir approves the
update and sees what changed; his credentials are kept; the old version is
kept too. `status` shows where installed integrations stand.

## Rules

- Work only in this task's folder. Claire's own code, prompts and settings are
  not yours to change, and nothing here needs them changed.
- Never ask for, print or store a credential. Sir enters them in the app.
- Declare every host and every credential. Mark every action that changes
  anything as `"change"`. The manifest is what sir approves; it must be true.
- Do not install anything with pip or npm for an integration.
- Your report ends with: what was built (actions, read or change), its status,
  and exactly what sir must do next.
