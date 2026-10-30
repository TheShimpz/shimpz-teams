//! A Sign-in's capsule as Team receives it (ADR-0117, Sign-ins amendment items 5 and 6).
//!
//! `browser-host profile export` writes one ustar archive of Chromium's profile; Team checks it again as it arrives, by
//! the same rules the Browser imports it under: regular files and directories only, admissible paths, each entry
//! once, within the entry, file, and total bounds, and a well-formed end. It also keeps the profile's cookie store,
//! the only file it reads, to refuse a profile that holds a session at a known identity provider's sign-in host. The
//! store is untrusted: it is opened in memory, read-only, defensively, without a trusted schema, with cell size
//! checks, within a bound of work, and only as Chromium's own table; a capsule with no store, another store, or one
//! that cannot be read so is held to name every such host.

use std::collections::{BTreeSet, HashSet};

use rusqlite::config::DbConfig;
use rusqlite::{Connection, MAIN_DB};
use strict_json::Value;
use team_protocol::sign_in::{MAX_SITES, canonical_site};

const BLOCK: usize = 512;
/// The most entries a capsule may hold.
pub const MAX_ENTRIES: usize = 4096;
/// The most file bytes a capsule may hold.
pub const MAX_TOTAL_BYTES: u64 = 128 * 1024 * 1024;
/// The largest file a capsule may hold.
pub const MAX_FILE_BYTES: u64 = 32 * 1024 * 1024;
const MAX_DEPTH: usize = 16;
const MAX_PATH_BYTES: usize = 255;
/// The one cookie store Team reads.
pub const COOKIES: &str = "Default/Cookies";
/// The most SQLite steps reading the cookie store may take, far beyond what a real store of 32 MiB needs.
pub const MAX_SQL_STEPS: u64 = 50_000_000;
const PROGRESS_EVERY: i32 = 10_000;
// Second-level labels under a country code that, with it, form a public suffix (com.br, co.uk, ...): presentation only.
const SECOND_LEVEL: [&str; 10] = [
    "ac", "co", "com", "edu", "go", "gov", "ne", "net", "or", "org",
];
/// The sign-in hosts of the identity providers a profile must not carry a session for (partial coverage, stated), each
/// with its own top-level site: a cookie partitioned under that site reaches the sign-in host's own pages too.
pub const IDENTITY_SITES: [(&str, &str); 5] = [
    ("accounts.google.com", "https://google.com"),
    ("login.microsoftonline.com", "https://microsoftonline.com"),
    ("login.live.com", "https://live.com"),
    ("appleid.apple.com", "https://apple.com"),
    ("idmsa.apple.com", "https://apple.com"),
];

/// The capsule is not one Team keeps.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CapsuleError;

impl std::fmt::Display for CapsuleError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the capsule is not one Team keeps")
    }
}

impl std::error::Error for CapsuleError {}

/// The bounds a check holds a capsule to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Entries.
    pub entries: usize,
    /// File bytes in all.
    pub total: u64,
    /// SQLite steps reading the cookie store.
    pub sql_steps: u64,
    /// How many steps pass between two checks of that bound.
    pub sql_every: i32,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            entries: MAX_ENTRIES,
            total: MAX_TOTAL_BYTES,
            sql_steps: MAX_SQL_STEPS,
            sql_every: PROGRESS_EVERY,
        }
    }
}

fn admissible(path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').collect();
    !path.is_empty()
        && path.len() <= MAX_PATH_BYTES
        && parts.len() <= MAX_DEPTH
        && parts
            .iter()
            .all(|part| !part.is_empty() && *part != "." && *part != "..")
        && !path
            .chars()
            .any(|character| character < ' ' || character == '\u{7f}')
}

fn text(block: &[u8], start: usize, end: usize) -> Result<&str, CapsuleError> {
    let field = &block[start..end];
    let field = field
        .iter()
        .position(|byte| *byte == 0)
        .map_or(field, |nul| &field[..nul]);
    std::str::from_utf8(field).map_err(|_| CapsuleError)
}

fn octal(block: &[u8], start: usize, end: usize) -> Result<u64, CapsuleError> {
    let digits = text(block, start, end)?.trim_matches(' ');
    if digits.is_empty() || !digits.bytes().all(|byte| (b'0'..=b'7').contains(&byte)) {
        return Err(CapsuleError);
    }
    u64::from_str_radix(digits, 8).map_err(|_| CapsuleError)
}

/// One header's path, whether it is a directory, and its size; anything but a file or a directory is refused.
fn header(block: &[u8]) -> Result<(String, bool, u64), CapsuleError> {
    if &block[257..263] != b"ustar\0" || &block[263..265] != b"00" {
        return Err(CapsuleError);
    }
    let sum: u64 = block[..148]
        .iter()
        .chain([b' '; 8].iter())
        .chain(block[156..].iter())
        .map(|byte| u64::from(*byte))
        .sum();
    if octal(block, 148, 156)? != sum {
        return Err(CapsuleError);
    }
    let (prefix, name) = (text(block, 345, 500)?, text(block, 0, 100)?);
    let joined = if prefix.is_empty() {
        name.to_owned()
    } else {
        format!("{prefix}/{name}")
    };
    let path = joined.trim_end_matches('/').to_owned();
    if !admissible(&path) || !text(block, 157, 257)?.is_empty() {
        return Err(CapsuleError);
    }
    let size = octal(block, 124, 136)?;
    match block[156] {
        b'5' if size == 0 => Ok((path, true, 0)),
        b'0' | 0 if size <= MAX_FILE_BYTES => Ok((path, false, size)),
        _ => Err(CapsuleError),
    }
}

/// Checks a capsule chunk by chunk, keeping only its cookie store; [`Check::finish`] proves it ended well.
#[derive(Debug)]
pub struct Check {
    limits: Limits,
    pending: Vec<u8>,
    seen: HashSet<String>,
    directories: HashSet<String>,
    total: u64,
    // The current file's remaining bytes and padding, and whether it is the cookie store.
    remaining: u64,
    padding: usize,
    keeping: bool,
    ended: u8,
    done: bool,
    cookies: Vec<u8>,
}

impl Default for Check {
    fn default() -> Self {
        Self::new(Limits::default())
    }
}

impl Check {
    /// A check holding a capsule to `limits`.
    #[must_use]
    pub fn new(limits: Limits) -> Self {
        Self {
            limits,
            pending: Vec::new(),
            seen: HashSet::new(),
            directories: HashSet::from([String::new()]),
            total: 0,
            remaining: 0,
            padding: 0,
            keeping: false,
            ended: 0,
            done: false,
            cookies: Vec::new(),
        }
    }

    /// The cookie store's bytes kept so far.
    #[must_use]
    pub fn cookies(&self) -> &[u8] {
        &self.cookies
    }

    /// Checks the next chunk.
    ///
    /// # Errors
    /// [`CapsuleError`] as soon as the capsule breaks a rule.
    pub fn feed(&mut self, chunk: &[u8]) -> Result<(), CapsuleError> {
        if self.done && !chunk.is_empty() {
            return Err(CapsuleError);
        }
        self.pending.extend_from_slice(chunk);
        while !self.done {
            if self.remaining > 0 {
                let take = usize::try_from(self.remaining)
                    .unwrap_or(usize::MAX)
                    .min(self.pending.len());
                if take == 0 {
                    return Ok(());
                }
                if self.keeping {
                    self.cookies.extend_from_slice(&self.pending[..take]);
                }
                self.pending.drain(..take);
                self.remaining -= u64::try_from(take).unwrap_or(u64::MAX);
                continue;
            }
            if self.padding > 0 {
                let take = self.padding.min(self.pending.len());
                if take == 0 {
                    return Ok(());
                }
                if self.pending[..take].iter().any(|byte| *byte != 0) {
                    return Err(CapsuleError);
                }
                self.pending.drain(..take);
                self.padding -= take;
                continue;
            }
            if self.pending.len() < BLOCK {
                return Ok(());
            }
            let block: Vec<u8> = self.pending.drain(..BLOCK).collect();
            self.block(&block)?;
        }
        Ok(())
    }

    fn block(&mut self, block: &[u8]) -> Result<(), CapsuleError> {
        if block.iter().all(|byte| *byte == 0) {
            self.ended += 1;
            self.done = self.ended == 2;
            return Ok(());
        }
        if self.ended > 0 {
            return Err(CapsuleError);
        }
        let (path, directory, size) = header(block)?;
        let (parent, leaf) = path.rsplit_once('/').unwrap_or(("", path.as_str()));
        if self.seen.contains(&path)
            || !self.directories.contains(parent)
            || (leaf == "Cookies" && path != COOKIES)
        {
            return Err(CapsuleError);
        }
        self.seen.insert(path.clone());
        if self.seen.len() > self.limits.entries {
            return Err(CapsuleError);
        }
        if directory {
            self.directories.insert(path);
            return Ok(());
        }
        self.total += size;
        if self.total > self.limits.total {
            return Err(CapsuleError);
        }
        self.remaining = size;
        self.padding = (BLOCK - usize::try_from(size % 512).unwrap_or_default()) % BLOCK;
        self.keeping = path == COOKIES;
        Ok(())
    }

    /// Proves the capsule ended with its two end blocks and nothing after them.
    ///
    /// # Errors
    /// [`CapsuleError`] when it ended early or has bytes after its end.
    pub fn finish(&self) -> Result<(), CapsuleError> {
        if self.done && self.pending.is_empty() {
            Ok(())
        } else {
            Err(CapsuleError)
        }
    }

    /// The profile's cookies as host and partition, or `None` when there is no store or it cannot be read so.
    fn read(&self) -> Option<BTreeSet<(String, String)>> {
        if self.cookies.is_empty() {
            return None;
        }
        cookies(&self.cookies, self.limits).ok()
    }

    /// The identity providers' sign-in hosts some cookie of the profile is sent to; all of them when unreadable.
    #[must_use]
    pub fn identity_hosts(&self) -> BTreeSet<&'static str> {
        let Some(cookies) = self.read() else {
            return IDENTITY_SITES.iter().map(|(host, _)| *host).collect();
        };
        IDENTITY_SITES
            .iter()
            .filter(|(host, site)| cookies.iter().any(|cookie| sent_to(cookie, host, site)))
            .map(|(host, _)| *host)
            .collect()
    }

    /// The sites found in the profile's first-party cookies, at most [`MAX_SITES`] in order, and whether more exist.
    /// An incomplete inventory for the person to read (ADR-0117, 2026-10-20 item 4), never a scope or every identity.
    #[must_use]
    pub fn sites(&self) -> (Vec<String>, bool) {
        let found: BTreeSet<String> = self
            .read()
            .unwrap_or_default()
            .iter()
            .filter(|(_, partition)| partition.is_empty())
            .filter_map(|(host, _)| registrable(host))
            .collect();
        let truncated = found.len() > MAX_SITES;
        (found.into_iter().take(MAX_SITES).collect(), truncated)
    }
}

/// Every cookie's host and partition, read only as Chromium's own `cookies` table, within `steps`.
fn cookies(store: &[u8], limits: Limits) -> Result<BTreeSet<(String, String)>, rusqlite::Error> {
    let mut connection = Connection::open_in_memory()?;
    connection.set_db_config(DbConfig::SQLITE_DBCONFIG_DEFENSIVE, true)?;
    connection.set_db_config(DbConfig::SQLITE_DBCONFIG_TRUSTED_SCHEMA, false)?;
    let mut taken: u64 = 0;
    let every = u64::try_from(limits.sql_every).unwrap_or(1);
    connection.progress_handler(
        limits.sql_every,
        Some(move || {
            taken += every;
            taken > limits.sql_steps
        }),
    )?;
    connection.deserialize_read_exact(MAIN_DB, store, store.len(), true)?;
    connection.pragma_update(None, "cell_size_check", true)?;
    connection.pragma_update(None, "query_only", true)?;
    let kind: Vec<String> = connection
        .prepare("SELECT type FROM sqlite_schema WHERE name = 'cookies'")?
        .query_map([], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let columns: HashSet<String> = connection
        .prepare("PRAGMA table_info(cookies)")?
        .query_map([], |row| row.get(1))?
        .collect::<Result<_, _>>()?;
    if kind != ["table"] || !columns.contains("host_key") || !columns.contains("top_frame_site_key")
    {
        return Err(rusqlite::Error::InvalidQuery);
    }
    let mut statement = connection.prepare("SELECT host_key, top_frame_site_key FROM cookies")?;
    let rows = statement.query_map([], |row| {
        Ok((text_value(row, 0)?.to_lowercase(), text_value(row, 1)?))
    })?;
    rows.collect()
}

/// A column as Python's `str()` writes it: text as is, an integer or real as its digits, NULL as `None`.
fn text_value(row: &rusqlite::Row<'_>, index: usize) -> Result<String, rusqlite::Error> {
    use rusqlite::types::ValueRef;
    Ok(match row.get_ref(index)? {
        ValueRef::Null => "None".to_owned(),
        ValueRef::Integer(value) => value.to_string(),
        ValueRef::Real(value) => value.to_string(),
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => {
            String::from_utf8_lossy(bytes).into_owned()
        }
    })
}

/// Whether a cookie reaches the sign-in `host`'s own pages: set for that host or a parent domain, and either not
/// partitioned or partitioned under the provider's own top-level site.
fn sent_to(cookie: &(String, String), host: &str, site: &str) -> bool {
    let (cookie_host, partition) = cookie;
    if !partition.is_empty() && partition != site {
        return false;
    }
    match cookie_host.strip_prefix('.') {
        Some(domain) => host == domain || host.ends_with(&format!(".{domain}")),
        None => host == cookie_host,
    }
}

/// A cookie host's registrable domain, by the last two labels or three under a known country second level.
fn registrable(host: &str) -> Option<String> {
    let lowered = host.trim_start_matches('.').to_lowercase();
    let labels: Vec<&str> = lowered.split('.').collect();
    let country = labels.len() >= 3
        && labels[labels.len() - 1].len() == 2
        && SECOND_LEVEL.contains(&labels[labels.len() - 2]);
    let keep = if country { 3 } else { 2 };
    let site = labels[labels.len().saturating_sub(keep)..].join(".");
    canonical_site(&Value::String(site)).map(str::to_owned)
}

#[cfg(test)]
pub(crate) mod tests;
