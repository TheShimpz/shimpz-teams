use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::time::Duration;

use egress_transport::audit::SystemDisk;
use egress_transport::fixtures::{
    FixedStamp, client_hello, exchange, pair, public, read_all, transport,
};
use egress_transport::handler::handle;
use egress_transport::{AuditWriter, Decision, Envelope, Fields, Outcome, Profile, Target, refuse};

use super::{
    Browser, LIMITS, PORT, REJECTED, SERVICE, Setup, Startup, event, probe, publish, run, start,
};

const BROWSER: &str = "10.231.0.10:40000";
const GENERATION: &str = "0123456789abcdef0123456789abcdef";

/// A private 0700 directory removed at drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new(name: &str) -> Self {
        let path =
            std::env::temp_dir().join(format!("browser-egress-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir(&path).expect("scratch");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).expect("mode");
        Self(path)
    }

    fn writer(&self) -> AuditWriter {
        AuditWriter::with_parts(
            self.0.join("audit.jsonl"),
            SERVICE,
            Box::new(FixedStamp::default()),
            Box::new(SystemDisk),
        )
    }

    fn lines(&self) -> Vec<String> {
        fs::read_to_string(self.0.join("audit.jsonl"))
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn browser(&self) -> Browser {
        Browser::new(self.writer(), GENERATION.to_owned())
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// The CONNECT Chromium sends through its proxy, headers included.
fn request(target: &str) -> Vec<u8> {
    format!(
        "CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Connection: keep-alive\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64)\r\n\r\n"
    )
    .into_bytes()
}

fn client() -> Fields {
    let mut fields = Fields::new();
    fields.insert("client", "10.231.0.10".to_owned());
    fields
}

#[test]
fn the_envelope_is_pinned() {
    let scratch = Scratch::new("envelope");
    let envelope = Envelope {
        idle_timeout: Duration::from_secs(120),
        request_limit: 4096,
        ..Envelope::default()
    };
    assert_eq!(scratch.browser().envelope(), envelope);
    assert_eq!(PORT, 3128);
    assert_eq!(
        (
            LIMITS.concurrency,
            LIMITS.source_concurrency,
            LIMITS.backlog
        ),
        (96, 96, 96)
    );
}

#[test]
fn admission_takes_a_public_hostname_on_443_in_canonical_form() {
    let scratch = Scratch::new("admit");
    let browser = scratch.browser();
    let ip = public(BROWSER).ip();
    for (target, host) in [
        ("example.com:443", "example.com"),
        ("WWW.Gov.BR:443", "www.gov.br"),
    ] {
        assert_eq!(
            browser.admit(&request(target), ip),
            Ok(Target::with_fields(host, 443, client()))
        );
    }
}

#[test]
fn admission_refuses_ports_literals_and_other_methods_without_naming_them() {
    let scratch = Scratch::new("refuse");
    let browser = scratch.browser();
    let ip = public(BROWSER).ip();
    let destination = refuse(403, "destination-rejected", REJECTED, client());
    for target in [
        "example.com:80",
        "93.184.216.34:443",
        "0x7f.1:443",
        "localhost:443",
        "example.com.:443",
    ] {
        assert_eq!(
            browser.admit(&request(target), ip),
            Err(destination.clone()),
            "{target}"
        );
    }
    let mut trailing = request("example.com:443");
    trailing.extend_from_slice(b"\x16\x03\x01");
    for (payload, code) in [
        (b"GET https://example.com/ HTTP/1.1\r\n\r\n".to_vec(), 405),
        (b"CONNECT example.com HTTP/1.1\r\n\r\n".to_vec(), 400),
        (trailing, 400),
    ] {
        assert_eq!(
            browser.admit(&payload, ip),
            Err(refuse(code, "request-rejected", REJECTED, client()))
        );
    }
}

#[test]
fn an_admitted_tunnel_is_recorded_with_the_browser_address_and_the_generation() {
    let scratch = Scratch::new("tunnel");
    let browser = scratch.browser();
    let upstream = TcpListener::bind("127.0.0.1:0").expect("bind");
    let (transport, _) = transport(
        vec![public("1.1.1.1:443")],
        upstream.local_addr().expect("address"),
        Vec::new(),
    );
    let (server, mut client) = pair();
    client
        .write_all(&request("example.com:443"))
        .expect("request");
    let worker = std::thread::spawn(move || handle(&browser, &transport, server, public(BROWSER)));
    let mut status = [0u8; 39];
    client.read_exact(&mut status).expect("200");
    let flight = client_hello("example.com");
    client.write_all(&flight).expect("hello");
    let (mut site, _) = upstream.accept().expect("upstream");
    site.set_read_timeout(Some(Duration::from_secs(10)))
        .expect("timeout");
    let mut received = vec![0u8; flight.len()];
    site.read_exact(&mut received).expect("relayed");
    assert_eq!(received, flight);
    drop(client);
    worker.join().expect("handled");
    let lines = scratch.lines();
    assert_eq!(lines.len(), 1);
    assert!(lines[0].starts_with(&format!(
        "{{\"client\":\"10.231.0.10\",\"code\":200,\"generation\":\"{GENERATION}\",\"operation\":\"connect\",\
         \"prev_sha256\":\"{}\",\"principal_class\":\"machine\",\"principal_id\":\"browser\",\"probe\":false,\
         \"reason\":\"allowed\",\"result\":\"ok\",\"service\":\"browser-egress\",\"subject\":\"example.com:443\"",
        "0".repeat(64)
    )));
}

#[test]
fn a_fronted_name_and_a_refused_literal_record_no_request_material() {
    let scratch = Scratch::new("fronted");
    let browser = scratch.browser();
    let upstream = TcpListener::bind("127.0.0.1:0").expect("bind");
    let (transport, _) = transport(
        vec![public("1.1.1.1:443")],
        upstream.local_addr().expect("address"),
        Vec::new(),
    );
    let (server, mut client) = pair();
    client
        .write_all(&request("example.com:443"))
        .expect("request");
    let worker = std::thread::spawn(move || {
        handle(&browser, &transport, server, public(BROWSER));
        (browser, transport)
    });
    let mut status = [0u8; 39];
    client.read_exact(&mut status).expect("200");
    client
        .write_all(&client_hello("attacker.example"))
        .expect("hello");
    assert_eq!(read_all(&mut client), b"");
    let (browser, transport) = worker.join().expect("handled");
    let response = exchange(
        &browser,
        &transport,
        &request("10.0.0.1:443"),
        public(BROWSER),
    );
    assert!(response.starts_with(b"HTTP/1.1 403"));
    let lines = scratch.lines();
    assert!(
        lines[1].contains("\"reason\":\"sni-mismatch\"")
            && lines[1].contains("\"subject\":\"example.com:443\"")
    );
    assert!(lines[2].contains("\"subject\":\"rejected-target\""));
    let text = lines.join("\n");
    for material in ["Mozilla", "attacker.example", "10.0.0.1"] {
        assert!(!text.contains(material), "{material}");
    }
}

#[test]
fn a_private_answer_is_refused_before_any_upstream_connection() {
    let scratch = Scratch::new("private");
    let browser = scratch.browser();
    let (transport, connector) = transport(
        vec![public("10.201.0.1:443")],
        public("127.0.0.1:9"),
        Vec::new(),
    );
    let response = exchange(
        &browser,
        &transport,
        &request("internal.example:443"),
        public(BROWSER),
    );
    assert!(response.starts_with(b"HTTP/1.1 403"));
    assert_eq!(connector.attempts.lock().expect("attempts").len(), 0);
    let line = &scratch.lines()[0];
    assert!(
        line.contains("\"reason\":\"destination-rejected\"")
            && line.contains("\"subject\":\"internal.example:443\"")
    );
}

#[test]
fn the_audit_admits_only_its_bounded_schema() {
    let ok = |subject: &str, fields: Fields| Decision {
        result: Outcome::Ok,
        ..refuse(200, "allowed", subject, fields)
    };
    let mut named = Fields::new();
    named.insert("client", "browser".to_owned());
    let mut extra = client();
    extra.insert("path", "/x".to_owned());
    for decision in [
        ok("example.com:80", client()),
        ok("not-evaluated", client()),
        ok(":443", client()),
        ok("example.com:443", Fields::new()),
        ok("example.com:443", named),
        ok("example.com:443", extra),
    ] {
        assert!(event(&decision, GENERATION).is_err(), "{decision:?}");
    }
    assert!(event(&ok("example.com:443", client()), GENERATION).is_ok());
}

#[test]
fn the_generation_is_published_whole_and_owner_only_before_the_listener() {
    let scratch = Scratch::new("publish");
    let path = scratch.0.join("generation");
    publish(&path, GENERATION).expect("published");
    publish(&path, GENERATION).expect("published again");
    assert_eq!(fs::read_to_string(&path).expect("generation"), GENERATION);
    assert_eq!(
        fs::metadata(&path)
            .expect("generation")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let names: Vec<String> = fs::read_dir(&scratch.0)
        .expect("listing")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    assert_eq!(names, ["generation"]);
    assert!(publish(&scratch.0.join("missing").join("generation"), GENERATION).is_err());
}

#[test]
fn the_healthcheck_names_the_listening_generation() {
    let scratch = Scratch::new("probe");
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let port = listener.local_addr().expect("address").port();
    let path = scratch.0.join("generation");
    assert_eq!(probe(port, &path), None);
    for (content, expected) in [
        (&b"not-a-generation"[..], None),
        (b"\xff", None),
        (&[b'A'; 32], None),
        (&[b'a'; 32], Some("a".repeat(32))),
    ] {
        fs::write(&path, content).expect("generation");
        assert_eq!(probe(port, &path), expected);
    }
    drop(listener);
    assert_eq!(probe(port, &path), None);
}

fn setup(scratch: &Scratch, port: u16) -> Setup {
    let (transport, _) = transport(Vec::new(), public("127.0.0.1:9"), Vec::new());
    Setup {
        audit: scratch.writer(),
        generation_path: scratch.0.join("generation"),
        port,
        transport,
    }
}

#[test]
fn startup_checks_custody_publishes_the_generation_then_binds() {
    let scratch = Scratch::new("startup");
    let server = start(setup(&scratch, 0)).expect("starts");
    let port = server.listener.local_addr().expect("address").port();
    let published = probe(port, &scratch.0.join("generation")).expect("a generation");
    assert_eq!(start(setup(&scratch, port)).err(), Some(Startup::Listen));
    assert_ne!(probe(port, &scratch.0.join("generation")), Some(published));
    let mut unpublished = setup(&scratch, 0);
    unpublished.generation_path = scratch.0.join("missing").join("generation");
    assert_eq!(start(unpublished).err(), Some(Startup::Generation));
    fs::set_permissions(&scratch.0, fs::Permissions::from_mode(0o750)).expect("mode");
    assert_eq!(start(setup(&scratch, 0)).err(), Some(Startup::Custody));
    assert_eq!(run(setup(&scratch, 0)), 1);
    fs::set_permissions(&scratch.0, fs::Permissions::from_mode(0o700)).expect("mode");
    for (startup, message) in [
        (
            Startup::Custody,
            "audit custody is unavailable; refusing to start",
        ),
        (
            Startup::Generation,
            "the generation cannot be published; refusing to start",
        ),
        (
            Startup::Listen,
            "the listener is unavailable; refusing to start",
        ),
    ] {
        assert_eq!(startup.to_string(), message);
    }
}

#[test]
fn a_started_profile_serves_until_the_process_ends() {
    let scratch = Scratch::new("serve");
    let port = TcpListener::bind("127.0.0.1:0")
        .expect("bind")
        .local_addr()
        .expect("address")
        .port();
    let serving = setup(&scratch, port);
    std::thread::spawn(move || run(serving));
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while probe(port, &scratch.0.join("generation")).is_none() {
        assert!(
            std::time::Instant::now() < deadline,
            "the profile never listened"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(super::main(), 1);
}
