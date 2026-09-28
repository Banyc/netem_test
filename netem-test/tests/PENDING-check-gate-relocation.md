# Queue: relocate the check-gate Rust test targets into `rtp_mux`

**Status: open.** Owner of the fix: an `rtp_mux` iteration (the checker's home).

The tooling move (commit `e423c41b`, "refactor(netem-test): move the mandate
tooling into rtp_mux") moved `netem-test/src/tools/` — including
`check_gate/` and `tools/json.rs` — into `rtp_mux` behind its `perf` feature,
but did not move the Rust test targets that exercise it. They stayed in
`netem-test/tests/` importing `netem_test::tools::check_gate::…`, a module
that no longer exists, so `cargo clippy --release --all-targets` was red with
14 `E0433` errors across five targets and `cargo test -p netem-test` could not
compile. The compilation repair in this crate removed the stale targets. This
file is the coverage record for that removal and the relocation queue — the
coverage is **not** deleted, it is unlanded until these targets run from
`rtp_mux`.

## No equivalent tests exist in `rtp_mux` (so the coverage is currently absent)

- `rtp_mux/src/tools/check_gate/` carries no test module: `rg -c '#\[test\]'
  src/tools/check_gate/*.rs` matches nothing, and there is no `#[cfg(test)]`.
- No file under `rtp_mux/tests/` references `check_gate`, `check_doc_counts`
  or `parse_env_tier`; `rtp_mux/tools/` holds no `test_*.py`.
- The only copies of the fixture (`EnvFixture`, `DocCountFixture`,
  `BASE_DESIGN`, `check_gate_common`, …) are the ones removed here.

## What was removed: 150 tests over five targets

### `check_gate_doc_counts.rs` — 15 tests

`check_gate::doc_counts::check_doc_counts` over a temp copy of the repository
docs: each derived count is re-derived from its command, and each failure mode
is shown to fire (a changed verified count names both values; a sentence that
left the prose fails rather than skips; a missing source is a failure; a
transcribed tally is refused). Tests:
`the_repository_docs_pass_and_the_check_looked_at_counts`,
`a_changed_verified_count_fails_naming_both_values`,
`a_sentence_that_left_the_prose_fails_rather_than_being_skipped`,
`a_missing_source_is_a_failure_not_a_skip`,
`the_producer_count_is_derived_from_the_registry`,
`the_harness_relation_counts_are_derived_from_its_own_blocks`,
`a_derived_claim_must_still_name_its_command`,
`the_draft_relation_counts_come_from_its_own_rows`,
`the_draft_row_count_is_the_parsed_row_count`,
`the_draft_cost_sums_come_from_its_rows`,
`the_draft_target_count_is_the_distinct_row_targets`,
`every_declared_reference_counts_both_declarations`,
`a_derived_claim_rejects_a_transcribed_tally`,
`a_transcription_split_across_lines_is_refused`,
`the_probe_selfcheck_tally_is_derived_too`.

### `check_gate_env.rs` — 43 tests

The `gate-env-tier` grammar and its enforcement: surface parsing, the
scriptless marker, load arithmetic, the reader-closure detection (direct
literal, `var_os`, one- and two-hop wrappers, closures, const aliases, key
positions, toolchain names). Tests:
`an_env_tier_only_failure_names_the_env_tier_and_not_the_manifest`,
`a_manifest_failure_gives_the_manifest_diagnostic`,
`an_undeclared_surface_is_named_rather_than_invisible`,
`a_well_formed_surface_passes_and_is_summarised`,
`a_surface_the_declaration_omits_fails`,
`a_declared_variable_no_source_reads_fails`,
`a_runner_naming_no_declared_variable_fails`,
`a_row_with_no_runner_field_fails`, `a_row_with_three_fields_fails`,
`a_runner_that_is_not_a_file_fails`, `a_repeated_variable_fails`,
`a_malformed_variable_name_fails`, `a_surface_with_no_coverage_cell_fails`,
`a_surface_that_measures_nothing_fails`,
`a_variable_only_the_sources_read_must_be_declared`,
`a_declared_scriptless_variable_passes`,
`the_note_names_a_surface_no_script_sets`,
`the_note_names_a_surface_no_script_sets_at_all`,
`a_surface_no_script_runs_is_declared_with_the_marker`,
`a_marked_surface_whose_variables_a_script_sets_fails`,
`a_load_shape_is_recorded_and_evaluated`,
`a_load_whose_total_is_not_arithmetic_fails`,
`a_load_total_no_variable_produces_fails`, `a_load_with_no_wall_clock_fails`,
`a_load_sizing_a_variable_the_runner_does_not_set_fails`,
`a_row_with_a_sixth_field_is_refused`,
`the_direct_literal_and_the_var_os_literal_are_reads`,
`a_direct_literal_the_declaration_omits_is_refused`,
`the_reader_closure_still_reaches_a_two_hop_wrapper`,
`a_name_only_a_mention_names_is_still_stale`,
`a_toolchain_name_is_not_demanded_of_a_surface`,
`a_toolchain_name_is_not_a_declarable_variable`,
`a_closure_forwarded_name_and_a_const_alias_are_reads`,
`a_closure_forwarded_name_the_declaration_omits_is_refused`,
`a_const_alias_name_the_declaration_omits_is_refused`,
`a_quoted_alias_declaration_cannot_shadow_the_real_one`,
`the_new_forms_only_a_mention_names_are_still_stale`,
`the_key_positions_of_the_fixture_are_the_reads`,
`a_name_shaped_argument_to_a_suffix_reader_is_not_demanded`,
`a_name_shaped_argument_to_a_suffix_reader_is_not_a_read`,
`a_non_key_argument_declared_is_refused_as_stale`,
`a_two_hop_key_position_is_still_a_read`,
`the_first_of_two_parameters_is_the_key`.

### `check_gate_perf.rs` — 77 tests

The `gate-perf-design` / `gate-budgets` grammar and derivation: tier
classification and budget sums, coverage cells and gaps, drift vs the measured
report, relation labels (orthogonal / composite / re-measurement / baseline),
baseline families, namespaces and membership. Tests:
`a_lib_entry_in_the_manifest_resolves`,
`a_lib_opt_in_a_perf_design_row_names_is_not_called_unclassified`,
`an_undeclared_lib_opt_in_is_reported_by_name`,
`a_lib_entry_in_gate_default_required_resolves`,
`a_zero_row_gap_only_declaration_passes`,
`a_zero_row_declaration_with_no_gap_fails`,
`a_zero_row_declaration_with_a_baseline_fails`,
`a_zero_row_declaration_still_refuses_a_reason_less_gap`,
`a_lib_entry_that_is_not_an_ignored_test_is_still_stale`,
`a_declared_lib_package_resolves_the_reserved_target`,
`a_declared_lib_package_that_lacks_the_test_fails`,
`a_gate_lib_package_block_naming_no_package_fails`,
`a_gate_lib_package_block_naming_two_packages_fails`,
`a_gate_lib_package_block_naming_an_invalid_package_fails`,
`declaration_without_perf_blocks_still_passes`, `unknown_test_fails`,
`unknown_target_fails`, `test_in_the_wrong_tier_fails`,
`wrong_tier_for_an_ignored_scenario_fails`, `over_budget_tier_sum_fails`,
`missing_tier_budget_fails`, `empty_coverage_cell_fails`,
`malformed_coverage_cell_fails`, `gap_without_a_reason_fails`,
`malformed_gap_cell_fails`, `measured_declared_drift_fails`,
`a_row_measured_over_its_tier_budget_fails`,
`drift_inside_the_tolerance_passes`, `a_stale_report_is_noted_not_failed`,
`a_report_without_per_test_timings_is_noted_not_failed`,
`a_missing_report_named_explicitly_fails`,
`a_report_with_an_unknown_schema_fails`,
`a_report_that_is_not_an_object_fails`,
`an_unlabelled_multi_dimension_row_fails`,
`a_row_differing_in_no_dimension_without_a_re_measurement_fails`,
`an_unlabelled_row_differing_in_no_dimension_fails`,
`an_ambiguous_row_fails`, `a_baseline_that_is_not_one_point_fails`,
`a_composite_label_that_misnames_the_dimensions_fails`,
`a_composite_label_on_a_one_dimension_row_fails`,
`a_re_measurement_label_on_a_multi_dimension_row_fails`,
`the_baseline_row_must_be_labelled_baseline`,
`a_non_baseline_row_may_not_be_labelled_baseline`,
`a_re_measurement_without_a_reason_fails`,
`a_composite_relation_naming_one_dimension_fails`,
`an_unknown_relation_fails`,
`a_row_without_a_relation_field_reports_it_as_a_field_count`,
`a_row_covering_two_cells_of_one_dimension_is_orthogonal`,
`a_row_whose_cells_vary_different_dimensions_is_composite`,
`budgets_without_a_design_block_fails`, `baseline_naming_no_row_fails`,
`a_budget_block_without_a_baseline_fails`,
`a_malformed_budget_line_fails`,
`a_row_naming_an_undeclared_baseline_family_fails`,
`a_named_baseline_naming_no_row_fails`,
`a_declared_baseline_no_row_uses_fails`,
`a_row_labelled_the_wrong_familys_baseline_fails`,
`a_named_familys_reference_must_carry_its_family_label`,
`a_row_that_is_two_families_reference_row_fails`,
`a_named_family_is_derived_against_its_own_baseline`,
`a_member_of_a_named_family_is_refused_the_wrong_label`,
`a_composite_label_mismatching_its_own_family_baseline_fails`,
`a_one_dimension_member_of_a_named_family_must_be_orthogonal`,
`a_named_family_without_a_namespace_fails`,
`a_family_whose_cells_share_no_prefix_says_so`,
`a_namespace_for_an_undeclared_family_fails`,
`a_malformed_namespace_fails`, `a_bare_members_line_declares_no_family`,
`a_duplicate_namespace_for_a_family_fails`,
`a_namespace_matching_no_row_fails`,
`a_namespace_not_covering_its_own_family_fails`,
`a_reference_row_outside_its_familys_namespace_fails`,
`a_row_outside_its_familys_namespace_fails`,
`a_row_whose_cell_name_belongs_to_another_family_fails`,
`a_cell_name_claimed_by_two_families_fails`,
`a_default_row_inside_a_named_namespace_fails`,
`the_default_familys_residual_namespace_is_a_derivation`.

### `check_gate_smoke.rs` — 6 tests

The manifest/target resolution: a well-formed declaration passes and prints
its panels; an unknown test or target is refused; a directory holding no
target is resolved from cargo rather than blamed; a directory holding some of
the targets fails; targets under more than one directory are an error. Tests:
`a_well_formed_declaration_passes_and_prints_its_panels`,
`an_unknown_test_is_refused`, `an_unknown_target_is_refused`,
`a_directory_holding_no_target_is_resolved_rather_than_blamed`,
`a_directory_holding_some_of_the_targets_fails`,
`targets_under_more_than_one_directory_are_an_error`.

### `check_gate_pending.rs` — 9 tests

The `rtp_mux` draft declaration in `tools/PERF_PENDING_rtp_mux.md`: its three
blocks exist, every row is unique and well formed, every relation agrees with
the dimensions its cells vary, the baselines are well formed and used, every
used tier has a budget and a measured row fits it, every gap names a cell and
a reason, every family has a proposed namespace, the proposal is rejected with
the recorded numbers, and the families spanning two cell names are recorded.
Tests: `the_three_blocks_exist`,
`every_row_is_a_unique_well_formed_declaration`,
`every_relation_agrees_with_the_dimensions_its_cells_vary`,
`the_baselines_are_well_formed_and_used`,
`every_used_tier_has_a_budget_and_a_measured_row_fits_it`,
`every_gap_names_a_cell_and_a_reason`,
`every_family_has_a_proposed_namespace`,
`the_proposal_is_rejected_with_the_recorded_numbers`,
`the_families_that_span_two_cell_names_are_recorded`.

### The fixture module

`check_gate_common/mod.rs` (included by `check_gate_env`, `check_gate_perf`
and `check_gate_smoke`) held the in-process fixture: the `Cargo`/`CargoFailure`
seam that substitutes the checker's cargo seam, the plan/manifest builders, the
JSON writer, and the env-reader fixture sources. It has no `#[test]` of its
own and moves with the targets.

## How to relocate

- Put the five `check_gate_*.rs` targets and `check_gate_common/mod.rs` under
  `rtp_mux/tests/` and rewrite the imports from
  `netem_test::tools::check_gate::…` / `netem_test::tools::json::…` to
  `rtp_mux::tools::check_gate::…` / `rtp_mux::tools::json::…`.
- Reachability: `pub mod tools` is behind `#[cfg(feature = "perf")]`
  (`rtp_mux/src/lib.rs:3`), while rtp_mux's self dev-dependency enables only
  `testing`. Add `perf` to a self dev-dependency (the pattern
  `rtp_mux = { path = ".", features = ["testing"] }` already exists) or run
  the targets under `--features perf`.
- Source paths: the doc-count fixture reads repository-root-relative files.
  Some moved to `rtp_mux` (`tools/mandate-baseline.json`,
  `netem-test/src/tools/mandate_compare.rs` is gone, the relation counts now
  come from `rtp_mux/GATE.md`); others (`tools/PERF_INFRA.md`,
  `tools/PERF_PENDING_rtp_mux.md`, `tests/GATE.md`) still live in the harness
  checkout. Each fixture path and each literal anchor must be re-pinned
  against the file that now holds it, and the suite must be shown to fail when
  each anchor is broken (the fixture already asserts its anchors' occurrence
  counts, so a wrong anchor surfaces as `ANCHOR-MISS` rather than a silent
  skip).

## A second consequence: harness-mode doc counts are red until the authorities move

Harness-mode `netem-tools check-gate` (no arguments, run from the harness root)
fails with **31 `DOC COUNT` lines**, and they are the run's only red: the perf
declaration, the env-tier block and the lane roles all pass. The sources every
documented count in `tools/PERF_INFRA.md` and `tools/MANDATE_SMOKE.md` is
derived from moved with the tooling, so the `doc_counts()` entries in
`rtp_mux/src/tools/check_gate/doc_counts.rs` still look under the harness root
and cannot determine a value. This is pre-existing at `e423c41b` (the sources
were deleted there) and is not caused by the removal of the fixture targets.

### The 31 lines, by cause

Four lines say `cannot derive: <path> does not exist` — the moved authority
itself. The other twenty-seven say `cannot be checked: nothing determined a
value for '<key>' (<authority>)` — one per prose instance of a count whose
value comes from one of the four. **Every one of the 31 is downstream of the
four `cannot derive` lines; no count has any other cause.**

| authority, harness path | new location | direct `cannot derive` | downstream `cannot be checked` |
| --- | --- | --- | --- |
| `tools/mandate-producers.json` | `rtp_mux/tools/mandate-producers.json` | 1 | 11 |
| `tools/mandate-arms.json` | `rtp_mux/mandate-arms.json` (the subdirectory changed too) | 1 | 4 |
| `tools/mandate-baseline.json` | `rtp_mux/mandate-baseline.json` (the subdirectory changed too) | 1 | 11 |
| `netem-test/src/tools/mandate_compare.rs` | `rtp_mux/src/tools/mandate_compare.rs` | 1 | 1 |

The four lines, verbatim (`cd crates/netem_test && netem-tools check-gate`):

```
DOC COUNT: cannot derive: tools/mandate-producers.json does not exist
DOC COUNT: cannot derive: tools/mandate-arms.json does not exist
DOC COUNT: cannot derive: tools/mandate-baseline.json does not exist
DOC COUNT: cannot derive: <root>/netem-test/src/tools/mandate_compare.rs does not exist, so COUNT_FLOORS_BYTES has no declared source
```

### The 15 declarations, their keys and their new owner

One row per `DocCount` entry of
`rtp_mux/src/tools/check_gate/doc_counts.rs`, with the prose that states it and
the declaration data that now owns the number (the `authority` string the
diagnostic prints today).

| `DocCount` label (keys) | prose | new owner |
| --- | --- | --- |
| `producers declared` (`producers`) | `tools/PERF_INFRA.md` "Two producers are declared and covered today"; `tools/MANDATE_SMOKE.md` "Two producers are declared today" | `rtp_mux/tools/mandate-producers.json`, the `producers[]` array |
| `the ordinal after the declared producers` (`next_producer`) | `tools/MANDATE_SMOKE.md` "A third producer is a registry entry" | the same array, one past its end |
| `evidence files per run` (`evidence_files`) | `tools/PERF_INFRA.md` "writes `mandate-check.json` alongside the eight evidence files" and "the eight evidence files"; `tools/MANDATE_SMOKE.md` "the eight evidence files", "writing all eight evidence files", "The eight expected evidence files" | `rtp_mux/tools/mandate-producers.json`, `2 x len(producers[id=rtp_mux].verdicts)` |
| `MANDATE lines per run` (`verdicts`) | `tools/MANDATE_SMOKE.md` "all four `MANDATE` lines" | the same `verdicts[]` array |
| `panels and plot files of a mandate-check run` (`baseline_panels`, `baseline_plot_files`) | `tools/PERF_INFRA.md` "12 panels, 24 SVG+PNG plot files" | `rtp_mux/mandate-baseline.json`, the summed `mandates[*].panels` (twice for the plot files) |
| `mandates and verified panels of the baseline run` (`verdicts`, `baseline_panels`) | `tools/PERF_INFRA.md` "passed all four mandates with 12 verified SVG panels" | both of the above |
| `arms recorded in the baseline run` (`arms_total`) | `tools/PERF_INFRA.md` "23 arms from both producers" | `rtp_mux/mandate-baseline.json`, `len(arms)` |
| `rtp_mux arms in the baseline run` (`arms_rtp_mux`) | `tools/PERF_INFRA.md` "23 arms from both producers" | the same `arms[]`, `producer == "rtp_mux"` |
| `per-mandate arm counts in the baseline run` (`arms_M1`..`arms_M4`) | `tools/PERF_INFRA.md` "3 M1, 3 M2, 3 M3 reps and 10 M4 arms" | the same `arms[]`, grouped by `mandate` |
| `probes recorded in the baseline run` (`baseline_probes`) | `tools/PERF_INFRA.md` "the 4 probes" | the same `arms[]`, `producer == "netem_test"` |
| `probe arms in the probe section` (`arms_probe`) | `tools/PERF_INFRA.md` "four report-only arms in the `probe` section"; `tools/MANDATE_SMOKE.md` "4 arms in the `probe` section" | `rtp_mux/mandate-arms.json`, the `probe/` keys of `cells` |
| `perf-tier probes of the harness` (`arms_probe`) | `tools/PERF_INFRA.md` "this workspace's four perf-tier probes"; `tools/MANDATE_SMOKE.md` "this workspace's four perf-tier probes" | the same `probe/` keys |
| `duration of the baseline run` (`baseline_duration`) | `tools/PERF_INFRA.md` "The run took **189.7 s**" | `rtp_mux/mandate-baseline.json`, `duration_seconds` |
| `producers a two-producer case runs` (`producers`) | `tools/MANDATE_SMOKE.md` "Its two-producer cases run both producers" | `rtp_mux/tools/mandate-producers.json`, the `producers[]` array |
| `counted floors applied to an unstated lane` (`count_floor_counters`) | `tools/MANDATE_SMOKE.md` "`COUNT_FLOORS_BYTES`, the two bulk-lane byte counters" | `rtp_mux/src/tools/mandate_compare.rs`, the `COUNT_FLOORS_BYTES` const array |

The prose here is **not** stale. Every sentence is still present, and the
ones that name a source already name the `rtp_mux` path (`tools/PERF_INFRA.md`:
the `rtp_mux/tools/mandate-producers.json` and `rtp_mux/mandate-arms.json`
references, and the `rtp_mux/src/tools/mandate_compare.rs` reference). Read
once by hand against
the moved data, every number they state still matches: two producers, eight
evidence files, 12 panels and 24 plot files, 23 arms (19 `rtp_mux` + 4 probes;
3 M1, 3 M2, 3 M3, 10 M4), 189.7 s, four `probe/` cells, two counted floors.
So nothing is deleted from the harness docs: **what is stale is the checker's
lookup, not the declaration.**

The hand check is one command (run from `crates/rtp_mux`):

```sh
python3 - <<'PY'
import json
from collections import Counter
b = json.load(open("mandate-baseline.json"))
arms = b["arms"]
producers = json.load(open("tools/mandate-producers.json"))["producers"]
print(len(producers), 2 * len([p for p in producers if p["id"] == "rtp_mux"][0]["verdicts"]))
print(len(arms), Counter(a["producer"] for a in arms), Counter(a["mandate"] for a in arms))
print(sum(int(v.get("panels") or 0) for v in b["mandates"].values()), b["duration_seconds"])
print(len([k for k in json.load(open("mandate-arms.json"))["cells"] if k.startswith("probe/")]))
PY
```

It prints `2 8`, then `23 Counter({'rtp_mux': 19, 'netem_test': 4})
Counter({'M4': 10, 'probe': 4, 'M1': 3, 'M2': 3, 'M3': 3})`, then `12
189.736`, then `4`.

### The fix belongs to `rtp_mux`, and this is exactly what it is

Point four lookups in `rtp_mux/src/tools/check_gate/doc_counts.rs` at the
mandate owner's checkout:

- `doc_count_values()` (`doc_counts.rs:659-661`) — `doc_json(root,
  "tools/mandate-producers.json", ...)`, `doc_json(root,
  "tools/mandate-arms.json", ...)` and `doc_json(root,
  "tools/mandate-baseline.json", ...)`. The first is at `tools/` under
  `rtp_mux`; the other two are at that crate's root.
- `count_floor_counters()` (`doc_counts.rs:608-615`) — the
  `root/netem-test/src/tools/mandate_compare.rs` join; the file is
  `rtp_mux/src/tools/mandate_compare.rs`.
- The 15 `authority` strings tabled above: they are printed in every
  diagnostic, so they must name the new paths once the lookups are fixed.

Harness mode's root is the harness checkout while these four live in the
mandate owner's. The checker already computes the sibling checkouts
(`Layout::crates_root`, `check_gate/mod.rs:190`), so the re-point can resolve
them there; alternatively the doc-count half can be declared to run only from a
checkout that holds them. Either way `tests/GATE.md`'s account of the
no-argument form has to say which checkout it needs.

**Vacuity for the relocated fixture.** `check_gate_doc_counts.rs` (15 tests,
removed here) drove `check_doc_counts` over a temp copy of the repository and
asserted each failure mode fires. When it is relocated it must be re-pinned
against the new authority paths and shown to fail — by the checker's own
message, or by the count it guards changing — when one of the four authorities
is broken, or the counts go unguarded while the checker reads green.

**Noticed while characterising this:** `tools/PERF_INFRA.md` never describes the
doc-count half at all (it documents the perf declaration and the env-tier
block, not the documented counts). That is why a reader of the harness docs
could not see that harness mode runs the check, or that it had gone red.

## The `gate-env-tier` rows that moved with the code

The same move left three stale rows in `tests/GATE.md`'s `gate-env-tier`
block — `browser-selection` (`NETEM_RENDER_BROWSER`, `PATH`),
`perf-history-archive` (`PERF_ARCHIVE_DIR`, `PERF_BASELINE_DIR`) and
`mandate-check-run-dir` (`TMPDIR`, `HOME`) — which the netem_test gate
correctly reports as declarations no source of this crate reads. They were
removed here and must be re-declared in `rtp_mux/GATE.md`'s `gate-env-tier`
block, whose readers are `src/tools/mandate_plot/render.rs` (`BROWSER_ENV`),
`src/bin/perf-history.rs` and `src/tools/mandate_check/producers.rs`
(`TMPDIR`, `HOME` via `expanduser`). The rows' `tools/render_graph.py` and
`tools/test_mandate_check.py` runners are files of the **harness** checkout,
not of `rtp_mux`, so in rtp_mux's manifest those rows must be scriptless
(`-`) with the runner named in prose — exactly as that manifest
already names rtp_mux's own `tools/mandate-check` (a sh shim the checker cannot
name, having no script suffix) in prose for its own seven surfaces.
