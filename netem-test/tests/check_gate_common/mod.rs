//! The fixture harness for the `check-gate` port's tests.
//!
//! The Python suite drove the checker as a subprocess with a stand-in `cargo`
//! on `PATH` that printed a JSON plan's test lists. The port keeps the fixture
//! and the plan, but substitutes the cargo *seam* instead of a program: the
//! checker's `Cargo` trait is the same one `SystemCargo` implements, so every
//! decision above the seam — the manifest blocks, the call graph, the env
//! scanner, the doc counts — is exercised in-process, and the real `cargo`
//! path is proven by the differential against a fresh Python run over every
//! crate's real invocation.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub use netem_test::tools::check_gate::Outcome;
use netem_test::tools::check_gate::{self, Args, Cargo, CargoFailure, Session};
use netem_test::tools::json::Json;

// -- the fixture sources -----------------------------------------------------

pub const ALPHA_RS: &str = r#"#[test]
fn t_ok() {
    assert!(true);
}

#[test]
#[ignore = "report-only"]
fn t_ig() {
    println!("report only");
}
"#;

pub const BETA_RS: &str = r#"#[test]
fn t_beta() {
    assert!(true);
}
"#;

pub const LIB_RS: &str = r#"pub fn helper() -> u8 {
    1
}

mod tests {
    #[test]
    fn t_lib_default() {
        assert_eq!(super::helper(), 1);
    }

    #[test]
    #[ignore = "release perf probe"]
    fn probe() {
        println!("report only");
    }
}
"#;

pub const BASE_DESIGN: &str =
    "beta::t_beta = default | 0.4 | baseline | conformance-beta@impairment=none
alpha::t_ok = default | 0.1 | orthogonal | conformance-alpha@impairment=delay20ms
alpha::t_ig = perf | 0.2 | composite(metric,scale) | probe-alpha@metric=throughput+scale=200-pkt
lib::tests::probe = perf | 0.3 | re-measurement(second-tier-repeat) | probe-runner@impairment=none";

pub const BASE_BUDGETS: &str = "default = 30
perf = 30
baseline = beta::t_beta
drift = 0.5
drift_floor_s = 2.0";

pub const BASE_GAPS: &str = "M1@lane=dual-lane = owned by rtp_mux, whose declaration is pending";

/// The env fixture's own `GATE.md`: no perf blocks at all.
pub const ENV_GATE: &str = "# the fixture gate\n\n```gate-manifest\nalpha::t_ig = perf\n```\n\n\
```gate-default-required\nalpha::t_ok\n```\n\n\
```gate-asserting\nalpha::t_ok\n```\n\n\
```gate-perf-guard-helpers\n```\n";

/// A crate-local env reader reached through a wrapper.
pub const ENV_KNOB_RS: &str = r#"fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

pub fn iterations() -> u64 {
    env_parse("FIXTURE_ITERATIONS", 10)
}

pub fn rounds() -> u64 {
    env_parse("FIXTURE_ROUNDS", 4)
}
"#;

/// A knob the sources read and no script sets.
pub const WAKE_KNOB_RS: &str = r#"fn env_parse(name: &str, default: u64) -> u64 {
    match std::env::var(name) {
        Ok(raw) => raw.parse().unwrap_or(default),
        Err(_) => default,
    }
}

pub fn wake_bound_ms() -> u64 {
    env_parse("FIXTURE_WAKE_BOUND", 50)
}
"#;

/// A runner that names the variables it sets for the child process.
pub const ENV_RUNNER_PY: &str = r#"#!/usr/bin/env python3
"""Runs the fixture's batches with the sizing the sweep needs."""

import os
import subprocess
import sys


def main():
    env = dict(os.environ)
    env["FIXTURE_ITERATIONS"] = "100"
    env["FIXTURE_ROUNDS"] = "8"
    return subprocess.run([sys.executable, "-c", "pass"], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
"#;

pub fn append_block(path: &Path, name: &str, body: &str) {
    let mut text = std::fs::read_to_string(path).expect("the file exists");
    text.push_str(&format!("\n```{name}\n{body}```\n"));
    std::fs::write(path, text).expect("the file is writable");
}

// -- the stand-in cargo ------------------------------------------------------

/// One package's explicit test targets: `(target name, source path)`.
pub type PackageTargets = (String, Vec<(String, String)>);

/// A JSON plan of the test names each `(package, target, ignored)` invocation
/// must report, and (optionally) an explicit target list.
#[derive(Debug, Clone)]
pub struct Plan {
    /// `"package|target"` -> (default names, ignored names).
    pub lists: BTreeMap<String, (Vec<String>, Vec<String>)>,
    /// Explicit metadata: package -> (target, src_path). `None` derives the
    /// target list from `lists`.
    pub metadata: Option<Vec<PackageTargets>>,
}

impl Default for Plan {
    fn default() -> Self {
        Plan::base()
    }
}

impl Plan {
    pub fn base() -> Plan {
        let mut lists: BTreeMap<String, (Vec<String>, Vec<String>)> = BTreeMap::new();
        lists.insert(
            "tests|alpha".to_string(),
            (vec!["t_ok".to_string()], vec!["t_ig".to_string()]),
        );
        lists.insert(
            "tests|beta".to_string(),
            (vec!["t_beta".to_string()], Vec::new()),
        );
        lists.insert(
            "tests|lib".to_string(),
            (
                vec!["tests::t_lib_default".to_string()],
                vec!["tests::probe".to_string()],
            ),
        );
        Plan {
            lists,
            metadata: None,
        }
    }

    pub fn set(
        &mut self,
        package: &str,
        target: &str,
        default: &[&str],
        ignored: &[&str],
    ) -> &mut Plan {
        self.lists.insert(
            format!("{package}|{target}"),
            (
                default.iter().map(|name| name.to_string()).collect(),
                ignored.iter().map(|name| name.to_string()).collect(),
            ),
        );
        self
    }

    pub fn remove(&mut self, package: &str, target: &str) -> &mut Plan {
        self.lists.remove(&format!("{package}|{target}"));
        self
    }

    fn metadata_json(&self, root: &Path) -> String {
        let packages: Vec<(String, Vec<(String, String)>)> = match &self.metadata {
            Some(explicit) => explicit.clone(),
            None => {
                let mut by_package: BTreeMap<String, Vec<String>> = BTreeMap::new();
                for key in self.lists.keys() {
                    let (package, target) =
                        key.split_once('|').expect("the plan key is pkg|target");
                    if target == "lib" {
                        continue;
                    }
                    by_package
                        .entry(package.to_string())
                        .or_default()
                        .push(target.to_string());
                }
                by_package
                    .into_iter()
                    .map(|(package, targets)| {
                        let targets = targets
                            .into_iter()
                            .map(|target| {
                                (
                                    target.clone(),
                                    format!("{}/tests/tests/{target}.rs", root.display()),
                                )
                            })
                            .collect();
                        (package, targets)
                    })
                    .collect()
            }
        };
        let mut package_values: Vec<Json> = Vec::new();
        for (package, targets) in packages {
            let targets: Vec<Json> = targets
                .into_iter()
                .map(|(name, src_path)| {
                    let src_path = src_path.replace("{root}", &root.display().to_string());
                    let mut target = BTreeMap::new();
                    target.insert("name".to_string(), Json::Str(name));
                    target.insert(
                        "kind".to_string(),
                        Json::Array(vec![Json::Str("test".to_string())]),
                    );
                    target.insert("src_path".to_string(), Json::Str(src_path));
                    Json::Object(target)
                })
                .collect();
            let mut entry = BTreeMap::new();
            entry.insert("name".to_string(), Json::Str(package));
            entry.insert("targets".to_string(), Json::Array(targets));
            package_values.push(Json::Object(entry));
        }
        let mut root_object = BTreeMap::new();
        root_object.insert("packages".to_string(), Json::Array(package_values));
        netem_test::tools::json::to_string(&Json::Object(root_object))
    }
}

/// The stand-in cargo: the plan, and no process.
pub struct FakeCargo {
    pub plan: Plan,
}

impl Cargo for FakeCargo {
    fn metadata(&self, root: &Path) -> Result<String, CargoFailure> {
        Ok(self.plan.metadata_json(root))
    }

    fn list(
        &self,
        _root: &Path,
        package: &str,
        target: Option<&str>,
        ignored: bool,
    ) -> Result<String, CargoFailure> {
        let key = format!("{package}|{}", target.unwrap_or("lib"));
        let mut out = String::new();
        if let Some((default, ignored_names)) = self.plan.lists.get(&key) {
            let names = if ignored { ignored_names } else { default };
            for name in names {
                out.push_str(name);
                out.push_str(": test\n");
            }
        }
        Ok(out)
    }
}

// -- the perf fixture --------------------------------------------------------

/// The `write_gate` knobs, with the Python fixture's defaults.
#[derive(Debug, Clone)]
pub struct GateOptions {
    pub design: Option<String>,
    pub budgets: String,
    pub gaps: String,
    pub manifest: String,
    pub required: String,
    pub asserting: String,
    pub lib_package: Option<String>,
}

impl Default for GateOptions {
    fn default() -> Self {
        GateOptions {
            design: Some(BASE_DESIGN.to_string()),
            budgets: BASE_BUDGETS.to_string(),
            gaps: BASE_GAPS.to_string(),
            manifest: "alpha::t_ig = perf".to_string(),
            required: "alpha::t_ok".to_string(),
            asserting: "alpha::t_ok".to_string(),
            lib_package: None,
        }
    }
}

/// A `mandate-check.json` timing entry.
#[derive(Debug, Clone)]
pub struct Timing {
    pub target: String,
    pub name: String,
    pub duration_seconds: f64,
}

impl Timing {
    pub fn new(target: &str, name: &str, duration_seconds: f64) -> Timing {
        Timing {
            target: target.to_string(),
            name: name.to_string(),
            duration_seconds,
        }
    }
}

/// A fixture crate root, a stand-in cargo and a `GATE.md`.
pub struct Fixture {
    tmp: PathBuf,
    pub root: PathBuf,
    pub plan: Plan,
    pub cargo: FakeCargo,
    pub report_path: PathBuf,
}

impl Fixture {
    pub fn new() -> Fixture {
        let tmp = unique_root("perf");
        let root = tmp.join("fixture");
        std::fs::create_dir_all(root.join("tests").join("tests")).expect("mkdir");
        std::fs::create_dir_all(root.join("src")).expect("mkdir");
        std::fs::write(root.join("tests").join("tests").join("alpha.rs"), ALPHA_RS).expect("write");
        std::fs::write(root.join("tests").join("tests").join("beta.rs"), BETA_RS).expect("write");
        std::fs::write(root.join("src").join("lib.rs"), LIB_RS).expect("write");
        let plan = Plan::base();
        let report_path = tmp.join("mandate-check.json");
        let mut fixture = Fixture {
            tmp,
            root,
            plan,
            cargo: FakeCargo { plan: Plan::base() },
            report_path,
        };
        fixture.write_plan(Plan::base());
        fixture.write_gate(GateOptions::default());
        fixture
    }

    pub fn tmp(&self) -> &Path {
        &self.tmp
    }

    pub fn write_plan(&mut self, plan: Plan) {
        self.plan = plan.clone();
        self.cargo = FakeCargo { plan };
    }

    pub fn write_gate(&self, options: GateOptions) {
        let lib_package = match &options.lib_package {
            Some(package) => format!("```gate-lib-package\n{package}\n```\n\n"),
            None => String::new(),
        };
        let design = options.design.clone().unwrap_or_default();
        let mut text = format!(
            "# the fixture gate\n\n{lib_package}```gate-manifest\n{manifest}\n```\n\n\
             ```gate-default-required\n{required}\n```\n\n\
             ```gate-asserting\n{asserting}\n```\n\n\
             ```gate-perf-guard-helpers\n```\n\n\
             ```gate-perf-design\n{design}\n```\n\n\
             ```gate-budgets\n{budgets}\n```\n\n\
             ```gate-coverage-gaps\n{gaps}\n```\n",
            manifest = options.manifest,
            required = options.required,
            asserting = options.asserting,
            budgets = options.budgets,
            gaps = options.gaps,
        );
        if options.design.is_none() {
            text = text.replace("```gate-perf-design\n\n```\n\n", "");
        }
        std::fs::write(self.root.join("tests").join("GATE.md"), text).expect("write");
    }

    pub fn write_report(&self, timings: &[Timing], schema: &str) {
        let entries: Vec<Json> = timings
            .iter()
            .map(|timing| {
                let mut entry = BTreeMap::new();
                entry.insert("target".to_string(), Json::Str(timing.target.clone()));
                entry.insert("name".to_string(), Json::Str(timing.name.clone()));
                entry.insert(
                    "duration_seconds".to_string(),
                    Json::Float(timing.duration_seconds),
                );
                entry.insert("state".to_string(), Json::Str("ok".to_string()));
                Json::Object(entry)
            })
            .collect();
        let mut timings_object = BTreeMap::new();
        timings_object.insert("tests".to_string(), Json::Array(entries));
        let mut payload = BTreeMap::new();
        payload.insert("schema".to_string(), Json::Str(schema.to_string()));
        payload.insert("timings".to_string(), Json::Object(timings_object));
        std::fs::write(
            &self.report_path,
            netem_test::tools::json::to_string(&Json::Object(payload)),
        )
        .expect("write");
    }

    pub fn write_report_raw(&self, text: &str) {
        std::fs::write(&self.report_path, text).expect("write");
    }

    pub fn check(&self) -> Outcome {
        self.check_with(None)
    }

    pub fn check_with(&self, report: Option<PathBuf>) -> Outcome {
        self.check_dir("tests/tests", report)
    }

    pub fn check_dir(&self, dir: &str, report: Option<PathBuf>) -> Outcome {
        let args = Args {
            crate_spec: Some((
                self.root.clone(),
                "tests".to_string(),
                PathBuf::from(dir),
                PathBuf::from("tests/GATE.md"),
            )),
            mandate_check_json: report.clone(),
        };
        match check_gate::build_layout(&args) {
            Err(message) => Outcome {
                stdout: String::new(),
                stderr: format!("{message}\n"),
                exit: 1,
            },
            Ok(layout) => {
                let session = Session::new(layout, &self.cargo);
                check_gate::run(session, report)
            }
        }
    }

    pub fn rejects(&self, fragment: &str) -> Outcome {
        self.rejects_with(fragment, None)
    }

    pub fn rejects_with(&self, fragment: &str, report: Option<PathBuf>) -> Outcome {
        let outcome = self.check_with(report);
        assert_ne!(
            outcome.exit, 0,
            "expected a non-zero exit; output={}{}",
            outcome.stdout, outcome.stderr
        );
        let output = format!("{}{}", outcome.stdout, outcome.stderr);
        assert!(
            output.contains(fragment),
            "expected {fragment:?} in output:\n{output}"
        );
        outcome
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.tmp);
    }
}

// -- the env fixture ---------------------------------------------------------

/// A crate root for an env-scaled-surface case: sources and a runner, and no
/// compiled-test lists to speak of.
pub struct EnvFixture {
    tmp: PathBuf,
    pub root: PathBuf,
    pub plan: Plan,
    pub cargo: FakeCargo,
}

impl EnvFixture {
    pub fn new() -> EnvFixture {
        let tmp = unique_root("env");
        let root = tmp.join("fixture");
        std::fs::create_dir_all(root.join("tests").join("tests")).expect("mkdir");
        std::fs::create_dir_all(root.join("src")).expect("mkdir");
        std::fs::create_dir_all(root.join("local")).expect("mkdir");
        std::fs::write(root.join("tests").join("tests").join("alpha.rs"), ALPHA_RS).expect("write");
        std::fs::write(root.join("src").join("env_knob.rs"), ENV_KNOB_RS).expect("write");
        std::fs::write(root.join("local").join("run_env.py"), ENV_RUNNER_PY).expect("write");
        std::fs::write(root.join("tests").join("GATE.md"), ENV_GATE).expect("write");
        let mut plan = Plan::base();
        plan.lists.clear();
        plan.set("tests", "alpha", &["t_ok"], &["t_ig"]);
        EnvFixture {
            tmp,
            root,
            cargo: FakeCargo { plan: plan.clone() },
            plan,
        }
    }

    pub fn write_source(&self, relpath: &str, text: &str) {
        let path = self.root.join(relpath);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("mkdir");
        }
        std::fs::write(path, text).expect("write");
    }

    pub fn declare(&self, body: &str) {
        append_block(
            &self.root.join("tests").join("GATE.md"),
            "gate-env-tier",
            body,
        );
    }

    pub fn replace_plan(&mut self, plan: Plan) {
        self.plan = plan.clone();
        self.cargo = FakeCargo { plan };
    }

    pub fn check_split(&self) -> Outcome {
        let args = Args {
            crate_spec: Some((
                self.root.clone(),
                "tests".to_string(),
                PathBuf::from("tests/tests"),
                PathBuf::from("tests/GATE.md"),
            )),
            mandate_check_json: None,
        };
        match check_gate::build_layout(&args) {
            Err(message) => Outcome {
                stdout: String::new(),
                stderr: format!("{message}\n"),
                exit: 1,
            },
            Ok(layout) => {
                let session = Session::new(layout, &self.cargo);
                check_gate::run(session, None)
            }
        }
    }

    pub fn check(&self) -> Outcome {
        self.check_split()
    }

    pub fn rejects(&self, fragment: &str) -> Outcome {
        let outcome = self.check();
        assert_ne!(
            outcome.exit, 0,
            "expected a non-zero exit; output={}{}",
            outcome.stdout, outcome.stderr
        );
        let output = format!("{}{}", outcome.stdout, outcome.stderr);
        assert!(
            output.contains(fragment),
            "expected {fragment:?} in output:\n{output}"
        );
        outcome
    }
}

impl Drop for EnvFixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.tmp);
    }
}

fn unique_root(tag: &str) -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let count = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    std::env::temp_dir().join(format!(
        "netem-check-gate-{tag}-{}-{nanos}-{count}",
        std::process::id()
    ))
}
