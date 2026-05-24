use crate::{config::MarketFilterConfig, models::GammaMarket};

pub struct MarketSelector {
    cfg: MarketFilterConfig,
}

impl MarketSelector {
    pub fn new(cfg: MarketFilterConfig) -> Self {
        Self { cfg }
    }

    pub fn select<'a>(&self, markets: &'a [GammaMarket]) -> Vec<&'a GammaMarket> {
        markets
            .iter()
            .filter(|m| {
                if self.cfg.require_active && m.active == Some(false) {
                    return false;
                }

                if self.cfg.require_open && m.closed == Some(true) {
                    return false;
                }

                let haystack = format!("{} {}", m.question, m.id).to_lowercase();

                if self
                    .cfg
                    .exclude_keywords
                    .iter()
                    .any(|kw| haystack.contains(&kw.to_lowercase()))
                {
                    return false;
                }

                self.cfg
                    .keywords
                    .iter()
                    .any(|kw| haystack.contains(&kw.to_lowercase()))
            })
            .collect()
    }
}
