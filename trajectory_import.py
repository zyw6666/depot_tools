# Copyright 2026 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Trajectory import, Associated Agentic Gerrit CL (CL_self), and Simulate CQ.

Implements Workstream 1 of the Gerrit-Native AI <-> Author Review Loop (Spec v6):
- Discovering and parsing local Jetski trajectories (transcript_full.jsonl)
  into code-modifying turns (1 Turn = 1 Self-Review Patchset on CL_self).
- Reconstructing intermediate Git trees per turn in a temporary GIT_INDEX_FILE
  starting from base_commit and pinning the final turn K to HEAD:.
- Deriving a deterministic Self-Review Change-Id (CL_self) separate from the
  canonical Peer-Review Change-Id (CL_peer), supporting configurable
  `--self-review-host` (`branch.<name>.gerritselfreviewserver`).
- Hard-gating `CL_self -> CL_peer` promotion (`VerifyPromotionReadiness`) on
  100% author-acknowledged AI Check-In prompts and passing `Simulate CQ` at the
  promoted `tree_sha` (with a `--force` escape hatch that sets
  `forced_promotion: True` and caps the Author-Review score at 0.65).
- Supporting configurable, opt-in draft reply staging (`--stage-draft-replies`,
  off by default), Round-2+ human comment bridging (`SyncPeerCommentsToSelfReview`),
  and automated archival of `CL_self` (`ArchiveSelfReviewCl`).
"""

from __future__ import annotations

import base64
import dataclasses
import difflib
import fnmatch
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import gerrit_util
import git_footers


DEFAULT_BRAIN_ROOT = os.path.expanduser("~/.gemini/jetski/brain")

SELF_REVIEW_ISSUE_CONFIG_KEY = "gerritselfreviewissue"
SELF_REVIEW_CHANGE_ID_CONFIG_KEY = "gerritselfreviewchangeid"
SELF_REVIEW_PATCHSET_CONFIG_KEY = "gerritselfreviewpatchset"
SELF_REVIEW_SERVER_CONFIG_KEY = "gerritselfreviewserver"
JETSKI_CONVERSATION_ID_CONFIG_KEY = "jetski-conversation-id"
SIMULATE_CQ_CACHE_CONFIG_KEY = "gerritsimulatecqjson"
CHECKIN_PROMPTS_CACHE_CONFIG_KEY = "gerritcheckinsjson"
STAGE_AI_DRAFT_REPLIES_CONFIG_KEY = "stage-ai-draft-replies"
TRAJECTORY_GRANULARITY_CONFIG_KEY = "gerrittrajectorygranularity"
MAX_SELF_REVIEW_PATCHSETS_CONFIG_KEY = "gerritmaxselfreviewpatchsets"

DEFAULT_CHROME_INTERNAL_SELF_REVIEW_HOST = (
    "https://chrome-internal-review.git.corp.google.com"
)
DEFAULT_CHROME_INTERNAL_PLAYGROUND_REMOTE = (
    "sso://chrome-internal/playground/cq"
)
DEFAULT_CHROME_INTERNAL_PLAYGROUND_BRANCH = "refs/heads/main"
CHROME_INTERNAL_PLAYGROUND_TRACKING_REF = (
    "refs/remotes/chrome-internal-playground/main"
)

VALID_TRAJECTORY_GRANULARITIES = ("auto", "turn", "phase", "step")

FORCED_PROMOTION_SCORE_CAP = 0.65


class PromotionReadinessError(RuntimeError):
    """Raised when `--promote-self-review` is blocked by the readiness gate."""


@dataclasses.dataclass
class FileEditOp:
    """Single file modification extracted from a trajectory turn."""

    rel_path: str
    abs_path: str
    op: str  # "edit" or "write"
    target: Optional[str] = None
    replacement: Optional[str] = None
    content: Optional[str] = None
    append: bool = False
    start_line: int = 1
    end_line: int = 1


@dataclasses.dataclass
class CheckInPrompt:
    """Structured AI-generated check-in prompt requiring author sign-off."""

    id: str
    path: str
    start_line: int
    end_line: int
    prompt: str
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class TrajectoryTurn:
    """Represents one code-modifying user-prompt execution turn."""

    turn_index: int
    step_index: int
    created_at: str
    user_prompt: str
    planner_summary: str
    short_title: str
    file_edits: List[FileEditOp] = dataclasses.field(default_factory=list)
    modified_files: List[str] = dataclasses.field(default_factory=list)
    check_in_prompts: List[CheckInPrompt] = dataclasses.field(
        default_factory=list
    )


@dataclasses.dataclass
class TurnSnapshot:
    """Reconstructed Git tree and metadata for a single trajectory turn."""

    turn: TrajectoryTurn
    tree_sha: str
    sparse_tree_sha: str = ""
    commit_sha: str = ""
    patchset_num: int = 0


def SelfReviewChangeId(canonical_change_id: str) -> str:
    """Derives a deterministic Gerrit Change-Id for CL_self from CL_peer's Change-Id."""
    clean_id = canonical_change_id.strip()
    digest = hashlib.sha1(f"self-review:{clean_id}".encode("utf-8")).hexdigest()
    return f"I{digest}"


def ReplaceChangeIdInDescription(description: str, new_change_id: str) -> str:
    """Replaces or appends the Change-Id footer in a commit description."""
    without_id = git_footers.remove_footer(description, "Change-Id")
    return git_footers.add_footer_change_id(without_id, new_change_id)


def NormalizeGerritHostName(host_or_url: str) -> str:
    """Normalizes a Gerrit host or URL into a bare `.googlesource.com` hostname for `gerrit_util`."""
    raw = (host_or_url or "").strip()
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        host = parsed.netloc or raw
    else:
        host = raw.rstrip("/")
    if host.endswith(".git.corp.google.com"):
        host = (
            host[: -len(".git.corp.google.com")] + ".googlesource.com"
        )
    return host


def FormatGerritHostUrl(host_or_url: str) -> str:
    """Formats a Gerrit hostname or URL as a full `https://<host>` URL for `self_review_host`."""
    raw = (host_or_url or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" in raw:
        return raw
    return f"https://{raw}"


def ResolveSelfReviewHost(
    default_host: str,
    explicit_host: Optional[str] = None,
    git_config_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Resolves the Gerrit host URL for `CL_self` (`self_review_host`).

    Order:
      1. Explicit `--self-review-host` CLI flag.
      2. `branch.<name>.gerritselfreviewserver` git config value.
      3. Target repository's default Gerrit host (`default_host`).
    """
    if explicit_host and explicit_host.strip():
        return FormatGerritHostUrl(explicit_host)
    if git_config_getter:
        cfg_host = git_config_getter(SELF_REVIEW_SERVER_CONFIG_KEY)
        if cfg_host and cfg_host.strip():
            return FormatGerritHostUrl(cfg_host)
    return FormatGerritHostUrl(default_host)


def IsChromeInternalSelfReviewHost(host_or_url: str) -> bool:
    """Returns True if `host_or_url` points to the `chrome-internal` Gerrit host."""
    clean = NormalizeGerritHostName(host_or_url).lower()
    return clean.startswith("chrome-internal-review.") or clean.startswith(
        "chrome-internal."
    )


def ResolveSelfReviewRemoteAndBranch(
    self_review_host_url: str,
    default_remote_url: str,
    default_branch: str,
) -> Tuple[str, str, bool]:
    """Resolves `(remote_url, target_branch, use_sparse_tree)` for `CL_self`.

    When `self_review_host_url` targets `chrome-internal-review`, routes `CL_self`
    pushes to `https://chrome-internal.git.corp.google.com/playground` on
    `refs/heads/main` using a Sparse Root Tree (`use_sparse_tree=True`).
    """
    if IsChromeInternalSelfReviewHost(self_review_host_url):
        return (
            DEFAULT_CHROME_INTERNAL_PLAYGROUND_REMOTE,
            DEFAULT_CHROME_INTERNAL_PLAYGROUND_BRANCH,
            True,
        )
    return default_remote_url, default_branch, False



def _TranscriptMatchesRepo(
    transcript_path: str,
    norm_repo: Optional[str],
    affected_set: Optional[set] = None,
) -> Tuple[bool, bool]:
    """Returns `(matched_repo, matched_affected)` for a transcript JSONL file."""
    matched_repo = False
    matched_affected = False
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                step = json.loads(line)
                for tc in step.get("tool_calls") or []:
                    if tc.get("name") not in ("edit_file", "write_file"):
                        continue
                    args = tc.get("args") or tc.get("arguments") or {}
                    if not isinstance(args, dict):
                        continue
                    file_path = args.get("Path") or ""
                    if not file_path or "/.gemini/jetski/brain/" in file_path:
                        continue
                    if norm_repo:
                        real_fp = os.path.realpath(file_path)
                        if real_fp.startswith(norm_repo + os.sep):
                            matched_repo = True
                            rel = os.path.relpath(real_fp, norm_repo).replace(
                                "\\", "/"
                            )
                            if not affected_set or rel in affected_set:
                                matched_affected = True
                    else:
                        matched_repo = True
                        matched_affected = True
    except (OSError, json.JSONDecodeError):
        pass
    return matched_repo, matched_affected


def ResolveConversationId(
    repo_root: Optional[str] = None,
    branch: Optional[str] = None,
    explicit_id: Optional[str] = None,
    affected_files: Optional[Sequence[str]] = None,
    brain_root: Optional[str] = None,
    git_config_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Resolves the active Jetski conversation ID using a 4-level fallback chain."""
    if explicit_id:
        return explicit_id.strip()

    if git_config_getter:
        cfg_id = git_config_getter(JETSKI_CONVERSATION_ID_CONFIG_KEY)
        if cfg_id and cfg_id.strip():
            return cfg_id.strip()

    root_dir = brain_root or DEFAULT_BRAIN_ROOT
    norm_repo = os.path.realpath(repo_root) if repo_root else None
    affected_set = {f.replace("\\", "/") for f in (affected_files or []) if f}

    env_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
    if env_id:
        env_tpath = ResolveTranscriptPath(env_id, brain_root=root_dir)
        if not env_tpath or not norm_repo:
            return env_id
        m_repo, _ = _TranscriptMatchesRepo(env_tpath, norm_repo, affected_set)
        if m_repo:
            return env_id

    if not os.path.isdir(root_dir):
        return env_id or None

    patterns = [
        os.path.join(
            root_dir, "*", ".system_generated", "logs", "transcript_full.jsonl"
        ),
        os.path.join(
            root_dir, "*", ".system_generated", "logs", "transcript.jsonl"
        ),
    ]
    candidates: List[str] = []
    seen_convs = set()
    for pat in patterns:
        for path in sorted(
            glob.glob(pat), key=lambda p: os.path.getmtime(p), reverse=True
        ):
            conv_id = os.path.basename(
                os.path.dirname(os.path.dirname(os.path.dirname(path)))
            )
            if conv_id not in seen_convs:
                seen_convs.add(conv_id)
                candidates.append(path)

    best_fallback: Optional[str] = None
    for path in candidates:
        conv_id = os.path.basename(
            os.path.dirname(os.path.dirname(os.path.dirname(path)))
        )
        matched_repo, matched_affected = _TranscriptMatchesRepo(
            path, norm_repo, affected_set
        )
        if matched_repo and matched_affected:
            return conv_id
        if matched_repo and best_fallback is None:
            best_fallback = conv_id

    return best_fallback or (env_id if env_id else None)


def ResolveTranscriptPath(
    conv_id_or_path: str, brain_root: Optional[str] = None
) -> Optional[str]:
    """Returns the path to transcript_full.jsonl (or transcript.jsonl)."""
    if os.path.isfile(conv_id_or_path):
        return os.path.abspath(conv_id_or_path)

    root_dir = brain_root or DEFAULT_BRAIN_ROOT
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        candidate = os.path.join(
            root_dir, conv_id_or_path, ".system_generated", "logs", fname
        )
        if os.path.isfile(candidate):
            return candidate
    return None


def ResolveTrajectoryGranularity(
    explicit_granularity: Optional[str] = None,
    git_config_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Resolves trajectory->patchset granularity (`auto`, `turn`, `phase`, `step`)."""
    for candidate in (
        explicit_granularity,
        git_config_getter(TRAJECTORY_GRANULARITY_CONFIG_KEY)
        if git_config_getter
        else None,
    ):
        if candidate:
            clean = candidate.strip().lower()
            if clean in VALID_TRAJECTORY_GRANULARITIES:
                return clean
    return "auto"


def _SummarizeTurnTitle(
    turn_index: int,
    user_prompt: str,
    tool_summaries: List[str],
    modified_files: List[str],
) -> str:
    """Creates a concise `[self-review][turn-k] <summary>` patchset title."""
    cleaned_prompt = " ".join(user_prompt.strip().split())
    cleaned_prompt = re.sub(r"<[^>]+>", "", cleaned_prompt).strip()
    if cleaned_prompt:
        summary = cleaned_prompt[:56].rstrip()
        if len(cleaned_prompt) > 56:
            summary += "..."
    elif tool_summaries:
        summary = tool_summaries[0][:56]
    elif modified_files:
        summary = f"Update {os.path.basename(modified_files[0])}"
    else:
        summary = f"Turn {turn_index} update"
    return f"[self-review][turn-{turn_index}] {summary}"


def _ClassifyFilePhase(rel_path: str, op: str) -> str:
    """Classifies a file edit into a logical review phase (`core`, `build_and_cleanup`, `tests`)."""
    base_lower = os.path.basename(rel_path).lower()
    if any(
        tok in base_lower
        for tok in ("unittest", "egtest", "browsertest", "_test.", "test_")
    ):
        return "tests"
    if (
        op == "delete"
        or base_lower in ("build.gn", "build", "deps", "cmakelists.txt")
        or "constants" in base_lower
        or "flags" in base_lower
    ):
        return "build_and_cleanup"
    return "core"


def _ClassifyStepPhase(edits: Sequence[FileEditOp]) -> str:
    """Determines the dominant engineering phase for a single model step's edits."""
    phases = [_ClassifyFilePhase(e.rel_path, e.op) for e in edits]
    if phases and all(p == "tests" for p in phases):
        return "tests"
    if any(p == "build_and_cleanup" for p in phases):
        return "build_and_cleanup"
    return "core"


_PHASE_LABELS = {
    "core": "Core implementation",
    "build_and_cleanup": "Build targets, constants & cleanup",
    "tests": "Unit & integration tests",
}


def _FormatPhaseTurnTitle(
    turn_index: int,
    phase: str,
    modified_files: Sequence[str],
) -> str:
    """Builds a descriptive `[self-review][turn-k] <Phase>: <files>` title."""
    label = _PHASE_LABELS.get(phase, "Implementation phase")
    basenames = [os.path.basename(f) for f in modified_files[:2]]
    suffix = ", ".join(basenames)
    if len(modified_files) > 2:
        suffix += f" (+{len(modified_files) - 2} more)"
    full = f"{label}: {suffix}" if suffix else label
    if len(full) > 56:
        full = full[:53].rstrip() + "..."
    return f"[self-review][turn-{turn_index}] {full}"


def _ExtractRmDeletedPaths(
    command_line: str,
    norm_repo: Optional[str],
    affected_files: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str]]:
    """Extracts `(rel_path, abs_path)` pairs deleted via `rm` / `git rm` in `run_command`."""
    if not command_line or "rm " not in command_line:
        return []
    deleted: List[Tuple[str, str]] = []
    affected_norm = [
        f.replace("\\", "/") for f in (affected_files or []) if f
    ]
    for match in re.finditer(
        r"(?:^|[;&|\n]\s*)(?:git\s+)?rm\s+([^;&|\n]+)", command_line
    ):
        rm_args_str = match.group(1)
        for token in rm_args_str.split():
            tok = token.strip("'\"")
            if (
                not tok
                or tok.startswith("-")
                or "=" in tok
                or "$" in tok
                or tok.startswith("out/")
            ):
                continue
            if norm_repo:
                # Resolve relative to norm_repo without following symlinks on wildcards
                if os.path.isabs(tok):
                    if not tok.startswith(norm_repo + os.sep):
                        continue
                    rel_pattern = os.path.relpath(tok, norm_repo).replace(
                        "\\", "/"
                    )
                else:
                    rel_pattern = tok.lstrip("./").replace("\\", "/")
                if rel_pattern.startswith("..") or rel_pattern.startswith(
                    "out/"
                ):
                    continue
                if "*" in rel_pattern or "?" in rel_pattern:
                    if affected_norm:
                        for af in affected_norm:
                            if fnmatch.fnmatch(af, rel_pattern):
                                deleted.append(
                                    (af, os.path.join(norm_repo, af))
                                )
                    else:
                        deleted.append(
                            (rel_pattern, os.path.join(norm_repo, rel_pattern))
                        )
                else:
                    if not affected_norm or rel_pattern in affected_norm:
                        deleted.append(
                            (rel_pattern, os.path.join(norm_repo, rel_pattern))
                        )
    return deleted


def _ExtractStepFileEdits(
    mstep: Dict[str, Any],
    norm_repo: Optional[str],
    affected_files: Optional[Sequence[str]] = None,
) -> Tuple[List[FileEditOp], List[str], Optional[str]]:
    """Extracts `(file_edits, tool_summaries, planner_text)` from a single model step."""
    step_edits: List[FileEditOp] = []
    step_summaries: List[str] = []
    content = (mstep.get("content") or "").strip()
    planner_text = (
        content if (content and mstep.get("type") == "PLANNER_RESPONSE") else None
    )

    for tc in mstep.get("tool_calls") or []:
        tname = tc.get("name")
        args = tc.get("args") or tc.get("arguments") or {}
        if not isinstance(args, dict):
            continue
        if tname == "run_command":
            if args.get("toolSummary"):
                step_summaries.append(str(args["toolSummary"]))
            cmd_line = str(args.get("CommandLine") or "")
            for rel_p, abs_p in _ExtractRmDeletedPaths(
                cmd_line, norm_repo, affected_files
            ):
                step_edits.append(
                    FileEditOp(
                        rel_path=rel_p,
                        abs_path=abs_p,
                        op="delete",
                        start_line=1,
                        end_line=1,
                    )
                )
            continue

        if tname not in ("edit_file", "write_file"):
            continue
        raw_path = args.get("Path") or ""
        if not raw_path or "/.gemini/jetski/brain/" in raw_path:
            continue
        if args.get("ArtifactMetadata"):
            continue

        if norm_repo:
            real_path = (
                os.path.realpath(raw_path)
                if os.path.isabs(raw_path)
                else os.path.realpath(os.path.join(norm_repo, raw_path))
            )
            if not real_path.startswith(norm_repo + os.sep):
                continue
            rel_path = os.path.relpath(real_path, norm_repo).replace("\\", "/")
        else:
            real_path = os.path.abspath(raw_path)
            rel_path = raw_path.lstrip("/")

        if tname == "edit_file":
            replacement = args.get("Replacement", "")
            line_span = max(1, replacement.count("\n") + 1)
            step_edits.append(
                FileEditOp(
                    rel_path=rel_path,
                    abs_path=real_path,
                    op="edit",
                    target=args.get("Target", ""),
                    replacement=replacement,
                    start_line=1,
                    end_line=line_span,
                )
            )
        else:
            content_str = args.get("Content", "")
            line_span = max(1, content_str.count("\n") + 1)
            step_edits.append(
                FileEditOp(
                    rel_path=rel_path,
                    abs_path=real_path,
                    op="write",
                    content=content_str,
                    append=bool(args.get("Append", False)),
                    start_line=1,
                    end_line=line_span,
                )
            )
    return step_edits, step_summaries, planner_text


def _BuildTrajectoryTurn(
    turn_idx: int,
    step_index: int,
    created_at: str,
    user_prompt: str,
    planner_summary: str,
    short_title: str,
    file_edits: List[FileEditOp],
) -> TrajectoryTurn:
    """Constructs a `TrajectoryTurn` along with its primary `CheckInPrompt`."""
    modified_files = sorted({e.rel_path for e in file_edits})
    non_delete_edits = [e for e in file_edits if e.op != "delete"]
    code_edits = [
        e
        for e in non_delete_edits
        if os.path.basename(e.rel_path) not in ("BUILD.gn", "BUILD", "DEPS")
    ]
    primary_edit = (
        code_edits[0]
        if code_edits
        else (non_delete_edits[0] if non_delete_edits else file_edits[0])
    )
    check_ins = [
        CheckInPrompt(
            id=f"checkin-turn-{turn_idx}-1",
            path=primary_edit.rel_path,
            start_line=primary_edit.start_line,
            end_line=min(primary_edit.end_line, primary_edit.start_line + 15),
            prompt=(
                f"AI Check-In: Please confirm Turn {turn_idx} changes in "
                f"{primary_edit.rel_path} ({short_title})."
            ),
            acknowledged=False,
        )
    ]
    return TrajectoryTurn(
        turn_index=turn_idx,
        step_index=step_index,
        created_at=created_at,
        user_prompt=user_prompt,
        planner_summary=planner_summary,
        short_title=short_title,
        file_edits=file_edits,
        modified_files=modified_files,
        check_in_prompts=check_ins,
    )


def ParseTrajectoryTurns(
    transcript_path: str,
    repo_root: Optional[str] = None,
    max_turns: int = 10,
    granularity: str = "auto",
    affected_files: Optional[Sequence[str]] = None,
) -> List[TrajectoryTurn]:
    """Parses a Jetski transcript JSONL into code-modifying TrajectoryTurns.

    Supported `granularity` modes:
      - `"auto"` (default): Uses `"turn"` (`1 USER_INPUT = 1 Patchset`) when the
        trajectory has >= 2 code-modifying user turns; automatically falls back
        to `"phase"` splitting when a single high-level user prompt produced
        multiple distinct implementation/build/test model batches.
      - `"turn"`: Strictly `1 USER_INPUT = 1 Patchset` (coalesced to `max_turns`).
      - `"phase"`: Groups chronological model edit steps into logical engineering
        phases (`Core implementation`, `Build targets, constants & cleanup`,
        `Unit & integration tests`), coalescing consecutive steps that touch
        overlapping files or share the same phase.
      - `"step"`: Splits each code-modifying `PLANNER_RESPONSE` step into its own
        patchset, coalescing consecutive steps that only edit the exact same file.
    """
    norm_repo = os.path.realpath(repo_root) if repo_root else None
    clean_granularity = (granularity or "auto").strip().lower()
    if clean_granularity not in VALID_TRAJECTORY_GRANULARITIES:
        clean_granularity = "auto"

    steps: List[Dict[str, Any]] = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            steps.append(json.loads(line))

    raw_turns: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    current_user_step: Optional[Dict[str, Any]] = None
    current_model_steps: List[Dict[str, Any]] = []

    for step in steps:
        step_type = step.get("type")
        step_source = step.get("source")
        if step_type == "USER_INPUT" or (
            step_source == "USER_EXPLICIT" and step.get("content")
        ):
            if current_user_step is not None or current_model_steps:
                raw_turns.append(
                    (
                        current_user_step
                        or {
                            "step_index": 0,
                            "created_at": step.get("created_at", ""),
                            "content": "Initial implementation",
                        },
                        current_model_steps,
                    )
                )
            current_user_step = step
            current_model_steps = []
        else:
            current_model_steps.append(step)

    if current_user_step is not None or current_model_steps:
        raw_turns.append(
            (
                current_user_step
                or {
                    "step_index": 0,
                    "created_at": "",
                    "content": "Initial implementation",
                },
                current_model_steps,
            )
        )

    # First collect per-user-turn step batches.
    user_turn_batches: List[
        Tuple[
            Dict[str, Any],
            List[Dict[str, Any]],
            List[str],
            List[str],
        ]
    ] = []
    for user_step, model_steps in raw_turns:
        step_records: List[Dict[str, Any]] = []
        all_summaries: List[str] = []
        all_planner_texts: List[str] = []
        for mstep in model_steps:
            s_edits, s_summaries, p_text = _ExtractStepFileEdits(
                mstep, norm_repo, affected_files
            )
            all_summaries.extend(s_summaries)
            if p_text:
                all_planner_texts.append(p_text)
            if s_edits:
                step_records.append(
                    {
                        "step_index": int(
                            mstep.get(
                                "step_index", user_step.get("step_index", 0)
                            )
                        ),
                        "created_at": str(
                            mstep.get(
                                "created_at", user_step.get("created_at", "")
                            )
                        ),
                        "edits": s_edits,
                        "files": {e.rel_path for e in s_edits},
                        "phase": _ClassifyStepPhase(s_edits),
                        "summaries": list(s_summaries),
                        "planner_text": p_text or "",
                    }
                )
        if step_records:
            user_turn_batches.append(
                (user_step, step_records, all_summaries, all_planner_texts)
            )

    effective_mode = clean_granularity
    if effective_mode == "auto":
        if (
            len(user_turn_batches) == 1
            and len(user_turn_batches[0][1]) >= 2
        ):
            effective_mode = "phase"
        else:
            effective_mode = "turn"

    turns: List[TrajectoryTurn] = []
    if effective_mode == "turn":
        for (
            user_step,
            step_records,
            all_summaries,
            all_planner_texts,
        ) in user_turn_batches:
            file_edits = [
                e for rec in step_records for e in rec["edits"]
            ]
            modified_files = sorted({e.rel_path for e in file_edits})
            turn_idx = len(turns) + 1
            user_prompt = (user_step.get("content") or "").strip()
            planner_summary = (
                all_planner_texts[-1]
                if all_planner_texts
                else "; ".join(all_summaries)
                or f"Modified {', '.join(modified_files)}"
            )
            short_title = _SummarizeTurnTitle(
                turn_idx, user_prompt, all_summaries, modified_files
            )
            turns.append(
                _BuildTrajectoryTurn(
                    turn_idx=turn_idx,
                    step_index=int(user_step.get("step_index", 0)),
                    created_at=str(user_step.get("created_at", "")),
                    user_prompt=user_prompt,
                    planner_summary=planner_summary,
                    short_title=short_title,
                    file_edits=file_edits,
                )
            )
    elif effective_mode in ("phase", "step"):
        for (
            user_step,
            step_records,
            all_summaries,
            all_planner_texts,
        ) in user_turn_batches:
            user_prompt = (user_step.get("content") or "").strip()
            fallback_summary = (
                all_planner_texts[-1] if all_planner_texts else ""
            )
            coalesced_groups: List[Dict[str, Any]] = []
            for rec in step_records:
                if not coalesced_groups:
                    coalesced_groups.append(
                        {
                            "step_index": rec["step_index"],
                            "created_at": rec["created_at"],
                            "edits": list(rec["edits"]),
                            "files": set(rec["files"]),
                            "phase": rec["phase"],
                            "summaries": list(rec["summaries"]),
                            "planner_texts": (
                                [rec["planner_text"]]
                                if rec["planner_text"]
                                else []
                            ),
                        }
                    )
                    continue

                prev = coalesced_groups[-1]
                should_merge = False
                if effective_mode == "phase":
                    # Merge consecutive steps if they belong to the same logical
                    # phase OR modify overlapping files.
                    if (
                        prev["phase"] == rec["phase"]
                        or bool(prev["files"] & rec["files"])
                    ):
                        should_merge = True
                else:
                    # In 'step' mode, only merge consecutive steps that edit the
                    # exact same file set (preventing 5 micro-edits to 1 file
                    # from becoming 5 separate patchsets).
                    if prev["files"] == rec["files"]:
                        should_merge = True

                if should_merge:
                    prev["edits"].extend(rec["edits"])
                    prev["files"].update(rec["files"])
                    prev["summaries"].extend(rec["summaries"])
                    if rec["planner_text"]:
                        prev["planner_texts"].append(rec["planner_text"])
                else:
                    coalesced_groups.append(
                        {
                            "step_index": rec["step_index"],
                            "created_at": rec["created_at"],
                            "edits": list(rec["edits"]),
                            "files": set(rec["files"]),
                            "phase": rec["phase"],
                            "summaries": list(rec["summaries"]),
                            "planner_texts": (
                                [rec["planner_text"]]
                                if rec["planner_text"]
                                else []
                            ),
                        }
                    )

            for grp_idx, grp in enumerate(coalesced_groups):
                turn_idx = len(turns) + 1
                mod_files = sorted(grp["files"])
                if effective_mode == "phase" and len(coalesced_groups) > 1:
                    short_title = _FormatPhaseTurnTitle(
                        turn_idx, grp["phase"], mod_files
                    )
                    phase_label = _PHASE_LABELS.get(
                        grp["phase"], "Implementation phase"
                    )
                    step_prompt = (
                        user_prompt
                        if grp_idx == 0
                        else f"[{phase_label}] Update {', '.join(os.path.basename(f) for f in mod_files[:3])}"
                    )
                else:
                    short_title = _SummarizeTurnTitle(
                        turn_idx,
                        user_prompt if grp_idx == 0 else "",
                        grp["summaries"],
                        mod_files,
                    )
                    step_prompt = (
                        user_prompt
                        if grp_idx == 0
                        else f"Step {grp['step_index']}: update {', '.join(os.path.basename(f) for f in mod_files[:3])}"
                    )

                p_summary = (
                    grp["planner_texts"][-1]
                    if grp["planner_texts"]
                    else "; ".join(grp["summaries"])
                    or fallback_summary
                    or f"Modified {', '.join(mod_files)}"
                )
                turns.append(
                    _BuildTrajectoryTurn(
                        turn_idx=turn_idx,
                        step_index=grp["step_index"],
                        created_at=grp["created_at"],
                        user_prompt=step_prompt,
                        planner_summary=p_summary,
                        short_title=short_title,
                        file_edits=grp["edits"],
                    )
                )

    if max_turns and len(turns) > max_turns:
        overflow = len(turns) - max_turns + 1
        merged_edits: List[FileEditOp] = []
        merged_files = set()
        for t in turns[:overflow]:
            merged_edits.extend(t.file_edits)
            merged_files.update(t.modified_files)
        first = turns[overflow - 1]
        first.file_edits = merged_edits
        first.modified_files = sorted(merged_files)
        turns = [first] + turns[overflow:]
        for idx, t in enumerate(turns, start=1):
            t.turn_index = idx
            t.short_title = re.sub(
                r"^\[self-review\]\[turn-\d+\]",
                f"[self-review][turn-{idx}]",
                t.short_title,
            )

    return turns


def _ApplyEditToBlob(old_text: str, edit: FileEditOp) -> Tuple[str, int, int]:
    """Applies a single FileEditOp to `old_text`, returning `(new_text, start_line, end_line)`."""
    if edit.op == "write":
        if edit.append:
            combined = old_text + (edit.content or "")
            start_line = max(1, old_text.count("\n") + 1)
            end_line = max(start_line, combined.count("\n") + 1)
            return combined, start_line, end_line
        new_text = edit.content or ""
        if old_text:
            old_lines = old_text.splitlines()
            new_lines = new_text.splitlines()
            matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
            changed_spans = [
                (j1 + 1, max(j1 + 1, j2))
                for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
                if tag != "equal"
            ]
            if changed_spans:
                return (
                    new_text,
                    changed_spans[0][0],
                    min(changed_spans[0][1], changed_spans[0][0] + 20),
                )
        return new_text, 1, max(1, min(25, new_text.count("\n") + 1))

    target = edit.target or ""
    replacement = edit.replacement or ""
    if target and target in old_text:
        idx = old_text.find(target)
        start_line = old_text[:idx].count("\n") + 1
        end_line = start_line + max(0, replacement.count("\n"))
        return old_text.replace(target, replacement), start_line, end_line

    if target:
        norm_old_lines = [l.rstrip() for l in old_text.splitlines()]
        norm_target_lines = [l.rstrip() for l in target.splitlines()]
        n_target = len(norm_target_lines)
        for i in range(len(norm_old_lines) - n_target + 1):
            if norm_old_lines[i : i + n_target] == norm_target_lines:
                orig_lines = old_text.splitlines(keepends=True)
                repl_lines = replacement.splitlines(keepends=True)
                if repl_lines and not repl_lines[-1].endswith("\n"):
                    repl_lines[-1] += "\n"
                updated = orig_lines[:i] + repl_lines + orig_lines[i + n_target :]
                start_line = i + 1
                end_line = start_line + max(0, len(repl_lines) - 1)
                return "".join(updated), start_line, end_line

    return old_text, 1, 1


def ReconstructTurnTrees(
    repo_root: str,
    base_commit: str,
    head_commit: str,
    turns: Sequence[TrajectoryTurn],
) -> List[TurnSnapshot]:
    """Reconstructs per-turn Git tree SHAs using a temporary GIT_INDEX_FILE.

    Replays `edit_file` and `write_file` operations starting from `base_commit`
    and pins the final turn K to `head_commit:` so PS_K is 100% byte-identical
    to the actual working branch HEAD commit tree.
    """
    head_tree_sha = subprocess.check_output(
        ["git", "rev-parse", f"{head_commit}:"],
        cwd=repo_root,
        text=True,
    ).strip()

    if not turns:
        return []

    snapshots: List[TurnSnapshot] = []
    file_state: Dict[str, str] = {}

    def _read_base_blob(rel_path: str) -> str:
        if rel_path in file_state:
            return file_state[rel_path]
        proc = subprocess.run(
            ["git", "show", f"{base_commit}:{rel_path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        text = proc.stdout if proc.returncode == 0 else ""
        file_state[rel_path] = text
        return text

    with tempfile.TemporaryDirectory(prefix="jetski_traj_idx_") as tmpdir:
        temp_index = os.path.join(tmpdir, "index")
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = temp_index

        subprocess.run(
            ["git", "read-tree", base_commit],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
        )

        for i, turn in enumerate(turns):
            is_last = i == len(turns) - 1
            for edit in turn.file_edits:
                if edit.op == "delete":
                    file_state.pop(edit.rel_path, None)
                    subprocess.run(
                        [
                            "git",
                            "update-index",
                            "--force-remove",
                            edit.rel_path,
                        ],
                        cwd=repo_root,
                        env=env,
                        check=False,
                        capture_output=True,
                    )
                    continue

                old_text = _read_base_blob(edit.rel_path)
                new_text, s_line, e_line = _ApplyEditToBlob(old_text, edit)
                edit.start_line = s_line
                edit.end_line = e_line
                file_state[edit.rel_path] = new_text

                blob_sha = subprocess.check_output(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=repo_root,
                    env=env,
                    input=new_text,
                    text=True,
                ).strip()
                subprocess.run(
                    [
                        "git",
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"100644,{blob_sha},{edit.rel_path}",
                    ],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                )

            if turn.check_in_prompts and turn.file_edits:
                non_del = [e for e in turn.file_edits if e.op != "delete"]
                code_ed = [
                    e
                    for e in non_del
                    if os.path.basename(e.rel_path)
                    not in ("BUILD.gn", "BUILD", "DEPS")
                ]
                first_edit = (
                    code_ed[0]
                    if code_ed
                    else (non_del[0] if non_del else turn.file_edits[0])
                )
                turn.check_in_prompts[0].path = first_edit.rel_path
                turn.check_in_prompts[0].start_line = first_edit.start_line
                turn.check_in_prompts[0].end_line = first_edit.end_line

            if is_last:
                tree_sha = head_tree_sha
            else:
                tree_sha = subprocess.check_output(
                    ["git", "write-tree"],
                    cwd=repo_root,
                    env=env,
                    text=True,
                ).strip()

            snapshots.append(
                TurnSnapshot(
                    turn=turn,
                    tree_sha=tree_sha,
                    patchset_num=len(snapshots) + 1,
                )
            )

    return snapshots


def BuildSparseBaseAndTurnTrees(
    repo_root: str,
    base_commit: str,
    head_commit: str,
    snapshots: Sequence[TurnSnapshot],
    affected_files: Sequence[str],
    remote_parent_commit: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Builds a sparse base tree and per-turn sparse trees containing only modified files.

    Used when pushing `CL_self` from a huge monorepo (like `chromium/src`) to a
    lightweight Gerrit-backed sandbox repo (like `chrome-internal/playground`).
    By seeding `base_sparse_tree_sha` with the `base_commit` blobs of the
    modified files and parenting each turn commit on a commit of
    `base_sparse_tree_sha`, Gerrit renders the exact line-by-line diff against
    `base_commit` while uploading only a few kilobytes of objects.
    """
    tracked_paths = sorted(
        {
            f.replace("\\", "/")
            for f in list(affected_files or [])
            + [
                mf
                for snap in snapshots
                for mf in snap.turn.modified_files
            ]
            if f
        }
    )

    def _populate_sparse_index_from_tree(
        tree_or_commit: str, env: Dict[str, str]
    ) -> str:
        if remote_parent_commit:
            subprocess.run(
                ["git", "read-tree", remote_parent_commit],
                cwd=repo_root,
                env=env,
                check=False,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "read-tree", "--empty"],
                cwd=repo_root,
                env=env,
                check=True,
                capture_output=True,
            )

        for rel_path in tracked_paths:
            proc = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", f"{tree_or_commit}:{rel_path}"],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                blob_sha = proc.stdout.strip()
                subprocess.run(
                    [
                        "git",
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"100644,{blob_sha},{rel_path}",
                    ],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                )
            else:
                subprocess.run(
                    ["git", "update-index", "--force-remove", rel_path],
                    cwd=repo_root,
                    env=env,
                    check=False,
                    capture_output=True,
                )

        return subprocess.check_output(
            ["git", "write-tree"],
            cwd=repo_root,
            env=env,
            text=True,
        ).strip()

    sparse_turn_trees: List[str] = []
    with tempfile.TemporaryDirectory(prefix="jetski_sparse_idx_") as tmpdir:
        temp_index = os.path.join(tmpdir, "index")
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = temp_index

        base_sparse_tree_sha = _populate_sparse_index_from_tree(
            base_commit, env
        )
        for i, snap in enumerate(snapshots):
            is_last = i == len(snapshots) - 1
            source_ref = head_commit if is_last else snap.tree_sha
            s_tree = _populate_sparse_index_from_tree(source_ref, env)
            snap.sparse_tree_sha = s_tree
            sparse_turn_trees.append(s_tree)

    return base_sparse_tree_sha, sparse_turn_trees



def ComputeAuthorReviewReceipt(
    turns: Sequence[TrajectoryTurn],
    total_affected_files: Sequence[str],
    gerrit_turns: int = 0,
    checkins_acknowledged: int = 0,
    forced_promotion: bool = False,
) -> Dict[str, Any]:
    """Computes the structured `author_review_receipt` payload for `SelfReviewMetadata`."""
    total_set = {f.replace("\\", "/") for f in total_affected_files if f}
    steered_files = set()
    for turn in turns:
        for f in turn.modified_files:
            if not total_set or f in total_set:
                steered_files.add(f)

    total_count = max(len(total_set), len(steered_files), 1)
    verified_count = len(steered_files)
    checkins_total = sum(len(t.check_in_prompts) for t in turns)

    base_coverage = verified_count / float(total_count)
    steering_bonus = 0.15 if (len(turns) > 1 or gerrit_turns > 0) else 0.0
    score = round(min(1.0, base_coverage * 0.85 + steering_bonus), 2)
    if forced_promotion:
        score = min(score, FORCED_PROMOTION_SCORE_CAP)

    return {
        "score": score,
        "local_turns": len(turns),
        "gerrit_turns": gerrit_turns,
        "verified_files_count": verified_count,
        "total_files_count": total_count,
        "checkins_acknowledged": checkins_acknowledged,
        "checkins_total": checkins_total,
    }


def DescribeBaseCommit(
    repo_root: str,
    base_commit: str,
    default_peer_host: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspects `base_commit` to extract SHA, subject, and peer Gerrit change metadata.

    Returns a dict with:
      - `base_commit_sha`: full 40-character commit hash
      - `base_subject`: one-line commit subject
      - `base_peer_change_num`: optional integer change number (from Reviewed-on, Change-Id query, or refs)
      - `base_peer_change_url`: normalized Gerrit URL on the peer host (.git.corp.google.com)
    """
    raw = subprocess.check_output(
        ["git", "show", "-s", "--format=%H%n%s%n%b", base_commit],
        cwd=repo_root,
        text=True,
    )
    lines = raw.split("\n")
    base_commit_sha = lines[0].strip() if lines else base_commit
    base_subject = lines[1].strip() if len(lines) > 1 else ""
    body = "\n".join(lines[2:]) if len(lines) > 2 else ""

    base_peer_change_num: Optional[int] = None
    m = re.search(r"Reviewed-on:\s*https?://[^\s]+/+(\d+)", body)
    if m:
        try:
            base_peer_change_num = int(m.group(1))
        except ValueError:
            base_peer_change_num = None

    if base_peer_change_num is None:
        cid_match = re.search(r"Change-Id:\s*(I[0-9a-fA-F]+)", body)
        if cid_match and default_peer_host:
            try:
                clean_host = NormalizeGerritHostName(default_peer_host)
                res = gerrit_util.QueryChanges(
                    clean_host,
                    [("change", cid_match.group(1))],
                    limit=1,
                )
                if res and isinstance(res, list) and "_number" in res[0]:
                    base_peer_change_num = int(res[0]["_number"])
            except Exception:  # pylint: disable=broad-except
                pass

    if base_peer_change_num is None:
        try:
            ref_proc = subprocess.run(
                [
                    "git",
                    "for-each-ref",
                    "--points-at",
                    base_commit_sha,
                    "--format=%(refname)",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            for rname in (ref_proc.stdout or "").splitlines():
                ref_m = re.search(r"refs/(?:changes/\d+|review)/(\d+)/", rname)
                if ref_m:
                    base_peer_change_num = int(ref_m.group(1))
                    break
        except Exception:  # pylint: disable=broad-except
            pass

    peer_host_clean = FormatGerritHostUrl(
        default_peer_host or "https://chromium-review.googlesource.com"
    )
    if ".googlesource.com" in peer_host_clean:
        peer_corp_url = peer_host_clean.replace(
            ".googlesource.com", ".git.corp.google.com"
        )
    else:
        peer_corp_url = peer_host_clean

    if base_peer_change_num is not None:
        if "chromium-review" in peer_corp_url:
            base_peer_change_url = (
                f"{peer_corp_url}/c/chromium/src/+/{base_peer_change_num}"
            )
        else:
            base_peer_change_url = f"{peer_corp_url}/c/{base_peer_change_num}"
    else:
        base_peer_change_url = f"{peer_corp_url}/q/{base_commit_sha}"

    return {
        "base_commit_sha": base_commit_sha,
        "base_subject": base_subject,
        "base_peer_change_num": base_peer_change_num,
        "base_peer_change_url": base_peer_change_url,
    }


def BuildSelfReviewMetadata(
    turn_index: int,
    rationale_scope: str,
    trajectory_id: str,
    self_review_host: Optional[str] = None,
    self_review_change_num: Optional[int] = None,
    peer_review_change_num: Optional[int] = None,
    promoted_tree_sha: Optional[str] = None,
    forced_promotion: bool = False,
    simulate_cq: Optional[Dict[str, Any]] = None,
    check_in_prompts: Optional[Sequence[Dict[str, Any]]] = None,
    author_review_receipt: Optional[Dict[str, Any]] = None,
    peer_review_host: Optional[str] = None,
    base_commit_sha: Optional[str] = None,
    base_peer_change_num: Optional[int] = None,
    base_peer_change_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Constructs the Spec v6 `SelfReviewMetadata` JSON wire contract."""
    metadata: Dict[str, Any] = {
        "turn_index": turn_index,
        "rationale_scope": rationale_scope,
        "trajectory_id": trajectory_id,
        "forced_promotion": bool(forced_promotion),
    }
    if self_review_host:
        metadata["self_review_host"] = FormatGerritHostUrl(self_review_host)
    if self_review_change_num is not None:
        metadata["self_review_change_num"] = int(self_review_change_num)
    if peer_review_change_num is not None:
        metadata["peer_review_change_num"] = int(peer_review_change_num)
    if peer_review_host:
        metadata["peer_review_host"] = FormatGerritHostUrl(peer_review_host)
    if base_commit_sha:
        metadata["base_commit_sha"] = str(base_commit_sha)
    if base_peer_change_num is not None:
        metadata["base_peer_change_num"] = int(base_peer_change_num)
    if base_peer_change_url:
        metadata["base_peer_change_url"] = str(base_peer_change_url)
    if promoted_tree_sha is not None:
        metadata["promoted_tree_sha"] = str(promoted_tree_sha)
    if simulate_cq is not None:
        metadata["simulate_cq"] = simulate_cq
    if check_in_prompts is not None:
        metadata["check_in_prompts"] = list(check_in_prompts)
    if author_review_receipt is not None:
        metadata["author_review_receipt"] = author_review_receipt
    return metadata


def VerifyPromotionReadiness(
    self_review_metadata: Dict[str, Any],
    current_tree_sha: str,
    force: bool = False,
) -> Tuple[bool, bool, List[str]]:
    """Enforces the Spec v6 Section 4.4.1 Hard-Blocked Promotion Gate (`CL_self -> CL_peer`).

    Checks:
      1. 100% of AI Check-In Prompts in `check_in_prompts` are acknowledged by
         the author (`acknowledged == True` and `checkins_acknowledged >= checkins_total`).
      2. `simulate_cq` is present, `status == "PASSED"`, and `tree_sha == current_tree_sha`.

    If checks fail and `force=False`, raises `PromotionReadinessError`.
    If checks fail and `force=True` (`--force` escape hatch), sets
    `forced_promotion=True` on `self_review_metadata` and caps
    `author_review_receipt["score"]` at `FORCED_PROMOTION_SCORE_CAP` (0.65).
    Always stamps `promoted_tree_sha = current_tree_sha`.

    Returns:
      `(passed_cleanly, forced_promotion, failure_reasons)`
    """
    reasons: List[str] = []

    prompts = self_review_metadata.get("check_in_prompts") or []
    unacked = [
        p.get("id", "<unknown>")
        for p in prompts
        if not p.get("acknowledged", False)
    ]
    if unacked:
        reasons.append(
            f"{len(unacked)}/{len(prompts)} AI Check-In prompt(s) are not yet "
            f"acknowledged by the author ({', '.join(unacked)})."
        )

    receipt = self_review_metadata.get("author_review_receipt") or {}
    ack_count = int(receipt.get("checkins_acknowledged", len(prompts) - len(unacked)))
    total_count = int(receipt.get("checkins_total", len(prompts)))
    if total_count > 0 and ack_count < total_count and not unacked:
        reasons.append(
            f"Only {ack_count}/{total_count} AI Check-In prompt(s) acknowledged in receipt."
        )

    sim_cq = self_review_metadata.get("simulate_cq")
    if not isinstance(sim_cq, dict):
        reasons.append(
            "Simulate CQ has not been run (`git cl simulate-cq` or `git cl upload --simulate-cq`)."
        )
    else:
        status = sim_cq.get("status")
        if status != "PASSED":
            reasons.append(f"Simulate CQ status is '{status}' (expected 'PASSED').")
        sim_tree = sim_cq.get("tree_sha")
        if sim_tree and current_tree_sha and sim_tree != current_tree_sha:
            reasons.append(
                f"Simulate CQ tree_sha ({sim_tree[:12]}) is stale vs current HEAD: tree ({current_tree_sha[:12]})."
            )

    self_review_metadata["promoted_tree_sha"] = current_tree_sha

    if reasons:
        if not force:
            joined = "\n  - ".join(reasons)
            raise PromotionReadinessError(
                "Promotion from Agentic Gerrit (CL_self) to Peer-Review CL (CL_peer) "
                "is blocked by the Promotion Readiness Gate:\n"
                f"  - {joined}\n"
                "Acknowledge all AI Check-In prompts on CL_self and pass `git cl simulate-cq`, "
                "or pass `--force` to override (which sets forced_promotion=true and caps "
                f"Author-Review score at {FORCED_PROMOTION_SCORE_CAP})."
            )
        self_review_metadata["forced_promotion"] = True
        if isinstance(receipt, dict):
            old_score = float(receipt.get("score", FORCED_PROMOTION_SCORE_CAP))
            receipt["score"] = min(old_score, FORCED_PROMOTION_SCORE_CAP)
            self_review_metadata["author_review_receipt"] = receipt
        return False, True, reasons

    self_review_metadata["forced_promotion"] = False
    return True, False, []


SELF_REVIEW_METADATA_LINK_PREFIX = (
    "\n\n[comments2code-self-review-metadata]: https://comments2code.internal/#"
)
SELF_REVIEW_METADATA_COMMENT_PREFIX = (
    "\n\n<!-- COMMENTS2CODE_SELF_REVIEW_METADATA_JSON:\n"
)
SELF_REVIEW_METADATA_COMMENT_SUFFIX = "\n-->"


def BuildRationalePayload(
    summary: str,
    file_rationales: Optional[Sequence[Dict[str, Any]]],
    self_review_metadata: Dict[str, Any],
    agent_id: str = "jetski",
) -> Dict[str, Any]:
    """Builds the POST body for `/changes/{id}/revisions/{rev}/comments2code~rationale`.

    Includes both top-level `PostRationale.Input` fields (`agent_id`, `cl_rationale`,
    `file_rationales`, `self_review_metadata`) and an invisible Markdown link
    reference definition (`[comments2code-self-review-metadata]: https://...#<base64>`)
    inside `cl_rationale`. Because PolyGerrit's `<gr-formatted-text>` (`marked`)
    suppresses Markdown link reference definitions (`def` tokens) while HTML-escaping
    `<!-- ... -->` comments, this envelope is 100% invisible even on un-upgraded
    production Gerrit clients.
    """
    cl_rationale = summary or ""
    if (
        self_review_metadata
        and "[comments2code-self-review-metadata]:" not in cl_rationale
        and "<!-- COMMENTS2CODE_SELF_REVIEW_METADATA_JSON:" not in cl_rationale
    ):
        encoded_meta = base64.b64encode(
            json.dumps(self_review_metadata, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        cl_rationale = (
            cl_rationale.rstrip()
            + SELF_REVIEW_METADATA_LINK_PREFIX
            + encoded_meta
        )
    file_list = list(file_rationales or [])
    return {
        "agent_id": agent_id,
        "cl_rationale": cl_rationale,
        "file_rationales": file_list,
        "self_review_metadata": self_review_metadata,
        "rationale": {
            "summary": summary,
            "file_rationales": file_list,
            "self_review_metadata": self_review_metadata,
        },
    }


def UploadRationale(
    host: str,
    change: str,
    revision: str,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Publishes AI Code Presentation & `self_review_metadata` to Gerrit."""
    clean_host = NormalizeGerritHostName(host)
    path = f"changes/{change}/revisions/{revision}/comments2code~rationale"
    try:
        return gerrit_util.CallGerritApi(
            clean_host,
            path,
            reqtype="POST",
            body=payload,
            accept_statuses=(200, 201, 204),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logging.debug(
            "comments2code~rationale endpoint unavailable on %s (%s); continuing.",
            clean_host,
            exc,
        )
        return None


def PublishTurnSteeringComment(
    host: str,
    change: str,
    prior_patchset: int,
    next_turn: TrajectoryTurn,
    prior_modified_files: Optional[Sequence[str]] = None,
) -> None:
    """Attaches Turn N+1's user prompt as a resolved comment on PS_N (`is_ai: False`)."""
    clean_host = NormalizeGerritHostName(host)
    primary_edit = next(
        (e for e in next_turn.file_edits if e.op != "delete"),
        next_turn.file_edits[0] if next_turn.file_edits else None,
    )
    if (
        primary_edit
        and (
            prior_modified_files is None
            or primary_edit.rel_path in set(prior_modified_files)
        )
    ):
        target_path = primary_edit.rel_path
    else:
        target_path = "/PATCHSET_LEVEL"

    comment_entry: Dict[str, Any] = {
        "message": (
            f"[Author Steering Prompt -> Turn {next_turn.turn_index}]\n"
            f"{next_turn.user_prompt}\n\n"
            f"Resolved in PS{next_turn.turn_index} ({next_turn.short_title}): "
            f"{next_turn.planner_summary}"
        ),
        "unresolved": False,
        "is_ai": False,
    }
    if (
        target_path != "/PATCHSET_LEVEL"
        and primary_edit
        and primary_edit.start_line > 0
    ):
        comment_entry["line"] = primary_edit.start_line

    try:
        gerrit_util.SetReview(
            clean_host,
            change,
            revision=str(prior_patchset),
            comments={target_path: [comment_entry]},
            notify=False,
            automatic_attention_set_update=False,
        )
    except gerrit_util.GerritError as exc:
        if exc.http_status == 400 and target_path != "/PATCHSET_LEVEL":
            comment_entry.pop("line", None)
            gerrit_util.SetReview(
                clean_host,
                change,
                revision=str(prior_patchset),
                comments={"/PATCHSET_LEVEL": [comment_entry]},
                notify=False,
                automatic_attention_set_update=False,
            )
        else:
            raise


def StageDraftRepliesOnPeerCl(
    host: str,
    peer_change_num: str,
    revision: str,
    draft_replies: Sequence[Dict[str, Any]],
    stage_enabled: bool = False,
) -> int:
    """Optionally stages unpublished Gerrit draft replies on `CL_peer` (Spec v6 Section 4.4.2).

    Off by default (`stage_enabled=False`) so human-to-human communication stays
    in the author's own words unless `--stage-draft-replies` or
    `gerrit.stage-ai-draft-replies=true` is explicitly enabled.
    """
    if not stage_enabled or not draft_replies:
        return 0

    clean_host = NormalizeGerritHostName(host)
    staged_count = 0
    for draft in draft_replies:
        body: Dict[str, Any] = {
            "path": draft.get("path", "/PATCHSET_LEVEL"),
            "message": draft.get("message", ""),
            "unresolved": bool(draft.get("unresolved", False)),
        }
        if draft.get("line"):
            body["line"] = int(draft["line"])
        if draft.get("in_reply_to"):
            body["in_reply_to"] = str(draft["in_reply_to"])
        gerrit_util.CreateDraft(
            clean_host,
            str(peer_change_num),
            revision=str(revision),
            body=body,
        )
        staged_count += 1
    return staged_count


def SyncPeerCommentsToSelfReview(
    peer_host: str,
    peer_change_num: str,
) -> List[Dict[str, Any]]:
    """Pulls unresolved human reviewer comments from `CL_peer` for Round-2+ self-review."""
    clean_host = NormalizeGerritHostName(peer_host)
    comments_by_path = gerrit_util.GetChangeComments(
        clean_host, str(peer_change_num)
    )
    unresolved_threads: List[Dict[str, Any]] = []
    if not isinstance(comments_by_path, dict):
        return unresolved_threads

    for path, comment_list in sorted(comments_by_path.items()):
        if not isinstance(comment_list, list):
            continue
        for c in comment_list:
            if not isinstance(c, dict):
                continue
            if c.get("unresolved") and not c.get("is_ai", False):
                unresolved_threads.append(
                    {
                        "id": c.get("id", ""),
                        "path": path,
                        "line": c.get("line", 0),
                        "patch_set": c.get("patch_set"),
                        "author": (c.get("author") or {}).get("email", ""),
                        "message": c.get("message", ""),
                    }
                )
    return unresolved_threads


def ArchiveSelfReviewCl(
    self_review_host: str,
    self_review_change_num: str,
    peer_change_num: str,
) -> Optional[Dict[str, Any]]:
    """Auto-transitions `CL_self` to ABANDONED when `CL_peer` is submitted or abandoned."""
    clean_host = NormalizeGerritHostName(self_review_host)
    msg = f"Archived Agentic Gerrit evidence for #{peer_change_num}"
    return gerrit_util.AbandonChange(
        clean_host, str(self_review_change_num), msg=msg
    )


def RunSimulateCq(
    repo_root: str,
    affected_files: Sequence[str],
    out_dir: str = "out/Default",
    head_commit: Optional[str] = None,
    tree_sha: Optional[str] = None,
    run_cmd_fn: Optional[
        Callable[[List[str], str], Tuple[int, str]]
    ] = None,
) -> Dict[str, Any]:
    """Runs local 'Simulate CQ' (`git cl presubmit` + `tools/autotest.py`) on `repo_root`.

    Returns the structured `simulate_cq` dictionary matching Spec v6 Section 4.2:
      {
        "status": "PASSED" | "FAILED",
        "presubmit_summary": str,
        "autotest_summary": str,
        "commit_sha": str,
        "tree_sha": str,
        "timestamp_ms": int,
      }
    """

    def _default_runner(cmd: List[str], cwd: str) -> Tuple[int, str]:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, combined

    runner = run_cmd_fn or _default_runner

    if not head_commit:
        rc, sha_out = runner(["git", "rev-parse", "HEAD"], repo_root)
        head_commit = sha_out.strip() if rc == 0 else "HEAD"

    if not tree_sha:
        rc, tree_out = runner(
            ["git", "rev-parse", f"{head_commit}:"], repo_root
        )
        tree_sha = tree_out.strip() if rc == 0 else ""

    # 1. Run `git cl presubmit --force`
    presubmit_cmd = ["git", "cl", "presubmit", "--force"]
    pre_rc, pre_out = runner(presubmit_cmd, repo_root)
    pre_lines = [l.strip() for l in pre_out.splitlines() if l.strip()]
    presubmit_summary = (
        pre_lines[-1]
        if pre_lines
        else ("Presubmit passed" if pre_rc == 0 else "Presubmit failed")
    )

    # 2. Identify test targets or affected files for `tools/autotest.py`
    autotest_script = os.path.join(repo_root, "tools", "autotest.py")
    test_candidates = [
        f
        for f in affected_files
        if "test" in os.path.basename(f).lower()
        or f.endswith((".cc", ".mm", ".py", ".js", ".ts"))
    ]
    if not test_candidates and affected_files:
        test_candidates = list(affected_files)[:5]

    auto_rc = 0
    if test_candidates and (
        run_cmd_fn is not None or os.path.isfile(autotest_script)
    ):
        vpython_bin = shutil.which("vpython3") or "vpython3"
        autotest_cmd = [
            vpython_bin,
            "tools/autotest.py",
            "-C",
            out_dir,
            *test_candidates,
        ]
        auto_rc, auto_out = runner(autotest_cmd, repo_root)
        auto_lines = [l.strip() for l in auto_out.splitlines() if l.strip()]
        autotest_summary = (
            auto_lines[-1]
            if auto_lines
            else (
                f"autotest.py passed ({len(test_candidates)} file(s))"
                if auto_rc == 0
                else "autotest.py failed"
            )
        )
    else:
        autotest_summary = "Skipped (no matching test targets or tools/autotest.py not found)"

    overall_status = "PASSED" if (pre_rc == 0 and auto_rc == 0) else "FAILED"
    return {
        "status": overall_status,
        "presubmit_summary": presubmit_summary[:240],
        "autotest_summary": autotest_summary[:240],
        "commit_sha": head_commit,
        "tree_sha": tree_sha,
        "timestamp_ms": int(time.time() * 1000),
    }
