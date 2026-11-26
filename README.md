# pr-lens

`pr-lens` is a small Python CLI that reviews GitHub pull requests with an
OpenAI-compatible LLM. It fetches the PR metadata and unified diff, parses the
diff into structured changes, asks the model for a focused review, and can post
the result back to the pull request as a Markdown comment.

## Features

- Reviews any GitHub pull request from `owner/repo` + number or a PR URL.
- Parses unified diffs into files, hunks, and line-level changes.
- Produces a consistent review with summary, key changes, risks, testing, and
  suggestions.
- Truncates very large diffs at file boundaries so prompts stay within budget.
- Formats a GitHub-ready comment with size statistics and a truncation notice.
- Optional `--post` support for posting the review as a PR comment.
- Works with OpenAI and any OpenAI-compatible chat completions endpoint.
- Includes a GitHub Actions workflow and an offline test suite.

## Requirements

- Python 3.12 or newer
- A GitHub token when posting comments (read access is enough for public PRs)
- An API key for an OpenAI-compatible LLM endpoint

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
automatically from the current directory.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `PR_LENS_GITHUB_TOKEN` | For `--post` | falls back to `GITHUB_TOKEN` | GitHub API token |
| `PR_LENS_GITHUB_API_URL` | No | `https://api.github.com` | GitHub Enterprise API base URL |
| `PR_LENS_LLM_API_KEY` | Yes | falls back to `OPENAI_API_KEY` | LLM API key |
| `PR_LENS_LLM_BASE_URL` | No | `https://api.openai.com/v1` | OpenAI-compatible API base URL |
| `PR_LENS_LLM_MODEL` | No | `gpt-4o-mini` | Model name |
| `PR_LENS_MAX_DIFF_CHARS` | No | `60000` | Maximum diff characters sent to the model |
| `PR_LENS_MAX_COMMENT_CHARS` | No | `60000` | Maximum comment length (GitHub limit is 65536) |

GitHub tokens only need `pull-requests: write` (or `issues: write` for the
comment endpoint) when using `--post`. Reading a public PR can work without a
token, subject to GitHub's unauthenticated rate limits.

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
  --model gpt-4o-mini \
  --base-url https://api.openai.com/v1
```

Useful options:

```text
--post                  Post the review as a PR comment.
--output PATH           Write the Markdown review to a file.
--model TEXT            LLM model name.
--base-url TEXT         OpenAI-compatible API base URL.
--max-diff-chars INT    Maximum diff characters sent to the model.
--max-comment-chars INT Maximum formatted comment length.
--temperature FLOAT     Sampling temperature (default 0.2).
--max-tokens INT        Maximum response tokens (default 2000).
--no-footer             Omit the generated-by footer.
```

Run `pr-lens --help` or `pr-lens review --help` for the full help.

## GitHub Actions

The workflow in `.github/workflows/pr-review.yml` runs on pull requests and
posts a review when the `PR_LENS_LLM_API_KEY` repository secret is configured.
The built-in `GITHUB_TOKEN` is used for the comment.

Optional repository variables:

- `PR_LENS_LLM_MODEL`
- `PR_LENS_LLM_BASE_URL`

Notes:

- The workflow uses the `pull_request` event. Secrets are not exposed to pull
  requests from forks, so the review step will fail there unless you adopt a
  separate, carefully reviewed trusted workflow.
- Draft pull requests are skipped.
- Consecutive updates to the same PR cancel older in-flight reviews.

## Review output

The model is asked to return these sections:

1. **Summary** - what the change does and why.
2. **Key changes** - the most important edits.
3. **Risks and edge cases** - concrete correctness, security, and compatibility concerns.
4. **Testing** - what is covered and what still needs verification.
5. **Suggestions** - actionable, specific improvements.

`pr-lens` wraps that content with PR metadata, diff statistics, an optional
truncation warning, and a short footer. The final comment is capped at the
configured character limit.

## Architecture

| Path | Responsibility |
| --- | --- |
| `src/pr_lens/diff_parser.py` | Parse unified diffs into files, hunks, and lines; compute stats. |
| `src/pr_lens/github_client.py` | GitHub REST client, PR metadata, diff download, comment posting. |
| `src/pr_lens/prompts.py` | System/user prompt construction and diff truncation. |
| `src/pr_lens/llm_engine.py` | OpenAI-compatible chat completions client and review orchestration. |
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

- PR titles, descriptions, and diffs are untrusted model input. The prompts
  explicitly instruct the model to ignore embedded instructions, but prompt
  injection can never be eliminated entirely. Treat automated reviews as a
  starting point, not an approval.
- Large diffs are truncated, so the model may not see every file.
- The review is a single PR-level comment, not inline review threads.
- The tool does not execute tests or inspect files outside the diff.
- Pin and review GitHub Actions versions according to your organization policy.
