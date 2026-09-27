//! The crate's own performance tooling, as library modules.
//!
//! The tooling lives here rather than in a single binary because two binaries
//! share it: `netem-tools` is the command-line face (`mandate-compare` today)
//! and `perf-history` reuses the same comparison for the coverage/claim axes of
//! `vs-prev.md`. One implementation, so the two callers cannot disagree about
//! the semantics.

pub mod json;
pub mod mandate_compare;
