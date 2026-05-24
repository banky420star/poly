use anyhow::{bail, Context, Result};
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde::Deserialize;
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, Deserialize)]
pub struct ApiCredentials {
    pub api_key: String,
    pub secret: String,
    pub passphrase: String,
}

#[derive(Debug, Clone)]
pub struct SigningClient {
    creds: ApiCredentials,
    http: Client,
    address: String,
}

impl SigningClient {
    pub fn from_env() -> Result<Self> {
        let api_key = std::env::var("POLY_API_KEY").context("POLY_API_KEY not set")?;
        let secret = std::env::var("POLY_API_SECRET").context("POLY_API_SECRET not set")?;
        let passphrase =
            std::env::var("POLY_PASSPHRASE").context("POLY_PASSPHRASE not set")?;

        Ok(Self {
            creds: ApiCredentials {
                api_key,
                secret,
                passphrase,
            },
            http: Client::builder().build()?,
            address: String::new(),
        })
    }

    pub fn credentials(&self) -> &ApiCredentials {
        &self.creds
    }

    /// Derive L2 signature for authenticated CLOB requests.
    /// Signs the request payload using HMAC-SHA256 with the API secret.
    pub fn sign_l2(&self, timestamp: i64, method: &str, path: &str, body: &str) -> String {
        let message = format!("{timestamp}{method}{path}{body}");
        let mut mac = HmacSha256::new_from_slice(self.creds.secret.as_bytes())
            .expect("HMAC key length is valid");
        mac.update(message.as_bytes());
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }

    /// Build the L2 authentication headers for a CLOB request.
    pub fn auth_headers(
        &self,
        timestamp: i64,
        method: &str,
        path: &str,
        body: &str,
    ) -> Vec<(&str, String)> {
        let signature = self.sign_l2(timestamp, method, path, body);
        vec![
            ("POLY_API_KEY", self.creds.api_key.clone()),
            ("POLY_SIGNATURE", signature),
            ("POLY_TIMESTAMP", timestamp.to_string()),
            ("POLY_PASSPHRASE", self.creds.passphrase.clone()),
        ]
    }
}

/// Derive API credentials from a private key (L1 auth).
/// This creates new API keys using the wallet's private key.
/// Kept as infrastructure for future live trading activation.
pub async fn derive_api_key(
    _clob_url: &str,
    _private_key: &str,
) -> Result<ApiCredentials> {
    bail!(
        "L1 credential derivation not yet implemented. \
         Set POLY_API_KEY, POLY_API_SECRET, and POLY_PASSPHRASE in .env instead."
    )
}