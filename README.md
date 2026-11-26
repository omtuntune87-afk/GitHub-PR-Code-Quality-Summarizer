# pr-lens

`pr-lens` 0.1.0 is a Python CLI that reviews a GitHub pull request with an
OpenAI-compatible LLM and renders the result as a GitHub-flavored Markdown
comment. It fetches PR metadata and the unified diff, removes noisy files,
asks for a focused review, validates the response, and can create or update one
marked PR-level comment.

## Features

- Reviews any GitHub PR given `owner/repo` plus a number, `owner/repo#number`, or
  a full GitHub PR URL.
- Uses Gemini's OpenAI-compatible endpoint and `gemini-3.5-flash-lite` by
  default; any compatible endpoint/model can be configured.
- Excludes common lockfiles, minified assets, and generated/binary-looking paths
  by path only. For renames, both old and new paths must match an exclusion.
- Truncates oversized diffs at a file/line boundary and labels the result as a
  partial review.
- Requires a successful `finish_reason: "stop"`; incomplete responses are never
  printed, saved, or posted.
- Parses a validated AI estimate from the first non-empty model line only, with
  an allowed verdict, whole-number 1-5 score, and one-line reason.
- Suppresses positive ratings when a review is partial, has invalid estimates,
  or has no reviewable files.
- Retries only 429, 5xx, and timeout failures with bounded backoff and capped
  `Retry-After`.
- Escapes untrusted HTML/mentions in rendered comments and preserves safety
  warnings when comments are truncated.
- Posts one hidden-marked comment and updates its own comment instead of
  duplicating it.
- Includes a GitHub Actions review workflow, read-only CI on Python 3.12/3.13,
  and an offline test suite.

## Requirements

- Python 3.12 or newer
- A Gemini API key, or a key for another OpenAI-compatible chat-completions
  endpoint
- A GitHub token only when posting comments; the token needs write access for
  the target repository's issue comments

## Installation

Install from a local checkout:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

With `uv`:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

Verify the installation:

```bash
pr-lens --version
pr-lens --help
```

The installed version is `0.1.0`.

## Configuration

`pr-lens` loads `.env` from the current working directory. Copy `.env.example`
to `.env` and fill in only the values you need. Command-line options take
priority over environment variables, which take priority over built-in defaults.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `PR_LENS_GITHUB_TOKEN` | For `--post` | falls back to `GITHUB_TOKEN` | GitHub API token |
| `PR_LENS_GITHUB_API_URL` | No | `https://api.github.com` | GitHub Enterprise API base URL |
| `PR_LENS_LLM_API_KEY` | Yes | falls back to `OPENAI_API_KEY` | LLM API key |
| `PR_LENS_LLM_BASE_URL` | No | `https://generativelanguage.googleapis.com/v1beta/openai/` | OpenAI-compatible API base URL |
| `PR_LENS_LLM_MODEL` | No | `gemini-3.5-flash-lite` | Model name |
| `PR_LENS_MAX_TOKENS` | No | `4000` | Maximum response tokens; valid range 1-100000 |
| `PR_LENS_MAX_DIFF_CHARS` | No | `60000` | Maximum diff characters sent to the model |
| `PR_LENS_MAX_COMMENT_CHARS` | No | `60000` | Maximum comment length; hard cap 65000 |
| `PR_LENS_EXCLUDE` | No | empty | Comma-separated additional file glob patterns |
| `PR_LENS_COMMENT_AUTHOR` | For idempotent posting | `github-actions[bot]` | Account allowed to own an existing pr-lens comment |

Minimal Gemini configuration:

```dotenv
PR_LENS_LLM_API_KEY=your-gemini-api-key
PR_LENS_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
PR_LENS_LLM_MODEL=gemini-3.5-flash-lite
```

For `--post`, add a GitHub token:

```dotenv
PR_LENS_GITHUB_TOKEN=your-github-token
```

Built-in exclusions are path-based and cover common lockfiles (`package-lock.json`,
`yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `Pipfile.lock`, `Cargo.lock`,
`go.sum`, `uv.lock`, `composer.lock`, `Gemfile.lock`), minified assets
(`*.min.js`, `*.min.cjs`, `*.min.mjs`, `*.min.css`, `*.map`), and known
generated/compiled extensions such as `*.pb.go`, `*_pb2.py`, `*.pyc`, `*.class`,
`*.jar`, fonts, and `*.snap`. Diff content is never used to exclude a file.
`PR_LENS_EXCLUDE` adds patterns; it does not disable the built-ins.

## Usage

Preview a review and print Markdown to stdout:

```bash
pr-lens review octocat/Hello-World 1347
```

Use a full URL or an `owner/repo#number` reference:

```bash
pr-lens review https://github.com/octocat/Hello-World/pull/1347
pr-lens review octocat/Hello-World#1347
```

Post the review and also save it locally:

```bash
pr-lens review octocat/Hello-World 1347 --post --output review.md
```

Choose a model or endpoint for one run:

```bash
pr-lens review octocat/Hello-World 1347 \
  --model gemini-3.5-flash-lite \
  --base-url https://generativelanguage.googleapis.com/v1beta/openai/
```

Exclude additional paths and control which account may own an existing comment:

```bash
pr-lens review octocat/Hello-World 1347 --post \
  --exclude 'internal/generated/**' --exclude '*.snap' \
  --comment-author github-actions[bot]
```

### Options

```text
--post                    Create or update the pr-lens review comment.
--output PATH             Write the Markdown review to a file in addition to stdout.
--model TEXT              LLM model name.
--base-url TEXT           OpenAI-compatible API base URL.
--max-diff-chars INT      Maximum diff characters sent to the model (100-2000000).
--max-comment-chars INT   Maximum formatted comment length (100-65000).
--max-tokens INT          Maximum response tokens (1-100000; default 4000).
--temperature FLOAT       Sampling temperature (0.0-2.0; default 0.2).
--exclude PATTERN         Additional file glob; repeatable or comma-separated.
--comment-author TEXT     Account allowed to own an existing pr-lens comment.
--no-footer               Omit the generated-by footer.
```

Run `pr-lens review --help` for the authoritative option list.

## Review output and ratings

The model is asked to begin with exactly one line, before any Markdown section:

```text
AI estimate: Looks good | 5/5 | Small, tested change.
```

Only the first non-empty response line is considered. Valid verdicts are
`Looks good`, `Needs attention`, and `High risk`; scores are whole numbers from
1 to 5; reasons must be one short line. An invalid, missing, or later-line
estimate renders as **Not rated**. A response is accepted only when its
`finish_reason` is `stop`; `length`, `MAX_TOKENS`, `content_filter`, missing, and
unknown values are rejected.

The model is then asked for these sections:

1. **Summary** — what the change does and why.
2. **Key changes** — the most important edits.
3. **Risks and edge cases** — concrete correctness, security, performance,
   compatibility, or migration concerns.
4. **Testing** — what is covered and what still needs verification.
5. **Suggestions** — actionable improvements, with blockers marked.

`pr-lens` wraps that content with PR metadata, diff statistics, the AI estimate,
skipped/unreviewable file notes, and an optional truncation warning. Untrusted
PR and model text is escaped before rendering: raw HTML is neutralized,
`@mentions` are made non-clickable, and fake pr-lens markers cannot create a
second marker. Comments are capped at `--max-comment-chars` (hard cap 65000),
and truncation keeps the marker, partial-review warning, and `Not rated` notice.

### Partial and no-review comments

A review is labeled **Partial review** and shown as **Not rated** when any of
the following occurs:

- the diff was truncated before the model saw it;
- one or more changed files were excluded from the prompt; or
- a changed file had no reviewable text, such as a binary file.

Even if the model returns a positive estimate, a partial review never displays
or posts that rating.

If all changed files are excluded, `pr-lens` skips the LLM call and emits a
**Nothing was reviewed** comment with **Not rated** and no score. With `--post`,
that comment replaces any previous pr-lens rating instead of leaving a stale
score visible.

## Posting to GitHub

Posted comments contain the hidden marker:

```html
<!-- pr-lens:review-comment -->
```

With `--post`, pr-lens lists issue comments page by page. It updates a marked
comment only when its author matches `PR_LENS_COMMENT_AUTHOR` (default
`github-actions[bot]`); otherwise it creates a new comment. This avoids
duplicates from repeated runs while not touching comments written by other
accounts.

If the model response is incomplete or the API fails, nothing is posted.
Credential values are redacted from provider error messages.

## GitHub Actions

### Review workflow

`.github/workflows/pr-review.yml` runs on `opened`, `reopened`, `synchronize`,
and `ready_for_review` pull request events and posts the review. Configure:

| Secret | Required | Purpose |
| --- | --- | --- |
| `PR_LENS_LLM_API_KEY` | Yes | Gemini API key passed directly to pr-lens |
| `GITHUB_TOKEN` | Automatic | Built-in token used for the comment; do not create it manually |

Optional repository variables:

| Variable | Workflow default |
| --- | --- |
| `PR_LENS_LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `PR_LENS_LLM_MODEL` | `gemini-3.5-flash-lite` |
| `PR_LENS_MAX_TOKENS` | `4000` |
| `PR_LENS_MAX_DIFF_CHARS` | `60000` |
| `PR_LENS_MAX_COMMENT_CHARS` | `60000` |
| `PR_LENS_EXCLUDE` | empty |
| `PR_LENS_COMMENT_AUTHOR` | `github-actions[bot]` |

Notes:

- Draft pull requests are skipped.
- Consecutive updates to the same PR cancel older in-flight reviews.
- The workflow uses the `pull_request` event. Fork PRs do not receive repository
  secrets and their built-in token is typically read-only, so the review step
  will fail there unless you add a separately reviewed trusted workflow.
- The expected comment author defaults to `github-actions[bot]`, the account
  used by the built-in token.

### CI workflow

`.github/workflows/ci.yml` runs Ruff and pytest on Python 3.12 and 3.13 for
pushes to `main` and pull requests. It declares only `contents: read` and uses
no secrets.

## Privacy and limitations

- PR metadata, descriptions, file paths, and diff content are sent to the
  configured LLM endpoint. Only use an endpoint you trust with repository
  content.
- `pr-lens` does not log API keys or tokens and redacts configured credentials
  from provider error messages. Do not put credentials in PR content.
- PR text is untrusted model input. The prompt instructs the model to ignore
  embedded instructions, but prompt injection cannot be eliminated. Treat the
  output as an aid, not an approval.
- The rating is a model-generated estimate, not a measured quality score.
- Large diffs are truncated, and excluded or binary files are not reviewed.
- The tool writes one PR-level comment; it does not create inline review
  threads.
- It does not run tests or inspect repository files outside the supplied diff.
- GitHub's comment size limit is respected, but very long comments are
  truncated with a notice.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `An LLM API key is required...` | Set `PR_LENS_LLM_API_KEY` or `OPENAI_API_KEY`. |
| `Posting requires a GitHub token...` | Set `PR_LENS_GITHUB_TOKEN` or `GITHUB_TOKEN` for `--post`. |
| `GitHub returned an empty diff...` | Check that the PR has changes and that the token can read the repository. |
| `LLM response was truncated at the token limit` | Raise `--max-tokens` or `PR_LENS_MAX_TOKENS`; pr-lens will not post a partial response. |
| `LLM response did not complete successfully` | The model did not return `finish_reason: stop`; retry or try another model. |
| `Nothing was reviewed` | All changed paths matched exclusions; remove an `--exclude`/`PR_LENS_EXCLUDE` pattern if that is unintended. |
| `Not rated` | The response lacked a valid first-line estimate, or the review was partial. Check the warnings in the comment. |
| Multiple pr-lens comments | Confirm `PR_LENS_COMMENT_AUTHOR` matches the account that owns the marked comment. |
| `pagination repeated` error | GitHub returned the same comment page twice; retry after the API recovers. |

## Architecture

| Path | Responsibility |
| --- | --- |
| `src/pr_lens/diff_parser.py` | Parse unified diffs into files, hunks, and lines; compute stats. |
| `src/pr_lens/github_client.py` | GitHub REST client, pagination, comment create/update, PR metadata, diff download. |
| `src/pr_lens/prompts.py` | File filtering, prompt construction, and diff truncation. |
| `src/pr_lens/llm_engine.py` | LLM client, retries, response validation, estimate parsing, review orchestration. |
| `src/pr_lens/formatter.py` | Render and sanitize GitHub-flavored Markdown comments. |
| `src/pr_lens/cli.py` | Click CLI, environment configuration, and output handling. |
| `tests/test_pr_lens.py` | Offline tests for all modules using `httpx.MockTransport` and fake clients. |

## Development

```bash
.venv/bin/ruff check .
.venv/bin/pytest
```

The test suite makes no network calls. HTTP behavior is exercised with
`httpx.MockTransport`.

## License

MIT. See `LICENSE`.
