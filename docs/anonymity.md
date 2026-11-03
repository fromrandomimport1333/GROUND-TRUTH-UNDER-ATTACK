# Anonymous submission preparation

The official PDF prohibits names, unit names, logos and identifying repository
metadata for competition Phase 1. The administrative committee assigns a team
code and sanitizes the repository, but the team must still submit anonymous work.

Before publication, inspect source, documentation, notebook outputs, logs,
screenshots, file metadata, filenames, absolute paths, Git author/committer
identity, commit messages, remote URL, repository owner/profile, issues, actions
and release metadata. Use the organizer-assigned team code only when supplied.
Do not fabricate team codes or author identities.

Private keys, TLS credentials, tokens, `.env` files, local datasets and runtime
state must not be committed. Gitignore does not remove previously tracked
secrets or sanitize repository history. Follow organizer guidance for creating
and submitting the anonymous repository.

## Status of this repository

- Authored files contain no names, unit names or logos (checked by a full-text search before submission).
- The Git history of the development repository records author names and e-mail addresses, and the
  repository sits under a personal account. **Do not submit that repository as is.** Submit either a
  fresh repository with a single commit by a neutral author, or a source archive without `.git`:

  ```bash
  git archive --format=zip -o submission.zip HEAD          # source only, no history
  # or a fresh history:
  git checkout --orphan submission && git add -A
  git -c user.name="Team" -c user.email="team@example.invalid" commit -m "Phase 1 submission"
  ```

- Check the report PDF's document properties (author, creator) before upload.
