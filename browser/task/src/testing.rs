//! Test fakes: a scripted Driver channel that answers each request from queued replies and records what Team sent.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use tokio::time::{Instant, sleep};

use crate::cua::{Channel, ChannelLost};

/// A JSON value from its text, members in the written order.
pub(crate) fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("a test value is JSON")
}

/// One queued reply: the members of a JSON-RPC answer, merged after `jsonrpc` and the request's `id`.
#[derive(Clone, Debug)]
pub(crate) enum Reply {
    Answer(Value),
}

/// `{"result": {"structuredContent": structured, **extra}}`.
pub(crate) fn tool(structured: &str) -> Reply {
    tool_with(structured, "{}")
}

/// `{"result": {"structuredContent": structured, **extra}}` with `extra` members.
pub(crate) fn tool_with(structured: &str, extra: &str) -> Reply {
    let mut result = vec![("structuredContent".to_owned(), json(structured))];
    if let Value::Object(members) = json(extra) {
        result.extend(members);
    }
    Reply::Answer(Value::Object(vec![(
        "result".to_owned(),
        Value::Object(result),
    )]))
}

/// An answer's members from JSON text.
pub(crate) fn answer(members: &str) -> Reply {
    Reply::Answer(json(members))
}

type Delay = Box<dyn FnMut(&Value, usize, Instant) -> Duration + Send>;

#[derive(Default)]
struct Inner {
    replies: VecDeque<Reply>,
    raw: VecDeque<Vec<u8>>,
    sent: Vec<Value>,
    lines: Vec<String>,
    deadlines: Vec<Instant>,
    send_delay: Option<Delay>,
    receive_delay: Duration,
    broken: bool,
    refuse_containing: Option<&'static str>,
}

/// The scripted Driver; clones share it, so a test inspects it after the session took its channel.
#[derive(Clone, Default)]
pub(crate) struct Script(Arc<Mutex<Inner>>);

impl Script {
    pub(crate) fn new(replies: impl IntoIterator<Item = Reply>) -> Self {
        let script = Self::default();
        script.extend(replies);
        script
    }

    fn inner(&self) -> MutexGuard<'_, Inner> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    pub(crate) fn extend(&self, replies: impl IntoIterator<Item = Reply>) {
        self.inner().replies.extend(replies);
    }

    pub(crate) fn raw(&self, line: &[u8]) {
        self.inner().raw.push_back(line.to_vec());
    }

    /// Each send first waits what `delay` says for the message, the count of messages sent before it, and its deadline.
    pub(crate) fn delay_sends(
        &self,
        delay: impl FnMut(&Value, usize, Instant) -> Duration + Send + 'static,
    ) {
        self.inner().send_delay = Some(Box::new(delay));
    }

    pub(crate) fn delay_receives(&self, delay: Duration) {
        self.inner().receive_delay = delay;
    }

    /// Every receive fails as a closed channel.
    pub(crate) fn break_receives(&self) {
        self.inner().broken = true;
    }

    /// A send whose bytes contain `text` fails as a closed channel.
    pub(crate) fn refuse_sends_containing(&self, text: &'static str) {
        self.inner().refuse_containing = Some(text);
    }

    pub(crate) fn sent(&self) -> Vec<Value> {
        self.inner().sent.clone()
    }

    /// Every line sent, exactly as encoded.
    pub(crate) fn lines(&self) -> Vec<String> {
        self.inner().lines.clone()
    }

    pub(crate) fn clear_sent(&self) {
        self.inner().sent.clear();
    }

    pub(crate) fn deadlines(&self) -> Vec<Instant> {
        self.inner().deadlines.clone()
    }

    pub(crate) fn left(&self) -> usize {
        self.inner().replies.len()
    }

    /// Every tool call sent: its name and arguments.
    pub(crate) fn calls(&self) -> Vec<(String, Value)> {
        self.sent()
            .iter()
            .filter(|message| {
                message.get("method") == Some(&Value::String("tools/call".to_owned()))
            })
            .filter_map(|message| {
                let params = message.get("params")?;
                let Some(Value::String(name)) = params.get("name") else {
                    return None;
                };
                Some((name.clone(), params.get("arguments")?.clone()))
            })
            .collect()
    }

    pub(crate) fn names(&self) -> Vec<String> {
        self.calls().into_iter().map(|(name, _)| name).collect()
    }

    pub(crate) fn channel(&self) -> Box<dyn Channel> {
        Box::new(self.clone())
    }
}

impl Channel for Script {
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async move {
            let text = std::str::from_utf8(data).map_err(|_| ChannelLost)?;
            let message = json(text.trim_end_matches('\n'));
            let delay = {
                let mut inner = self.inner();
                let count = inner.sent.len();
                inner
                    .send_delay
                    .as_mut()
                    .map_or(Duration::ZERO, |delay| delay(&message, count, deadline))
            };
            sleep(delay).await;
            let mut inner = self.inner();
            if inner
                .refuse_containing
                .is_some_and(|refused| text.contains(refused))
            {
                return Err(ChannelLost);
            }
            inner.sent.push(message);
            inner.lines.push(text.to_owned());
            Ok(())
        })
    }

    fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            assert_eq!(limit, crate::cua::MAX_MESSAGE_BYTES);
            let delay = {
                let mut inner = self.inner();
                inner.deadlines.push(deadline);
                if inner.broken {
                    return Err(ChannelLost);
                }
                inner.receive_delay
            };
            sleep(delay).await;
            let mut inner = self.inner();
            if let Some(raw) = inner.raw.pop_front() {
                return Ok(raw);
            }
            let id = inner
                .sent
                .last()
                .and_then(|request| request.get("id"))
                .cloned()
                .unwrap_or(Value::Null);
            match inner.replies.pop_front().ok_or(ChannelLost)? {
                Reply::Answer(Value::Object(members)) => {
                    let mut answer = vec![
                        ("jsonrpc".to_owned(), Value::String("2.0".to_owned())),
                        ("id".to_owned(), id),
                    ];
                    answer.extend(members);
                    let options = team_protocol::python::json::Options {
                        ensure_ascii: true,
                        sort_keys: false,
                        compact: false,
                    };
                    Ok(
                        team_protocol::python::json::dumps(&Value::Object(answer), options)
                            .into_bytes(),
                    )
                }
                Reply::Answer(_) => Err(ChannelLost),
            }
        })
    }
}

/// Padded standard base64, as `base64.b64encode` writes it.
pub(crate) fn base64(bytes: &[u8]) -> String {
    crate::cua::base64(bytes)
}

/// One element a fake page offers.
#[derive(Clone, Debug)]
pub(crate) struct Element {
    pub(crate) reference: String,
    pub(crate) role: String,
    pub(crate) name: String,
    pub(crate) actions: Vec<&'static str>,
}

/// A page the fake Browser shows at a URL instead of its default page.
#[derive(Clone, Debug, Default)]
pub(crate) struct View {
    pub(crate) url: String,
    pub(crate) title: String,
    pub(crate) elements: Vec<Element>,
    pub(crate) outline: String,
}

/// A fake Browser behind the Cua Driver: its page, its faults, and every page command it received.
#[derive(Debug)]
pub(crate) struct Browser {
    pub(crate) url: String,
    pub(crate) links: Vec<(String, String)>,
    /// The page's other refs, each a button named `b`.
    pub(crate) refs: Vec<(String, Vec<&'static str>)>,
    pub(crate) views: std::collections::HashMap<String, View>,
    pub(crate) refuse: Vec<&'static str>,
    pub(crate) fatal: Vec<&'static str>,
    pub(crate) unloaded: Vec<&'static str>,
    /// Every page read fails so: `tool-failed` or `browser-read-refused`.
    pub(crate) fail_reads: Option<&'static str>,
    /// The page read of this 1-based number fails as `tool-failed`.
    pub(crate) fail_read: Option<usize>,
    pub(crate) read_delay: Duration,
    pub(crate) clicks: std::collections::HashMap<String, String>,
    pub(crate) fatal_refs: Vec<String>,
    pub(crate) links_after_click: Option<Vec<(String, String)>>,
    pub(crate) calls: Vec<Vec<String>>,
    pub(crate) screenshots: Vec<bool>,
    pub(crate) reads: usize,
    /// A file every page command is appended to as it reaches the page, so it outlives a crashed process.
    pub(crate) log: Option<std::path::PathBuf>,
}

impl Default for Browser {
    fn default() -> Self {
        Self {
            url: "https://example.com".to_owned(),
            links: Vec::new(),
            refs: vec![
                ("p1:0".to_owned(), vec!["type"]),
                ("p1:1".to_owned(), vec!["click"]),
            ],
            views: std::collections::HashMap::new(),
            refuse: Vec::new(),
            fatal: Vec::new(),
            unloaded: Vec::new(),
            fail_reads: None,
            fail_read: None,
            read_delay: Duration::ZERO,
            clicks: std::collections::HashMap::new(),
            fatal_refs: Vec::new(),
            links_after_click: None,
            calls: Vec::new(),
            screenshots: Vec::new(),
            reads: 0,
            log: None,
        }
    }
}

/// The default page's text as the Driver's observation shows it, at `url`.
pub(crate) fn page_text(url: &str) -> String {
    format!(
        "url: {url}\ntitle: Example\nactions:\np1:0 button \"b\" (type)\np1:1 button \"b\" (click)\npage:\n"
    )
}

impl Browser {
    fn view(&self) -> View {
        if let Some(view) = self.views.get(&self.url) {
            return view.clone();
        }
        let mut elements: Vec<Element> = self
            .links
            .iter()
            .map(|(reference, name)| Element {
                reference: reference.clone(),
                role: "link".to_owned(),
                name: name.clone(),
                actions: vec!["click"],
            })
            .collect();
        elements.extend(
            self.refs
                .iter()
                .filter(|(reference, _)| !self.links.iter().any(|(link, _)| link == reference))
                .map(|(reference, actions)| Element {
                    reference: reference.clone(),
                    role: "button".to_owned(),
                    name: "b".to_owned(),
                    actions: actions.clone(),
                }),
        );
        View {
            url: self.url.clone(),
            title: "Example".to_owned(),
            elements,
            outline: String::new(),
        }
    }

    fn state(&self, screenshot: bool) -> Value {
        let view = self.view();
        let refs = view
            .elements
            .iter()
            .map(|element| {
                Value::Object(vec![
                    ("ref".to_owned(), Value::String(element.reference.clone())),
                    ("role".to_owned(), Value::String(element.role.clone())),
                    ("name".to_owned(), Value::String(element.name.clone())),
                    (
                        "actions".to_owned(),
                        Value::Array(
                            element
                                .actions
                                .iter()
                                .map(|action| Value::String((*action).to_owned()))
                                .collect(),
                        ),
                    ),
                ])
            })
            .collect();
        let structured = Value::Object(vec![
            (
                "page".to_owned(),
                Value::Object(vec![
                    ("url".to_owned(), Value::String(view.url)),
                    ("title".to_owned(), Value::String(view.title)),
                ]),
            ),
            ("refs".to_owned(), Value::Array(refs)),
            ("outline".to_owned(), Value::String(view.outline)),
        ]);
        let mut result = vec![("structuredContent".to_owned(), structured)];
        if screenshot {
            let png = [crate::cua::PNG_SIGNATURE, b"pixels"].concat();
            result.push((
                "content".to_owned(),
                json(&format!(
                    r#"[{{"type": "image", "mimeType": "image/png", "data": "{}"}}]"#,
                    base64(&png)
                )),
            ));
        }
        Value::Object(result)
    }

    fn act(&mut self, tool: &str, kind: &'static str, call: Vec<String>) -> Option<Value> {
        if let Some(log) = &self.log {
            use std::io::Write as _;
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(log)
                .expect("effect log");
            writeln!(file, "{}", call.join(" ")).expect("effect logged");
        }
        self.calls.push(call);
        if self.fatal.contains(&kind) {
            return Some(json(r#"{"structuredContent": {"x": 1}, "isError": true}"#));
        }
        if self.refuse.contains(&kind) {
            return Some(json(r#"{"structuredContent": {"status": "refused"}}"#));
        }
        if tool == "browser_navigate" && self.unloaded.contains(&kind) {
            return Some(json(
                r#"{"content": [{"type": "text", "text": "navigation failed: net::ERR_TUNNEL_CONNECTION_FAILED"}], "isError": true, "structuredContent": {"code": "tool_invocation_failed"}}"#,
            ));
        }
        None
    }

    fn navigate(&mut self, tool: &str, url: String) -> Value {
        if let Some(refused) = self.act(tool, "navigate", vec!["navigate".to_owned(), url.clone()])
        {
            return refused;
        }
        self.url = url;
        json(r#"{"structuredContent": {"status": "ok"}}"#)
    }

    fn click(&mut self, tool: &str, reference: &str) -> Value {
        if let Some(refused) = self.act(
            tool,
            "click",
            vec!["click".to_owned(), reference.to_owned()],
        ) {
            return refused;
        }
        if self.fatal_refs.iter().any(|fatal| fatal == reference) {
            return json(r#"{"structuredContent": {"x": 1}, "isError": true}"#);
        }
        if let Some(url) = self.clicks.get(reference) {
            self.url = url.clone();
        }
        if let Some(links) = self.links_after_click.clone() {
            self.links = links;
        }
        json(r#"{"structuredContent": {"status": "ok"}}"#)
    }

    fn read(&mut self, arguments: &Value) -> (Value, Duration) {
        self.reads += 1;
        let screenshot = arguments.get("include_screenshot") == Some(&Value::Bool(true));
        self.screenshots.push(screenshot);
        let failing = self.fail_reads.or(if self.fail_read == Some(self.reads) {
            Some("tool-failed")
        } else {
            None
        });
        let result = match failing {
            Some("browser-read-refused") => json(
                r#"{"structuredContent": {"status": "refused", "refusal": {"code": "authorization_host_failed", "message": "timed out"}}, "isError": true}"#,
            ),
            Some(_) => json(r#"{"structuredContent": {}, "isError": true}"#),
            None => self.state(screenshot),
        };
        (result, self.read_delay)
    }

    /// The result of one tool call, and how long it takes.
    fn answer(&mut self, tool: &str, arguments: &Value) -> (Value, Duration) {
        let argument = |name: &str| match arguments.get(name) {
            Some(Value::String(text)) => text.clone(),
            _ => String::new(),
        };
        let ok = json(r#"{"structuredContent": {"status": "ok"}}"#);
        let result = match tool {
            "list_windows" => json(
                r#"{"structuredContent": {"windows": [{"pid": 20, "window_id": 7, "app_name": "Chromium-browser"}]}}"#,
            ),
            "get_browser_state" if arguments.get("snapshot_format").is_none() => json(
                r#"{"structuredContent": {"binding_quality": "exact", "target_id": "bt-1", "tabs": [{"tab_id": "tab-1", "active": true}]}}"#,
            ),
            "get_browser_state" => return self.read(arguments),
            "browser_navigate" => self.navigate(tool, argument("url")),
            "browser_click" => self.click(tool, &argument("ref")),
            "browser_type" => {
                let call = vec!["type".to_owned(), argument("ref"), argument("text")];
                self.act(tool, "type", call).unwrap_or(ok)
            }
            "browser_pointer" => {
                let direction = match arguments.get("delta_y") {
                    Some(Value::Number(strict_json::Number::Int(delta))) if *delta < 0 => "up",
                    _ => "down",
                };
                self.act(
                    tool,
                    "scroll",
                    vec!["scroll".to_owned(), direction.to_owned()],
                )
                .unwrap_or(ok)
            }
            _ => ok,
        };
        (result, Duration::ZERO)
    }
}

/// One answer line the fake Driver owes, and how long it takes to give it.
type Pending = (Vec<u8>, Duration);

/// A fake Browser's shared handle and the Driver channel to it.
#[derive(Clone, Debug, Default)]
pub(crate) struct Driver {
    browser: Arc<Mutex<Browser>>,
    pending: Arc<Mutex<VecDeque<Pending>>>,
}

impl Driver {
    pub(crate) fn new(browser: Browser) -> Self {
        Self {
            browser: Arc::new(Mutex::new(browser)),
            pending: Arc::default(),
        }
    }

    pub(crate) fn browser(&self) -> MutexGuard<'_, Browser> {
        self.browser.lock().unwrap_or_else(PoisonError::into_inner)
    }

    pub(crate) fn calls(&self) -> Vec<Vec<String>> {
        self.browser().calls.clone()
    }
}

impl Channel for Driver {
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        _deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async move {
            let text = std::str::from_utf8(data).map_err(|_| ChannelLost)?;
            let message = json(text.trim_end_matches('\n'));
            let Some(id) = message.get("id").cloned() else {
                return Ok(());
            };
            let (result, took) = match message.get("method") {
                Some(Value::String(method)) if method == "tools/call" => {
                    let params = message.get("params").cloned().unwrap_or(Value::Null);
                    let tool = match params.get("name") {
                        Some(Value::String(tool)) => tool.clone(),
                        _ => String::new(),
                    };
                    let arguments = params.get("arguments").cloned().unwrap_or(Value::Null);
                    self.browser().answer(&tool, &arguments)
                }
                _ => (json(r#"{"capabilities": {}}"#), Duration::ZERO),
            };
            let answer = Value::Object(vec![
                ("jsonrpc".to_owned(), Value::String("2.0".to_owned())),
                ("id".to_owned(), id),
                ("result".to_owned(), result),
            ]);
            let options = team_protocol::python::json::Options {
                ensure_ascii: false,
                sort_keys: false,
                compact: true,
            };
            let line = team_protocol::python::json::dumps(&answer, options).into_bytes();
            self.pending
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push_back((line, took));
            Ok(())
        })
    }

    fn receive_line(
        &mut self,
        _deadline: Instant,
        _limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            let next = self
                .pending
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .pop_front();
            let (line, took) = next.ok_or(ChannelLost)?;
            sleep(took).await;
            Ok(line)
        })
    }
}
