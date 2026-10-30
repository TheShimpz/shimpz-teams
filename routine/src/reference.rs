//! Which one Routine a person's recording send refers to, read with no model (ADR-0122).
//!
//! A send refers to a Routine when it comes from that Routine's Refine entry, which names it by id, or when its
//! person-authored text names it: the Routine's name, compared without case after Unicode composition, with no letter,
//! digit, or underscore on either side. A Refine entry for a Routine the Team does not list, or text naming several
//! Routines, refers to none Team can resolve; a send that names none refers to nothing at all, so the span keeps what
//! an earlier send of it selected.

use team_protocol::python::text::{casefold, is_word, nfc};

/// What one send says about the listed Routines.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Referred {
    /// Exactly one Routine, by id.
    Selected(String),
    /// None Team can resolve.
    Unresolved,
    /// Nothing about any Routine.
    Nothing,
}

fn folded(text: &str) -> String {
    casefold(&nfc(text))
}

/// Whether a folded text names a folded name as a whole word.
fn names(text: &str, name: &str) -> bool {
    text.char_indices().any(|(start, _)| {
        text[start..].starts_with(name)
            && !text[..start].chars().next_back().is_some_and(is_word)
            && !text[start + name.len()..]
                .chars()
                .next()
                .is_some_and(is_word)
    })
}

/// What a send says about the listed `routines` (each its id and name): one selected by id, none Team can resolve, or
/// nothing.
///
/// `segments` are the person-authored segments of its message; `refine` the id its Refine entry sends, if any.
#[must_use]
pub fn referred(
    routines: &[(String, String)],
    segments: &[String],
    refine: Option<&str>,
) -> Referred {
    if let Some(refine) = refine {
        return if routines.iter().any(|(routine_id, _)| routine_id == refine) {
            Referred::Selected(refine.to_owned())
        } else {
            Referred::Unresolved
        };
    }
    let texts: Vec<String> = segments.iter().map(|segment| folded(segment)).collect();
    let found: Vec<&String> = routines
        .iter()
        .filter(|(_, name)| {
            let name = folded(name);
            !name.is_empty() && texts.iter().any(|text| names(text, &name))
        })
        .map(|(routine_id, _)| routine_id)
        .collect();
    match found.as_slice() {
        [] => Referred::Nothing,
        [one] => Referred::Selected((*one).clone()),
        _ => Referred::Unresolved,
    }
}

#[cfg(test)]
mod tests {
    //! `test_routine_reference.py`: which Routine a recording send selects.

    use super::{Referred, referred};

    const DNS: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const NEWS: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const SHORT: &str = "cccccccccccccccccccccccccccccccc";

    fn listed(names: &[(&str, &str)]) -> Vec<(String, String)> {
        names
            .iter()
            .map(|(id, name)| ((*id).to_owned(), (*name).to_owned()))
            .collect()
    }

    fn said(text: &str) -> Vec<String> {
        vec![text.to_owned()]
    }

    #[test]
    fn a_refine_entry_selects_its_listed_routine_and_nothing_else() {
        let routines = listed(&[(DNS, "DNS de shimpz.com"), (NEWS, "Notícias")]);
        assert_eq!(
            referred(&routines, &said("notícias"), Some(DNS)),
            Referred::Selected(DNS.to_owned())
        );
        assert_eq!(
            referred(&routines, &[], Some(&"f".repeat(32))),
            Referred::Unresolved
        );
    }

    #[test]
    fn the_persons_words_name_one_routine_without_case_or_composition() {
        let routines = listed(&[(DNS, "DNS de shimpz.com"), (NEWS, "Notícias")]);
        let decomposed = "Na rotina NOTI\u{301}CIAS, inclua esportes";
        assert_eq!(
            referred(&routines, &said(decomposed), None),
            Referred::Selected(NEWS.to_owned())
        );
        assert_eq!(
            referred(&routines, &said("Ajuste dns de SHIMPZ.com."), None),
            Referred::Selected(DNS.to_owned())
        );
    }

    #[test]
    fn a_name_inside_a_longer_word_names_nothing() {
        let routines = listed(&[(SHORT, "DNS")]);
        assert_eq!(
            referred(&routines, &said("os DNSSEC estão ok"), None),
            Referred::Nothing
        );
        assert_eq!(
            referred(&routines, &said("ver o DNS!"), None),
            Referred::Selected(SHORT.to_owned())
        );
        assert_eq!(
            referred(&routines, &said("dnsdns e dns"), None),
            Referred::Selected(SHORT.to_owned())
        );
    }

    #[test]
    fn several_names_resolve_to_none_and_no_name_says_nothing() {
        let routines = listed(&[
            (DNS, "DNS de shimpz.com"),
            (NEWS, "Notícias"),
            (SHORT, "DNS"),
        ]);
        assert_eq!(
            referred(&routines, &said("DNS e Notícias"), None),
            Referred::Unresolved
        );
        assert_eq!(
            referred(&routines, &said("DNS de shimpz.com"), None),
            Referred::Unresolved
        );
        assert_eq!(
            referred(&routines, &said("Bom dia"), None),
            Referred::Nothing
        );
    }
}
