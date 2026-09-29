# Workspace guide

[Home](../README.md) / [Docs](README.md) / Workspace guide

SavFlux separates asking, reviewing, editing, and publishing. Start with an indexed repository; use the source beside an answer to check its evidence.

## Navigation

| Area | Destination | Use it for |
| --- | --- | --- |
| Work | Agent | Ask code questions or run a task |
| Work | Review | Inspect static findings and model commentary |
| Work | Write | Generate code or edits |
| Understand | Files · Graph · Health | Browse indexed sources, dependencies, and security posture |
| Ship | Repositories | Connect GitHub and index a selected branch |
| Ship | Changes | Compare remote branches, Agent worktrees, or indexed files |
| Keep | Library | Prompts, snippets, history, activity, inbox, commands, bulk tools, metrics |
| Account | Profile, in the sidebar footer | Account details, connections, models, appearance, privacy |

The sidebar toggle sits beside **Work**. Collapse/expand is explicit, works with keyboard and touch, and persists only a layout preference; hovering does not reopen it. Profile remains accessible in the collapsed rail.

## A useful first session

1. **Bring code in.** Choose a repository and branch, then index it; or upload files.
2. **Ask a focused question.** Try `@auth.py where is the token checked?` to scope retrieval.
3. **Inspect evidence.** Click a citation to open the source drawer at its cited lines. Trust scores describe retrieved evidence, not proof that a generated answer is correct.
4. **Review a few files.** Static analyzer findings and AI reviewer comments are labeled separately. Provider failures are not shown as successful deep reviews.
5. **Inspect the proposal.** Copying a replacement or marking a comment reviewed does not apply an edit. Test changes before approving a patch or PR.

## Choose the right Changes view

| View | Source of truth | Does it modify the repository? |
| --- | --- | --- |
| Branches | Remote GitHub `base...head` comparison | No |
| Agent workspace | SavFlux-managed local worktree based on the indexed commit | Inspection is read-only; reset is explicit |
| Indexed files | Two file contents stored in the index | No; not a branch diff or live checkout |

See [Changes workspace](CHANGES_WORKSPACE.md) for public GitHub access, explicit selectors, preview limits, and comparison errors.

## Review and suggested fixes

[The review guide](REVIEW_WORKSPACE.md) covers the continuous source view, hash-checked anchors, unified/before-and-after suggestions, and keyboard controls. A matched source location does not prove the proposed behavior is correct. Re-index changed code and run a new review rather than applying an old suggestion to different content.

New reviews do not reuse review-result caches. **Mark reviewed** is transient discussion state, not personalization or a saved source edit. During a running repository review, individual fix generation waits until completion or Stop to avoid competing requests in that workspace.

## Keyboard and appearance

| Shortcut | Action |
| --- | --- |
| `⌘K` / `Ctrl+K` | Command palette |
| `g`, then `a/r/w/e/g/h/o/d/l/p` | Agent / Review / Write / Files / Graph / Health / Repositories / Changes / Library / Profile |
| `/` | Focus search |
| `?` | Shortcut help; suppressed while typing |
| `Esc` | Close the current palette, dialog, or drawer |

Light/dark theme preferences apply before first paint. Source and review code use self-hosted Geist Mono. Documentation uses GitHub's own readable Markdown typography; the app's font settings do not alter GitHub pages.

## Profile and consent

Profile groups account, GitHub, models, appearance, and privacy settings. Display-name changes reach Supabase only when you select **Save profile**. Provider keys are server-side secrets, not profile metadata. Having an account is not consent to use source or reviews for personalization or training; see [Security and limits](SECURITY.md).

**Continue:** [Review guide](REVIEW_WORKSPACE.md) · [Changes guide](CHANGES_WORKSPACE.md) · [GitHub workflow](GITHUB_WORKFLOW.md)
