/// Live order executor for Polymarket CLOB.
///
/// Bridges the bot's `QuoteIntent` objects to real signed orders on the
/// Polymarket Central Limit Order Book using the official Rust SDK.
use crate::paper::QuoteIntent;
use anyhow::{Context, Result};
use rust_decimal::Decimal;
use std::str::FromStr;
use tracing::{error, info, debug};
use std::time::{SystemTime, UNIX_EPOCH};

use polymarket_client_sdk_v2::auth::{LocalSigner, Signer, Normal};
use polymarket_client_sdk_v2::auth::state::Authenticated;
use polymarket_client_sdk_v2::clob::{Client, Config};
use polymarket_client_sdk_v2::clob::types::{AssetType, Side, SignatureType};
use polymarket_client_sdk_v2::clob::types::request::BalanceAllowanceRequest;
use hmac::Mac;

/// Result of placing an order — tells the caller whether it actually filled.
pub struct OrderResult {
    pub order_id: String,
    pub filled: bool,
    /// Number of shares filled (not USDC notional).
    pub shares_filled: Decimal,
}

/// The live executor. Handles L2 auth, order placement, and heartbeat.
pub struct LiveExecutor {
    pub client: Client<Authenticated<Normal>>,
    pub http: reqwest::Client,
    pub clob_base_url: String,
    pub funder_address: String,
    pub max_order_usdc: Decimal,
    pub orders_placed: u64,
    pub orders_failed: u64,
    private_key: String,
}

impl LiveExecutor {
    /// Create a new executor from environment variables.
    pub async fn from_env(clob_base_url: &str, max_order_usdc: Decimal) -> Result<Self> {
        let private_key = std::env::var("PRIVATE_KEY").context("PRIVATE_KEY not set")?;
        let funder_address =
            std::env::var("DEPOSIT_WALLET_ADDRESS").context("DEPOSIT_WALLET_ADDRESS not set")?;

        // Validate private key
        let pk_hex = private_key.strip_prefix("0x").unwrap_or(&private_key);
        if pk_hex.len() != 64 {
            anyhow::bail!(
                "PRIVATE_KEY must be 32 bytes (64 hex chars). Got {} chars. \
                 This looks like an address, not a private key.",
                pk_hex.len()
            );
        }

        let signer = LocalSigner::from_str(&private_key)?
            .with_chain_id(Some(137)); // 137 = Polygon Mainnet
        let signer_address = format!("0x{}", hex::encode(signer.address()));

        info!(
            signer = %signer_address,
            funder = %funder_address,
            "Authenticating with EOA→proxy wallet pair"
        );

        // Let SDK derive credentials and auto-derive proxy wallet
        let client = Client::new(clob_base_url, Config::default())?
            .authentication_builder(&signer)
            .signature_type(SignatureType::Proxy)
            .authenticate()
            .await?;

        // Log the actual credentials being used
        let actual_creds = client.credentials();
        info!(
            api_key = %actual_creds.key(),
            client_address = %client.address(),
            "Post-auth: SDK-derived API key and client address"
        );

        info!(
            funder = %funder_address,
            max_order = %max_order_usdc,
            "LiveExecutor initialized successfully"
        );

        Ok(Self {
            client,
            http: reqwest::Client::new(),
            clob_base_url: clob_base_url.trim_end_matches('/').to_string(),
            funder_address: funder_address.to_string(),
            max_order_usdc,
            orders_placed: 0,
            orders_failed: 0,
            private_key,
        })
    }

    /// Place a limit order and check whether it filled immediately.
    /// Returns OrderResult with fill status. The caller should only record
    /// a paper fill when `filled == true`.
    /// size_usdc is USDC notional. Risk engine validates CLOB minimums before
    /// this point, so we convert directly to shares and place the order.
    pub async fn execute_order(&mut self, quote: &QuoteIntent) -> Result<OrderResult> {
        let notional = quote.size_usdc.min(self.max_order_usdc);
        let shares = (notional / quote.price).round_dp(6);

        let side = if quote.side.to_uppercase() == "BUY" {
            Side::Buy
        } else {
            Side::Sell
        };

        info!(
            token_id = &quote.token_id[..16],
            outcome = %quote.outcome,
            side = %quote.side,
            price = %quote.price,
            shares = %shares,
            notional_usdc = %notional,
            "LIVE ORDER → placing limit order"
        );

        let post_only = matches!(quote.order_type, crate::paper::OrderType::Maker);
        let order = self.client.limit_order()
            .token_id(quote.token_id.parse()?)
            .price(quote.price.to_string().parse()?)
            .size(shares.to_string().parse()?)
            .side(side)
            .post_only(post_only) // Maker = 0% fee (arb), Taker = fills now (directional)
            .build()
            .await?;

        let signer = LocalSigner::from_str(&self.private_key)?
            .with_chain_id(Some(137));
        let signed_order = self.client.sign(&signer, order).await?;
        let response = self.client.post_order(signed_order).await;

        match response {
            Ok(resp) => {
                self.orders_placed += 1;
                let order_id = resp.order_id.clone();
                info!(order_id = %order_id, "LIVE ORDER ✓ accepted, checking fill...");

                // Poll for fill status — limit orders may match instantly
                // if they cross the spread, or sit on the book if not.
                match self.check_fill(&order_id).await {
                    Ok(filled_amount) => {
                        if filled_amount > Decimal::ZERO {
                            info!(order_id = %order_id, filled = %filled_amount, "LIVE ORDER ✓ filled");
                            Ok(OrderResult {
                                order_id,
                                filled: true,
                                shares_filled: filled_amount,
                            })
                        } else {
                            debug!(order_id = %order_id, "LIVE ORDER — resting on book (not yet filled)");
                            Ok(OrderResult {
                                order_id,
                                filled: false,
                                shares_filled: Decimal::ZERO,
                            })
                        }
                    }
                    Err(e) => {
                        // If we can't check fill status, assume not filled
                        debug!(order_id = %order_id, error = %e, "LIVE ORDER — fill check failed, assuming unfilled");
                        Ok(OrderResult {
                            order_id,
                            filled: false,
                            shares_filled: Decimal::ZERO,
                        })
                    }
                }
            }
            Err(e) => {
                self.orders_failed += 1;
                error!(error = %e, "LIVE ORDER ✗ failed");
                anyhow::bail!("order failed: {}", e)
            }
        }
    }

    /// Check whether an order has been filled. Returns the matched size.
    async fn check_fill(&self, order_id: &str) -> Result<Decimal> {
        let order = self.client.order(order_id).await?;
        let matched = order.size_matched;
        Ok(matched)
    }

    /// Cancel a single order by ID.
    pub async fn cancel_order(&self, order_id: &str) -> Result<()> {
        info!(order_id = %order_id, "cancelling order");
        self.client.cancel_order(order_id).await?;
        Ok(())
    }

    /// Send heartbeat to keep the auth session and WS connection alive.
    pub async fn send_heartbeat(&mut self) -> Result<()> {
        let path = "/orders";
        let headers = self.l2_headers("GET", path, "");
        let url = format!("{}{}", self.clob_base_url, path);
        let resp = self
            .http
            .get(&url)
            .header("POLY_API_KEY", &headers[0].1)
            .header("POLY_SIGNATURE", &headers[1].1)
            .header("POLY_TIMESTAMP", &headers[2].1)
            .header("POLY_PASSPHRASE", &headers[3].1)
            .send()
            .await?;
        let status = resp.status();
        debug!(status = %status, "heartbeat sent");
        Ok(())
    }

    /// Generate L2 auth headers using SDK-derived credentials.
    fn l2_headers(&self, method: &str, path: &str, body: &str) -> Vec<(String, String)> {
        let creds = self.client.credentials();
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string();

        let message = format!("{}{}{}{}", timestamp, method, path, body);

        use base64::engine::general_purpose::URL_SAFE;
        use base64::Engine as _;
        let decoded_secret = URL_SAFE.decode(polymarket_client_sdk_v2::auth::ExposeSecret::expose_secret(creds.secret())).expect("Valid base64 secret");
        let api_key = creds.key().to_string();

        let mut mac =
            hmac::Hmac::<sha2::Sha256>::new_from_slice(&decoded_secret)
                .expect("HMAC key length is valid");
        hmac::Mac::update(&mut mac, message.as_bytes());
        let signature = URL_SAFE.encode(mac.finalize().into_bytes());

        vec![
            ("POLY_API_KEY".to_string(), api_key),
            ("POLY_SIGNATURE".to_string(), signature),
            ("POLY_TIMESTAMP".to_string(), timestamp),
            ("POLY_PASSPHRASE".to_string(), polymarket_client_sdk_v2::auth::ExposeSecret::expose_secret(creds.passphrase()).to_string()),
        ]
    }

    /// Cancel all open orders using the SDK's authenticated method.
    pub async fn cancel_all_orders(&self) -> Result<()> {
        info!("cancelling all open orders...");
        match self.client.cancel_all_orders().await {
            Ok(_) => {
                info!("all orders cancelled");
                Ok(())
            }
            Err(e) => {
                error!(error = %e, "cancel all orders failed");
                Err(anyhow::anyhow!("cancel all orders failed: {}", e))
            }
        }
    }

    /// Fetch the real USDC balance from the CLOB API.
    /// The API returns balance in micro-USDC (6 decimal places),
    /// so we divide by 1,000,000 to get the actual dollar amount.
    pub async fn get_balance(&self) -> Result<Decimal> {
        let request = BalanceAllowanceRequest::builder()
            .asset_type(AssetType::Collateral)
            .build();

        match self.client.balance_allowance(request).await {
            Ok(resp) => {
                let raw = resp.balance;
                // CLOB returns micro-USDC (6 decimals). Convert to dollars.
                let usdc = raw / Decimal::from(1_000_000);
                info!(raw_balance = %raw, usdc_balance = %usdc, "CLOB balance fetched");
                Ok(usdc)
            }
            Err(e) => {
                tracing::warn!(error = %e, "balance query failed");
                Ok(Decimal::ZERO)
            }
        }
    }
}
