//! One helper request: a bounded JSON header line naming the kind and exact size (and a PDF page's number), then the
//! original's bytes. Only an exact request is prepared.

use strict_json::{Number, Value};

use crate::limits::{MAX_HELPER_HEADER_BYTES, MAX_IMAGE_BYTES, MAX_PDF_BYTES, MAX_PDF_PAGES};

/// What the worker makes of the original.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// An image's derivative.
    Image,
    /// A PDF's text.
    Pdf,
    /// One page of a PDF, drawn.
    PdfPage,
}

impl Kind {
    /// The kind's name in the header.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Image => "image",
            Self::Pdf => "pdf",
            Self::PdfPage => "pdf-page",
        }
    }

    const fn ceiling(self) -> usize {
        match self {
            Self::Image => MAX_IMAGE_BYTES,
            Self::Pdf | Self::PdfPage => MAX_PDF_BYTES,
        }
    }
}

/// One decoded request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request<'a> {
    /// Its kind.
    pub kind: Kind,
    /// The original's exact bytes.
    pub data: &'a [u8],
    /// The page (from 1), for a PDF page only.
    pub page: Option<i64>,
}

/// Frames one request. Only a PDF page names a page.
#[must_use]
pub fn encode(kind: Kind, data: &[u8], page: Option<i64>) -> Vec<u8> {
    let page = page.map_or_else(String::new, |page| format!(r#","page":{page}"#));
    let header = format!(
        r#"{{"kind":"{}","size":{}{page}}}"#,
        kind.name(),
        data.len()
    ) + "\n";
    let mut framed = header.into_bytes();
    framed.extend_from_slice(data);
    framed
}

/// The kind, exact bytes, and page of one framed request, or `None` for anything else.
#[must_use]
pub fn decode(raw: &[u8]) -> Option<Request<'_>> {
    let separator = raw.iter().position(|byte| *byte == b'\n')?;
    let (header, data) = (&raw[..separator], &raw[separator + 1..]);
    if header.len() > MAX_HELPER_HEADER_BYTES {
        return None;
    }
    let Value::Object(members) = strict_json::parse(header).ok()? else {
        return None;
    };
    let field = |name: &str| {
        members
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value)
    };
    let kind = match field("kind")? {
        Value::String(name) => [Kind::Image, Kind::Pdf, Kind::PdfPage]
            .into_iter()
            .find(|kind| kind.name() == name)?,
        _ => return None,
    };
    let paged = kind == Kind::PdfPage;
    if members.len() != if paged { 3 } else { 2 } {
        return None;
    }
    let size = integer(field("size")?)?;
    if !(1..=kind.ceiling()).contains(&usize::try_from(size).ok()?)
        || usize::try_from(size).ok()? != data.len()
    {
        return None;
    }
    let page = if paged {
        Some(integer(field("page")?).filter(|page| (1..=MAX_PDF_PAGES).contains(page))?)
    } else {
        None
    };
    Some(Request { kind, data, page })
}

const fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(number)) => Some(*number),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::{Kind, Request, decode, encode};
    use crate::limits::MAX_HELPER_HEADER_BYTES;

    #[test]
    fn an_encoded_request_decodes_to_itself() {
        let framed = encode(Kind::PdfPage, b"%PDF", Some(3));
        assert!(framed.starts_with(br#"{"kind":"pdf-page","size":4,"page":3}"#));
        assert_eq!(
            decode(&framed),
            Some(Request {
                kind: Kind::PdfPage,
                data: b"%PDF",
                page: Some(3)
            })
        );
        assert_eq!(
            decode(&encode(Kind::Image, b"x", None)).map(|request| request.page),
            Some(None)
        );
    }

    #[test]
    fn only_an_exact_bounded_request_is_prepared() {
        let long_header = [vec![b'x'; MAX_HELPER_HEADER_BYTES + 1], b"\n".to_vec()].concat();
        for raw in [
            &b""[..],
            b"no header separator",
            br#"{"kind":"script","size":1}
x"#,
            br#"{"kind":"image","size":2}
x"#,
            br#"{"kind":"image","size":true}
x"#,
            br#"{"kind":"image","size":1.0}
x"#,
            br#"{"kind":"image","size":1,"extra":1}
x"#,
            br#"{"kind":"image","size":1,"page":1}
x"#,
            br#"{"kind":"pdf-page","size":1}
x"#,
            br#"{"kind":"pdf-page","size":1,"page":51}
x"#,
            br#"{"kind":"pdf-page","size":1,"page":0}
x"#,
            br#"{"kind":"image","kind":"image","size":1}
x"#,
            b"[1]\nx",
            b"{\"kind\":\"pdf\",\"size\":0}\n",
            &long_header,
        ] {
            assert_eq!(decode(raw), None, "{}", String::from_utf8_lossy(raw));
        }
    }
}
