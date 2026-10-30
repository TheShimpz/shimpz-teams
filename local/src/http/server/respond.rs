//! The answers the server writes: bounded JSON, problems, bytes, NDJSON streams, and the `101` of an upgrade. Every
//! answer but the `101` is never cached, never sniffed, and closes its connection.

use std::convert::Infallible;
use std::pin::Pin;
use std::task::{Context, Poll};

use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper::body::{Body, Bytes, Frame};
use hyper::header::{
    CACHE_CONTROL, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, HeaderName, HeaderValue,
    SEC_WEBSOCKET_ACCEPT, SEC_WEBSOCKET_PROTOCOL, UPGRADE, WWW_AUTHENTICATE,
    X_CONTENT_TYPE_OPTIONS,
};
use hyper::{Response, StatusCode};
use strict_json::Value;
use team_protocol::python::json::{CANONICAL, dumps};
use tokio::sync::mpsc;

use crate::http::service::Problem;

/// The body of every answer.
pub type AnswerBody = BoxBody<Bytes, std::io::Error>;
/// One answer.
pub type Answer = Response<AnswerBody>;

/// The fixed answer to a JSON document over its bound (it carries no trace id).
pub const OVER_LIMIT: &[u8] = br#"{"error":"response exceeded its limit"}"#;

fn full(bytes: impl Into<Bytes>) -> AnswerBody {
    Full::new(bytes.into())
        .map_err(|never: Infallible| match never {})
        .boxed()
}

fn status(code: u16) -> StatusCode {
    StatusCode::from_u16(code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
}

fn private(response: &mut Answer) {
    let headers = response.headers_mut();
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    headers.insert(CONNECTION, HeaderValue::from_static("close"));
}

/// One JSON document, compact with sorted keys and UTF-8 text, within `limit` bytes; over it, `500` with the fixed
/// [`OVER_LIMIT`] document.
#[must_use]
pub fn json(code: u16, document: &Value, limit: usize) -> Answer {
    let mut encoded = dumps(document, CANONICAL).into_bytes();
    let mut code = code;
    if encoded.len() > limit {
        code = 500;
        encoded = OVER_LIMIT.to_vec();
    }
    let length = encoded.len();
    let mut response = Response::new(full(encoded));
    *response.status_mut() = status(code);
    let headers = response.headers_mut();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    headers.insert(CONTENT_LENGTH, HeaderValue::from(length));
    private(&mut response);
    if code == 401 {
        response.headers_mut().insert(
            WWW_AUTHENTICATE,
            HeaderValue::from_static("Bearer realm=\"shimpz-local\""),
        );
    }
    response
}

/// A problem's document: its message, its code when it has one, its retry delay when busy, and the trace id.
#[must_use]
pub fn problem_document(problem: &Problem, trace_id: &str) -> Value {
    let mut members = vec![(
        "error".to_owned(),
        Value::String(problem.message.clone().into_owned()),
    )];
    if let Some(code) = problem.code {
        members.push(("code".to_owned(), Value::String(code.to_owned())));
    }
    if let Some(retry) = problem.retry_after_ms {
        members.push((
            "retry_after_ms".to_owned(),
            team_protocol::python::value::int(retry.into()),
        ));
    }
    members.push(("trace_id".to_owned(), Value::String(trace_id.to_owned())));
    Value::Object(members)
}

/// `500` `{"error": "internal error"}` with the trace id when there is one: the answer to a failure whose detail
/// stays private, including an audit journal that could not record.
#[must_use]
pub fn internal(trace_id: Option<&str>, limit: usize) -> Answer {
    let mut members = vec![(
        "error".to_owned(),
        Value::String("internal error".to_owned()),
    )];
    if let Some(trace_id) = trace_id {
        members.push(("trace_id".to_owned(), Value::String(trace_id.to_owned())));
    }
    json(500, &Value::Object(members), limit)
}

/// `200` with bytes of one media type, its extra headers first.
#[must_use]
pub fn binary(
    media_type: &'static str,
    body: Vec<u8>,
    extra: &[(&'static str, String)],
) -> Option<Answer> {
    let length = body.len();
    let mut response = Response::new(full(body));
    let headers = response.headers_mut();
    for (name, value) in extra {
        headers.insert(
            HeaderName::from_bytes(name.as_bytes()).ok()?,
            HeaderValue::from_str(value).ok()?,
        );
    }
    headers.insert(CONTENT_TYPE, HeaderValue::from_static(media_type));
    headers.insert(CONTENT_LENGTH, HeaderValue::from(length));
    private(&mut response);
    Some(response)
}

/// `101` for an upgrade the far end accepted with the same key.
#[must_use]
pub fn switching(accept: &str, subprotocol: &'static str) -> Option<Answer> {
    let mut response = Response::new(full(Bytes::new()));
    *response.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    let headers = response.headers_mut();
    headers.insert(UPGRADE, HeaderValue::from_static("websocket"));
    headers.insert(CONNECTION, HeaderValue::from_static("Upgrade"));
    headers.insert(SEC_WEBSOCKET_ACCEPT, HeaderValue::from_str(accept).ok()?);
    headers.insert(
        SEC_WEBSOCKET_PROTOCOL,
        HeaderValue::from_static(subprotocol),
    );
    Some(response)
}

/// The sending side of a streamed body: each data frame is written as one chunk; an error aborts the connection
/// without the terminating chunk.
pub type Frames = mpsc::Sender<Result<Frame<Bytes>, std::io::Error>>;

/// A body fed frame by frame from a channel; it ends when every sender is dropped.
struct Channel(mpsc::Receiver<Result<Frame<Bytes>, std::io::Error>>);

impl Body for Channel {
    type Data = Bytes;
    type Error = std::io::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, std::io::Error>>> {
        self.0.poll_recv(cx)
    }
}

/// `200` `application/x-ndjson`, chunked, fed by the returned sender.
#[must_use]
pub fn ndjson() -> (Answer, Frames) {
    let (sender, receiver) = mpsc::channel(4);
    let mut response = Response::new(Channel(receiver).boxed());
    response.headers_mut().insert(
        CONTENT_TYPE,
        HeaderValue::from_static("application/x-ndjson"),
    );
    private(&mut response);
    (response, sender)
}
