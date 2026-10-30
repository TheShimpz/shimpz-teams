//! Team Integrations (ADR-0123, plan phase R7, workstream W5): the broker client through the Account egress, PKCE,
//! the challenge store, the encrypted credential store, and the authorization flow. It replaces `integrations/`.
//!
//! Team never holds an OAuth Client Secret: the platform broker on shimpz.com does. Team binds each grant with PKCE
//! S256, seals the token set it receives in its own encrypted store, and hands an Action's provider calls the bearer
//! only through its provider broker, never to the Assistant.

pub mod broker;
pub mod capability;
pub mod challenge;
pub mod https;
pub mod pending;
pub mod pkce;
pub mod providers;
pub mod service;
pub mod store;
pub mod text;
pub mod token;
