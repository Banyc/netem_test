//! The crate's own performance tooling, as library modules.
//!
//! The tooling lives here rather than in a single binary because two binaries
//! share it: `netem-tools` is the command-line face (`mandate-compare` today)
//! and `perf-history` reuses the same comparison for the coverage/claim axes of
//! `vs-prev.md`. One implementation, so two callers cannot disagree about the
//! semantics.
//!
//! `pyjson` and `pyre` are the substrate the migration to Rust needs: Python's
//! `json` plus `repr` and the `html` escapes, and the subset of `re` the ported
//! patterns are written in. They exist so a ported tool keeps its twin's
//! *messages* and *spelling*, not merely its decisions -- a refusal that words
//! the same problem differently is not the same refusal, and a summary written
//! with a different float spelling is not the same document.

pub mod json;
pub mod mandate_compare;
pub mod pyformat;
pub mod pyjson;
pub mod pyre;
