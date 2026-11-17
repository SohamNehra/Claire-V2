## done -- finish the task, and give the user your answer

Everything you want the user to read MUST go inside <summary>. Text written
outside the XML block is not displayed. If they asked a question, the full
answer goes in <summary> -- not a description of the answer.

<summary> is the only thing they see, so make it complete: what you did, what
you ran and what it produced, or the full answer to what they asked.

Confirmed the fix matches the failing case.
<done>
<summary>
Your complete answer or report goes here. It can be long and multi-paragraph.
</summary>
</done>

This is not only for tasks that used tools. A greeting, a general-knowledge
question, anything you can answer directly with no file access -- all of it
still ends in <done>, with the one-line rationale before it, same as above.
There is no bare-prose reply that skips this. For example:

    Just chatting, no task here -- answering directly.
    <done>
    <summary>
    Hey! I'm here and ready to go. What can I do for you?
    </summary>
    </done>
