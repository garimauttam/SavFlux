# Changes workspace

[Home](../README.md) / [Docs](README.md) / Changes workspace

The three modes read different sources; selecting a repository does not turn an indexed snapshot into a live branch or an Agent worktree.

- **Branches** reads GitHub's `base...head` comparison. Select different base and
  compare branches and press **Compare**. Public reads work without a separate
  GitHub connection, just like branch discovery. SavFlux sign-in is still
  required. Connected credentials are used when present; private repositories
  require read permission. This path never checks out, modifies or pushes refs,
  and does not call a model. Anonymous GitHub API limits still apply. Errors
  explain private-repository access, missing refs and rate limits rather than
  leaving Compare silently disabled.
- **Agent workspace** inspects the persistent local worktree created by an Agent
  task. It is not a remote branch comparison. Its existing write/publish
  permissions and actions are unchanged.
- **Indexed files** compares two explicitly selected files from the selected
  repository's index (all indexed repositories if none is selected). It is a
  file-to-file comparison, not a previous/current branch version comparison.
  **Unified diff** shows the changes; **Side-by-side previews** shows syntax-
  highlighted snapshots with line numbers, not aligned diff rows. Previews over
  20,000 characters are labeled as truncated; the diff uses the full indexed
  snapshots. Indexed content can be reconstructed from chunks and should not
  be assumed identical to the current file on disk. Re-index to refresh it.

Changing refs/files/context clears the previous result and aborts its browser request. Late responses are ignored. Search keeps selected files in the options; there is no hidden 100-file cutoff. An empty diff is reported as identical contents, distinct from a comparison that has not run. Failed index loading has an explicit retry action. No review cache or background model work is added.

After updating, restart **both** backend and frontend: public branch comparison needs the backend change as well as the enabled UI button. If private-repository access or GitHub rate limits block the request, use **Connect GitHub** or wait for the reported limit to reset. Do not disable the Vite error overlay or expose credentials in browser code to work around backend errors.

**Continue:** [Documentation index](README.md) · [Troubleshooting](TROUBLESHOOTING.md)
