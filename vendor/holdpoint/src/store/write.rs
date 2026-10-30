//! The lease holder's writes: one transaction that validates (or claims) the lease, observes host inputs that
//! arrived since the holder last looked, and appends the holder's events.

use rusqlite::{OptionalExtension, Transaction, params};
use serde_json::Value;

use super::{Obligation, Store, read_events};
use crate::error::{Error, LimitKind};
use crate::event::{Event, Outcome, StepKind};
use crate::json;

/// A drive session's view of its lease.
#[derive(Debug, Clone)]
pub(crate) struct Lease {
    /// Unique per drive session.
    pub(crate) session: String,
    /// The worker process that owns the session.
    pub(crate) incarnation: String,
    /// The fencing token once the session claimed the run.
    pub(crate) fence: Option<u64>,
    /// The newest sequence number the session has seen.
    pub(crate) seen: u64,
}

/// An obligation the session records: an attempt that may still be acting, or (`attempt: None`) everything the
/// session owned when its clean release failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct NewObligation {
    pub(crate) fence: u64,
    pub(crate) attempt: Option<(u32, u32)>,
}

/// The attempt a write dispatches; the store stamps it with the session's fencing token.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Dispatch {
    pub(crate) step: u32,
    pub(crate) attempt: u32,
    pub(crate) at_ms: i64,
    pub(crate) reserved: u64,
}

/// How the write leaves the lease.
#[derive(Debug, Clone, Copy)]
pub(crate) enum End<'a> {
    /// Keep holding the run.
    Hold,
    /// Keep holding the run, committing an effect's receipt with the run end's headroom past the history bounds.
    Receipt,
    /// Release the run cleanly until `wake_ms` (or until a host input, when `None`).
    Park { wake_ms: Option<i64> },
    /// End the run with `outcome`, unless a cancellation was committed first: then the run ends cancelled with
    /// `cancelled` as its progress. The store decides inside the terminal transaction and appends the run's end.
    Finish { outcome: &'a Outcome, cancelled: &'a Value },
}

/// One write of the lease holder.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Write<'a> {
    pub(crate) events: &'a [Event],
    /// A `StepStarted` appended only when the run is not cancelled and carries no obligation.
    pub(crate) dispatch: Option<Dispatch>,
    pub(crate) end: End<'a>,
    pub(crate) blobs: &'a [(u64, &'a [u8])],
    pub(crate) obligations: &'a [NewObligation],
    pub(crate) proven: &'a [i64],
    pub(crate) now_ms: i64,
    pub(crate) lease_ms: u64,
}

/// What the write found and did.
#[derive(Debug, Default)]
pub(crate) struct Written {
    /// Host inputs appended since the session last looked, in order.
    pub(crate) foreign: Vec<Event>,
    /// The run's obligations still unproven.
    pub(crate) obligations: Vec<Obligation>,
    /// The `StepStarted` appended, stamped with the fence.
    pub(crate) dispatched: Option<Event>,
    /// The park was refused because a host input arrived after the decision; nothing but the claim was written.
    pub(crate) refused_park: bool,
    /// The session read its history before another session wrote (or ended the run): nothing was written, and the
    /// session must start over from the ledger.
    pub(crate) stale: bool,
    /// How the run ended, when the write ended it.
    pub(crate) finished: Option<Outcome>,
}

struct Row {
    open: bool,
    seq: u64,
    bytes: u64,
    fence: u64,
    session: Option<String>,
    expires_ms: Option<i64>,
    cancel: bool,
}

fn to_i64(value: u64) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

fn to_u64(value: i64) -> u64 {
    u64::try_from(value).unwrap_or(0)
}

impl Store {
    /// Validates or claims `lease`, then applies `write` in one transaction.
    pub(crate) fn write(&self, run_id: &str, lease: &mut Lease, write: &Write<'_>) -> Result<Written, Error> {
        let context = Context { run_id, event_bytes: self.event_bytes(), limits: &self.limits };
        let (written, fence, seq) = self.transaction(|transaction| {
            let row = load_row(transaction, run_id)?;
            if lease.fence.is_none() && (!row.open || row.seq > lease.seen) {
                // Another session wrote since this one read the history: whatever it decided is stale.
                return Ok((Written { stale: true, ..Written::default() }, 0, lease.seen));
            }
            if !row.open {
                return Err(Error::LeaseLost(format!("run {run_id} was ended by another session")));
            }
            let fence = claim(transaction, run_id, &row, lease, write.now_ms)?;
            let foreign = if row.seq > lease.seen {
                read_events(transaction, run_id, lease.seen, context.event_bytes)?
            } else {
                vec![]
            };
            let mut written = Written { foreign, ..Written::default() };
            if matches!(write.end, End::Park { .. }) && !written.foreign.is_empty() {
                written.refused_park = true;
                update_lease(transaction, run_id, fence, &lease.session, write.now_ms, write.lease_ms)?;
                return Ok((written, fence, row.seq));
            }
            let seq = apply(transaction, &context, &row, fence, lease, write, &mut written)?;
            Ok((written, fence, seq))
        })?;
        if !written.stale {
            lease.fence = Some(fence);
            lease.seen = seq.max(lease.seen);
        }
        Ok(written)
    }
}

/// How many events, and how many maximal values, past the history's bounds an effect's receipt may add.
const RECEIPT_HEADROOM: (u64, u64) = (8, 2);
/// How many past the bounds the run's end may add: more than any receipt, so a run can always end after one.
const TERMINAL_HEADROOM: (u64, u64) = (64, 6);

/// Refuses a write past the history's bounds plus its headroom: none for ordinary writes, a little for an effect's
/// receipt, more for the run's end.
fn check_size(
    context: &Context<'_>,
    seq: u64,
    bytes: u64,
    (extra_events, extra_values): (u64, u64),
) -> Result<(), Error> {
    let limits = context.limits;
    let extra_bytes = u64::try_from(limits.max_value_bytes).unwrap_or(u64::MAX).saturating_mul(extra_values);
    let full =
        seq > limits.max_events_per_run + extra_events || bytes > limits.max_run_bytes.saturating_add(extra_bytes);
    if full { Err(Error::Limit(LimitKind::History)) } else { Ok(()) }
}

struct Context<'a> {
    run_id: &'a str,
    event_bytes: usize,
    limits: &'a crate::limits::Limits,
}

/// Applies a validated write: proofs, the dispatch (only without cancellation or obligations), the events, blobs,
/// new obligations, and the run's new lease state. Returns the new sequence number.
fn apply(
    transaction: &Transaction<'_>,
    context: &Context<'_>,
    row: &Row,
    fence: u64,
    lease: &Lease,
    write: &Write<'_>,
    written: &mut Written,
) -> Result<u64, Error> {
    let run_id = context.run_id;
    prove(transaction, run_id, write.proven)?;
    written.obligations = obligations(transaction, run_id)?;
    let allowed = !row.cancel && written.obligations.is_empty();
    written.dispatched = write.dispatch.filter(|_| allowed).map(|dispatch| Event::StepStarted {
        step: dispatch.step,
        attempt: dispatch.attempt,
        fence,
        at_ms: dispatch.at_ms,
        reserved: dispatch.reserved,
    });
    let ending = match write.end {
        End::Finish { outcome, cancelled } if row.cancel && !matches!(outcome, Outcome::Cancelled { .. }) => {
            Some(Event::RunCompleted { outcome: Outcome::Cancelled { progress: cancelled.clone() } })
        }
        End::Finish { outcome, .. } => Some(Event::RunCompleted { outcome: outcome.clone() }),
        End::Hold | End::Receipt | End::Park { .. } => None,
    };
    let mut events: Vec<&Event> = write.events.iter().collect();
    events.extend(written.dispatched.as_ref());
    events.extend(ending.as_ref());
    let (seq, bytes) = append(transaction, run_id, row.seq, row.bytes, &events, context.event_bytes)?;
    let headroom = match write.end {
        End::Finish { .. } => TERMINAL_HEADROOM,
        End::Receipt => RECEIPT_HEADROOM,
        End::Hold | End::Park { .. } => (0, 0),
    };
    check_size(context, seq, bytes, headroom)?;
    if let Some(Event::RunCompleted { outcome }) = ending {
        written.finished = Some(outcome);
    }
    store_blobs(transaction, run_id, write.blobs)?;
    record_obligations(transaction, run_id, write.obligations, &lease.incarnation, write.now_ms)?;
    finish_row(transaction, run_id, &lease.session, (fence, seq, bytes), write, written.finished.as_ref())?;
    Ok(seq)
}

fn load_row(transaction: &Transaction<'_>, run_id: &str) -> Result<Row, Error> {
    let row = transaction
        .query_row(
            "SELECT state, seq, bytes, fence, lease_session, lease_expires_ms, cancel FROM runs WHERE id = ?1",
            [run_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    Row {
                        open: false,
                        seq: to_u64(row.get(1)?),
                        bytes: to_u64(row.get(2)?),
                        fence: to_u64(row.get(3)?),
                        session: row.get(4)?,
                        expires_ms: row.get(5)?,
                        cancel: row.get::<_, i64>(6)? == 1,
                    },
                ))
            },
        )
        .optional()?;
    match row {
        None => Err(Error::State(format!("unknown run {run_id}"))),
        Some((state, row)) => Ok(Row { open: state == "open", ..row }),
    }
}

/// Validates the session's lease, or claims the run: a claim after a session that ended without a clean release
/// records that session as an obligation, and every claim takes a new fencing token.
fn claim(transaction: &Transaction<'_>, run_id: &str, row: &Row, lease: &Lease, now_ms: i64) -> Result<u64, Error> {
    match lease.fence {
        // Every claim takes a new fence, so a matching fence is this session's own valid lease.
        Some(fence) if row.fence == fence => return Ok(fence),
        Some(_) => return Err(Error::LeaseLost(format!("run {run_id} is held by another session"))),
        None => {}
    }
    if let (Some(_), Some(expires_ms)) = (&row.session, row.expires_ms) {
        if expires_ms > now_ms {
            return Err(Error::Busy(format!("run {run_id} is held by another session")));
        }
        transaction.execute(
            "INSERT INTO obligations (run_id, id, kind, fence, incarnation, created_ms) \
             VALUES (?1, (SELECT coalesce(max(id), 0) + 1 FROM obligations WHERE run_id = ?1), 'session', ?2, ?3, ?4)",
            params![run_id, to_i64(row.fence), lease.incarnation, now_ms],
        )?;
    }
    Ok(row.fence + 1)
}

fn update_lease(
    transaction: &Transaction<'_>,
    run_id: &str,
    fence: u64,
    session: &str,
    now_ms: i64,
    lease_ms: u64,
) -> Result<(), Error> {
    transaction.execute(
        "UPDATE runs SET fence = ?2, lease_session = ?3, lease_expires_ms = ?4, wake_ms = ?5, updated_ms = ?5 WHERE id = ?1",
        params![run_id, to_i64(fence), session, now_ms.saturating_add(to_i64(lease_ms)), now_ms],
    )?;
    Ok(())
}

fn prove(transaction: &Transaction<'_>, run_id: &str, proven: &[i64]) -> Result<(), Error> {
    let mut statement = transaction.prepare_cached("DELETE FROM obligations WHERE run_id = ?1 AND id = ?2")?;
    for id in proven {
        statement.execute(params![run_id, id])?;
    }
    Ok(())
}

pub(crate) fn obligations(connection: &rusqlite::Connection, run_id: &str) -> Result<Vec<Obligation>, Error> {
    let mut statement = connection.prepare_cached(
        "SELECT id, fence, step, attempt, incarnation FROM obligations WHERE run_id = ?1 ORDER BY id",
    )?;
    let rows = statement.query_map([run_id], |row| {
        let step: Option<i64> = row.get(2)?;
        let attempt: Option<i64> = row.get(3)?;
        Ok(Obligation {
            id: row.get(0)?,
            fence: to_u64(row.get(1)?),
            attempt: step.zip(attempt).map(|(step, attempt)| {
                (u32::try_from(step).unwrap_or(u32::MAX), u32::try_from(attempt).unwrap_or(u32::MAX))
            }),
            incarnation: row.get(4)?,
        })
    })?;
    rows.collect::<Result<_, _>>().map_err(Error::from)
}

fn append(
    transaction: &Transaction<'_>,
    run_id: &str,
    mut seq: u64,
    mut bytes: u64,
    events: &[&Event],
    event_bytes: usize,
) -> Result<(u64, u64), Error> {
    let mut insert = transaction.prepare_cached("INSERT INTO events (run_id, seq, body) VALUES (?1, ?2, ?3)")?;
    for event in events {
        let body = event.encode(event_bytes)?;
        seq += 1;
        bytes = bytes.saturating_add(u64::try_from(body.len()).unwrap_or(u64::MAX));
        insert.execute(params![run_id, to_i64(seq), body])?;
        side_tables(transaction, run_id, event)?;
    }
    Ok((seq, bytes))
}

fn side_tables(transaction: &Transaction<'_>, run_id: &str, event: &Event) -> Result<(), Error> {
    match event {
        Event::StepScheduled { step, kind: StepKind::Deferred, name, input: Some(request), .. } => {
            transaction.execute(
                "INSERT INTO deferred (run_id, step, name, request) VALUES (?1, ?2, ?3, ?4)",
                params![run_id, step, name, json::to_canonical(request)],
            )?;
        }
        Event::StepCompleted { release, .. } => {
            let mut delete = transaction.prepare_cached("DELETE FROM blobs WHERE run_id = ?1 AND id = ?2")?;
            for id in release {
                delete.execute(params![run_id, to_i64(*id)])?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn store_blobs(transaction: &Transaction<'_>, run_id: &str, blobs: &[(u64, &[u8])]) -> Result<(), Error> {
    // The context already kept only the blobs within the run's bounds.
    let mut insert = transaction.prepare_cached("INSERT INTO blobs (run_id, id, data) VALUES (?1, ?2, ?3)")?;
    for (id, data) in blobs {
        insert.execute(params![run_id, to_i64(*id), data])?;
    }
    Ok(())
}

fn record_obligations(
    transaction: &Transaction<'_>,
    run_id: &str,
    obligations: &[NewObligation],
    incarnation: &str,
    now_ms: i64,
) -> Result<(), Error> {
    let mut insert = transaction.prepare_cached(
        "INSERT INTO obligations (run_id, id, kind, fence, step, attempt, incarnation, created_ms) \
         VALUES (?1, (SELECT coalesce(max(id), 0) + 1 FROM obligations WHERE run_id = ?1), ?2, ?3, ?4, ?5, ?6, ?7)",
    )?;
    for obligation in obligations {
        let kind = if obligation.attempt.is_some() { "attempt" } else { "session" };
        let (step, attempt) = obligation.attempt.unzip();
        insert.execute(params![run_id, kind, to_i64(obligation.fence), step, attempt, incarnation, now_ms])?;
    }
    Ok(())
}

/// An outcome as a JSON value. It is plain data with string keys, so the conversion cannot fail.
#[cfg_attr(coverage_nightly, coverage(off))]
fn outcome_value(outcome: &Outcome) -> Result<Value, Error> {
    serde_json::to_value(outcome).map_err(|error| Error::Contract(error.to_string()))
}

fn finish_row(
    transaction: &Transaction<'_>,
    run_id: &str,
    session: &str,
    (fence, seq, bytes): (u64, u64, u64),
    write: &Write<'_>,
    finished: Option<&Outcome>,
) -> Result<(), Error> {
    let now = write.now_ms;
    match write.end {
        End::Hold | End::Receipt => {
            transaction.execute(
                "UPDATE runs SET seq = ?2, bytes = ?3 WHERE id = ?1",
                params![run_id, to_i64(seq), to_i64(bytes)],
            )?;
            update_lease(transaction, run_id, fence, session, now, write.lease_ms)?;
        }
        End::Park { wake_ms } => {
            transaction.execute(
                "UPDATE runs SET seq = ?2, bytes = ?3, fence = ?4, lease_session = NULL, lease_expires_ms = NULL, \
                 wake_ms = ?5, updated_ms = ?6 WHERE id = ?1",
                params![run_id, to_i64(seq), to_i64(bytes), to_i64(fence), wake_ms, now],
            )?;
        }
        End::Finish { outcome, .. } => {
            let value = outcome_value(finished.unwrap_or(outcome))?;
            transaction.execute(
                "UPDATE runs SET state = 'closed', outcome = ?2, outcome_digest = ?3, seq = ?4, bytes = ?5, fence = ?6, \
                 lease_session = NULL, lease_expires_ms = NULL, wake_ms = NULL, updated_ms = ?7 WHERE id = ?1",
                params![
                    run_id,
                    json::to_canonical(&value),
                    json::digest(&value),
                    to_i64(seq),
                    to_i64(bytes),
                    to_i64(fence),
                    now
                ],
            )?;
        }
    }
    Ok(())
}
