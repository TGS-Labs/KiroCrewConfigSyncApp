"""Tests for backend/propagate.py (tasks.md 5.3).

Covers design.md's "backend/propagate.py — per-class propagation, verified"
table and requirements.md Requirement 5 acceptance criteria 5.1-5.5, 5.8, 5.9:

- 5.1: every tracked configuration class has a propagation classification,
  reported per applied file in the apply result.
- 5.2: a `steering/**` change is reported "live in a new session" — never
  claimed live in an already-running session, no gateway restart implied.
- 5.3: a `SKILL.md` *body* change is reported "live now".
- 5.4: a skill add/remove/rename, or a change to a skill's frontmatter
  triggers, invalidates the skill-discovery cache — reported distinctly
  from a body-only edit.
- 5.5: when the in-process invalidation path is unreachable (true for this
  out-of-process backend — design.md's ratified "Skill-cache lag accepted"
  decision), the bounded 60-second staleness window is reported honestly
  ("skill index visible within 60s") rather than claiming live availability.
- 5.8: `config.json` / model-pin changes are reported "live on next
  resolution; running sessions keep their resolved model".
- 5.9: the apply result distinguishes propagation state **per applied
  file** and never collapses multiple files' states into one summary
  message — this is the central property this test module proves.

Ground truth used throughout (read from the real modules, not guessed):

- `backend.allowlist.PropagationClass` has exactly four members:
  `LIVE_IN_NEW_SESSION`, `LIVE_IMMEDIATE`, `LIVE_WITHIN_60S`,
  `LIVE_ON_NEXT_RESOLUTION`. `propagate.py` reports against these four
  states (design.md's table maps 1:1 onto them) and must never invent a
  fifth or collapse two into one.
- `backend.classify.classify_paths(root, changed_paths)` returns a
  `Result` whose `.classified` maps a path to the `PropagationClass` its
  *allowlist entry* carries. That per-entry classification is necessarily
  coarse: `skills/**/SKILL.md` and `skills/**/scripts/**` both carry
  `LIVE_IMMEDIATE` in the allowlist (backend/allowlist.py), because
  allowlist classification is a pure function of the *path pattern*, not
  of what kind of change happened to that path. The add/remove/rename-vs.
  body-edit distinction that requirements.md 5.4 requires is therefore a
  property of the *change itself* (was this SKILL.md added, removed,
  renamed, or just edited in place; did its frontmatter triggers change),
  which `propagate.py` must accept as input and use to *refine* a
  `SKILL.md`'s classification from the allowlist's baseline
  `LIVE_IMMEDIATE` up to `LIVE_WITHIN_60S` when the change is
  discoverability-shaped rather than body-only.

This module intentionally imports `propagate` (backend/propagate.py), which
does not exist yet. Every test below is expected to fail at collection time
with `ModuleNotFoundError: No module named 'backend.propagate'` until
software-engineer implements it — this is the correct TDD starting state,
not a test defect. Do not implement backend/propagate.py from this file.
"""

from __future__ import annotations

import inspect

import pytest

from backend.allowlist import PropagationClass
from backend import propagate


# ---------------------------------------------------------------------------
# Helpers — build one "applied file" record the way apply.py (tasks.md 5.1,
# not yet built) will hand propagate.py its per-file change list: a root, a
# relative path, and how the file changed. propagate.py is expected to
# expose a `ChangeKind`-shaped enum of its own (it is the first and only
# module in this codebase that needs to reason about add/remove/rename/
# modify, per requirements.md 5.4) — tests reach it via `propagate.ChangeKind`
# rather than a hand-rolled string, so a typo in the kind name fails loudly
# at attribute-access time instead of silently mis-classifying.
# ---------------------------------------------------------------------------


def _applied_file(
    root: str,
    relpath: str,
    kind: str,
    *,
    frontmatter_changed: bool = False,
) -> "propagate.AppliedFile":
    """Construct one applied-file record via `propagate.AppliedFile`.

    Kept as a thin wrapper (rather than importing a dataclass directly at
    module scope) so a missing/renamed field on `propagate.AppliedFile`
    surfaces as a clear `TypeError` at the call site instead of an import
    error masking which test triggered it.
    """
    return propagate.AppliedFile(
        root=root,
        relpath=relpath,
        kind=getattr(propagate.ChangeKind, kind),
        frontmatter_changed=frontmatter_changed,
    )


# ---------------------------------------------------------------------------
# (a) requirements.md 5.2 — steering/** -> "live in a new session".
# ---------------------------------------------------------------------------


class TestSteeringReportsLiveInNewSession:
    def test_steering_change_maps_to_live_in_new_session_class(self) -> None:
        applied = [_applied_file("A", "steering/foo.md", "modified")]

        report = propagate.build_report(applied)

        entry = report.entries["steering/foo.md"]
        assert entry.propagation_class == PropagationClass.LIVE_IN_NEW_SESSION

    def test_steering_message_names_new_session_and_not_already_running(
        self,
    ) -> None:
        applied = [_applied_file("A", "steering/foo.md", "modified")]

        report = propagate.build_report(applied)

        message = report.entries["steering/foo.md"].message.lower()
        assert "new session" in message
        # 5.2 explicitly forbids claiming the change is live in an
        # already-running session.
        assert "already running" not in message
        assert "immediately" not in message

    def test_steering_message_does_not_require_or_trigger_a_restart(self) -> None:
        applied = [_applied_file("A", "steering/bar.md", "added")]

        report = propagate.build_report(applied)

        entry = report.entries["steering/bar.md"]
        assert "restart" not in entry.message.lower()
        assert entry.requires_restart is False


# ---------------------------------------------------------------------------
# (b) requirements.md 5.3 — a SKILL.md *body* change -> "live now"
#     (LIVE_IMMEDIATE), distinct from an add/remove/rename/trigger change.
# ---------------------------------------------------------------------------


class TestSkillBodyEditReportsLiveNow:
    def test_skill_body_only_edit_maps_to_live_immediate(self) -> None:
        applied = [
            _applied_file(
                "A",
                "skills/deploy/SKILL.md",
                "modified",
                frontmatter_changed=False,
            )
        ]

        report = propagate.build_report(applied)

        entry = report.entries["skills/deploy/SKILL.md"]
        assert entry.propagation_class == PropagationClass.LIVE_IMMEDIATE

    def test_skill_body_message_says_live_now(self) -> None:
        applied = [
            _applied_file(
                "A",
                "skills/deploy/SKILL.md",
                "modified",
                frontmatter_changed=False,
            )
        ]

        report = propagate.build_report(applied)

        message = report.entries["skills/deploy/SKILL.md"].message.lower()
        assert "live now" in message or "now" in message
        assert "60" not in message

    def test_skill_scripts_change_also_maps_to_live_immediate(self) -> None:
        applied = [_applied_file("A", "skills/deploy/scripts/run.sh", "modified")]

        report = propagate.build_report(applied)

        entry = report.entries["skills/deploy/scripts/run.sh"]
        assert entry.propagation_class == PropagationClass.LIVE_IMMEDIATE


# ---------------------------------------------------------------------------
# (c) requirements.md 5.4 / 5.5 — a skill add/remove/rename, or a
#     frontmatter-trigger change, escalates to LIVE_WITHIN_60S and reports
#     the bounded 60s staleness window honestly (not "invalidated").
# ---------------------------------------------------------------------------


class TestSkillAddRemoveRenameOrTriggerChangeReportsBoundedWindow:
    @pytest.mark.parametrize("kind", ["added", "removed"])
    def test_skill_add_or_remove_escalates_to_live_within_60s(self, kind: str) -> None:
        applied = [_applied_file("A", "skills/deploy/SKILL.md", kind)]

        report = propagate.build_report(applied)

        entry = report.entries["skills/deploy/SKILL.md"]
        assert entry.propagation_class == PropagationClass.LIVE_WITHIN_60S

    def test_frontmatter_trigger_change_escalates_even_when_kind_is_modified(
        self,
    ) -> None:
        # requirements.md 5.4: "...or changes a skill's frontmatter
        # triggers" is its own escalation condition, independent of
        # add/remove/rename — a SKILL.md can be edited in place (kind
        # stays "modified") while its trigger description changes.
        applied = [
            _applied_file(
                "A",
                "skills/deploy/SKILL.md",
                "modified",
                frontmatter_changed=True,
            )
        ]

        report = propagate.build_report(applied)

        entry = report.entries["skills/deploy/SKILL.md"]
        assert entry.propagation_class == PropagationClass.LIVE_WITHIN_60S

    def test_body_only_modification_does_not_escalate(self) -> None:
        # The negative case proving the distinguishing logic actually
        # distinguishes: kind="modified" and frontmatter_changed=False
        # must NOT escalate to LIVE_WITHIN_60S.
        applied = [
            _applied_file(
                "A",
                "skills/deploy/SKILL.md",
                "modified",
                frontmatter_changed=False,
            )
        ]

        report = propagate.build_report(applied)

        entry = report.entries["skills/deploy/SKILL.md"]
        assert entry.propagation_class != PropagationClass.LIVE_WITHIN_60S

    def test_bounded_window_message_reports_60_seconds_honestly(self) -> None:
        applied = [_applied_file("A", "skills/deploy/SKILL.md", "added")]

        report = propagate.build_report(applied)

        message = report.entries["skills/deploy/SKILL.md"].message.lower()
        assert "60" in message
        # 5.5: report the bounded staleness window rather than claiming
        # the skill is immediately/live available.
        assert "live now" not in message
        assert "immediately available" not in message

    def test_rename_is_modelled_as_remove_plus_add_and_both_get_reported(
        self,
    ) -> None:
        # requirements.md 5.4 groups "adds, removes, or renames a skill"
        # together; design.md/task 5.3 explicitly calls out the rename
        # edge case as "both add + remove". A rename must therefore
        # surface as two distinct applied-file entries (the old path
        # removed, the new path added), each independently reported —
        # never silently merged into one entry that hides the old path.
        applied = [
            _applied_file("A", "skills/old-name/SKILL.md", "removed"),
            _applied_file("A", "skills/new-name/SKILL.md", "added"),
        ]

        report = propagate.build_report(applied)

        assert "skills/old-name/SKILL.md" in report.entries
        assert "skills/new-name/SKILL.md" in report.entries
        old_entry = report.entries["skills/old-name/SKILL.md"]
        new_entry = report.entries["skills/new-name/SKILL.md"]
        assert old_entry.propagation_class == PropagationClass.LIVE_WITHIN_60S
        assert new_entry.propagation_class == PropagationClass.LIVE_WITHIN_60S
        # The two paths must remain two separate report entries, not one
        # entry representing "the rename" under a single key.
        assert old_entry is not new_entry


# ---------------------------------------------------------------------------
# (d) requirements.md 5.8 — config.json / model pins -> "live on next
#     resolution; running sessions keep their resolved model".
# ---------------------------------------------------------------------------


class TestConfigAndModelPinsReportLiveOnNextResolution:
    @pytest.mark.parametrize(
        "relpath",
        ["config.json", "agent_model_state.json", "hooks.json", "mcp.json"],
    )
    def test_next_resolution_files_map_to_live_on_next_resolution(
        self, relpath: str
    ) -> None:
        applied = [_applied_file("A", relpath, "modified")]

        report = propagate.build_report(applied)

        entry = report.entries[relpath]
        assert entry.propagation_class == PropagationClass.LIVE_ON_NEXT_RESOLUTION

    def test_config_json_message_says_running_sessions_keep_resolved_model(
        self,
    ) -> None:
        applied = [_applied_file("A", "config.json", "modified")]

        report = propagate.build_report(applied)

        message = report.entries["config.json"].message.lower()
        assert "next resolution" in message
        assert "keep" in message and (
            "resolved" in message or "already resolved" in message
        )

    def test_agents_json_root_b_also_maps_to_live_on_next_resolution(self) -> None:
        applied = [_applied_file("B", "agents/my-agent.json", "modified")]

        report = propagate.build_report(applied)

        entry = report.entries["agents/my-agent.json"]
        assert entry.propagation_class == PropagationClass.LIVE_ON_NEXT_RESOLUTION


# ---------------------------------------------------------------------------
# (e) requirements.md 5.9 — the central "never collapses" property: a
#     multi-file apply result keeps a DISTINCT propagation state per file,
#     never merged into one summary message or one dominant class.
# ---------------------------------------------------------------------------


class TestMultiFileResultNeverCollapsesDistinctStates:
    def test_all_four_propagation_classes_can_appear_together_and_stay_distinct(
        self,
    ) -> None:
        applied = [
            _applied_file("A", "steering/foo.md", "modified"),
            _applied_file(
                "A", "skills/deploy/SKILL.md", "modified", frontmatter_changed=False
            ),
            _applied_file("A", "skills/newskill/SKILL.md", "added"),
            _applied_file("A", "config.json", "modified"),
        ]

        report = propagate.build_report(applied)

        assert (
            report.entries["steering/foo.md"].propagation_class
            == PropagationClass.LIVE_IN_NEW_SESSION
        )
        assert (
            report.entries["skills/deploy/SKILL.md"].propagation_class
            == PropagationClass.LIVE_IMMEDIATE
        )
        assert (
            report.entries["skills/newskill/SKILL.md"].propagation_class
            == PropagationClass.LIVE_WITHIN_60S
        )
        assert (
            report.entries["config.json"].propagation_class
            == PropagationClass.LIVE_ON_NEXT_RESOLUTION
        )

        # The defining property: four files, four DIFFERENT classes
        # represented among the entries — not reduced to one.
        classes_seen = {e.propagation_class for e in report.entries.values()}
        assert classes_seen == {
            PropagationClass.LIVE_IN_NEW_SESSION,
            PropagationClass.LIVE_IMMEDIATE,
            PropagationClass.LIVE_WITHIN_60S,
            PropagationClass.LIVE_ON_NEXT_RESOLUTION,
        }

    def test_report_has_no_single_overall_summary_message_field(self) -> None:
        # requirements.md 5.9 forbids collapsing into "a single success
        # message" — assert the report object carries no such field, so
        # a future edit cannot reintroduce one without this test naming
        # exactly what would violate the requirement.
        applied = [
            _applied_file("A", "steering/foo.md", "modified"),
            _applied_file("A", "config.json", "modified"),
        ]

        report = propagate.build_report(applied)

        for forbidden_attr in ("message", "summary", "overall_message"):
            assert not hasattr(report, forbidden_attr), (
                f"propagate.Report must not carry a single collapsing "
                f"'{forbidden_attr}' field — requirements.md 5.9 requires "
                f"a distinct state per applied file"
            )

    def test_entry_count_equals_applied_file_count_for_distinct_paths(self) -> None:
        applied = [
            _applied_file("A", "steering/a.md", "modified"),
            _applied_file("A", "steering/b.md", "modified"),
            _applied_file("A", "config.json", "modified"),
        ]

        report = propagate.build_report(applied)

        assert len(report.entries) == len(applied)

    def test_empty_applied_list_yields_empty_report_without_error(self) -> None:
        report = propagate.build_report([])

        assert report.entries == {}


# ---------------------------------------------------------------------------
# (f) requirements.md 5.1 — propagate.py must be able to report a
#     classification for every real allowlist entry's propagation class
#     (no class the allowlist actually uses is unreportable).
# ---------------------------------------------------------------------------


class TestEveryRealPropagationClassIsReportable:
    def test_all_four_enum_members_are_reachable_through_build_report(self) -> None:
        applied = [
            _applied_file("A", "steering/x.md", "modified"),
            _applied_file(
                "A", "skills/x/SKILL.md", "modified", frontmatter_changed=False
            ),
            _applied_file("A", "skills/y/SKILL.md", "added"),
            _applied_file("A", "config.json", "modified"),
        ]

        report = propagate.build_report(applied)

        reachable = {e.propagation_class for e in report.entries.values()}
        assert reachable == set(PropagationClass)

    def test_no_fifth_propagation_state_is_ever_produced(self) -> None:
        applied = [
            _applied_file("A", "steering/x.md", "modified"),
            _applied_file("A", "skills/x/SKILL.md", "added"),
            _applied_file("A", "config.json", "modified"),
            _applied_file(
                "A", "skills/y/SKILL.md", "modified", frontmatter_changed=False
            ),
        ]

        report = propagate.build_report(applied)

        valid_classes = set(PropagationClass)
        for entry in report.entries.values():
            assert entry.propagation_class in valid_classes


# ---------------------------------------------------------------------------
# propagate.py must reuse classify.py / allowlist.py's own classification
# rather than reimplementing path-to-class matching from scratch — same
# reuse discipline classify.py itself is held to.
# ---------------------------------------------------------------------------


class TestPropagateReusesClassifyAndAllowlistRatherThanReimplementing:
    def test_build_report_calls_into_classify_or_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend import classify as classify_module
        from backend import allowlist as allowlist_module

        calls: list[str] = []

        original_classify_paths = classify_module.classify_paths
        original_entry_matches = allowlist_module.entry_matches
        original_is_tracked = allowlist_module.is_tracked

        def _spy_classify_paths(
            root: str, changed_paths: list[str]
        ) -> classify_module.Result:
            calls.append("classify_paths")
            return original_classify_paths(root, changed_paths)

        def _spy_entry_matches(
            entry: allowlist_module.AllowlistEntry, relpath: str
        ) -> bool:
            calls.append("entry_matches")
            return original_entry_matches(entry, relpath)

        def _spy_is_tracked(root: str, relpath: str) -> bool:
            calls.append("is_tracked")
            return original_is_tracked(root, relpath)

        monkeypatch.setattr(classify_module, "classify_paths", _spy_classify_paths)
        monkeypatch.setattr(allowlist_module, "entry_matches", _spy_entry_matches)
        monkeypatch.setattr(allowlist_module, "is_tracked", _spy_is_tracked)
        for name, spy in (
            ("classify_paths", _spy_classify_paths),
            ("entry_matches", _spy_entry_matches),
            ("is_tracked", _spy_is_tracked),
        ):
            if hasattr(propagate, name):
                monkeypatch.setattr(propagate, name, spy)

        propagate.build_report([_applied_file("A", "steering/foo.md", "modified")])

        assert calls, (
            "build_report never called classify.classify_paths or "
            "allowlist.entry_matches/is_tracked — propagate.py must reuse "
            "the existing classification rather than reimplementing "
            "path-to-PropagationClass mapping"
        )

    def test_propagate_module_does_not_reimplement_glob_compilation(self) -> None:
        source = inspect.getsource(propagate)
        assert "_compile_pattern" not in source
        assert "fnmatch" not in source
