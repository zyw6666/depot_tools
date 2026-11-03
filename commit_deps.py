#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Analyzes commit ranges and recursively expands DEPS / submodule rolls."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import contextlib
import csv
from dataclasses import dataclass, field
import enum
import importlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import sys
import types
from typing import Any, NamedTuple

import gclient_eval
import gclient_paths
import gclient_utils
import git_common
import scm
import setup_color
import subprocess2

_Z = scm.ZERO_SHA
_VarMap = Mapping[str, Any]
_Diffs = Sequence[scm.DiffTreeEntry]
_Shas = tuple[tuple[str, str], ...]
_RootCtx = tuple[Mapping[str, str], bool, str, _VarMap]
_POS_RE = re.compile(r"(?i)^(?:cr-commit-position:\s*)?([\w/.-]*)@\{#(\d+)\}$")
_URL_RE = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)")
_ERRS = (gclient_utils.Error, SyntaxError, ValueError, KeyError, TypeError)
_EPILOG = (
    "examples:\n  git commit-deps origin/main..HEAD\n"
    "  git commit-deps 'Cr-Commit-Position: refs/heads/main@{#1412300}'\n"
    "  git commit-deps -n 10 --filter v8 --fetch\n"
    "  git checkout --detach && git commit-deps A..B --sync-command\n"
)


class CommitDepsError(Exception):
    pass


class CommitRange(NamedTuple):
    """Commit-ish range before resolution; 40-char git SHAs after resolution."""

    start: str
    end: str
    is_range: bool = True
    use_merge_base: bool = False

    @property
    def is_single_commit(self) -> bool:
        return not self.is_range

    @classmethod
    def from_args(
        cls, commits: Sequence[str] = (), *, max_count: int | None = None
    ) -> CommitRange:
        if len(commits) == 2:
            return cls(commits[0], commits[1], True)
        if len(commits) > 2 or (not commits and max_count is None):
            raise CommitDepsError(f"Expected 1..2 commits, got {len(commits)}.")
        p = cls.parse(commits[0]) if commits else cls("HEAD", "HEAD", False)
        if max_count is not None and (not commits or ".." not in commits[0]):
            return cls(p.start, p.end, max_count > 1)
        return p

    @classmethod
    def parse(cls, expr: str) -> CommitRange:
        if ".." in expr:
            sep = "..." if "..." in expr else ".."
            a, _, b = expr.partition(sep)
            if not (a or b):
                raise CommitDepsError(f"Invalid empty range {expr!r}.")
            return cls(a or "HEAD", b or "HEAD", True, sep == "...")
        return cls(ref := expr.removesuffix("^!") or "HEAD", ref, False)


@dataclass(frozen=True)
class SubrepoRoll:
    """A subrepository roll between two 40-char git commit SHAs."""

    rel_path: str
    old_sha: str
    new_sha: str

    @classmethod
    def from_diff_entry(cls, diff: scm.DiffTreeEntry) -> SubrepoRoll:
        return cls(diff.file_path, diff.old_sha, diff.new_sha)


@dataclass(frozen=True)
class GitCommitEntry:
    """A collected repository commit entry (`commit` is a 40-char git SHA)."""

    path: str
    level: int
    commit: str
    roll: bool = False
    subject: str = field(default="", compare=False)
    commit_position: str = field(default="", compare=False)
    pinned_shas: _Shas = field(default=(), repr=False, compare=False)

    @classmethod
    def build_sync_commands(
        cls, entries: Sequence[GitCommitEntry], *, standalone: bool = False
    ) -> list[str]:
        if standalone:
            return [f"git checkout --detach {e.commit}" for e in entries]
        out: list[str] = []
        for shas in cls.build_pinned_shas(entries):
            flags = " ".join(f"--revision {p}@{s}" for p, s in shas.items())
            out.append(f"gclient sync {flags}")
        return out

    @classmethod
    def build_pinned_shas(
        cls, entries: Sequence[GitCommitEntry]
    ) -> list[dict[str, str]]:
        anc: list[GitCommitEntry] = []
        out: list[dict[str, str]] = []
        for e in entries:
            anc = [*anc[: e.level], e]
            out.append(dict(e.pinned_shas) or {a.path: a.commit for a in anc})
        return out


class OutputFormat(enum.StrEnum):
    TABLE = "table"
    CSV = "csv"
    JSON = "json"

    def format_entries(
        self,
        entries: Sequence[GitCommitEntry],
        *,
        sync_command: bool = False,
        standalone: bool = False,
    ) -> str:
        fn = GitCommitEntry.build_sync_commands
        cmds = fn(entries, standalone=standalone) if sync_command else ()
        if self is OutputFormat.JSON:
            pinned_shas_list = GitCommitEntry.build_pinned_shas(entries)
            ks = ("path", "level", "commit", "roll", "subject")
            out = []
            for i, (e, shas) in enumerate(zip(entries, pinned_shas_list)):
                d = {k: getattr(e, k) for k in (*ks, "commit_position")}
                ex = {"sync_command": cmds[i]} if cmds else {}
                out.append({**d, "pinned_shas": shas, **ex})
            return f"{json.dumps(out, indent=2)}\n"
        hdrs = ("Path", "Level", "commit hash", "roll", "subject")
        hdrs = (*hdrs, "sync command") if cmds else hdrs
        tbl, rows = self is OutputFormat.TABLE, []
        for i, e in enumerate(entries):
            s = e.subject
            s = f"{s[:69]}..." if tbl and len(s) > 72 else s
            r = (e.path, str(e.level), e.commit, str(e.roll).lower(), s)
            rows.append((*r, cmds[i]) if cmds else r)
        if self is OutputFormat.CSV:
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows((hdrs, *rows))
            return buf.getvalue()
        ws = [max(len(r[i]) for r in (hdrs, *rows)) for i in range(len(hdrs))]
        all_r = (hdrs, tuple("-" * w for w in ws), *rows)
        lns = (" | ".join(f"{c:<{w}}" for c, w in zip(r, ws)) for r in all_r)
        return "".join(f"| {ln} |\n" for ln in lns)


class InputParser:
    @classmethod
    def parse_args(
        cls, argv: Sequence[str] | None = None
    ) -> argparse.Namespace:
        p = argparse.ArgumentParser(
            prog="git commit-deps",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description="Collects commits and recursively expands DEPS rolls.",
            epilog=_EPILOG,
        )
        add, fmts = p.add_argument, tuple(OutputFormat)
        fkw: dict[str, Any] = {"choices": fmts, "default": fmts[0]}
        add("commits", nargs="*", type=cls._parse_commit_ref)
        add("--repo", default=".")
        add("--format", type=lambda v: OutputFormat(v.strip().lower()), **fkw)
        add("--include-start", action="store_true")
        add("--first-parent", action="store_true")
        add("--max-level", type=int)
        add("-n", "--max-count", type=int)
        add("--filter", action="append", type=_parse_filter, default=[])
        add("--fetch", action="store_true")
        add("--sync-command", "--sync-commands", action="store_true")
        ns = p.parse_args(argv)
        cnt = 1 if ns.max_count is None else ns.max_count
        if not (r := ns.repo.strip()) or (ns.max_level or 0) < 0 or cnt < 1:
            p.error("invalid option value")
        ns.repo = Path(r).expanduser()
        return ns

    @classmethod
    def _parse_commit_ref(cls, val: str) -> str:
        pat = r"(?i)\bcr-commit-position:\s*"
        s = re.sub(pat, "Cr-Commit-Position: ", val.strip())
        rest = s.replace("Cr-Commit-Position: ", "")
        if not s or not s.isprintable() or any(c.isspace() for c in rest):
            raise ValueError(val)
        return s


class GitRepo(NamedTuple):
    path: Path
    display_path: str
    gclient_root: Path | None = None
    main_worktree: Path | None = None
    first_parent: bool = False

    @classmethod
    def resolve(
        cls,
        repo: Path = Path("."),
        cwd: Path | None = None,
        first_parent: bool = False,
    ) -> GitRepo:
        cand = _abspath((cwd or Path(".")) / repo.expanduser())
        rd = cand if (cand / ".git").exists() else None
        mwt: Path | None = None
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            if not rd and cand.is_dir():
                if top := scm.GIT.GetCheckoutRoot(str(cand)):
                    rd = _abspath(Path(top))
            if rd:
                cdir = _abspath(Path(scm.GIT.GetCommonGitDir(str(rd))))
                wc = cdir.parent if cdir.name == ".git" else None
                cfg = "" if wc else scm.GIT.GetConfig(str(rd), "core.worktree")
                wc = _abspath(cdir / cfg) if cfg else wc
                if wc and wc.is_dir() and wc.resolve() != rd.resolve():
                    mwt = wc
        if not rd:
            raise CommitDepsError(f"Could not resolve repository: {repo!r}")
        found = gclient_paths.FindGclientRoot(str(rd))
        anc = rd if found else (mwt or rd)
        found = found or (mwt and gclient_paths.FindGclientRoot(str(mwt)))
        groot = _abspath(Path(found)) if found else None
        ok = bool(groot and anc == groot)
        disp = anc.name if ok else (rd.name or ".")
        for c, r in ((anc, groot), (anc.resolve(), groot and groot.resolve())):
            if r and not ok:
                try:
                    disp, ok = c.relative_to(r).as_posix(), True
                except ValueError:
                    pass
        return cls(rd, disp or ".", groot if ok else None, mwt, first_parent)

    def subrepo(self, rel_path: str) -> GitRepo | None:
        n = posixpath.normpath(rel_path.replace("\\", "/")).lstrip("/")
        sub, mw = _abspath(self.path / n), self.main_worktree
        wt = _abspath(mw / n) if mw else None
        c = sub if (sub / ".git").exists() else wt
        if not c or not (c / ".git").exists():
            return None
        swt = wt if c == sub and wt and wt.is_dir() else None
        dp = self.child_display_path(n)
        return GitRepo(c, dp, self.gclient_root, swt, self.first_parent)

    def child_display_path(self, rel_path: str) -> str:
        n = posixpath.normpath(rel_path.replace("\\", "/")).lstrip("/")
        d = self.display_path
        j = n if d in ("", ".") else posixpath.normpath(f"{d}/{n}")
        return d if j == "." else j


class _ParsedDeps(NamedTuple):
    git_shas: Mapping[str, str]
    non_git: Mapping[str, Any]
    urls: Mapping[str, str]
    disabled: frozenset[str]
    recursedeps: Mapping[str, str]
    eff_vars: _VarMap
    is_sync: bool = False


_EMPTY_DEPS = _ParsedDeps({}, {}, {}, frozenset(), {}, {})


class CommitDepsCollector:
    def __init__(
        self,
        repo: Path = Path("."),
        *,
        cwd: Path | None = None,
        first_parent: bool = False,
        max_level: int | None = None,
        max_count: int | None = None,
        filters: Sequence[str] = (),
        fetch: bool = False,
    ) -> None:
        r = GitRepo.resolve(repo, cwd=cwd, first_parent=first_parent)
        self._root = r
        self.repo_dir, self.display_path = r.path, r.display_path
        self.is_standalone = r.gclient_root is None
        self.max_level, self.max_count, self.fetch = max_level, max_count, fetch
        flts: list[str] = []
        for raw in filters:
            with contextlib.suppress(ValueError):
                flts.append(_parse_filter(raw))
        self._flts = tuple(flts)
        self._deps_cache: dict[Any, _ParsedDeps | None] = {}
        self._gm_cache: dict[Any, tuple[dict[str, str], ...]] = {}
        self._active_ranges: set[tuple[Path, str, str]] = set()
        self._gc: Any = None
        rc: _RootCtx = ({}, True, gclient_eval.DEPS, {})
        self._root_ctx = rc if self.is_standalone else self._resolve_root_ctx()

    def collect(
        self,
        start_ref: CommitRange | str,
        end_ref: str | None = None,
        *,
        include_start: bool = False,
        is_range: bool = True,
        use_merge_base: bool = False,
        max_count: int | None = None,
    ) -> list[GitCommitEntry]:
        lim = self.max_count if max_count is None else max_count
        if isinstance(start_ref, CommitRange):
            cr = start_ref
        elif end_ref is None:
            cr = CommitRange.from_args([start_ref], max_count=lim)
        else:
            cr = CommitRange(start_ref, end_ref, is_range, use_merge_base)
        c = (*self._root_ctx, self._flts, 0, include_start, lim)
        return self._collect_repo(self._root, cr, c)

    def _resolve_root_ctx(self) -> _RootCtx:
        groot, (_, _, _, _, sol, df) = self._root.gclient_root, self._gclient()
        if not (groot and sol and self.display_path.startswith(f"{sol}/")):
            return {}, True, df, {}
        cur: GitRepo | None = GitRepo(_abspath(groot / sol), sol, groot)
        base_shas: dict[str, str] = {}
        rem, pv = self.display_path[len(sol) + 1 :], {}
        while rem and cur and (sha := _rev_sha(cur, "HEAD")):
            base_shas[cur.display_path] = sha
            b = _rev_sha(cur, f"{sha}:{df}") or _rev_sha(cur, f"{sha}:DEPS")
            deps = self._parse(cur, b or _Z, f"{sha}:.gitmodules", df, pv)
            recs = deps.recursedeps if deps and deps is not _EMPTY_DEPS else {}
            hit = next((p for p in recs if f"{rem}/".startswith(f"{p}/")), "")
            if not hit:
                return base_shas, False, df, {}
            pv = deps.eff_vars if deps else pv
            df, rem = recs[hit], rem[len(hit) + 1 :]
            cur = cur.subrepo(hit) if rem else cur
        return base_shas, bool(base_shas and not rem), df, pv

    def _collect_repo(self, r: GitRepo, cr: CommitRange, c: Any) -> Any:
        ctx8 = (*c, False, None)[:8]
        resolved_base_shas, parse_rolls, df, pvars, flts, lv, inc, lim = ctx8
        shas = self._resolve_repo_range(r, cr, lv)
        rkey = (r.path, shas.start, shas.end) if shas else None
        if not shas or not rkey or rkey in self._active_ranges:
            return []
        if not (cs := _list_commits(r, shas, cr, (lv, inc, lim))):
            return []
        dp, tip = r.display_path, cs[-1].sha
        init = {d.file_path: d.old_sha for cm in reversed(cs) for d in cm.diffs}
        if parse_rolls and df != "DEPS" and df not in init:
            df = df if _rev_sha(r, f"{tip}:{df}") else "DEPS"
        cur_b = init.get(df, "")
        gm = init.get(".gitmodules", f"{tip}:.gitmodules")
        cur_deps: _ParsedDeps | None = None
        entries: list[GitCommitEntry] = []
        self._active_ranges.add(rkey)
        try:
            for cm in cs:
                rolls: list[tuple[SubrepoRoll, bool]] = []
                is_roll, by_p = False, {d.file_path: d for d in cm.diffs}
                dd, gd = by_p.get(df), by_p.get(".gitmodules")
                gls = [d for d in cm.diffs if d.is_gitlink_change]
                if parse_rolls and (dd or gd or gls):
                    cur_b = cur_b or _rev_sha(r, f"{tip}:{df}") or _Z
                    ob, nb = (dd.old_sha, dd.new_sha) if dd else (cur_b, cur_b)
                    og, ng = (gd.old_sha, gd.new_sha) if gd else (gm, gm)
                    od = cur_deps
                    if (ob, og) != (cur_b, gm) or od is None:
                        od = self._parse(r, ob, og, df, pvars)
                    nd = od
                    if (ob, og) != (nb, ng):
                        prev = (od, ob, gls if og == ng else ())
                        nd = self._parse(r, nb, ng, df, pvars, prev)
                    rolls, is_roll = _diff_deps(r, gls, (od, nd), dd, flts)
                    cur_deps, cur_b, gm = nd, nb, ng
                if rolls and (self.max_level is None or lv < self.max_level):
                    psha = _rev_sha(r, f"{cm.sha}~1") or cm.sha
                    child_shas = {**resolved_base_shas, dp: psha}
                    meta = cur_deps is not None and cur_deps is not _EMPTY_DEPS
                    recs = cur_deps.recursedeps if cur_deps else {}
                    cv = cur_deps.eff_vars if (cur_deps and meta) else pvars
                    for rl, direct in rolls:
                        if (sub := r.subrepo(rl.rel_path)) is None:
                            cdp = r.child_display_path(rl.rel_path)
                            _warn_skip(cdp, rl.old_sha, rl.new_sha)
                            continue
                        cp = not meta or rl.rel_path in recs
                        cf = recs.get(rl.rel_path, "DEPS")
                        cfl = () if direct else flts
                        cc = ({**child_shas}, cp, cf, cv, cfl, lv + 1)
                        crng = CommitRange(rl.old_sha, rl.new_sha)
                        entries.extend(self._collect_repo(sub, crng, cc))
                        child_shas[sub.display_path] = rl.new_sha
                entry_shas = tuple({**resolved_base_shas, dp: cm.sha}.items())
                s, p = cm.subject, cm.commit_position
                e = GitCommitEntry(dp, lv, cm.sha, is_roll, s, p, entry_shas)
                entries.append(e)
            return entries
        finally:
            self._active_ranges.discard(rkey)

    def _resolve_repo_range(
        self, repo: GitRepo, commit_range: CommitRange, lv: int
    ) -> CommitRange | None:
        s_ref, e_ref = commit_range.start, commit_range.end
        if lv == 0:
            s, e = _resolve_commit(repo, s_ref), _resolve_commit(repo, e_ref)
            if commit_range.use_merge_base:
                msg = f"No merge-base for {s_ref!r} and {e_ref!r}"
                if not (s := _git_cap(repo.path, "merge-base", s, e).lower()):
                    raise CommitDepsError(msg)
            return CommitRange(s, e, is_range=commit_range.is_range)
        s, e = _rev_sha(repo, s_ref), _rev_sha(repo, e_ref)
        if (not s or not e) and self.fetch:
            ms = dict.fromkeys(r for r, x in ((s_ref, s), (e_ref, e)) if not x)
            _git_cap(repo.path, "fetch", "--no-tags", "--quiet", "origin", *ms)
            s, e = s or _rev_sha(repo, s_ref), e or _rev_sha(repo, e_ref)
        if s and e:
            return CommitRange(s, e, True)
        _warn_skip(repo.display_path, s_ref, e_ref, True)
        return None

    def _parse(
        self, r: GitRepo, b: str, gm: str, df: str, pv: _VarMap, pr: Any = None
    ) -> _ParsedDeps | None:
        key = (r.path, b, gm, repr(pv))
        if key in self._deps_cache:
            return self._deps_cache[key]
        tmpl, mfn, c_vars, c_deps, _, _ = self._gclient()
        bvars, pmap = tmpl.get_builtin_vars(), {}
        eff = _apply_vars(mfn, pv, bvars, c_vars)
        if b != _Z:
            if (txt := scm.GIT.GetBlob(str(r.path), b)) is None:
                return None
            if pr and pr[0] and pr[0].is_sync and pr[1] != _Z:
                gr = [d for d in pr[2] if d.is_gitlink_roll]
                ot = scm.GIT.GetBlob(str(r.path), pr[1]) if gr else None
                for d in gr:
                    ot = ot.replace(d.old_sha, d.new_sha) if ot else None
                if ot == txt:
                    gs = pr[0].git_shas
                    u = {d[4]: d[3] for d in gr if d[4] in gs}
                    res = pr[0]._replace(git_shas={**gs, **u})
                    return self._deps_cache.setdefault(key, res)
            try:
                kw = {"vars_override": eff, "builtin_vars": bvars}
                pmap = gclient_eval.Parse(txt, df, **kw)
            except _ERRS:
                return self._deps_cache.setdefault(key, None)
            if tos := pmap.get("target_os"):
                saved, tmpl.local_target_os = tmpl.local_target_os, list(tos)
                bvars = tmpl.get_builtin_vars()
                tmpl.local_target_os = saved
            eff = _apply_vars(mfn, pmap.get("vars", {}), pv, bvars, c_vars)
        mode = pmap.get("git_dependencies", gclient_eval.DEPS)
        mods: tuple[dict[str, str], ...] = ()
        if gm not in ("", _Z) and (b == _Z or mode == "SUBMODULES"):
            if (cached := self._gm_cache.get((r.path, gm))) is None:
                by_nm: dict[str, dict[str, str]] = {}
                raw = _git_cap(r.path, "config", "--blob", gm, "-l")
                for ln in raw.splitlines():
                    k, sep, v = ln.partition("=")
                    if sep and k.startswith("submodule.") and "." in k[10:]:
                        nm, sk = k[10:].rsplit(".", 1)
                        by_nm.setdefault(nm, {})[sk] = v.strip()
                cached = self._gm_cache[(r.path, gm)] = tuple(by_nm.values())
            mods = cached
        ok = b != _Z or bool(mods)
        res = _extract_deps(r, pmap, mods, eff, c_deps) if ok else _EMPTY_DEPS
        return self._deps_cache.setdefault(key, res)

    def _gclient(self) -> tuple[Any, Any, _VarMap, _VarMap, str, str]:
        if self._gc is None:
            r, gmod = self._root, importlib.import_module("gclient")
            opts = types.SimpleNamespace(deps_os=None, verbose=False)
            gc = gmod.GClient(str(r.gclient_root or r.path), opts)
            cfg = r.gclient_root / ".gclient" if r.gclient_root else None
            if cfg and cfg.is_file():
                with contextlib.suppress(*_ERRS, OSError):
                    gc.SetConfig(gclient_utils.FileRead(str(cfg)))
            dps = f"{self.display_path}/"
            sol = gc.dependencies[0] if gc.dependencies else None
            for s in gc.dependencies:
                if (n := (s.name or "").strip("/")) and dps.startswith(f"{n}/"):
                    sol = s
                    break
            cv, cd = (sol.custom_vars, sol.custom_deps) if sol else ({}, {})
            sn = (sol.name or "").strip("/") if sol else ""
            raw_df = (sol.deps_file or "DEPS") if sol else "DEPS"
            df = posixpath.normpath(raw_df.replace("\\", "/"))
            self._gc = (sol or gc, gmod.merge_vars, cv, cd, sn, df)
        return self._gc


def _parse_filter(val: str) -> str:
    if _URL_RE.match(s := val.strip()):
        return s.rstrip("/")
    b = s.replace("\\", "/").strip("/")
    if (n := posixpath.normpath(b) if b else "") in ("", ".", ".."):
        raise ValueError(val)
    return n


def _abspath(p: Path) -> Path:
    return Path(os.path.abspath(p))


def _list_commits(r: GitRepo, s: CommitRange, cr: CommitRange, opt: Any) -> Any:
    lv, inc, lim = opt
    cwd, fp, log = str(r.path), r.first_parent, scm.GIT.GetCommitLogDiffs
    if s.is_single_commit:
        return log(cwd, s.end, first_parent=fp, single_commit=True)
    if s.start == s.end:
        if lim and cr.start == cr.end and not cr.use_merge_base:
            return log(cwd, s.end, first_parent=fp, max_count=lim)
        res = log(cwd, s.end, first_parent=fp, single_commit=True)
        return res if inc else []
    cs = log(cwd, f"{s.start}..{s.end}", first_parent=fp, max_count=lim)
    if inc:
        head = log(cwd, s.start, first_parent=fp, single_commit=True)
        seen = {e.sha for e in head}
        merged = [*head, *(c for c in cs if c.sha not in seen)]
        return merged[-lim:] if lim is not None else merged
    if not cs and lv > 0:
        fwd = log(cwd, f"{s.end}..{s.start}", first_parent=fp)
        e0 = fwd and log(cwd, s.end, first_parent=fp, single_commit=True)
        dt = scm.DiffTreeEntry
        for x, t in zip(reversed(fwd), [*reversed(fwd[:-1]), *(e0 or ())]):
            ds = tuple(dt(d[1], d[0], d[3], d[2], d[4]) for d in x.diffs)
            cs.append(t._replace(diffs=ds))
    return cs


def _resolve_commit(repo: GitRepo, commit_ref: str) -> str:
    err = f"Cannot resolve {commit_ref!r} in {repo.path}"
    raw, num, strict = "", "", False
    if m := _POS_RE.match(commit_ref):
        raw, num, strict = m.group(1), m.group(2), True
    elif bm := re.match(r"(?i)^r?(\d{1,8})$", commit_ref):
        raw, num = "*", bm.group(1)
    if num:
        rx = r"^(?:refs/(?:remotes/(?:origin/)?|heads/)|origin/)"
        br = re.sub(rx, "", raw)
        if raw == "*" or br in ("", "HEAD", "main", "master"):
            pfx = ".*" if raw == "*" else "refs/heads/(master|main)"
            pat = rf"{pfx}@\{{#{num}\}}"
            rs = "remotes/origin/main remotes/origin/master"
            hs = "heads/main heads/master"
            cands: Any = (*(f"refs/{x}" for x in f"{rs} {hs}".split()), "HEAD")
        else:
            sub = "" if br.startswith("branch-heads/") else "heads/"
            rid = raw if raw.startswith("refs/") else f"refs/{sub}{br}"
            pat = rf"{re.escape(rid)}@\{{#{num}\}}"
            cands = (f"refs/remotes/origin/{br}", rid, raw, "HEAD")
        cmd = ("rev-list", "-n", "1", "--extended-regexp")
        g_arg, seen = f"--grep=^Cr-Commit-Position: {pat}", set()
        for ref in cands:
            if (tip := _rev_sha(repo, ref)) and tip not in seen:
                seen.add(tip)
                sha = _git_cap(repo.path, *cmd, g_arg, ref).lower()
                if gclient_utils.IsFullGitSha(sha):
                    return sha
        if strict:
            raise CommitDepsError(err)
    low = commit_ref.lower()
    if low.startswith("cr-commit-position:") or "@{#" in low:
        raise CommitDepsError(err)
    if sha := _rev_sha(repo, commit_ref):
        return sha
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", commit_ref):
        hint = f"fetch tags with 'git fetch origin tag {commit_ref}'"
        err += f" ({hint} or 'gclient sync --with_tags')"
    raise CommitDepsError(err)


def _rev_sha(repo: GitRepo, spec: str) -> str | None:
    with contextlib.suppress(subprocess2.CalledProcessError, OSError):
        res = scm.GIT.ResolveCommit(str(repo.path), spec)
        return res.strip().lower() if res else None
    return None


def _git_cap(cwd: Path, *args: str) -> str:
    with contextlib.suppress(subprocess2.CalledProcessError, OSError):
        out = scm.GIT.Capture(list(args), cwd=str(cwd))
        return out.strip() if isinstance(out, str) else ""
    return ""


def _warn_skip(dp: str, old: str, new: str, uf: bool = False) -> None:
    why = "commit not fetched" if uf else "subrepository checkout not found"
    pfx = f"Warning: Skipping unexpanded roll in {dp} ({old[:12]}..{new[:12]})"
    print(f"{pfx}: {why} locally.", file=sys.stderr)


def _apply_vars(fn: Any, *maps: _VarMap) -> dict[str, Any]:
    cs, out = gclient_eval.ConstantString, {}
    for m in maps:
        cp = {k: cs(v.value) if isinstance(v, cs) else v for k, v in m.items()}
        fn(out, cp)
    return out


def _diff_deps(r: GitRepo, gls: _Diffs, deps: Any, dd: Any, flts: Any) -> Any:
    od, nd = deps
    rmap: dict[str, SubrepoRoll] = {}
    oth: set[str] = set()
    if dd and od and nd:
        ong, nng = od.non_git, nd.non_git
        oth |= {k for k in ong.keys() | nng.keys() if ong.get(k) != nng.get(k)}
        if not (od.is_sync and nd.is_sync):
            og, ng = od.git_shas, nd.git_shas
            oth |= og.keys() ^ ng.keys()
            for p in og.keys() & ng.keys():
                if og[p] != ng[p] and _Z not in (og[p], ng[p]):
                    rmap[p] = SubrepoRoll(p, og[p], ng[p])
    odis = od.disabled if od else frozenset()
    ndis = nd.disabled if nd else frozenset()
    for d in gls:
        fp, add = d.file_path, d.new_mode == scm.GITLINK_MODE
        if d.is_gitlink_roll and fp in ndis:
            rmap.pop(fp, None)
        elif d.is_gitlink_roll:
            rmap[fp] = SubrepoRoll.from_diff_entry(d)
        elif fp not in (ndis if add else odis):
            prev = od.git_shas.get(fp) if (od and add) else None
            if prev and d.new_sha not in (_Z, prev):
                rmap[fp] = SubrepoRoll(fp, prev, d.new_sha)
            else:
                oth.add(fp)
    urls = {**(od.urls if od else {}), **(nd.urls if nd else {})}

    def match(rel: str) -> tuple[bool, bool]:
        if not flts:
            return True, True
        cs, fm = (rel, r.child_display_path(rel)), gclient_utils.FuzzyMatchRepo
        u = urls.get(rel)
        if not u and any(_URL_RE.match(f) for f in flts):
            if sub := r.subrepo(rel):
                u = scm.GIT.GetConfig(str(sub.path), "remote.origin.url")
        u0 = gclient_utils.SplitUrlRevision(u)[0] if u else ""
        in_path = any(f"/{f}/" in f"/{c}/" for c in cs for f in flts)
        if in_path or any(fm(c, flts) for c in (*cs, *([u0] if u0 else ()))):
            return True, True
        return any(f.startswith(f"{c}/") for c in cs for f in flts), False

    rolls = [(rmap[p], hit[1]) for p in sorted(rmap) if (hit := match(p))[0]]
    return rolls, bool(rolls) or any(match(p)[0] for p in oth - rmap.keys())


def _extract_deps(r: GitRepo, pm: Any, mods: Any, eff: Any, cd: Any) -> Any:
    urel = bool(pm.get("use_relative_paths"))
    rec: dict[str, str] = {}
    for e in pm.get("recursedeps", ()):
        rk, rf = (e, "DEPS") if isinstance(e, str) else e
        if rp := _norm_dep(r, rk, urel):
            rec.setdefault(rp, posixpath.normpath(rf.replace("\\", "/")))
    items = [(urel, k, v) for k, v in pm.get("deps", {}).items()]
    for m in mods:
        if rp := m.get("path", ""):
            cnd = m.get("gclient-condition", "")
            d = {"dep_type": "git", "url": m.get("url", ""), "condition": cnd}
            items.append((True, rp, d))
            if m.get("gclient-recursedeps", "").lower() == "true":
                if np := _norm_dep(r, rp, True):
                    rec.setdefault(np, "DEPS")
    shas, ngit, urls, conds = {}, {}, {}, {}
    dis: set[str] = set()

    def cond_ok(c: Any) -> bool:
        if not isinstance(c, str) or not c:
            return True
        if c not in conds:
            with contextlib.suppress(*_ERRS):
                conds[c] = bool(gclient_eval.EvaluateCondition(c, eff))
        return conds.setdefault(c, False)

    for is_rel, dkey, info in items:
        if not (rel := _norm_dep(r, dkey, is_rel)):
            continue
        cands = (dkey, r.child_display_path(rel), rel)
        if ck := next((k for k in cands if k in cd), ""):
            cv = cd[ck]
            ok = isinstance(cv, str) and not cv.endswith("@unmanaged")
            u0, crev = gclient_utils.SplitUrlRevision(cv) if ok else ("", "x")
            if u0:
                urls[rel] = u0
            if crev:
                dis.add(rel)
                continue
        dt = info.get("dep_type", "git")
        if not cond_ok(info.get("condition")):
            dis.update((rel,) if dt == "git" else ())
            continue
        if dt == "cipd":
            ngit[rel] = ("cipd", info.get("packages"))
        elif dt == "gcs":
            objs = info.get("objects", ())
            if act := [x for x in objs if cond_ok(x.get("condition"))]:
                ngit[rel] = ("gcs", info.get("bucket"), act)
        elif dt == "git" and (u := info.get("url")) is None:
            dis.add(rel)
        elif dt == "git" and isinstance(u, str):
            with contextlib.suppress(gclient_utils.Error):
                orig, sha = gclient_utils.SplitUrlRevision(u)
                if orig:
                    urls.setdefault(rel, orig)
                if sha and gclient_utils.IsFullGitSha(sha):
                    shas[rel] = sha.lower()
    shas = {k: v for k, v in shas.items() if k not in dis}
    is_sync = pm.get("git_dependencies", gclient_eval.DEPS) != "DEPS"
    return _ParsedDeps(shas, ngit, urls, frozenset(dis), rec, eff, is_sync)


def _norm_dep(repo: GitRepo, key: str, urel: bool) -> str:
    raw = key.replace("\\", "/").strip("/")
    dep = posixpath.normpath(raw) if raw else ""
    if dep and not urel:
        pfxs = (repo.display_path, repo.path.name)
        hit = next((p for p in pfxs if p and dep.startswith(f"{p}/")), "")
        if hit or (dep.startswith("src/") and not (repo.path / dep).exists()):
            dep = posixpath.normpath(dep.removeprefix(f"{hit or 'src'}/"))
    return "" if dep == "." else dep


def main(argv: Sequence[str] | None = None) -> int:
    a = InputParser.parse_args(argv)
    try:
        if gclient_utils.IsEnvCog():
            msg = "commit_deps is not supported in non-git environments."
            raise CommitDepsError(msg)
        commit_range = CommitRange.from_args(a.commits, max_count=a.max_count)
        kw: dict[str, Any] = {"first_parent": a.first_parent, "fetch": a.fetch}
        kw |= {"max_level": a.max_level, "max_count": a.max_count}
        c = CommitDepsCollector(a.repo, filters=a.filter, **kw)
        entries = c.collect(commit_range, include_start=a.include_start)
        ref, sc, st = "", a.sync_command, c.is_standalone
        with contextlib.suppress(OSError):
            ref = scm.GIT.GetBranchRef(str(c.repo_dir)) if sc and not st else ""
        if ref:
            br = scm.GIT.ShortBranchName(ref)
            w = f"Warning: {c.display_path} is on branch {br!r}; run "
            s = f"{w}'git checkout --detach' before 'gclient sync --revision'."
            print(s, file=sys.stderr)
        fmt: OutputFormat = a.format
        out = fmt.format_entries(entries, sync_command=sc, standalone=st)
        setup_color.init()
        env = os.environ
        pgr = env.get("GIT_PAGER", env.get("PAGER", "less"))
        tty = bool(entries and setup_color.IS_TTY and shutil.which("less"))
        ok = (env.get("TERM") or "dumb") != "dumb" and pgr not in ("", "cat")
        if fmt is OutputFormat.TABLE and tty and ok:
            with git_common.less() as pager:
                pager.write(out.encode("utf-8", "replace"))
            return 0
        sys.stdout.write(out)
        sys.stdout.flush()
        return 0
    except CommitDepsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            os.dup2(fd := os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            os.close(fd)
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
