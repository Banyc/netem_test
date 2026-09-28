//! The `rtp_mux` draft declaration's conformance cases, ported from
//! `PendingDeclarationTest` / `PendingMembershipTest` in the (now deleted)
//! `tools/test_pending_declaration.py` suite.
//!
//! `tools/PERF_PENDING_rtp_mux.md` is the block set another crate applies to its
//! own `GATE.md`. It cannot be checked by `check-gate` here (its rows name tests
//! in a sibling checkout), so these cases pin the properties the checker would
//! reject on the day it is applied, using the draft's own text and the
//! checker's own parsers.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use netem_test::tools::check_gate::perf::{
    PerfBudgets, PerfRow, cell_name, cell_problem, check_perf_membership, check_perf_relations,
    parse_perf_budgets, parse_perf_design, parse_relation, prefix_hint,
};
use netem_test::tools::check_gate::{Layout, PERF_TIERS, membership_prefix_re};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("the crate sits in the repository")
        .to_path_buf()
}

fn draft_text() -> String {
    std::fs::read_to_string(repo().join("tools").join("PERF_PENDING_rtp_mux.md"))
        .expect("the draft exists")
}

/// The draft's rows as `(name, tier, cost, relation, coverage)`.
fn draft_rows(text: &str) -> Vec<(String, String, String, String, String)> {
    let design = Layout::fenced_block(text, "gate-perf-design").expect("the design block exists");
    let mut rows = Vec::new();
    for (number, raw) in design.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (name, separator, rest) = netem_test::tools::check_gate::partition(line, " = ");
        assert!(
            !separator.is_empty(),
            "line {} is not '<name> = <tier> | ...': {line:?}",
            number + 1
        );
        let fields: Vec<String> = rest
            .split('|')
            .map(|field| field.trim().to_string())
            .collect();
        assert_eq!(
            fields.len(),
            4,
            "{name}: expected '<tier> | <cost> | <relation> | <coverage>', got {fields:?}"
        );
        rows.push((
            name.trim().to_string(),
            fields[0].clone(),
            fields[1].clone(),
            fields[2].clone(),
            fields[3].clone(),
        ));
    }
    rows
}

struct Draft {
    design: String,
    budgets_text: String,
    gaps: String,
    baseline: Option<String>,
    named: BTreeMap<String, String>,
}

fn draft() -> Draft {
    let text = draft_text();
    let design = Layout::fenced_block(&text, "gate-perf-design").expect("design block");
    let budgets_text = Layout::fenced_block(&text, "gate-budgets").expect("budgets block");
    let gaps = Layout::fenced_block(&text, "gate-coverage-gaps").expect("gaps block");
    let mut baseline = None;
    let mut named: BTreeMap<String, String> = BTreeMap::new();
    for raw in budgets_text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, separator, value) = netem_test::tools::check_gate::partition(line, " = ");
        assert!(
            !separator.is_empty(),
            "budget line {line:?} is not '<key> = <value>'"
        );
        let key = key.trim().to_string();
        let value = value.trim().to_string();
        if key == "baseline" {
            baseline = Some(value);
        } else if let Some(family) = key.strip_prefix("baseline.") {
            assert!(
                !named.contains_key(family),
                "baseline {family:?} is declared twice"
            );
            assert!(
                netem_test::tools::check_gate::cell_key_re().is_match(family),
                "baseline family {family:?} is not a name"
            );
            named.insert(family.to_string(), value);
        }
    }
    Draft {
        design,
        budgets_text,
        gaps,
        baseline,
        named,
    }
}

fn parse_rows(design: &str) -> Vec<PerfRow> {
    let rows = draft_rows(&format!("```gate-perf-design\n{design}```\n"));
    let mut parsed = Vec::new();
    for (name, tier, cost, relation, coverage) in rows {
        let (relation, problem) = parse_relation(&relation);
        assert!(problem.is_none(), "{name}: {}", problem.unwrap_or_default());
        let cost = if cost == "TBD" {
            0.0
        } else {
            cost.parse::<f64>().expect("a cost")
        };
        let cells = coverage
            .split(',')
            .map(|cell| cell.trim().to_string())
            .filter(|cell| !cell.is_empty())
            .collect();
        parsed.push(PerfRow {
            name,
            tier,
            cost,
            cells,
            relation,
        });
    }
    parsed
}

#[test]
fn the_three_blocks_exist() {
    let text = draft_text();
    for name in ["gate-perf-design", "gate-budgets", "gate-coverage-gaps"] {
        assert!(
            Layout::fenced_block(&text, name).is_some(),
            "the draft has no ```{name} block"
        );
    }
}

#[test]
fn every_row_is_a_unique_well_formed_declaration() {
    let parsed = draft();
    let rows = draft_rows(&format!("```gate-perf-design\n{}```\n", parsed.design));
    let mut seen: BTreeSet<String> = BTreeSet::new();
    for (name, tier, cost, relation, coverage) in &rows {
        assert!(name.contains("::"), "{name:?} is not '<target>::<test>'");
        assert!(!name.contains(' '), "{name:?} contains whitespace");
        assert!(seen.insert(name.clone()), "{name} is declared twice");
        assert!(
            PERF_TIERS.contains(&tier.as_str()),
            "{name}: unknown tier {tier:?}"
        );
        if cost != "TBD" {
            assert!(
                cost.parse::<f64>().expect("a cost") >= 0.0,
                "{name}: cost {cost:?} is not a non-negative number"
            );
        }
        let (relation, problem) = parse_relation(relation);
        assert!(problem.is_none(), "{name}: {}", problem.unwrap_or_default());
        assert!(relation.is_some(), "{name}: relation did not parse");
        let cells: Vec<String> = coverage
            .split(',')
            .map(|cell| cell.trim().to_string())
            .filter(|cell| !cell.is_empty())
            .collect();
        assert!(!cells.is_empty(), "{name}: no coverage cell");
        for cell in &cells {
            assert!(
                cell_problem(cell).is_none(),
                "{name}: malformed cell {cell:?}"
            );
        }
    }
    assert!(!rows.is_empty(), "the draft declares no row");
}

#[test]
fn every_relation_agrees_with_the_dimensions_its_cells_vary() {
    let parsed = draft();
    let rows = parse_rows(&parsed.design);
    let mut tiers: BTreeMap<String, f64> = BTreeMap::new();
    tiers.insert("default".to_string(), 300.0);
    tiers.insert("standard".to_string(), 600.0);
    tiers.insert("full".to_string(), 7200.0);
    tiers.insert("perf".to_string(), 7200.0);
    let budgets = PerfBudgets {
        tiers,
        baseline: parsed.baseline.clone(),
        drift: 0.5,
        drift_floor_seconds: 10.0,
        named: parsed.named.clone(),
        members: BTreeMap::new(),
    };
    let mut problems: Vec<String> = Vec::new();
    let summary = check_perf_relations(&rows, &budgets, &mut problems);
    assert!(problems.is_empty(), "{}", problems.join("\n"));
    assert!(
        summary
            .iter()
            .any(|line| line.contains("gate-perf-relations:")),
        "{summary:?}"
    );
}

#[test]
fn the_baselines_are_well_formed_and_used() {
    let parsed = draft();
    let rows = draft_rows(&format!("```gate-perf-design\n{}```\n", parsed.design));
    let names: BTreeSet<String> = rows.iter().map(|row| row.0.clone()).collect();
    let baseline = parsed
        .baseline
        .clone()
        .expect("gate-budgets names no baseline row");
    assert!(
        names.contains(&baseline),
        "the default baseline {baseline:?} is not a row"
    );
    for (family, row) in &parsed.named {
        assert!(
            names.contains(row),
            "baseline.{family} names {row:?}, which is not a row"
        );
    }
    let mut families: Vec<Option<String>> = vec![None];
    families.extend(parsed.named.keys().cloned().map(Some));
    for family in families {
        let row = match &family {
            None => baseline.clone(),
            Some(name) => parsed.named[name].clone(),
        };
        let used = rows
            .iter()
            .any(|(name, _tier, _cost, relation, _coverage)| {
                if name == &row {
                    return false;
                }
                match parse_relation(relation).0 {
                    Some(relation) => relation.family == family,
                    None => false,
                }
            });
        assert!(
            used,
            "no row is stated against the baseline {}",
            family.clone().unwrap_or_else(|| "default".to_string())
        );
    }
}

#[test]
fn every_used_tier_has_a_budget_and_a_measured_row_fits_it() {
    let parsed = draft();
    let rows = draft_rows(&format!("```gate-perf-design\n{}```\n", parsed.design));
    let mut budgets: BTreeMap<String, f64> = BTreeMap::new();
    for raw in parsed.budgets_text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, separator, value) = netem_test::tools::check_gate::partition(line, " = ");
        if separator.is_empty() || key.starts_with("baseline") || key.trim() == "baseline" {
            continue;
        }
        let key = key.trim().to_string();
        if PERF_TIERS.contains(&key.as_str()) {
            budgets.insert(key, value.trim().parse::<f64>().expect("a budget"));
        }
    }
    let names: BTreeSet<String> = rows.iter().map(|row| row.0.clone()).collect();
    let baseline = parsed.baseline.clone().expect("a baseline row");
    assert!(
        names.contains(&baseline),
        "baseline {baseline:?} is not a declared row"
    );
    let mut sums: BTreeMap<String, f64> = BTreeMap::new();
    for (_name, tier, cost, _relation, _coverage) in &rows {
        assert!(
            budgets.contains_key(tier),
            "no budget declared for the {tier} tier"
        );
        if cost != "TBD" {
            *sums.entry(tier.clone()).or_insert(0.0) += cost.parse::<f64>().expect("a cost");
        }
    }
    for (tier, total) in sums {
        assert!(
            total <= budgets[&tier],
            "the {tier} rows sum to {total} over {}",
            budgets[&tier]
        );
    }
}

#[test]
fn every_gap_names_a_cell_and_a_reason() {
    let parsed = draft();
    let gaps: Vec<String> = parsed
        .gaps
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(str::to_string)
        .collect();
    assert!(!gaps.is_empty(), "the draft records no coverage gap");
    for line in gaps {
        let (cell, separator, reason) = netem_test::tools::check_gate::partition(&line, " = ");
        assert!(
            !separator.is_empty(),
            "gap line {line:?} is not '<cell> = <reason>'"
        );
        assert!(
            cell_problem(cell.trim()).is_none(),
            "gap cell {cell:?} is malformed"
        );
        assert!(!reason.trim().is_empty(), "gap {cell:?} records no reason");
    }
}

// -- the membership proposal -------------------------------------------------

struct Membership {
    rows: Vec<PerfRow>,
    members: BTreeMap<String, String>,
    budgets: PerfBudgets,
}

fn membership() -> Membership {
    let text = draft_text();
    let design = Layout::fenced_block(&text, "gate-perf-design").expect("design block");
    let design = design.replace("| TBD |", "| 0 |");
    let rows = parse_perf_design(&design, &mut Vec::new());
    let mut members: BTreeMap<String, String> = BTreeMap::new();
    let proposed = Layout::fenced_block(&text, "gate-members-proposed").expect("members block");
    for raw in proposed.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, separator, value) = netem_test::tools::check_gate::partition(line, " = ");
        assert!(
            !separator.is_empty(),
            "membership line {line:?} is not '<key> = <prefix>'"
        );
        assert!(key.starts_with("members."), "{key}");
        assert!(
            membership_prefix_re().is_match(value.trim()),
            "{key} = {value:?} is not a cell-name namespace"
        );
        members.insert(
            key["members.".len()..].to_string(),
            value.trim().to_string(),
        );
    }
    let budgets_text = Layout::fenced_block(&text, "gate-budgets").expect("budgets block");
    let mut problems: Vec<String> = Vec::new();
    let budgets = parse_perf_budgets(&budgets_text, &mut problems);
    assert!(problems.is_empty(), "{}", problems.join("\n"));
    assert!(
        !budgets_text.contains("members"),
        "the pasteable gate-budgets block must not carry the rejected membership lines"
    );
    Membership {
        rows,
        members,
        budgets,
    }
}

#[test]
fn every_family_has_a_proposed_namespace() {
    let parsed = membership();
    let proposed: Vec<String> = parsed.members.keys().cloned().collect();
    let declared: Vec<String> = parsed.budgets.named.keys().cloned().collect();
    assert_eq!(proposed, declared);
}

#[test]
fn the_proposal_is_rejected_with_the_recorded_numbers() {
    let parsed = membership();
    let budgets = PerfBudgets {
        tiers: parsed.budgets.tiers.clone(),
        baseline: parsed.budgets.baseline.clone(),
        drift: parsed.budgets.drift,
        drift_floor_seconds: parsed.budgets.drift_floor_seconds,
        named: parsed.budgets.named.clone(),
        members: parsed.members.clone(),
    };
    let mut problems: Vec<String> = Vec::new();
    let summary = check_perf_membership(&parsed.rows, &budgets, &mut problems);
    assert_eq!(parsed.rows.len(), 94);
    assert_eq!(problems.len(), 103, "{}", problems.join("\n"));
    let collision = problems
        .iter()
        .filter(|problem| problem.contains("a cell name belongs to exactly one family"))
        .count();
    assert_eq!(collision, 14);
    assert!(
        summary.iter().any(|line| line
            == "  gate-perf-membership: 28 cell-name namespace(s) declared, 22 cell name(s) \
                claimed, 81 row(s) stated outside the namespace of the family they name"),
        "{summary:?}"
    );
    let outside = problems
        .iter()
        .filter(|problem| problem.contains("the row's cells and the family it names disagree"))
        .count();
    assert_eq!(outside, 13);
    let joined = problems.join("\n");
    assert!(
        joined.contains(
            "gate-perf-design row rtp_mux_jitter::jitter_nonloss_impairments: its cells are \
             named non-loss-impairment, which members.lone-tail = M1* does not claim"
        ),
        "{joined}"
    );
    let ambiguous = problems
        .iter()
        .filter(|problem| {
            problem.contains("which")
                && problem.contains("families claim")
                && !problem.contains("disagree")
        })
        .count();
    assert_eq!(ambiguous, 68);
    assert_eq!(outside + ambiguous, 81);
    assert!(
        joined.contains(
            "gate-perf-design row rtp_mux_jitter::jitter_interactive_bulk_and_loss: its cells \
             are named M2, which 2 families claim"
        ),
        "{joined}"
    );
}

#[test]
fn the_families_that_span_two_cell_names_are_recorded() {
    let parsed = membership();
    let mut spanning: Vec<String> = Vec::new();
    for family in parsed.members.keys() {
        let names: Vec<String> = parsed
            .rows
            .iter()
            .filter(|row| {
                row.relation
                    .as_ref()
                    .and_then(|relation| relation.family.as_ref())
                    == Some(family)
            })
            .flat_map(|row| row.cells.iter().map(|cell| cell_name(cell)))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        if prefix_hint(&names).is_empty() {
            spanning.push(family.clone());
        }
    }
    assert_eq!(
        spanning,
        vec![
            "decomposition",
            "dual-lane",
            "fairness",
            "fec",
            "hol-dual-lane-frame",
            "hostile-probes",
            "lone-tail",
            "mux-over-rtp",
        ]
    );
}
