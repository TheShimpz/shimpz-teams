//! A `read` step: Team opens up to ten links of the current page in turn (ADR-0117, 2026-10-22 amendment §1).
//!
//! The Driver exposes no link destination, so a target is the link at its position in the page's ordered list of
//! offered link texts, re-resolved to the fresh ref at that position on every return. The batch goes on only while the
//! returned page has the same URL and the same ordered list; otherwise the remaining targets are not attempted. This is
//! a heuristic on public signed-out pages, not identity: each record carries the URL and title actually opened, and a
//! click whose page never changed is reported as not opened. Before every Driver command the caller proves the task
//! may still act. Each record is kept as soon as it is captured, so a failure later in the batch loses none, and the
//! records join the next observation within its bound.

use std::collections::{BTreeSet, HashMap};
use std::fmt::Write as _;
use std::future::Future;
use std::time::Duration;

use strict_json::Value;
use team_protocol::browser::MAX_OBSERVATION_BYTES;
use tokio::time::sleep;

use crate::cua::{CuaError, CuaSession, Observation, prefix};

/// The most characters of one read page's text a record keeps.
pub const READ_PAGE_CHARS: usize = 2_000;
/// The most a read's section adds to the next observation, in UTF-8 bytes.
pub const MAX_SECTION_BYTES: usize = 12 * 1024;
/// A click is read once its page's URL differs from the page it left, polled at this interval.
pub const SETTLE_SECONDS: Duration = Duration::from_millis(300);
/// How many times a click's page is polled.
pub const SETTLE_TRIES: usize = 10;
/// The heading of a read's section.
pub const HEADING: &str = "\nPages you read last step:\n";

/// How a read that met a Driver failure ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadFailure {
    /// The Driver failed before the read's first click: the batch had no effect on the page.
    Unstarted(CuaError),
    /// The Driver failed once a link was clicked, or refused a command: the page's state is unknown.
    Failed(CuaError),
}

/// What a read reports to its task as it goes.
pub trait Reading: Send {
    /// Whether the task may still send its next Driver command.
    fn may_act(&mut self) -> impl Future<Output = bool> + Send;

    /// A page the read opened, and the text it kept of it.
    fn seen(&mut self, url: &str, text: &str);

    /// The section the read's pages add to the next observation so far.
    fn keep(&mut self, section: String);
}

fn link_names(observation: &Observation) -> Vec<&str> {
    observation
        .links
        .iter()
        .map(|(_, name)| name.as_str())
        .collect()
}

/// The positions, among the page's links, of the refs a read names; a ref the agent was not shown drops.
#[must_use]
pub fn targets(decision: &Value, observation: &Observation) -> Vec<usize> {
    let mut positions: HashMap<&str, usize> = HashMap::new();
    for (index, (reference, _)) in observation.links.iter().enumerate() {
        if observation.actions(reference).is_some() {
            positions.insert(reference.as_str(), index);
        }
    }
    let Some(Value::Array(named)) = decision.get("refs") else {
        return Vec::new();
    };
    named
        .iter()
        .filter_map(|reference| match reference {
            Value::String(reference) => positions.get(reference.as_str()).copied(),
            _ => None,
        })
        .collect()
}

fn web(url: &str) -> bool {
    url.starts_with("https://") || url.starts_with("http://")
}

/// The read's outcome, `ok` once a page was read and `refused` otherwise; the reader keeps each section as it grows.
///
/// # Errors
/// [`ReadFailure`] for a Driver failure, unstarted when it came before the first click.
pub async fn run(
    session: &mut CuaSession,
    decision: &Value,
    observation: &Observation,
    reader: &mut impl Reading,
) -> Result<&'static str, ReadFailure> {
    let chosen = targets(decision, observation);
    if chosen.is_empty() || !web(&observation.url) {
        return Ok("refused");
    }
    let names = link_names(observation);
    let mut records: Vec<String> = Vec::new();
    let mut clicked = false;
    let result = async {
        for position in &chosen {
            if !reader.may_act().await {
                break;
            }
            let fresh = session.observe(false).await?;
            if fresh.url != observation.url
                || link_names(&fresh) != names
                || !reader.may_act().await
            {
                break;
            }
            clicked = true;
            let record = open(session, &fresh.links[*position], &observation.url, reader).await?;
            records.push(record);
            reader.keep(section(&records, chosen.len() - records.len()));
            if !reader.may_act().await {
                break;
            }
            session.navigate(&observation.url).await?;
        }
        Ok::<(), CuaError>(())
    }
    .await;
    match result {
        Err(error) if error.fatal && !clicked => Err(ReadFailure::Unstarted(error)),
        Err(error) => Err(ReadFailure::Failed(error)),
        Ok(()) => Ok(if records.is_empty() { "refused" } else { "ok" }),
    }
}

async fn open(
    session: &mut CuaSession,
    link: &(String, String),
    source: &str,
    reader: &mut impl Reading,
) -> Result<String, CuaError> {
    let (reference, name) = link;
    match session.click(reference).await {
        Err(error) if error.fatal => return Err(error),
        Err(_) => return Ok(format!("--- not loaded: {name} ---")),
        Ok(()) => {}
    }
    let mut page: Option<Observation> = None;
    for _ in 0..SETTLE_TRIES {
        sleep(SETTLE_SECONDS).await;
        if !reader.may_act().await {
            break;
        }
        let observed = session.observe(false).await?;
        let moved = observed.url != source;
        page = Some(observed);
        if moved {
            break;
        }
    }
    let Some(page) = page.filter(|page| page.url != source) else {
        return Ok(format!(
            "--- not opened: {name} (the page did not change) ---"
        ));
    };
    let text = page
        .text
        .split_once("\npage:\n")
        .map_or(page.text.as_str(), |(_, rest)| rest);
    let body: String = text.chars().take(READ_PAGE_CHARS).collect();
    reader.seen(&page.url, &body);
    Ok(format!(
        "--- read {} | {} ---\n{body}",
        page.url, page.title
    ))
}

fn section(records: &[String], left: usize) -> String {
    let mut section = format!("{HEADING}{}", records.join("\n"));
    if left > 0 {
        // Writing to a String cannot fail.
        let _ = write!(
            section,
            "\n({left} links not read: the page changed or the task could not go on)"
        );
    }
    prefix(&section, MAX_SECTION_BYTES).to_owned()
}

/// The ref a line offers when it starts with `p<digits>:<digits> `, as the Driver's action lines do.
fn line_ref(line: &str) -> Option<&str> {
    let rest = line.strip_prefix('p')?;
    let (page, rest) = rest.split_once(':')?;
    let element_length = rest.bytes().take_while(u8::is_ascii_digit).count();
    let digits = |text: &str| {
        (1..=6).contains(&text.len()) && text.bytes().all(|byte| byte.is_ascii_digit())
    };
    if !digits(page)
        || !digits(&rest[..element_length])
        || rest.as_bytes().get(element_length) != Some(&b' ')
    {
        return None;
    }
    Some(&line[..1 + page.len() + 1 + element_length])
}

/// The observation the agent sees: a notice before its text and a read section after, all within the bound.
///
/// Only refs whose complete action line, as the Driver gave it, is still shown stay actionable, so a cut can never
/// leave a hidden ref clickable and no read page's text can offer one. The page's full ordered link list stays, as a
/// read compares it with the page it returns to.
#[must_use]
pub fn shown(observation: &Observation, notice: &str, section: &str) -> Observation {
    let room = MAX_OBSERVATION_BYTES.saturating_sub(section.len());
    let whole = format!("{notice}{}", observation.text);
    let kept = prefix(&whole, room);
    let (head, separated) = match kept.split_once("\npage:\n") {
        Some((head, _)) => (head, true),
        None => (kept, false),
    };
    let mut lines: Vec<&str> = head.lines().collect();
    if !separated && !lines.is_empty() {
        // The cut fell inside the action lines: the last one may be partial.
        lines.pop();
    }
    let original_head = observation
        .text
        .split_once("\npage:\n")
        .map_or(observation.text.as_str(), |(head, _)| head);
    let original: BTreeSet<&str> = original_head.lines().collect();
    let visible: BTreeSet<&str> = lines
        .iter()
        .filter(|line| original.contains(*line))
        .filter_map(|line| line_ref(line))
        .collect();
    Observation {
        text: format!("{kept}{section}"),
        refs: observation
            .refs
            .iter()
            .filter(|(reference, _)| visible.contains(reference.as_str()))
            .cloned()
            .collect(),
        ..observation.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::line_ref;

    #[test]
    fn a_ref_line_starts_with_the_exact_ref_form() {
        assert_eq!(line_ref("p1:0 button \"b\" (click)"), Some("p1:0"));
        assert_eq!(line_ref("p123456:654321 x"), Some("p123456:654321"));
        for line in [
            "p1:0",
            "p1:0x b",
            "p1234567:0 b",
            "q1:0 b",
            "p:0 b",
            "p1: b",
            " p1:0 b",
        ] {
            assert_eq!(line_ref(line), None);
        }
    }
}
