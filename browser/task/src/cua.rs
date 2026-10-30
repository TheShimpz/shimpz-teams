//! Team's MCP session with the Cua Driver inside one Browser (ADR-0117 §3.5).
//!
//! Team is the only MCP client. It speaks newline-delimited JSON-RPC over the channel its Local profile opens (a
//! `docker exec` of `browser-host mcp`), calls only the closed set of tools below with arguments it builds itself, and
//! bounds every answer before anything reaches the Brain. Page content is untrusted: it is returned as bounded text,
//! never interpreted.

use std::collections::BTreeSet;
use std::time::Duration;

use strict_json::{Number, Value};
use team_core::future::BoxFuture;
use team_protocol::browser::{MAX_TITLE_CHARS, MAX_URL_CHARS, REF_RE};
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::text::{isprintable_char, isspace};
use team_protocol::python::value::eq_int;
use tokio::time::{Instant, sleep};

/// The MCP protocol version Team speaks.
pub const PROTOCOL_VERSION: &str = "2025-06-18";
/// The one lifecycle session label every call of a task carries; refs belong to it.
pub const SESSION: &str = "task";
/// The closed set of tools Team calls.
pub const TOOLS: [&str; 7] = [
    "list_windows",
    "browser_prepare",
    "get_browser_state",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_pointer",
];
/// A page action the Driver explicitly refused leaves the Browser usable; any other failure may have had an effect.
const ACTIONS: [&str; 4] = [
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_pointer",
];
/// The longest answer line.
pub const MAX_MESSAGE_BYTES: usize = 4 * 1024 * 1024;
/// The most text one observation carries, in UTF-8 bytes.
pub const MAX_OBSERVATION_BYTES: usize = 24 * 1024;
/// The largest screenshot.
pub const MAX_SCREENSHOT_BYTES: usize = 2 * 1024 * 1024;
/// The most refs one observation offers.
pub const MAX_REFS: usize = 200;
/// The longest element name an observation shows.
pub const MAX_NAME_CHARS: usize = 120;
/// One call's own bound.
pub const CALL_SECONDS: Duration = Duration::from_secs(30);
/// A tab mid-navigation is briefly reported with no tab marked active; binding checks again at this interval.
pub const BIND_POLL_SECONDS: Duration = Duration::from_millis(100);
/// How far a scroll moves the page.
pub const SCROLL_PIXELS: i64 = 600;
/// The PNG signature a screenshot starts with.
pub const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";
/// The Driver's grant on the Browser's window lapses with use; Team renews it before binding once it is this old.
pub const REGRANT_SECONDS: Duration = Duration::from_secs(240);
/// A refused page read is asked again at this interval within the read's one bound.
pub const READ_RETRY_SECONDS: Duration = Duration::from_secs(1);
/// One read's whole bound, its retries included.
pub const READ_SECONDS: Duration = Duration::from_secs(60);
/// The longest the Driver takes to refuse a read: its own 20-second CDP wait, and a second more.
pub const REFUSAL_SECONDS: Duration = Duration::from_secs(21);
/// The refusal code of a lapsed grant.
pub const LAPSED_CODE: &str = "browser_consent_required";
/// The refusal reason of a lapsed grant.
pub const LAPSED_REASON: &str = "consumer_profile_endpoint_requires_grant";
/// The same lapse as an action meets it: the code.
pub const ENDED_CODE: &str = "protected_resource_scope_invalid";
/// The same lapse as an action meets it: the message.
pub const ENDED_MESSAGE: &str =
    "the existing-profile grant ended; approve and bind the browser again";
/// `json.dumps(message, separators=(",", ":"))`.
const COMPACT_ASCII: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};

/// The channel failed, closed, or missed its deadline.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChannelLost;

/// A byte channel to the Cua Driver's stdio: whole writes, and one bounded line before an absolute deadline.
pub trait Channel: Send {
    /// Writes all of `data` before `deadline`.
    ///
    /// # Errors
    /// [`ChannelLost`] when the channel failed, closed, or missed the deadline.
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>>;

    /// The next line without its newline, of at most `limit` bytes, before `deadline`.
    ///
    /// # Errors
    /// [`ChannelLost`] when the channel failed, closed, overflowed, or missed the deadline.
    fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>>;
}

/// A closed failure of the session: the Browser cannot be trusted to continue, or one action was refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CuaError {
    /// The closed code.
    pub code: &'static str,
    /// Whether the session must end (`false` only for an action the page refused without effect).
    pub fatal: bool,
}

impl CuaError {
    /// A failure that ends the session.
    #[must_use]
    pub const fn fatal(code: &'static str) -> Self {
        Self { code, fatal: true }
    }

    /// An action refused without effect; the session goes on.
    #[must_use]
    pub const fn refused(code: &'static str) -> Self {
        Self { code, fatal: false }
    }
}

impl std::fmt::Display for CuaError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for CuaError {}

/// Internal distinctions of a call's failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Fault {
    /// A closed failure.
    Error(CuaError),
    /// The Driver refused to read the page this time; nothing changed.
    ReadRefused,
    /// The Driver's grant on the window lapsed: the call had no effect.
    GrantLapsed,
}

impl From<CuaError> for Fault {
    fn from(error: CuaError) -> Self {
        Self::Error(error)
    }
}

impl From<Fault> for CuaError {
    fn from(fault: Fault) -> Self {
        match fault {
            Fault::Error(error) => error,
            Fault::ReadRefused => Self::fatal("browser-read-refused"),
            Fault::GrantLapsed => Self::fatal("browser-grant-lapsed"),
        }
    }
}

/// The refs one observation offers, in page order, each with the actions a step may take on it.
pub type Refs = Vec<(String, BTreeSet<&'static str>)>;

/// The bounded page state the Brain sees: its text, the refs each action may target, and an optional screenshot.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Observation {
    /// The page's URL, one bounded printable line.
    pub url: String,
    /// The page's title, one bounded printable line.
    pub title: String,
    /// The text shown to the agent.
    pub text: String,
    /// The refs offered, in page order.
    pub refs: Refs,
    /// The PNG screenshot, when one was asked for and admitted.
    pub screenshot: Option<Vec<u8>>,
    /// The offered links, in page order: each ref with role `link` offered for click, and its name.
    pub links: Vec<(String, String)>,
}

impl Observation {
    /// The actions offered on `reference`, when it is offered.
    #[must_use]
    pub fn actions(&self, reference: &str) -> Option<&BTreeSet<&'static str>> {
        self.refs
            .iter()
            .find(|(offered, _)| offered == reference)
            .map(|(_, actions)| actions)
    }
}

/// Page-supplied text as one bounded line without control characters.
fn clean(value: Option<&Value>, limit: usize) -> String {
    let Some(Value::String(text)) = value else {
        return String::new();
    };
    let printable: String = text
        .chars()
        .map(|character| {
            if isprintable_char(character) {
                character
            } else {
                ' '
            }
        })
        .collect();
    let joined = printable
        .split(isspace)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    joined.chars().take(limit).collect()
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

/// `{**base, **extra}` for objects.
fn merged(base: &Value, extra: Vec<(&str, Value)>) -> Value {
    let mut members = match base {
        Value::Object(members) => members.clone(),
        _ => Vec::new(),
    };
    for (key, value) in extra {
        if let Some(slot) = members.iter_mut().find(|(name, _)| name == key) {
            slot.1 = value;
        } else {
            members.push((key.to_owned(), value));
        }
    }
    Value::Object(members)
}

fn is_object(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Object(_)))
}

fn get<'a>(value: Option<&'a Value>, key: &str) -> Option<&'a Value> {
    value.and_then(|value| value.get(key))
}

fn is_str(value: Option<&Value>, expected: &str) -> bool {
    matches!(value, Some(Value::String(found)) if found == expected)
}

/// The one window Team admitted, by its process and window ids.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Window {
    pid: i64,
    window_id: i64,
}

impl Window {
    fn value(&self) -> Value {
        object(vec![
            ("pid", int(self.pid)),
            ("window_id", int(self.window_id)),
            ("session", text(SESSION)),
        ])
    }
}

/// One task's session: initialize, bind Chromium's one window exactly, then observe and act on its tab.
pub struct CuaSession {
    channel: Box<dyn Channel>,
    deadline: Option<Instant>,
    identifier: i64,
    window: Option<Window>,
    target: Option<(String, String)>,
    /// When the Driver last granted the window.
    granted: Option<Instant>,
}

impl std::fmt::Debug for CuaSession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CuaSession")
            .field("deadline", &self.deadline)
            .field("bound", &self.target.is_some())
            .finish_non_exhaustive()
    }
}

impl CuaSession {
    /// A session over `channel`; nothing is sent yet.
    #[must_use]
    pub fn new(channel: Box<dyn Channel>) -> Self {
        Self {
            channel,
            deadline: None,
            identifier: 0,
            window: None,
            target: None,
            granted: None,
        }
    }

    /// Sets the task's absolute deadline, which no call outlives.
    pub fn set_deadline(&mut self, deadline: Option<Instant>) {
        self.deadline = deadline;
    }

    /// The task's absolute deadline.
    #[must_use]
    pub fn deadline(&self) -> Option<Instant> {
        self.deadline
    }

    /// The MCP handshake; nothing is bound yet.
    ///
    /// # Errors
    /// [`CuaError`] when the Driver did not complete it.
    pub async fn initialize(&mut self) -> Result<(), CuaError> {
        let client = object(vec![("name", text("shimpz-team")), ("version", text("1"))]);
        let params = object(vec![
            ("protocolVersion", text(PROTOCOL_VERSION)),
            ("capabilities", Value::Object(Vec::new())),
            ("clientInfo", client),
        ]);
        self.request("initialize", params, None).await?;
        let deadline = self.call_deadline(None)?;
        let initialized = b"{\"jsonrpc\": \"2.0\", \"method\": \"notifications/initialized\"}\n";
        self.channel
            .send(initialized, deadline)
            .await
            .map_err(|_| CuaError::fatal("channel-lost"))
    }

    /// How many top-level Chromium windows the display shows (the Driver's own cursor overlay is not one).
    ///
    /// # Errors
    /// [`CuaError`] when the windows cannot be listed.
    pub async fn chromium_windows(&mut self) -> Result<usize, CuaError> {
        let listed = self
            .call("list_windows", object(vec![]), false, None)
            .await?;
        let Some(Value::Array(windows)) = listed.0.get("windows") else {
            return Err(CuaError::fatal("browser-window-unavailable"));
        };
        if !windows.iter().all(|window| is_object(Some(window))) {
            return Err(CuaError::fatal("browser-window-unavailable"));
        }
        Ok(windows.iter().filter(|window| chromium(window)).count())
    }

    /// Initializes, admits Chromium's one window, attaches the Driver to it, and binds its active tab.
    ///
    /// # Errors
    /// [`CuaError`] when any step fails.
    pub async fn open(&mut self) -> Result<(), CuaError> {
        self.initialize().await?;
        let listed = self
            .call("list_windows", object(vec![]), false, None)
            .await?;
        let window = match listed.0.get("windows") {
            Some(Value::Array(windows)) if windows.len() == 1 && is_object(windows.first()) => {
                &windows[0]
            }
            _ => return Err(CuaError::fatal("browser-window-unavailable")),
        };
        let (Some(Value::Number(Number::Int(pid))), Some(Value::Number(Number::Int(window_id)))) =
            (window.get("pid"), window.get("window_id"))
        else {
            return Err(CuaError::fatal("browser-window-unavailable"));
        };
        if !chromium(window) {
            return Err(CuaError::fatal("browser-window-unavailable"));
        }
        self.window = Some(Window {
            pid: *pid,
            window_id: *window_id,
        });
        self.prepare(None).await?;
        self.bind(None).await?;
        Ok(())
    }

    /// Attaches the Driver to the admitted window, or renews its lapsed grant there; the same call either way.
    async fn prepare(&mut self, within: Option<Instant>) -> Result<(), Fault> {
        let window = self
            .window
            .as_ref()
            .map_or(Value::Object(Vec::new()), Window::value);
        let arguments = merged(
            &window,
            vec![("strategy", object(vec![("kind", text("existing_profile"))]))],
        );
        let (answer, _) = self
            .call("browser_prepare", arguments, false, within)
            .await?;
        if !is_str(answer.get("status"), "ok") {
            return Err(CuaError::fatal("browser-attach-refused").into());
        }
        self.granted = Some(Instant::now());
        Ok(())
    }

    /// Binds the window and follows its active tab, so a link that opened a new tab is where the task continues.
    ///
    /// An old grant is renewed first, and a grant the Driver says lapsed is renewed once, within the same bound.
    async fn bind(&mut self, within: Option<Instant>) -> Result<(), Fault> {
        if self.window.is_none() {
            return Err(CuaError::fatal("browser-not-open").into());
        }
        let settle = self.call_deadline(within)?;
        if self
            .granted
            .is_none_or(|granted| Instant::now().duration_since(granted) >= REGRANT_SECONDS)
        {
            self.prepare(Some(settle)).await?;
        }
        let mut renewed = false;
        let target = loop {
            match self.bound(settle).await {
                Err(Fault::GrantLapsed) => {
                    if renewed {
                        return Err(CuaError::fatal("browser-binding-inexact").into());
                    }
                    self.prepare(Some(settle)).await?;
                    renewed = true;
                }
                Err(Fault::ReadRefused) => {
                    if Instant::now() + BIND_POLL_SECONDS + REFUSAL_SECONDS > settle {
                        return Err(CuaError::fatal("browser-read-refused").into());
                    }
                    sleep(BIND_POLL_SECONDS).await;
                }
                Err(error) => return Err(error),
                Ok(Some(target)) => break target,
                Ok(None) => {
                    // A poll that would leave no time for the next check ends the binding.
                    if Instant::now() + BIND_POLL_SECONDS >= settle {
                        return Err(CuaError::fatal("browser-binding-inexact").into());
                    }
                    sleep(BIND_POLL_SECONDS).await;
                }
            }
        };
        self.target = Some(target);
        Ok(())
    }

    /// The exact binding and its one active tab, or `None` while no tab is marked active yet.
    async fn bound(&mut self, settle: Instant) -> Result<Option<(String, String)>, Fault> {
        let window = self
            .window
            .as_ref()
            .map_or(Value::Object(Vec::new()), Window::value);
        let (bound, _) = self
            .call("get_browser_state", window, false, Some(settle))
            .await?;
        let inexact = || Fault::Error(CuaError::fatal("browser-binding-inexact"));
        let (Some(Value::String(target)), Some(Value::Array(tabs))) =
            (bound.get("target_id"), bound.get("tabs"))
        else {
            return Err(inexact());
        };
        if !is_str(bound.get("binding_quality"), "exact") {
            return Err(inexact());
        }
        // Every tab marked active counts, so a malformed one never hides a second.
        let active: Vec<Option<&Value>> = tabs
            .iter()
            .filter(|tab| tab.get("active") == Some(&Value::Bool(true)))
            .map(|tab| tab.get("tab_id"))
            .collect();
        if active.len() > 1 {
            return Err(inexact());
        }
        match active.first() {
            None => Ok(None),
            Some(Some(Value::String(tab))) => Ok(Some((target.clone(), tab.clone()))),
            Some(_) => Err(inexact()),
        }
    }

    fn tab(&self) -> Result<Value, CuaError> {
        let (target, tab) = self
            .target
            .as_ref()
            .ok_or(CuaError::fatal("browser-not-open"))?;
        Ok(object(vec![
            ("session", text(SESSION)),
            ("target_id", text(target)),
            ("tab_id", text(tab)),
        ]))
    }

    /// The bound tab's page: its bounded text, the refs each action may target, and a screenshot when asked.
    ///
    /// # Errors
    /// [`CuaError`] when the page cannot be bound or read.
    pub async fn observe(&mut self, screenshot: bool) -> Result<Observation, CuaError> {
        self.bind(None).await?;
        let (state, image) = match self.read(screenshot).await {
            Err(Fault::GrantLapsed) => {
                // The grant lapsed between binding and capture: renewed, the tab is bound again and captured once more.
                self.prepare(None).await?;
                self.bind(None).await?;
                match self.read(screenshot).await {
                    Err(Fault::GrantLapsed) => {
                        return Err(CuaError::fatal("browser-binding-inexact"));
                    }
                    other => other?,
                }
            }
            other => other?,
        };
        let page = match state.get("page") {
            Some(page @ Value::Object(_)) => Some(page),
            _ => None,
        };
        let url = clean(get(page, "url"), MAX_URL_CHARS);
        let title = clean(get(page, "title"), MAX_TITLE_CHARS);
        let head = format!("url: {url}\ntitle: {title}\nactions:\n");
        let budget = MAX_OBSERVATION_BYTES - head.len() - "\npage:\n".len();
        let (refs, shown, links) = offered(state.get("refs"), budget);
        let outline = match state.get("outline") {
            Some(Value::String(outline)) => outline.as_str(),
            _ => "",
        };
        let mut text = head + &shown.join("\n") + "\npage:\n";
        let room = MAX_OBSERVATION_BYTES.saturating_sub(text.len());
        text.push_str(prefix(outline, room));
        Ok(Observation {
            url,
            title,
            text,
            refs,
            screenshot: if screenshot { image } else { None },
            links,
        })
    }

    /// Opens `url` in the bound tab.
    ///
    /// # Errors
    /// [`CuaError`], refused (not fatal) when the page refused it or it did not load for a network error.
    pub async fn navigate(&mut self, url: &str) -> Result<(), CuaError> {
        let arguments = merged(&self.tab()?, vec![("url", text(url))]);
        self.act("browser_navigate", arguments).await
    }

    /// Clicks `reference`.
    ///
    /// # Errors
    /// [`CuaError`], refused (not fatal) when the page refused it.
    pub async fn click(&mut self, reference: &str) -> Result<(), CuaError> {
        // The container's one window is nobody else's, so trusted input may bring it to the front (ADR-0117).
        let arguments = merged(
            &self.tab()?,
            vec![
                ("ref", text(reference)),
                ("delivery_mode", text("foreground")),
            ],
        );
        self.act("browser_click", arguments).await
    }

    /// Types `value` into `reference`, replacing what it held.
    ///
    /// # Errors
    /// [`CuaError`], refused (not fatal) when the page refused it.
    pub async fn type_text(&mut self, reference: &str, value: &str) -> Result<(), CuaError> {
        let arguments = merged(
            &self.tab()?,
            vec![
                ("ref", text(reference)),
                ("text", text(value)),
                ("mode", text("insert_text")),
                ("replace", Value::Bool(true)),
            ],
        );
        self.act("browser_type", arguments).await
    }

    /// Scrolls the page `up` or `down`.
    ///
    /// # Errors
    /// [`CuaError`], refused (not fatal) when the page refused it.
    pub async fn scroll(&mut self, direction: &str) -> Result<(), CuaError> {
        let delta = match direction {
            "down" => SCROLL_PIXELS,
            "up" => -SCROLL_PIXELS,
            _ => return Err(CuaError::fatal("tool-not-admitted")),
        };
        let arguments = merged(
            &self.tab()?,
            vec![
                ("action", text("scroll")),
                ("delta_y", int(delta)),
                ("x", int(640)),
                ("y", int(400)),
                ("delivery_mode", text("foreground")),
            ],
        );
        self.act("browser_pointer", arguments).await
    }

    /// The page's state, bound afresh and read again while the Driver refuses it, until its bound is spent.
    async fn read(&mut self, screenshot: bool) -> Result<(Value, Option<Vec<u8>>), Fault> {
        let mut settle = Instant::now() + READ_SECONDS;
        if let Some(deadline) = self.deadline {
            settle = settle.min(deadline);
        }
        loop {
            let arguments = merged(
                &self.tab()?,
                vec![
                    ("snapshot_format", text("semantic_v2")),
                    ("include_screenshot", Value::Bool(screenshot)),
                ],
            );
            match self
                .call("get_browser_state", arguments, screenshot, Some(settle))
                .await
            {
                Err(Fault::ReadRefused) => {
                    if Instant::now() + READ_RETRY_SECONDS + CALL_SECONDS > settle {
                        return Err(CuaError::fatal("browser-read-refused").into());
                    }
                    sleep(READ_RETRY_SECONDS).await;
                    // The page that was bound may have gone: the window's live tab is bound afresh before the next
                    // read, which is asked only while a whole call still fits after the binding.
                    self.bind(Some(settle)).await?;
                    if Instant::now() + CALL_SECONDS > settle {
                        return Err(CuaError::fatal("browser-read-refused").into());
                    }
                }
                other => return other,
            }
        }
    }

    async fn act(&mut self, tool: &'static str, arguments: Value) -> Result<(), CuaError> {
        match self.call(tool, arguments, false, None).await {
            Err(Fault::GrantLapsed) => {
                // Refused with no effect: the grant is renewed, and the action is never repeated on refs it
                // invalidated.
                self.prepare(None).await?;
                Err(CuaError::refused("action-refused"))
            }
            Err(fault) => Err(fault.into()),
            Ok((answer, _)) if refused(&answer) => Err(CuaError::refused("action-refused")),
            Ok(_) => Ok(()),
        }
    }

    async fn call(
        &mut self,
        tool: &str,
        arguments: Value,
        images: bool,
        within: Option<Instant>,
    ) -> Result<(Value, Option<Vec<u8>>), Fault> {
        if !TOOLS.contains(&tool) {
            return Err(CuaError::fatal("tool-not-admitted").into());
        }
        let params = object(vec![("name", text(tool)), ("arguments", arguments)]);
        let result = self.request("tools/call", params, within).await?;
        let structured = result.get("structuredContent");
        if lapsed(structured) {
            return Err(Fault::GrantLapsed);
        }
        if tool == "get_browser_state"
            && structured.is_some_and(|value| is_object(Some(value)) && refused(value))
        {
            return Err(Fault::ReadRefused);
        }
        let Some(structured @ Value::Object(_)) = structured else {
            return Err(failure(tool, &result, None).into());
        };
        if result.get("isError") == Some(&Value::Bool(true)) {
            return Err(failure(tool, &result, Some(structured)).into());
        }
        let image = if images {
            screenshot(result.get("content"))
        } else {
            None
        };
        Ok((structured.clone(), image))
    }

    async fn request(
        &mut self,
        method: &str,
        params: Value,
        within: Option<Instant>,
    ) -> Result<Value, CuaError> {
        self.identifier += 1;
        let identifier = self.identifier;
        let message = object(vec![
            ("jsonrpc", text("2.0")),
            ("id", int(identifier)),
            ("method", text(method)),
            ("params", params),
        ]);
        let deadline = self.call_deadline(within)?;
        let mut line = dumps(&message, COMPACT_ASCII).into_bytes();
        line.push(b'\n');
        self.channel
            .send(&line, deadline)
            .await
            .map_err(|_| CuaError::fatal("channel-lost"))?;
        self.answer(identifier, deadline).await
    }

    /// When the next call must have ended: its own bound, never past the task's or `within`; none if passed.
    fn call_deadline(&self, within: Option<Instant>) -> Result<Instant, CuaError> {
        let now = Instant::now();
        let mut deadline = now + CALL_SECONDS;
        for bound in [self.deadline, within].into_iter().flatten() {
            deadline = deadline.min(bound);
        }
        if deadline <= now {
            return Err(CuaError::fatal("deadline-passed"));
        }
        Ok(deadline)
    }

    async fn answer(&mut self, identifier: i64, deadline: Instant) -> Result<Value, CuaError> {
        loop {
            let line = self
                .channel
                .receive_line(deadline, MAX_MESSAGE_BYTES)
                .await
                .map_err(|_| CuaError::fatal("channel-lost"))?;
            let answer =
                strict_json::parse(&line).map_err(|_| CuaError::fatal("protocol-invalid"))?;
            let Value::Object(members) = &answer else {
                return Err(CuaError::fatal("protocol-invalid"));
            };
            let Some((_, id)) = members.iter().find(|(name, _)| name == "id") else {
                // A notification the session does not use.
                continue;
            };
            if !eq_int(id, identifier) {
                return Err(CuaError::fatal("protocol-invalid"));
            }
            if answer.get("error").is_some() {
                return Err(CuaError::fatal("protocol-error"));
            }
            return match answer.get("result") {
                Some(result @ Value::Object(_)) => Ok(result.clone()),
                _ => Err(CuaError::fatal("protocol-error")),
            };
        }
    }
}

/// Whether a listed window is a Chromium window.
fn chromium(window: &Value) -> bool {
    matches!(window.get("app_name"), Some(Value::String(name)) if name.starts_with("Chromium"))
}

/// The failure a call that answered an error, or no structured answer, ends with.
fn failure(tool: &str, result: &Value, structured: Option<&Value>) -> CuaError {
    if ACTIONS.contains(&tool) && structured.is_some_and(refused) {
        return CuaError::refused("action-refused");
    }
    if tool == "browser_navigate" && network_failure(result) {
        return CuaError::refused("navigation-failed");
    }
    CuaError::fatal("tool-failed")
}

/// Whether the text is exactly `navigation failed: net::ERR_[A-Z0-9_]{1,64}`.
fn network_error_text(text: &str) -> bool {
    text.strip_prefix("navigation failed: net::ERR_")
        .is_some_and(|code| {
            (1..=64).contains(&code.len())
                && code
                    .bytes()
                    .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
        })
}

/// Whether the Driver says only that the page did not load for a network error, the Browser itself still sound.
///
/// The text is matched, never forwarded: the agent learns only that the navigation failed.
fn network_failure(result: &Value) -> bool {
    let Some(Value::Array(content)) = result.get("content") else {
        return false;
    };
    result.get("isError") == Some(&Value::Bool(true))
        && content.len() == 1
        && is_str(content[0].get("type"), "text")
        && matches!(content[0].get("text"), Some(Value::String(text)) if network_error_text(text))
}

/// Whether the Driver refused because its grant on the window lapsed, and for nothing else.
fn lapsed(structured: Option<&Value>) -> bool {
    if !is_str(get(structured, "status"), "refused") {
        return false;
    }
    let refusal = get(structured, "refusal");
    if !is_object(refusal) {
        return false;
    }
    let detail = get(refusal, "detail");
    let consent = is_str(get(refusal, "code"), LAPSED_CODE)
        && is_object(detail)
        && is_str(get(detail, "reason"), LAPSED_REASON);
    consent
        || (is_str(get(refusal, "code"), ENDED_CODE)
            && is_str(get(refusal, "message"), ENDED_MESSAGE))
}

/// Whether the Driver's answer names an explicit refusal of the action, which had no effect.
fn refused(structured: &Value) -> bool {
    is_str(structured.get("effect"), "refused") || is_str(structured.get("status"), "refused")
}

/// The longest prefix of `text` of at most `bytes` UTF-8 bytes that ends on a character boundary.
pub(crate) fn prefix(text: &str, bytes: usize) -> &str {
    let mut end = bytes.min(text.len());
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

/// The refs the observation offers, their lines, and its offered links, within `budget` UTF-8 bytes.
///
/// Only a ref shown is offered; a ref outside the protocol's form is never offered, since no step could name it.
fn offered(
    entries: Option<&Value>,
    mut budget: usize,
) -> (Refs, Vec<String>, Vec<(String, String)>) {
    let mut refs: Refs = Vec::new();
    let mut shown = Vec::new();
    let mut offered_links = Vec::new();
    let entries = match entries {
        Some(Value::Array(entries)) => entries.as_slice(),
        _ => &[],
    };
    for entry in entries {
        if refs.len() >= MAX_REFS || !is_object(Some(entry)) {
            break;
        }
        let (Some(Value::String(reference)), Some(Value::Array(actions))) =
            (entry.get("ref"), entry.get("actions"))
        else {
            continue;
        };
        if !REF_RE.is_full(reference) {
            continue;
        }
        let admitted: BTreeSet<&'static str> = ["click", "type", "scroll", "pointer"]
            .into_iter()
            .filter(|action| actions.iter().any(|offered| is_str(Some(offered), action)))
            .collect();
        if admitted.is_empty() {
            continue;
        }
        let role = clean(entry.get("role"), 32);
        let name = clean(entry.get("name"), MAX_NAME_CHARS);
        let actions = admitted.iter().copied().collect::<Vec<_>>().join(", ");
        let line = format!("{reference} {role} \"{name}\" ({actions})");
        // Each line costs its bytes and the newline that joins it.
        let cost = line.len() + 1;
        if cost > budget {
            break;
        }
        budget -= cost;
        if role == "link" && admitted.contains("click") {
            offered_links.push((reference.clone(), name));
        }
        if let Some(slot) = refs.iter_mut().find(|(known, _)| known == reference) {
            slot.1 = admitted;
        } else {
            refs.push((reference.clone(), admitted));
        }
        shown.push(line);
    }
    (refs, shown, offered_links)
}

/// The standard base64 alphabet.
pub(crate) const BASE64: &[u8; 64] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Padded standard base64, as `base64.b64encode` writes it.
pub(crate) fn base64(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let group = chunk
            .iter()
            .enumerate()
            .fold(0_u32, |group, (index, byte)| {
                group | (u32::from(*byte) << (16 - 8 * index))
            });
        for index in 0..4 {
            if index <= chunk.len() {
                let sextet = (group >> (18 - 6 * index)) & 0x3f;
                text.push(char::from(
                    BASE64[usize::try_from(sextet).unwrap_or_default()],
                ));
            } else {
                text.push('=');
            }
        }
    }
    text
}

/// Python's `base64.b64decode(data, validate=True)`: whole quads of the standard alphabet, padding only at the end.
fn strict_base64(data: &str) -> Option<Vec<u8>> {
    let body = data.trim_end_matches('=');
    let padding = data.len() - body.len();
    if !data.len().is_multiple_of(4) || padding > 2 {
        return None;
    }
    let mut bytes = Vec::with_capacity(body.len() * 3 / 4);
    let (mut buffer, mut bits) = (0_u32, 0_u32);
    for character in body.bytes() {
        let sextet = BASE64.iter().position(|known| *known == character)?;
        buffer = (buffer << 6) | u32::try_from(sextet).ok()?;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            bytes.push(u8::try_from((buffer >> bits) & 0xff).ok()?);
        }
    }
    Some(bytes)
}

/// The one PNG image a snapshot carries, within its bound, or `None`.
fn screenshot(content: Option<&Value>) -> Option<Vec<u8>> {
    let Some(Value::Array(items)) = content else {
        return None;
    };
    let item = items.iter().find(|item| {
        is_object(Some(item))
            && is_str(item.get("type"), "image")
            && is_str(item.get("mimeType"), "image/png")
    })?;
    let Some(Value::String(data)) = item.get("data") else {
        return None;
    };
    if data.chars().count() > MAX_SCREENSHOT_BYTES * 4 / 3 + 4 {
        return None;
    }
    let image = strict_base64(data)?;
    (image.starts_with(PNG_SIGNATURE) && image.len() <= MAX_SCREENSHOT_BYTES).then_some(image)
}

#[cfg(test)]
mod tests;
