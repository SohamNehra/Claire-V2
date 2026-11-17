## git -- inspect the repository (read-only)

<what> is one of: status, diff, staged, log, branch, show.
Optional <path> narrows to one file. For anything that CHANGES the repo
(add, commit, checkout) use run_command instead, so it goes through approval.

<git>
<what>diff</what>
<path>src/api/routes.py</path>
</git>
