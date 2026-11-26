"""Command-line interface for pr-lens."""

from __future__ import annotations

import os
from pathlib import Path

import click
from dotenv import load_dotenv

from pr_lens import __version__
from pr_lens.diff_parser import parse_diff
from pr_lens.formatter import (
    DEFAULT_MAX_COMMENT_CHARS,
    PR_LENS_COMMENT_MARKER,
    format_review,
)
from pr_lens.github_client import (
    DEFAULT_COMMENT_AUTHOR,
    DEFAULT_GITHUB_API_URL,
    GitHubClient,
    GitHubError,
    parse_pr_reference,
)
from pr_lens.llm_engine import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_MAX_TOKENS,
    LLMClient,
    LLMError,
    review_diff,
)
from pr_lens.prompts import DEFAULT_MAX_DIFF_CHARS, filter_reviewable_files

GITHUB_COMMENT_LIMIT = 65_000


def _env(primary: str, *fallbacks: str, default: str | None = None) -> str | None:
    for name in (primary, *fallbacks):
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _excludes(values: tuple[str, ...]) -> tuple[str, ...]:
    raw = list(values)
    configured = _env("PR_LENS_EXCLUDE")
    if configured:
        raw.extend(configured.split(","))
    return tuple(item.strip() for item in raw if item.strip())


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise click.ClickException(f"{name} must be an integer, got {raw!r}.") from exc
    if value < 1:
        raise click.ClickException(f"{name} must be positive.")
    return value


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pr-lens")
def cli() -> None:
    """Summarize GitHub pull requests with an LLM."""
    load_dotenv()


@cli.command()
@click.argument("target")
@click.argument("number", required=False, type=int)
@click.option(
    "--post",
    is_flag=True,
    help="Post the formatted review as a comment on the pull request.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write the Markdown review to a file in addition to stdout.",
)
@click.option("--model", default=None, help="LLM model name (env: PR_LENS_LLM_MODEL).")
@click.option(
    "--base-url",
    default=None,
    help="OpenAI-compatible base URL (env: PR_LENS_LLM_BASE_URL).",
)
@click.option(
    "--max-diff-chars",
    type=click.IntRange(min=100, max=2_000_000),
    default=None,
    help="Maximum diff characters sent to the model (env: PR_LENS_MAX_DIFF_CHARS).",
)
@click.option(
    "--max-comment-chars",
    type=click.IntRange(min=100, max=GITHUB_COMMENT_LIMIT),
    default=None,
    help="Maximum comment length (env: PR_LENS_MAX_COMMENT_CHARS).",
)
@click.option(
    "--temperature",
    type=click.FloatRange(min=0.0, max=2.0),
    default=0.2,
    show_default=True,
    help="LLM sampling temperature.",
)
@click.option(
    "--max-tokens",
    type=click.IntRange(min=1, max=100_000),
    default=None,
    help="Maximum tokens in the model response (env: PR_LENS_MAX_TOKENS).",
)
@click.option(
    "--exclude",
    "excludes",
    multiple=True,
    help=(
        "Additional file glob to exclude from review; repeatable or comma-separated "
        "(env: PR_LENS_EXCLUDE)."
    ),
)
@click.option(
    "--comment-author",
    default=None,
    help=(
        "Account that owns an existing pr-lens comment "
        "(env: PR_LENS_COMMENT_AUTHOR, default: github-actions[bot])."
    ),
)
@click.option("--no-footer", is_flag=True, help="Omit the generated-by footer.")
def review(
    target: str,
    number: int | None,
    post: bool,
    output: Path | None,
    model: str | None,
    base_url: str | None,
    max_diff_chars: int | None,
    max_comment_chars: int | None,
    temperature: float,
    max_tokens: int | None,
    excludes: tuple[str, ...],
    comment_author: str | None,
    no_footer: bool,
) -> None:
    """Review TARGET, which is OWNER/REPO plus NUMBER or a pull request URL."""
    try:
        repository, pr_number = parse_pr_reference(target, number)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    github_token = _env("PR_LENS_GITHUB_TOKEN", "GITHUB_TOKEN")
    llm_api_key = _env("PR_LENS_LLM_API_KEY", "OPENAI_API_KEY")
    if not llm_api_key:
        raise click.ClickException(
            "An LLM API key is required. Set PR_LENS_LLM_API_KEY or OPENAI_API_KEY."
        )
    if post and not github_token:
        raise click.ClickException(
            "Posting requires a GitHub token with pull request write access. "
            "Set PR_LENS_GITHUB_TOKEN or GITHUB_TOKEN."
        )

    resolved_model = (
        model or _env("PR_LENS_LLM_MODEL", default=DEFAULT_LLM_MODEL) or DEFAULT_LLM_MODEL
    )
    resolved_base_url = (
        base_url
        or _env("PR_LENS_LLM_BASE_URL", default=DEFAULT_LLM_BASE_URL)
        or DEFAULT_LLM_BASE_URL
    )
    github_api_url = (
        _env("PR_LENS_GITHUB_API_URL", default=DEFAULT_GITHUB_API_URL) or DEFAULT_GITHUB_API_URL
    )
    resolved_max_diff = max_diff_chars or _env_int("PR_LENS_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS)
    resolved_max_comment = max_comment_chars or _env_int(
        "PR_LENS_MAX_COMMENT_CHARS", DEFAULT_MAX_COMMENT_CHARS
    )
    resolved_max_tokens = max_tokens or _env_int("PR_LENS_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    resolved_excludes = _excludes(excludes)
    resolved_comment_author = (
        comment_author
        or _env("PR_LENS_COMMENT_AUTHOR", default=DEFAULT_COMMENT_AUTHOR)
        or DEFAULT_COMMENT_AUTHOR
    )
    if resolved_max_comment > GITHUB_COMMENT_LIMIT:
        raise click.ClickException(
            f"PR_LENS_MAX_COMMENT_CHARS must not exceed {GITHUB_COMMENT_LIMIT}."
        )

    try:
        with GitHubClient(
            token=github_token,
            api_url=github_api_url,
        ) as github:
            click.echo(
                f"Fetching {repository}#{pr_number} from GitHub...",
                err=True,
            )
            pull_request = github.get_pull_request(repository, pr_number)
            diff_text = github.get_diff(repository, pr_number)
            parsed = parse_diff(diff_text)
            if parsed.is_empty:
                raise click.ClickException(
                    f"GitHub returned an empty diff for {repository}#{pr_number}."
                )

            filtered = filter_reviewable_files(diff_text, parsed, resolved_excludes)
            if filtered.parsed.is_empty:
                click.echo(
                    "No reviewable files remained after exclusions; skipping LLM review.",
                    err=True,
                )
                return

            click.echo(
                f"Reviewing {filtered.parsed.changed_files} of {parsed.changed_files} file(s) "
                f"with {resolved_model}...",
                err=True,
            )
            with LLMClient(
                llm_api_key,
                base_url=resolved_base_url,
                model=resolved_model,
                temperature=temperature,
                max_tokens=resolved_max_tokens,
            ) as llm:
                result = review_diff(
                    pull_request,
                    filtered.parsed,
                    filtered.diff_text,
                    llm,
                    max_diff_chars=resolved_max_diff,
                    skipped_files=tuple(filtered.skipped_files),
                )

            comment = format_review(
                result,
                pull_request,
                parsed,
                max_chars=resolved_max_comment,
                include_footer=not no_footer,
            )

            if output is not None:
                output.write_text(comment + "\n", encoding="utf-8")
                click.echo(f"Wrote review to {output}", err=True)

            click.echo(comment)

            if post:
                action, url = github.upsert_review_comment(
                    repository,
                    pr_number,
                    comment,
                    marker=PR_LENS_COMMENT_MARKER,
                    expected_author=resolved_comment_author,
                )
                if url:
                    click.echo(f"{action.capitalize()} review comment: {url}", err=True)
                else:
                    click.echo(f"{action.capitalize()} review comment.", err=True)
    except (GitHubError, LLMError) as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(f"Could not write output: {exc}") from exc


def main() -> None:
    """Console script entry point."""
    cli()


if __name__ == "__main__":
    main()
