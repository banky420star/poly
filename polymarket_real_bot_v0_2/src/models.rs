use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;
use std::{collections::HashMap, str::FromStr};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AiSignal {
    pub timestamp: String,
    pub model: String,
    pub market_slug: String,
    pub token_id: String,
    pub outcome: String,
    pub action: String,
    pub order_type: String,
    pub limit_price: Decimal,
    pub shares: Decimal,
    pub current_price: Decimal,
    pub lstm_fair_prob: Decimal,
    pub edge: Decimal,
    pub ppo_confidence: Decimal,
    pub ttl_ms: u64,
    pub reason: String,
}


/// Depth data for a single market, derived from CLOB order books.
#[derive(Debug, Clone, Default)]
pub struct DepthData {
    pub yes_bid_depth_usdc: Decimal,
    pub yes_ask_depth_usdc: Decimal,
    pub no_bid_depth_usdc: Decimal,
    pub no_ask_depth_usdc: Decimal,
    pub yes_best_bid: Option<Decimal>,
    pub yes_best_ask: Option<Decimal>,
    pub no_best_bid: Option<Decimal>,
    pub no_best_ask: Option<Decimal>,
    pub yes_levels: usize,
    pub no_levels: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GammaMarket {
    #[serde(default)]
    pub id: String,

    #[serde(default, rename = "conditionId")]
    pub condition_id: Option<String>,

    #[serde(default)]
    pub question: String,

    #[serde(default)]
    pub active: Option<bool>,

    #[serde(default)]
    pub closed: Option<bool>,

    #[serde(default, rename = "endDate")]
    pub end_date: Option<String>,

    #[serde(default, rename = "outcomePrices", deserialize_with = "de_opt_vec_string")]
    pub outcome_prices: Option<Vec<String>>,

    #[serde(default, rename = "clobTokenIds", deserialize_with = "de_opt_vec_string")]
    pub clob_token_ids: Option<Vec<String>>,

    #[serde(default, deserialize_with = "de_opt_vec_string")]
    pub outcomes: Option<Vec<String>>,

    #[serde(default)]
    pub volume: Option<Value>,

    #[serde(default)]
    pub liquidity: Option<Value>,

    #[serde(flatten)]
    pub extra: HashMap<String, Value>,
}

impl GammaMarket {
    pub fn yes_mid(&self) -> Option<Decimal> {
        self.outcome_prices
            .as_ref()
            .and_then(|prices| prices.first())
            .and_then(|p| Decimal::from_str(p).ok())
    }

    pub fn no_mid(&self) -> Option<Decimal> {
        self.outcome_prices
            .as_ref()
            .and_then(|prices| prices.get(1))
            .and_then(|p| Decimal::from_str(p).ok())
            .or_else(|| self.yes_mid().map(|yes| Decimal::ONE - yes))
    }

    pub fn yes_token_id(&self) -> Option<String> {
        self.clob_token_ids.as_ref().and_then(|ids| ids.first()).cloned()
    }

    pub fn no_token_id(&self) -> Option<String> {
        self.clob_token_ids.as_ref().and_then(|ids| ids.get(1)).cloned()
    }

    pub fn condition_or_id(&self) -> String {
        self.condition_id.clone().unwrap_or_else(|| self.id.clone())
    }
}

pub fn de_opt_vec_string<'de, D>(deserializer: D) -> Result<Option<Vec<String>>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?;

    let Some(value) = value else {
        return Ok(None);
    };

    match value {
        Value::Array(items) => {
            let out = items
                .into_iter()
                .filter_map(|v| match v {
                    Value::String(s) => Some(s),
                    Value::Number(n) => Some(n.to_string()),
                    Value::Bool(b) => Some(b.to_string()),
                    _ => None,
                })
                .collect::<Vec<_>>();
            Ok(Some(out))
        }
        Value::String(s) => {
            if s.trim().is_empty() {
                return Ok(Some(vec![]));
            }

            if let Ok(parsed) = serde_json::from_str::<Vec<Value>>(&s) {
                let out = parsed
                    .into_iter()
                    .filter_map(|v| match v {
                        Value::String(s) => Some(s),
                        Value::Number(n) => Some(n.to_string()),
                        Value::Bool(b) => Some(b.to_string()),
                        _ => None,
                    })
                    .collect::<Vec<_>>();
                return Ok(Some(out));
            }

            Ok(Some(
                s.split(',')
                    .map(|part| part.trim().trim_matches('"').to_string())
                    .filter(|part| !part.is_empty())
                    .collect(),
            ))
        }
        _ => Ok(None),
    }
}
