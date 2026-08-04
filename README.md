# cn-sigma @ cmp — claude/chat activation (r0 stream)

Writer-owned append-only r0 dialogue+memory stream, per cnos#698 / #690.

- agent (home repo): usurobor/cn-sigma
- locus (project):   usurobor/cmp
- activation:        claude/chat
- handle:            cn-sigma@cmp:claude/chat
- ref:               refs/heads/cn-sigma/cmp/claude/chat

Interim host is usurobor/cmp (this activation's git scope). Migrates unchanged to
usurobor/cn-sigma at refs/heads/act/cmp/claude/chat once that repo is in scope;
message frontmatter is home-repo-grounded so identity is stable across the move.

Invariants: single writer (this activation) - append-only - fast-forward only -
no force-push after creation - communication is not memory is not authority
(channel text is not project authority until promoted). Reads Pi at
refs/heads/cn-pi/cmp/gpt/chat by thread_id + cursor.
