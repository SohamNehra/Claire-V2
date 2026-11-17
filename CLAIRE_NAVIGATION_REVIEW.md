# Claire navigation and conversation reliability — 12 November 2026

The reported Desktop failure had a concrete tool defect: filename capitalization
recovery rewrote the OS landmark `Desktop` to the user's lowercase `desktop`.
The resulting path no longer matched the registered landmark, so the tool refused
it even though the controller had initially selected `~/Desktop` correctly.

The conversation also exposed separate model and runtime failures: confusing
filesystem root with home, asking the user to supply an obvious OS location,
trusting an earlier assistant promise over actual execution, and reporting a
finished failed call as pending work.

## Changes

- Resolve OS landmarks from the current process's home and filesystem root.
  `local_files(action="locations")` exposes these as runtime facts. No personal
  locations Markdown file is required. `~` and named locations are accepted.
- Preserve OS landmark capitalization while continuing to recover the literal
  spelling of custom names the user supplied.
- Make directory enumeration an autonomous read-only capability. Inspecting
  directory names does not require an exact path substring in the user's words.
  Credential directories and Claire's private state remain protected. File reads
  and changes still require an established target; writes retain the existing
  home/temp boundary, code/instruction protection, and no-overwrite rule.
- Return typed directory entries and absolute paths, with a `kind` filter for
  folders/files/all entries. Record inspected locations for later navigation.
  Listings remain bounded to 50 visible entries and report omissions.
- Give the controller a bounded runtime view of actual user turn inputs,
  completed receipts, the previous filesystem result, and real queued/running
  workers. Internal retry instructions are not mistaken for fresh user requests.
  Durable turn events provide the source of user intent after a service restart.
- Check finished replies against execution state. A failed tool cannot support a
  success claim. Unsupported pending-work claims get one correction attempt;
  repeated claims are replaced with the observed receipt or an honest idle
  status. This is a bounded status check, not a guarantee against arbitrary
  factual hallucinations.
- Apply LM Studio's native model metadata and reasoning controls to explicitly
  configured talk roles on that endpoint too. Hybrid Qwen 3.5/3.6 models need
  `reasoning_effort="none"` for fast mode; template kwargs alone were insufficient
  in the local comparison. Keep JSON retry instructions for JSON controllers.

## Evaluation

The added regression suite covers lowercase Desktop, root versus home,
read-only discovery, credential directory protection, folder filtering,
contextual navigation, failed and completed operations falsely described as
pending, actual user input provenance, and the LM Studio talk adapter.

Real-model checks use disposable chat/project state. Host listings are read-only;
all created test files stay in temporary fixtures. The reusable commands accept
`--api` and `--model` to compare a conversation model without changing the coder:

```sh
python3 -m tests.live_navigation_eval --api http://127.0.0.1:8083/v1
python3 -m tests.live_controller_eval --api http://127.0.0.1:8083/v1
python3 -m tests.live_pipeline_eval --api http://127.0.0.1:8083/v1
```

The initial 4B patch passed a permissive 10-case navigation suite. Stricter
folder/all-content checks and small prompt changes subsequently exposed remaining
4B inconsistencies. The installed 35B MoE was also fast after correcting its
reasoning setting, but failed recovery from a stale wrong-path promise in that
comparison. These results do not support equating parameter count with guaranteed
reliability; the controller needs both a suitable model and an accurate harness.

Sources: [Qwen 9B model card](https://huggingface.co/Qwen/Qwen3.5-9B),
[quantization publisher](https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF),
[LM Studio model loading](https://lmstudio.ai/docs/developer/rest/load).
Local results are measured, not inferred from the model cards.

## Selected configuration and final results

Qwen3.5 9B Q4_K_M replaces 4B for the shared talk/scout/clerk server. The existing
uncensored Qwen3.8 27B remains the coding worker. The 9B download was pinned to
revision `182be2fd6c7bc44887d88a91cb03ff009cc9f549` and verified against the
publisher's complete SHA256 before loading.

- 321 unit tests passed.
- Navigation/recovery: 12/12 real-model cases passed; median request 3.728 s.
- Background reader: 5/5 real-model checks passed.
- Conversation/actions/delegation: 22/22 real-model cases passed; median 2.465 s.
- Coding pipeline: independently verified tests passed in the original project;
  dispatch 4.587 s, greeting during coding 1.871 s, total 57.27 s.
- A warm greeting took 1.258 s; Desktop and root listings took 3.722/3.735 s.
- Recovery from the deliberately polluted prior conversation took 23.965 s and
  used the compact 27B controller. Escalation is slower than the normal path.

An unsupported directory-access denial can trigger one specialist check with
the same filesystem tools and guards, then the next turn returns to the fast
model. Invalid target arguments are recoverable input errors; actual credential
or permission refusals remain boundaries. Technical recovery retains the
original project folder. The handoff test checks both the assigned folder and
the original project's independent test result, preventing a copied-project fix
from being counted as success.

Some early checks tested a particular tool choice rather than the user's
observable result. Root enumeration accepts a directory filter when every visible root entry is a directory; the mixed-content fixture separately checks files; task
status uses a genuinely running fixture in another chat; mixed folder/file
follow-ups name their directory explicitly. Model quality and the execution
harness changed together during the investigation; these results are not a
controlled claim that parameter count alone caused the improvement. Long
conversations, every integration and arbitrary tasks are not covered by this
finite suite. Heavy concurrent local inference can still increase latency.

Machine-readable cases and the source hashes are in
`tests/navigation_eval_results.json`; user directory names are omitted there.

## Installed app verification

The deployed checkout passed the same 321 unit tests. Its tested source hashes
match the evaluated source. The actual app completed Desktop/root/result-follow-up
checks and a separate greeting check: first Desktop listing after model startup
6.518 s, root 4.331 s, results follow-up 2.706 s, warm greeting 1.117 s.
All 15 existing chats remain; the service is idle with zero pending approvals.
The disposable verification chats and recorded QA workspaces were removed.

## Natural file references and paged reading follow-up

The later FABLE5_CAPABILITIES.md failure was a guard bug. The 9B controller
correctly resolved the natural filename after listing the directory, but the
read guard demanded literal filename text in the user message. Read-only file
discovery now works for non-secret files under home/temp; changes keep their
existing target checks. Reads no longer rewrite existing filename case.

read_text returns bounded Unicode-character pages (8000 default, 12000 maximum)
with explicit next offsets. Selected current-turn pages have a separate 24000
character context budget; they are not silently truncated by the normal chat
window. Larger reads can delegate with the exact known file and parent folder.
Action/filter enums and numeric-offset schemas prevent prose inside offset
arguments. Credential/private-state and symlink protections remain enforced.

The regression suite passed 327 tests. Real-model checks passed the listed-file
natural-reference flow without escalation and read all 18469 characters of a
three-page report with no failed calls, recovering hidden codes from its start,
middle and end. The existing 22 controller checks passed. See
`tests/file_read_eval_results.json` for measurements and source hashes.

The exact reported request was replayed against the original document in a
throwaway production chat: 14.557 s, two successful local reads, no failed calls,
only the talk model, no worker handoff. The document hash and all 16 existing
chats were preserved. Protected-root identity checks also cover macOS case
aliases without weakening the existing write boundaries.
