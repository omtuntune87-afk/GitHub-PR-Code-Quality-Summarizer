# pr-lens

`pr-lens` is a small Python CLI that reviews GitHub pull requests with an
OpenAI-compatible LLM. It fetches the PR metadata and unified diff, parses the
diff into structured changes, asks the model for a focused review, and can post
the result back to the pull request as a Markdown comment.

## Features

- Reviews any GitHub pull request from `owner/repo` + number or a PR URL.
- Uses Gemini's OpenAI-compatible endpoint by default.
- Checks `finish_reason` and refuses to present or post token-truncated reviews.
- Retries rate limits (429), temporary server errors (5xx), and timeouts with
  capped exponential backoff and `Retry-After` support.
- Updates its own hidden-marked comment instead of posting duplicates.
- Excludes lockfiles, minified assets, and custom paths from the prompt, then
  reports what was skipped.
- Includes a validated AI quality estimate with a verdict, 1-5 score, and short
  reasons.
- Truncates very large diffs at file boundaries so prompts stay within budget.
- Includes a GitHub Actions review workflow, read-only CI, and an offline test
  suite.

## Requirements

- Python 3.12 or newer
- A GitHub token when posting comments (write access for private repositories)
- A Gemini API key, or a key for another OpenAI-compatible endpoint

## Installation

From this repository:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Alternatively, with `uv`:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

## Configuration

Copy `.env.example` to `.env` and fill in the values. `pr-lens` loads `.env`
automatically from the current directory. Command-line options take priority
over environment variables.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `PR_LENS_GITHUB_TOKEN` | For `--post` | falls back to `GITHUB_TOKEN` | GitHub API token |
| `PR_LENS_GITHUB_API_URL` | No | `https://api.github.com` | GitHub Enterprise API base URL |
| `PR_LENS_LLM_API_KEY` | Yes | falls back to `OPENAI_API_KEY` | LLM API key |
| `PR_LENS_LLM_BASE_URL` | No | `https://generativelanguage.googleapis.com/v1beta/openai/` | OpenAI-compatible API base URL |
| `PR_LENS_LLM_MODEL` | No | `gemini-3.5-flash-lite` | Model name |
| `PR_LENS_MAX_TOKENS` | No | `4000` | Maximum tokens in the model response |
| `PR_LENS_MAX_DIFF_CHARS` | No | `60000` | Maximum diff characters sent to the model |
| `PR_LENS_MAX_COMMENT_CHARS` | No | `60000` | Maximum comment length (GitHub limit is 65536) |
| `PR_LENS_EXCLUDE` | No | empty | Comma-separated additional file glob patterns to exclude |
| `PR_LENS_COMMENT_AUTHOR` | For idempotent posting | `github-actions[bot]` | Account allowed to own an existing pr-lens comment |

Built-in exclusions cover common lockfiles (`package-lock.json`, `yarn.lock`,
`Cargo.lock`, `poetry.lock`, `uv.lock`, and others), minified assets
(`*.min.js`, `*.min.css`, source maps), and known generated-file extensions such
as `*.pb.go` and `*_pb2.py`. Diff content is never used to exclude a file. For
renames, both the old and new path must match an exclusion before the file is
skipped. `PR_LENS_EXCLUDE` adds patterns; it does not disable the built-ins.

GitHub tokens need `pull-requests: write` (or `issues: write` for the comment
endpoint) when using `--post` on private repositories. Reading a public PR can
work without a token, subject to GitHub's unauthenticated rate limits.

## Usage

Review a pull request and print the Markdown to stdout:

```bash
pr-lens review octocat/Hello-World 1347
```

Use a full URL or an `owner/repo#number` reference:

```bash
pr-lens review https://github.com/octocat/Hello-World/pull/1347
pr-lens review octocat/Hello-World#1347
```

Post the review as a comment and also save it locally:

```bash
pr-lens review octocat/Hello-World 1347 --post --output review.md
```

Choose a model or endpoint for one run:

```bash
pr-lens review octocat/Hello-World 1347 \
  --model gemini-3.5-flash-lite \
  --base-url https://generativelanguage.googleapis.com/v1beta/openai/
```

Exclude additional files and control the posting identity:

```bash
pr-lens review octocat/Hello-World 1347 --post \
  --exclude 'internal/generated/**' --exclude '*.snap' \
  --comment-author github-actions[bot]
```

Useful options:

```text
--post                    Create or update the pr-lens review comment.
--output PATH             Write the Markdown review to a file.
--model TEXT              LLM model name.
--base-url TEXT           OpenAI-compatible API base URL.
--max-diff-chars INT      Maximum diff characters sent to the model.
--max-comment-chars INT   Maximum formatted comment length.
--max-tokens INT          Maximum response tokens (default 4000).
--temperature FLOAT       Sampling temperature (default 0.2).
--exclude PATTERN         Additional file glob; repeatable or comma-separated.
--comment-author TEXT     Account allowed to own an existing pr-lens comment.
--no-footer               Omit the generated-by footer.
```

Run `pr-lens --help` or `pr-lens review --help` for the full help.

### Reliability and posting behavior

- If the model reports a token-limit `finish_reason`, the CLI fails with a
  clear message. No partial review is printed, saved, or posted. Raise
  `PR_LENS_MAX_TOKENS` or pass `--max-tokens` if this happens.
- Transient 429, 5xx, and timeout failures are retried a limited number of
  times. Other 4xx responses fail immediately.
- Posted reviews contain a hidden `<!-- pr-lens:review-comment -->` marker.
  With `--post`, pr-lens paginates issue comments and updates a marked comment
  only when it was written by `PR_LENS_COMMENT_AUTHOR`. Otherwise it creates a
  new comment.
- If every changed file is excluded, pr-lens skips the LLM call and posts
  nothing.

## GitHub Actions

### Review workflow

`.github/workflows/pr-review.yml` runs on pull request events and posts a
review. Configure these repository secrets:

| Secret | Required | Purpose |
| --- | --- | --- |
| `PR_LENS_LLM_API_KEY` | Yes | Gemini API key passed directly to pr-lens |
| `GITHUB_TOKEN` | Automatic | Built-in token used for the comment; do not create it manually |

Optional repository variables:

| Variable | Default used by workflow |
| --- | --- |
| `PR_LENS_LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `PR_LENS_LLM_MODEL` | `gemini-3.5-flash-lite` |
| `PR_LENS_MAX_TOKENS` | `4000` |
| `PR_LENS_MAX_DIFF_CHARS` | `60000` |
| `PR_LENS_MAX_COMMENT_CHARS` | `60000` |
| `PR_LENS_EXCLUDE` | empty |
| `PR_LENS_COMMENT_AUTHOR` | `github-actions[bot]` |

Notes:

- The workflow uses the `pull_request` event. Secrets are not exposed to pull
  requests from forks, so the review step will fail there unless you adopt a
  separate, carefully reviewed trusted workflow.
- Draft pull requests are skipped.
- Consecutive updates to the same PR cancel older in-flight reviews.
- The expected comment author defaults to `github-actions[bot]`, which is the
  account that posts with the built-in token.

### CI workflow

`.github/workflows/ci.yml` runs Ruff and pytest on Python 3.12 and 3.13 for
pushes to `main` and pull requests. It declares only `contents: read` and does
not consume any secrets.

## Review output

The model is asked to start with a validated line in this form:

```text
AI estimate: Looks good | 5/5 | Small, tested change.
```

Valid verdicts are `Looks good`, `Needs attention`, and `High risk`; scores are
whole numbers from 1 to 5. Invalid or missing estimates render as **Not
rated**. The block is labeled as an automated estimate and is not a substitute
for human review.

The model is then asked to return these sections:

1. **Summary** - what the change does and why.
2. **Key changes** - the most important edits.
3. **Risks and edge cases** - concrete correctness, security, and compatibility concerns.
4. **Testing** - what is covered and what still needs verification.
5. **Suggestions** - actionable, specific improvements.

`pr-lens` wraps that content with PR metadata, the AI estimate, diff statistics,
a skipped-files note, an optional truncation warning, and a short footer. The
final comment is capped at the configured character limit.

## Architecture

| Path | Responsibility |
| --- | --- |
| `src/pr_lens/diff_parser.py` | Parse unified diffs into files, hunks, and lines; compute stats. |
| `src/pr_lens/github_client.py` | GitHub REST client, pagination, comment create/update, PR metadata, diff download. |
| `src/pr_lens/prompts.py` | File filtering, system/user prompt construction, and diff truncation. |
| `src/pr_lens/llm_engine.py` | LLM client, retries, finish-reason checks, quality parsing, review orchestration. |
| `src/pr_lens/formatter.py` | Render GitHub-flavored Markdown comments. |
| `src/pr_lens/cli.py` | Click CLI, environment configuration, and output handling. |
| `tests/test_pr_lens.py` | Offline tests for all modules using `httpx.MockTransport`. |

## Development

```bash
.venv/bin/ruff check .
.venv/bin/pytest
```

The test suite does not make network calls. HTTP behavior is tested with
`httpx.MockTransport`.

## Security and limitations

- PR titles, descriptions, file paths, and diffs are untrusted model input. The
  prompts explicitly instruct the model to ignore embedded instructions, but
  prompt injection can never be eliminated entirely. Treat automated reviews as
  a starting point, not an approval.
- The AI estimate is generated by the model and validated only for allowed
  verdicts, score ranges, and non-empty reasons.
- Large diffs are truncated, so the model may not see every file. Excluded
  files are listed in the comment.
- The review is a single PR-level comment, not inline review threads.
- The tool does not execute tests or inspect files outside the diff.
- Pin and review GitHub Actions versions according to your organization policy.
