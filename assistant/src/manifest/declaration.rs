//! Outbound hosts, Integration declarations, and Stored Input declarations with their placements (ADR-0106).

use strict_json::Value;
use team_protocol::payload::{canonical_help_url, canonical_identifier};
use team_protocol::python::value::py_eq;

use super::ManifestContract;
use crate::ManifestError;
use crate::provider::{integration_intent, resolve};
use crate::text::{display_text, public_text};

/// The most outbound hosts an Assistant declares.
pub const MAX_ALLOWED_HOSTS: usize = 32;
/// The most Integrations an Assistant declares.
pub const MAX_INTEGRATIONS: usize = 16;
/// The most Stored Inputs an Assistant declares.
pub const MAX_STORED_INPUTS: usize = 8;
/// Fields Team owns in every provider call, so no Stored Input is ever placed in one (ADR-0106).
pub const RESERVED_HEADERS: [&str; 12] = [
    "accept-encoding",
    "connection",
    "content-length",
    "expect",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];
const NON_PUBLIC_SUFFIXES: [&str; 11] = [
    ".arpa",
    ".example",
    ".home",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
];
const REQUIRED_FIELDS: [&str; 6] = ["kind", "label", "description", "help_url", "host", "routes"];
const FIELDS: [&str; 10] = [
    "kind",
    "label",
    "description",
    "help_url",
    "host",
    "header",
    "query",
    "scheme",
    "hmac",
    "routes",
];
const LABEL_CHARS: usize = 80;

/// Public provider intent for one controller-owned Integration.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct IntegrationDeclaration {
    /// The Integration id (a reviewed provider id).
    pub id: String,
    /// The provider.
    pub provider: String,
    /// The reviewed scopes, sorted.
    pub scopes: Vec<String>,
}

/// Public intent for one controller-custodied persistent Action input.
#[derive(Clone, Debug)]
pub struct StoredInputDeclaration {
    /// Its id.
    pub id: String,
    /// Always `password`.
    pub kind: String,
    /// The label.
    pub label: String,
    /// The English help text a person reads before entering the value.
    pub description: String,
    /// The official page where a person creates or finds the value.
    pub help_url: String,
    /// The one host that receives the value.
    pub host: String,
    /// The header Team places the value in.
    pub header: Option<String>,
    /// Or the query parameter Team places it in.
    pub query: Option<String>,
    /// An optional header scheme.
    pub scheme: Option<String>,
    /// An optional HMAC proof over another Stored Input's value.
    pub hmac: Option<String>,
    /// The only endpoints on its host that ever receive the value, exactly as declared.
    pub routes: Value,
}

impl PartialEq for StoredInputDeclaration {
    fn eq(&self, other: &Self) -> bool {
        (
            &self.id,
            &self.kind,
            &self.label,
            &self.description,
            &self.help_url,
        ) == (
            &other.id,
            &other.kind,
            &other.label,
            &other.description,
            &other.help_url,
        ) && self.placement_eq(other)
    }
}

impl Eq for StoredInputDeclaration {}

impl StoredInputDeclaration {
    /// Whether the placement (host, field, scheme, proof, and routes) equals `other`'s.
    #[must_use]
    pub fn placement_eq(&self, other: &Self) -> bool {
        (
            &self.host,
            &self.header,
            &self.query,
            &self.scheme,
            &self.hmac,
        ) == (
            &other.host,
            &other.header,
            &other.query,
            &other.scheme,
            &other.hmac,
        ) && py_eq(&self.routes, &other.routes)
    }

    /// The closed declaration fields after its id, as manifests, resolutions, and records carry them.
    #[must_use]
    pub fn metadata(&self) -> Vec<(String, Value)> {
        let text = |value: &String| Value::String(value.clone());
        let mut fields = vec![
            ("kind".to_owned(), text(&self.kind)),
            ("label".to_owned(), text(&self.label)),
            ("description".to_owned(), text(&self.description)),
            ("help_url".to_owned(), text(&self.help_url)),
            ("host".to_owned(), text(&self.host)),
        ];
        for (name, value) in [
            ("header", &self.header),
            ("query", &self.query),
            ("scheme", &self.scheme),
            ("hmac", &self.hmac),
        ] {
            if let Some(value) = value {
                fields.push((name.to_owned(), text(value)));
            }
        }
        fields.push(("routes".to_owned(), self.routes.clone()));
        fields
    }

    /// One declaration as a resolution or Local record lists it: its id, then [`Self::metadata`].
    #[must_use]
    pub fn document(&self) -> Value {
        let mut fields = vec![("id".to_owned(), Value::String(self.id.clone()))];
        fields.extend(self.metadata());
        Value::Object(fields)
    }
}

/// The host and case-folded header, or exact query parameter, one Stored Input occupies.
#[must_use]
pub fn placement_field(declaration: &StoredInputDeclaration) -> (String, &'static str, String) {
    match &declaration.header {
        Some(header) => (declaration.host.clone(), "header", header.to_lowercase()),
        None => (
            declaration.host.clone(),
            "query",
            declaration.query.clone().unwrap_or_default(),
        ),
    }
}

/// One public DNS name of two or more lowercase labels whose last label starts with a letter.
fn public_host(host: &str) -> bool {
    let labels: Vec<&str> = host.split('.').collect();
    let label = |part: &str| {
        let bytes = part.as_bytes();
        (1..=63).contains(&bytes.len())
            && bytes
                .iter()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
            && bytes[0] != b'-'
            && bytes[bytes.len() - 1] != b'-'
    };
    (1..=253).contains(&host.len())
        && labels.len() >= 2
        && labels.iter().all(|part| label(part))
        && labels
            .last()
            .is_some_and(|last| last.as_bytes()[0].is_ascii_lowercase())
        && !NON_PUBLIC_SUFFIXES
            .iter()
            .any(|suffix| host.ends_with(suffix))
}

/// One deterministic sorted list of exact public DNS host names.
///
/// # Errors
/// [`ManifestError`] for any other value.
pub fn canonical_allowed_hosts(value: &Value) -> Result<Vec<String>, ManifestError> {
    let refused = ManifestError::Invalid("allowed_hosts");
    let Value::Array(items) = value else {
        return Err(refused);
    };
    if items.len() > MAX_ALLOWED_HOSTS {
        return Err(refused);
    }
    let mut hosts: Vec<String> = Vec::with_capacity(items.len());
    for item in items {
        match item {
            Value::String(host) if public_host(host) && !hosts.contains(host) => {
                hosts.push(host.clone());
            }
            _ => return Err(refused),
        }
    }
    hosts.sort();
    Ok(hosts)
}

fn identifier<'a>(value: &'a str, kind: &'static str) -> Result<&'a str, ManifestError> {
    canonical_identifier(&Value::String(value.to_owned()))
        .map(|_| value)
        .ok_or(ManifestError::Invalid(kind))
}

/// Integrations whose ids are controller-reviewed provider ids, sorted.
///
/// # Errors
/// [`ManifestError`] for more than [`MAX_INTEGRATIONS`], an unknown provider, or unreviewed scopes.
pub fn canonical_integration_declarations(
    declarations: &[(String, Value)],
) -> Result<Vec<IntegrationDeclaration>, ManifestError> {
    if declarations.len() > MAX_INTEGRATIONS {
        return Err(ManifestError::Invalid("integration declarations"));
    }
    let mut admitted = declarations
        .iter()
        .map(|(id, scopes)| {
            let id = identifier(id, "integration identifier")?;
            let (provider, scopes) = integration_intent(&Value::String(id.to_owned()), scopes)
                .map_err(|_| ManifestError::Invalid("integration declaration"))?;
            Ok(IntegrationDeclaration {
                id: id.to_owned(),
                provider: provider.id.to_owned(),
                scopes,
            })
        })
        .collect::<Result<Vec<_>, ManifestError>>()?;
    admitted.sort();
    Ok(admitted)
}

/// Bounded Stored Input declarations and their placements, sorted, never accepting a credential value.
///
/// # Errors
/// [`ManifestError`] for any refused declaration or placement.
pub fn canonical_stored_input_declarations(
    declarations: &[(String, Value)],
    allowed_hosts: &[String],
) -> Result<Vec<StoredInputDeclaration>, ManifestError> {
    if declarations.len() > MAX_STORED_INPUTS {
        return Err(ManifestError::Invalid("Stored Input declarations"));
    }
    let mut admitted = declarations
        .iter()
        .map(|(id, metadata)| stored_input(id, metadata, allowed_hosts))
        .collect::<Result<Vec<_>, _>>()?;
    require_distinct_placements(&admitted)?;
    admitted.sort_by(|left, right| left.id.cmp(&right.id));
    Ok(admitted)
}

fn optional<'a>(members: &'a [(String, Value)], key: &str) -> Option<&'a Value> {
    members
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
        .filter(|value| **value != Value::Null)
}

fn stored_input(
    id: &str,
    metadata: &Value,
    allowed_hosts: &[String],
) -> Result<StoredInputDeclaration, ManifestError> {
    let id = identifier(id, "Stored Input identifier")?;
    let Value::Object(members) = metadata else {
        return Err(ManifestError::Invalid("Stored Input declaration"));
    };
    let has = |key: &str| members.iter().any(|(name, _)| name == key);
    if !REQUIRED_FIELDS.iter().all(|key| has(key))
        || !members
            .iter()
            .all(|(name, _)| FIELDS.contains(&name.as_str()))
    {
        return Err(ManifestError::Invalid("Stored Input declaration"));
    }
    let get = |key: &str| metadata.get(key).unwrap_or(&Value::Null);
    if !py_eq(get("kind"), &Value::String("password".to_owned())) {
        return Err(ManifestError::Invalid("Stored Input kind"));
    }
    let help_url = canonical_help_url(get("help_url"))
        .ok_or(ManifestError::Invalid("Stored Input help_url"))?;
    let label = public_text(get("label"), "Stored Input label", LABEL_CHARS)?;
    let description = display_text(
        get("description"),
        "Stored Input description",
        assistant_protocol::catalog::DESCRIPTION_CHARS,
    )?;
    let mut declaration = placement(members, allowed_hosts)?;
    id.clone_into(&mut declaration.id);
    label.clone_into(&mut declaration.label);
    description.clone_into(&mut declaration.description);
    help_url.clone_into(&mut declaration.help_url);
    Ok(declaration)
}

fn grammar(
    value: &str,
    maximum: usize,
    first: impl Fn(u8) -> bool,
    rest: impl Fn(u8) -> bool,
) -> bool {
    let bytes = value.as_bytes();
    (1..=maximum).contains(&bytes.len())
        && first(bytes[0])
        && bytes[1..].iter().all(|byte| rest(*byte))
}

fn header_name(value: &str) -> bool {
    let token = |byte: u8| byte.is_ascii_alphanumeric() || b"!#$%&'*+.^_`|~-".contains(&byte);
    grammar(value, 64, token, token)
}

fn query_name(value: &str) -> bool {
    let unreserved = |byte: u8| byte.is_ascii_alphanumeric() || b"._~-".contains(&byte);
    grammar(value, 64, unreserved, unreserved)
}

fn scheme_name(value: &str) -> bool {
    grammar(
        value,
        32,
        |byte| byte.is_ascii_alphabetic(),
        |byte| byte.is_ascii_alphanumeric() || byte == b'-',
    )
}

fn text_field(
    value: Option<&Value>,
    valid: impl Fn(&str) -> bool,
) -> Result<Option<&str>, ManifestError> {
    match value {
        None => Ok(None),
        Some(Value::String(text)) if valid(text) => Ok(Some(text)),
        Some(_) => Err(ManifestError::Invalid("Stored Input placement")),
    }
}

/// An allowed host, exactly one field Team does not own there, and reviewed routes kept exactly as declared.
fn placement(
    members: &[(String, Value)],
    allowed_hosts: &[String],
) -> Result<StoredInputDeclaration, ManifestError> {
    let refused = ManifestError::Invalid("Stored Input placement");
    let host = match optional(members, "host") {
        Some(Value::String(host)) if allowed_hosts.contains(host) => host.clone(),
        _ => return Err(refused),
    };
    let header = text_field(optional(members, "header"), |name| {
        header_name(name) && !RESERVED_HEADERS.contains(&name.to_lowercase().as_str())
    })?;
    let query = text_field(optional(members, "query"), query_name)?;
    let scheme = text_field(optional(members, "scheme"), scheme_name)?;
    let hmac = text_field(optional(members, "hmac"), |name| {
        canonical_identifier(&Value::String(name.to_owned())).is_some()
    })?;
    let routes = optional(members, "routes").cloned().unwrap_or(Value::Null);
    if header.is_none() == query.is_none()
        || (scheme.is_some() && header.is_none())
        || assistant_protocol::route::routes_error(&routes).is_some()
    {
        return Err(refused);
    }
    let owned = |value: Option<&str>| value.map(str::to_owned);
    Ok(StoredInputDeclaration {
        id: String::new(),
        kind: "password".to_owned(),
        label: String::new(),
        description: String::new(),
        help_url: String::new(),
        host,
        header: owned(header),
        query: owned(query),
        scheme: owned(scheme),
        hmac: owned(hmac),
        routes,
    })
}

/// No two values share a field on one host, and a proof signs exactly one plain Stored Input of its own host.
fn require_distinct_placements(
    declarations: &[StoredInputDeclaration],
) -> Result<(), ManifestError> {
    for declaration in declarations {
        let Some(signed) = &declaration.hmac else {
            continue;
        };
        let target = declarations
            .iter()
            .rev()
            .find(|candidate| &candidate.id == signed);
        let valid = target.is_some_and(|target| {
            !std::ptr::eq(target, declaration)
                && target.hmac.is_none()
                && target.host == declaration.host
        });
        if !valid {
            return Err(ManifestError::Invalid("Stored Input proof"));
        }
    }
    let fields: Vec<_> = declarations.iter().map(placement_field).collect();
    if fields
        .iter()
        .enumerate()
        .any(|(index, field)| fields[..index].contains(field))
    {
        return Err(ManifestError::Invalid("Stored Input placements overlap"));
    }
    Ok(())
}

/// The declaration list a resolution or binding carries, each entry its id plus its closed fields.
///
/// # Errors
/// [`ManifestError`] for an entry without a string id, or any refused declaration.
pub fn stored_input_declarations_from_documents(
    documents: &[Value],
    allowed_hosts: &[String],
) -> Result<Vec<StoredInputDeclaration>, ManifestError> {
    let mut declarations: Vec<(String, Value)> = Vec::new();
    for document in documents {
        let Some(Value::String(id)) = document.get("id") else {
            return Err(ManifestError::Invalid("Stored Input declaration"));
        };
        let Value::Object(members) = document else {
            return Err(ManifestError::Invalid("Stored Input declaration"));
        };
        let fields = Value::Object(
            members
                .iter()
                .filter(|(name, _)| name != "id")
                .cloned()
                .collect(),
        );
        match declarations.iter_mut().find(|(existing, _)| existing == id) {
            Some(entry) => entry.1 = fields,
            None => declarations.push((id.clone(), fields)),
        }
    }
    canonical_stored_input_declarations(&declarations, allowed_hosts)
}

/// One deterministic contract for package and reviewed registry comparison.
///
/// # Errors
/// [`ManifestError`] for any refused part, or a Stored Input in the `Authorization` header an Integration bearer owns.
pub fn canonical_manifest_contract(
    allowed_hosts: &Value,
    integrations: &[(String, Value)],
    stored_inputs: &[(String, Value)],
) -> Result<ManifestContract, ManifestError> {
    let integrations = canonical_integration_declarations(integrations)?;
    let allowed_hosts = canonical_allowed_hosts(allowed_hosts)?;
    let stored_inputs = canonical_stored_input_declarations(stored_inputs, &allowed_hosts)?;
    let bearer_hosts: Vec<&str> = integrations
        .iter()
        .filter_map(|integration| resolve(&Value::String(integration.provider.clone())).ok())
        .flat_map(|provider| provider.api_hosts.iter().copied())
        .collect();
    let owned = stored_inputs.iter().any(|item| {
        let (host, kind, name) = placement_field(item);
        kind == "header" && name == "authorization" && bearer_hosts.contains(&host.as_str())
    });
    if owned {
        return Err(ManifestError::Invalid("Stored Input placements overlap"));
    }
    Ok(ManifestContract {
        allowed_hosts,
        integrations,
        stored_inputs,
    })
}

/// Whether a successor stays within the installed outbound-host and credential-placement envelope: a retained
/// Stored Input keeps its value only while its value keeps its exact placement (ADR-0106).
#[must_use]
pub fn automatic_update_preserves_egress(
    previous: &ManifestContract,
    successor: &ManifestContract,
) -> bool {
    successor
        .allowed_hosts
        .iter()
        .all(|host| previous.allowed_hosts.contains(host))
        && successor.stored_inputs.iter().all(|item| {
            previous
                .stored_inputs
                .iter()
                .find(|retained| retained.id == item.id)
                .is_none_or(|retained| retained.placement_eq(item))
        })
}

/// The controller-owned registry's declarations, normalized again without trusting package input.
///
/// # Errors
/// [`ManifestError`] when an Integration's provider differs from its id, or any part is refused.
pub fn reviewed_manifest_contract(
    allowed_hosts: &Value,
    integrations: &[IntegrationDeclaration],
    stored_inputs: &[StoredInputDeclaration],
) -> Result<ManifestContract, ManifestError> {
    if integrations
        .iter()
        .any(|integration| integration.id != integration.provider)
    {
        return Err(ManifestError::Invalid("reviewed integration provider"));
    }
    let integration_scopes: Vec<(String, Value)> = integrations
        .iter()
        .map(|integration| {
            let scopes = integration
                .scopes
                .iter()
                .map(|scope| Value::String(scope.clone()))
                .collect();
            (integration.id.clone(), Value::Array(scopes))
        })
        .collect();
    let declarations: Vec<(String, Value)> = stored_inputs
        .iter()
        .map(|stored_input| {
            (
                stored_input.id.clone(),
                Value::Object(stored_input.metadata()),
            )
        })
        .collect();
    canonical_manifest_contract(allowed_hosts, &integration_scopes, &declarations)
}
