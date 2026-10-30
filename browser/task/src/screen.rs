//! Provenance screening of a Browser task's answer (ADR-0117, 2026-10-22 amendment §4).
//!
//! Team keeps the URLs of the pages the task observed or read and the URLs mentioned in the text it kept, each a
//! bounded, oldest-evicted set captured at observation time. An answer's cited URLs are extracted as exact tokens; one
//! in neither set is unscreened. This is screening, not verification: a page that showed a URL does not prove it
//! supports the answer.

use std::collections::{BTreeMap, HashMap};

use team_protocol::python::text::isspace;

/// The most URLs each set keeps.
pub const MAX_URLS: usize = 2_000;
const TRAILING: &str = ".,;:!?";

/// Whether `character` may continue a URL token: anything up to whitespace or a quote or bar an answer puts around it.
fn in_url(character: char) -> bool {
    !isspace(character) && !matches!(character, '<' | '>' | '"' | '\'' | '`' | '|')
}

/// The scheme a URL token starts with at the start of `text`, `https://` or `http://`, and its length.
fn scheme(text: &str) -> Option<usize> {
    if text.starts_with("https://") {
        Some(8)
    } else if text.starts_with("http://") {
        Some(7)
    } else {
        None
    }
}

/// Every non-overlapping match of the pattern `https?://` followed by characters [`in_url`] admits, left to right.
fn find_all(text: &str) -> Vec<&str> {
    let mut found = Vec::new();
    let mut position = 0;
    while position < text.len() {
        let rest = &text[position..];
        if let Some(length) = scheme(rest) {
            let run: usize = rest[length..]
                .chars()
                .take_while(|character| in_url(*character))
                .map(char::len_utf8)
                .sum();
            if run > 0 {
                found.push(&rest[..length + run]);
                position += length + run;
                continue;
            }
        }
        position += rest.chars().next().map_or(1, char::len_utf8);
    }
    found
}

/// A URL token without the sentence punctuation or unbalanced closing brackets that follow it in prose.
fn token(mut token: &str) -> &str {
    while let Some(last) = token.chars().last() {
        let opener = match last {
            ')' => Some('('),
            ']' => Some('['),
            '}' => Some('{'),
            _ => None,
        };
        let unbalanced = opener
            .is_some_and(|opener| token.matches(last).count() > token.matches(opener).count());
        if TRAILING.contains(last) || unbalanced {
            token = &token[..token.len() - last.len_utf8()];
        } else {
            break;
        }
    }
    token
}

/// Each Markdown link destination, read up to its balanced closing parenthesis, and the text without them.
///
/// One pass over the text: a destination that never closes is left as prose, and scanning goes on after it.
fn destinations(text: &str) -> (Vec<&str>, String) {
    let mut found = Vec::new();
    let mut rest = String::new();
    let (mut start, mut position) = (0, 0);
    while let Some(offset) = text[position..].find("](") {
        let opened = position + offset;
        let begin = opened + 2;
        if scheme(&text[begin..]).is_none() {
            position = begin;
            continue;
        }
        let mut depth = 1_i64;
        let mut end = begin;
        for character in text[begin..].chars() {
            if depth == 0 || isspace(character) {
                break;
            }
            depth += match character {
                '(' => 1,
                ')' => -1,
                _ => 0,
            };
            end += character.len_utf8();
        }
        position = end;
        if depth != 0 {
            continue;
        }
        found.push(&text[begin..end - 1]);
        rest.push_str(&text[start..=opened]);
        start = end;
    }
    rest.push_str(&text[start..]);
    (found, rest)
}

/// The distinct URLs a text names, in order: a link destination as written, a URL in prose as its token.
#[must_use]
pub fn urls(text: &str) -> Vec<String> {
    let (linked, prose) = destinations(text);
    let mut distinct: Vec<String> = Vec::new();
    let tokens = find_all(&prose)
        .into_iter()
        .map(token)
        .filter(|found| !found.is_empty())
        .map(str::to_owned)
        .collect::<Vec<_>>();
    for url in linked.into_iter().map(str::to_owned).chain(tokens) {
        if !distinct.contains(&url) {
            distinct.push(url);
        }
    }
    distinct
}

/// A bounded set that remembers insertion order, a re-inserted value moving to the newest end.
#[derive(Debug, Default)]
struct Recent {
    stamps: HashMap<String, u64>,
    order: BTreeMap<u64, String>,
    next: u64,
}

impl Recent {
    fn remember(&mut self, value: String) {
        if let Some(stamp) = self.stamps.remove(&value) {
            self.order.remove(&stamp);
        }
        self.order.insert(self.next, value.clone());
        self.stamps.insert(value, self.next);
        self.next += 1;
        if self.stamps.len() > MAX_URLS
            && let Some((_, oldest)) = self.order.pop_first()
        {
            self.stamps.remove(&oldest);
        }
    }

    fn contains(&self, value: &str) -> bool {
        self.stamps.contains_key(value)
    }
}

/// What one task observed: the pages it saw and the URLs their kept text mentioned.
#[derive(Debug, Default)]
pub struct Screen {
    seen: Recent,
    mentioned: Recent,
}

impl Screen {
    /// Records a page the task observed or read, and the URLs its kept text mentions.
    pub fn observed(&mut self, url: &str, text: &str) {
        self.seen.remember(url.to_owned());
        for mentioned in urls(text) {
            self.mentioned.remember(mentioned);
        }
    }

    /// The answer's cited URLs that no page of the task showed, as its address or in its kept text.
    #[must_use]
    pub fn unscreened(&self, answer: &str) -> Vec<String> {
        urls(answer)
            .into_iter()
            .filter(|url| !self.seen.contains(url) && !self.mentioned.contains(url))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use std::time::Instant;

    use team_protocol::browser::MAX_SUMMARY_BYTES;

    use super::{MAX_URLS, Screen, urls};

    const NONE: [String; 0] = [];

    #[test]
    fn screening_a_malformed_answer_stays_linear() {
        let started = Instant::now();
        let _ = urls(&"](https://a".repeat(MAX_SUMMARY_BYTES / 11 * 8));
        assert!(started.elapsed().as_secs_f64() < 1.0);
    }

    #[test]
    fn a_bracket_that_opens_no_link_and_an_evicted_url_screen_as_prose() {
        assert_eq!(
            urls("veja ](texto) e https://example.com/a"),
            ["https://example.com/a"]
        );
        let mut checked = Screen::default();
        checked.observed("https://example.com/first", "");
        for number in 0..MAX_URLS {
            checked.observed(&format!("https://example.com/{number}"), "");
        }
        assert_eq!(
            checked.unscreened("https://example.com/first"),
            ["https://example.com/first"]
        );
    }

    #[test]
    fn screening_compares_whole_url_tokens_exactly() {
        let mut checked = Screen::default();
        checked.observed(
            "https://example.com/job",
            "veja https://example.com/item(2).",
        );
        assert_eq!(
            checked.unscreened("https://example.com/job e (https://example.com/item(2))"),
            NONE
        );
        assert_eq!(
            checked.unscreened("https://example.com/job/"),
            ["https://example.com/job/"]
        );
        assert_eq!(
            checked.unscreened("https://example.com/item"),
            ["https://example.com/item"]
        );
        // A Markdown link's destination is taken as written.
        assert_eq!(
            checked.unscreened("[vaga](https://example.com/job!)"),
            ["https://example.com/job!"]
        );
        assert_eq!(checked.unscreened("[vaga](https://example.com/job)."), NONE);
        assert_eq!(
            urls("[v](https://example.com/job(1).html)"),
            ["https://example.com/job(1).html"]
        );
    }

    #[test]
    fn urls_end_at_quotes_bars_and_whitespace_and_are_distinct() {
        assert_eq!(
            urls("'https://a.example/x'|https://b.example/y\u{a0}z https://a.example/x"),
            ["https://a.example/x", "https://b.example/y"]
        );
        assert_eq!(urls("http:// and https://"), NONE);
    }
}
