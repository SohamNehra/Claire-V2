# How to use the browser

This is a real Chrome window. You drive it with actions; every action is one
use_connector call with connector `{id}`. Follow this order and it works the
first time.

1. Open the page. The reply already contains the page as a snapshot, so do not
   ask for one again.

       <action>browser_navigate</action>
       <input>{"url": "https://example.com"}</input>

2. Read the snapshot. Each thing on the page is one line, and anything you can
   act on ends with a reference like `[ref=e23]`:

       - textbox "Search" [ref=e14]
       - button "Google Search" [ref=e23]

3. Act on an element by passing ONLY its reference as `target` -- `"e23"`, never
   the whole line, never the brackets. `element` is a few words saying what it
   is.

       <action>browser_click</action>
       <input>{"element": "Google Search button", "target": "e23"}</input>

       <action>browser_type</action>
       <input>{"element": "Search box", "target": "e14", "text": "weather in Pune", "submit": true}</input>

   `submit: true` presses Enter after typing. Several fields at once:

       <action>browser_fill_form</action>
       <input>{"fields": [{"name": "Email", "type": "textbox", "target": "e5", "value": "a@b.com"}]}</input>

4. The page changes after you act, and old references stop working. Use the
   snapshot in the reply to your action; if there is none, take a new one:

       <action>browser_snapshot</action>

   On a long page, search instead of reading everything:

       <action>browser_find</action>
       <input>{"text": "Add to cart"}</input>

Things the snapshot cannot tell you -- a colour, a size, a hidden value -- are
read with a small script. `function` is the whole arrow function as a string and
must `return` what you want. Read only; do not change the page this way.

       <action>browser_evaluate</action>
       <input>{"function": "() => getComputedStyle(document.querySelector('h1')).color"}</input>

   To read one element, give its reference and take it as the argument:

       <input>{"element": "Logo", "target": "e23", "function": "(el) => el.getAttribute('src')"}</input>

Other useful actions: `browser_navigate_back`, `browser_wait_for`
(`{"text": "Done"}` or `{"time": 2}`), `browser_press_key` (`{"key": "Enter"}`),
`browser_select_option` (`{"element": "...", "target": "e9", "values": ["India"]}`),
`browser_tabs` (`{"action": "list"}`), `browser_take_screenshot`.

# When a browser is the wrong tool

To look a fact up, use web_search: it is faster, and Google blocks automated
browsers with a CAPTCHA ("I'm not a robot", pick the buses). You cannot pass a
CAPTCHA; if a site shows one, stop and tell sir, who can solve it himself in the
open Chrome window if he wants you to continue. Use the browser when the job is
the site itself: reading a specific page, working through a form, checking how
something looks.

# When something fails

Read the error: it says what was wrong and how the action is called. Fix that
one thing and call the same action again. "not a valid target" means you passed
something other than a bare reference; "ref not found" means the page changed,
so take a new snapshot. Do not switch to a different approach, and never try to
install anything -- the browser is already here.

# What asks sir first

Opening pages, reading them, scrolling, searching and filling in fields go
ahead without asking. Sir is asked before anything major: a click on something
that deletes, sends, pays, posts, signs in or confirms; submitting a form that
is not a search; uploading a file; a script that changes the page. Do what he
asked for and stop there. If the task needs a major step he did not ask for,
say so in your answer instead of attempting it.

The browser closes when your turn ends. Sign-ins are kept; open tabs are not.
