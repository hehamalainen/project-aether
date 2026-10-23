use crate::db;
use chrono::Utc;
use models::entities::AgentMetric;

fn logit(x: f64) -> f64 {
    let x_clamped = x.clamp(0.001, 0.999);
    (x_clamped / (1.0 - x_clamped)).ln()
}

fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

// Feature-gated SQL queries
#[cfg(all(feature = "db-sqlite", not(feature = "db-postgres")))]
mod queries {
    pub const GET_METRICS: &str =
        "SELECT * FROM aether_agent_metrics WHERE agent_id = ? AND scope = ?";
    pub const INSERT_METRICS: &str =
        "INSERT INTO aether_agent_metrics (agent_id, scope, total_proposals, promoted_count, hard_fail_count, neutral_loss_count, trust_alpha, trust_beta, last_seen, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)";
    pub const UPSERT_METRICS: &str =
        "INSERT INTO aether_agent_metrics (agent_id, scope, total_proposals, promoted_count, hard_fail_count, neutral_loss_count, trust_alpha, trust_beta, last_seen, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (agent_id, scope) \
         DO UPDATE SET \
            total_proposals = aether_agent_metrics.total_proposals + EXCLUDED.total_proposals, \
            promoted_count = aether_agent_metrics.promoted_count + EXCLUDED.promoted_count, \
            hard_fail_count = aether_agent_metrics.hard_fail_count + EXCLUDED.hard_fail_count, \
            neutral_loss_count = aether_agent_metrics.neutral_loss_count + EXCLUDED.neutral_loss_count, \
            trust_alpha = aether_agent_metrics.trust_alpha + ?, \
            trust_beta = aether_agent_metrics.trust_beta + ?, \
            last_seen = EXCLUDED.last_seen, \
            updated_at = EXCLUDED.updated_at";
}

#[cfg(feature = "db-postgres")]
mod queries {
    pub const GET_METRICS: &str =
        "SELECT * FROM aether_agent_metrics WHERE agent_id = $1 AND scope = $2";
    pub const INSERT_METRICS: &str =
        "INSERT INTO aether_agent_metrics (agent_id, scope, total_proposals, promoted_count, hard_fail_count, neutral_loss_count, trust_alpha, trust_beta, last_seen, updated_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)";
    pub const UPSERT_METRICS: &str =
        "INSERT INTO aether_agent_metrics (agent_id, scope, total_proposals, promoted_count, hard_fail_count, neutral_loss_count, trust_alpha, trust_beta, last_seen, updated_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) \
         ON CONFLICT (agent_id, scope) \
         DO UPDATE SET \
            total_proposals = aether_agent_metrics.total_proposals + EXCLUDED.total_proposals, \
            promoted_count = aether_agent_metrics.promoted_count + EXCLUDED.promoted_count, \
            hard_fail_count = aether_agent_metrics.hard_fail_count + EXCLUDED.hard_fail_count, \
            neutral_loss_count = aether_agent_metrics.neutral_loss_count + EXCLUDED.neutral_loss_count, \
            trust_alpha = aether_agent_metrics.trust_alpha + $11, \
            trust_beta = aether_agent_metrics.trust_beta + $12, \
            last_seen = EXCLUDED.last_seen, \
            updated_at = EXCLUDED.updated_at";
}

#[derive(Debug, Default, Clone)]
pub struct AgentMetricDelta {
    pub total_proposals: i64,
    pub promoted_count: i64,
    pub hard_fail_count: i64,
    pub neutral_loss_count: i64,
    pub trust_alpha_delta: f64,
    pub trust_beta_delta: f64,
}


pub struct TrustService {
    pool: db::Pool,
    alpha0: f64,
    beta0: f64,
    lambda_trust: f64,
}

impl TrustService {
    pub fn new(pool: db::Pool) -> Self {
        Self {
            pool,
            alpha0: 3.0,
            beta0: 3.0,
            lambda_trust: 0.5,
        }
    }

    pub async fn get_metrics(
        &self,
        agent_id: &str,
        scope: &str,
    ) -> Result<AgentMetric, sqlx::Error> {
        let mut conn = self.pool.acquire().await?;
        self.get_metrics_tx(&mut conn, agent_id, scope).await
    }

    pub async fn get_metrics_tx(
        &self,
        tx: &mut db::Connection,
        agent_id: &str,
        scope: &str,
    ) -> Result<AgentMetric, sqlx::Error> {
        let metric = sqlx::query_as::<_, AgentMetric>(queries::GET_METRICS)
            .bind(agent_id)
            .bind(scope)
            .fetch_optional(&mut *tx)
            .await?;

        if let Some(m) = metric {
            Ok(m)
        } else {
            let now = Utc::now();
            let new_metric = AgentMetric {
                agent_id: agent_id.to_string(),
                scope: scope.to_string(),
                total_proposals: 0,
                promoted_count: 0,
                hard_fail_count: 0,
                neutral_loss_count: 0,
                trust_alpha: self.alpha0,
                trust_beta: self.beta0,
                last_seen: now,
                updated_at: now,
            };

            sqlx::query(queries::INSERT_METRICS)
                .bind(&new_metric.agent_id)
                .bind(&new_metric.scope)
                .bind(new_metric.total_proposals)
                .bind(new_metric.promoted_count)
                .bind(new_metric.hard_fail_count)
                .bind(new_metric.neutral_loss_count)
                .bind(new_metric.trust_alpha)
                .bind(new_metric.trust_beta)
                .bind(new_metric.last_seen)
                .bind(new_metric.updated_at)
                .execute(&mut *tx)
                .await?;

            Ok(new_metric)
        }
    }

    pub fn calculate_trust_score(&self, metric: &AgentMetric) -> f64 {
        metric.trust_alpha / (metric.trust_alpha + metric.trust_beta)
    }

    pub fn compute_combined_score(&self, confidence: f64, trust: f64) -> f64 {
        let l_conf = logit(confidence);
        let l_trust = logit(trust);
        let l_combined = l_conf + self.lambda_trust * l_trust;
        sigmoid(l_combined)
    }

    pub async fn get_combined_agent_score(
        &self,
        agent_id: &str,
        raw_confidence: f64,
        scope: &str,
    ) -> Result<f64, sqlx::Error> {
        let mut conn = self.pool.acquire().await?;
        self.get_combined_agent_score_tx(&mut conn, agent_id, raw_confidence, scope)
            .await
    }

    pub async fn get_combined_agent_score_tx(
        &self,
        tx: &mut db::Connection,
        agent_id: &str,
        raw_confidence: f64,
        scope: &str,
    ) -> Result<f64, sqlx::Error> {
        let metric = self.get_metrics_tx(tx, agent_id, scope).await?;
        let trust = self.calculate_trust_score(&metric);
        Ok(self.compute_combined_score(raw_confidence, trust))
    }

    pub async fn get_metrics_readonly(
        &self,
        pool: &db::Pool,
        agent_id: &str,
        scope: &str,
    ) -> Result<AgentMetric, sqlx::Error> {
        let metric = sqlx::query_as::<_, AgentMetric>(queries::GET_METRICS)
            .bind(agent_id)
            .bind(scope)
            .fetch_optional(pool)
            .await?;

        if let Some(m) = metric {
            Ok(m)
        } else {
            let now = Utc::now();
            Ok(AgentMetric {
                agent_id: agent_id.to_string(),
                scope: scope.to_string(),
                total_proposals: 0,
                promoted_count: 0,
                hard_fail_count: 0,
                neutral_loss_count: 0,
                trust_alpha: self.alpha0,
                trust_beta: self.beta0,
                last_seen: now,
                updated_at: now,
            })
        }
    }

    pub async fn get_combined_agent_score_readonly(
        &self,
        pool: &db::Pool,
        agent_id: &str,
        raw_confidence: f64,
        scope: &str,
    ) -> Result<(f64, std::collections::HashMap<String, f64>), sqlx::Error> {
        let t_checkout = std::time::Instant::now();
        let mut conn = pool.acquire().await?;
        let checkout_ms = t_checkout.elapsed().as_secs_f64() * 1000.0;
        
        let t_exec = std::time::Instant::now();
        let metric = sqlx::query_as::<_, AgentMetric>(queries::GET_METRICS)
            .bind(agent_id)
            .bind(scope)
            .fetch_optional(&mut *conn)
            .await?;
        let exec_ms = t_exec.elapsed().as_secs_f64() * 1000.0;
        
        let t_compute = std::time::Instant::now();
        let metric = if let Some(m) = metric {
            m
        } else {
            AgentMetric {
                agent_id: agent_id.to_string(),
                scope: scope.to_string(),
                total_proposals: 0,
                promoted_count: 0,
                hard_fail_count: 0,
                neutral_loss_count: 0,
                trust_alpha: self.alpha0,
                trust_beta: self.beta0,
                last_seen: Utc::now(),
                updated_at: Utc::now(),
            }
        };
        let trust = self.calculate_trust_score(&metric);
        let score = self.compute_combined_score(raw_confidence, trust);
        let compute_ms = t_compute.elapsed().as_secs_f64() * 1000.0;
        
        let mut timings = std::collections::HashMap::new();
        timings.insert("t_db_checkout_trust".to_string(), checkout_ms);
        timings.insert("t_db_exec_trust".to_string(), exec_ms);
        timings.insert("t_trust_compute".to_string(), compute_ms);
        
        Ok((score, timings))
    }

    pub async fn record_metric_deltas(
        &self,
        agent_id: &str,
        scope: &str,
        delta: &AgentMetricDelta,
    ) -> Result<std::collections::HashMap<String, f64>, sqlx::Error> {
        let t_checkout = std::time::Instant::now();
        let mut conn = self.pool.acquire().await?;
        let checkout_ms = t_checkout.elapsed().as_secs_f64() * 1000.0;
        
        let t_exec = std::time::Instant::now();
        let now = Utc::now();
        
        #[cfg(all(feature = "db-sqlite", not(feature = "db-postgres")))]
        {
            sqlx::query(queries::UPSERT_METRICS)
                .bind(agent_id)
                .bind(scope)
                .bind(delta.total_proposals)
                .bind(delta.promoted_count)
                .bind(delta.hard_fail_count)
                .bind(delta.neutral_loss_count)
                .bind(self.alpha0 + delta.trust_alpha_delta)
                .bind(self.beta0 + delta.trust_beta_delta)
                .bind(now)
                .bind(now)
                .bind(delta.trust_alpha_delta)
                .bind(delta.trust_beta_delta)
                .execute(&mut *conn)
                .await?;
        }
        
        #[cfg(feature = "db-postgres")]
        {
            sqlx::query(queries::UPSERT_METRICS)
                .bind(agent_id)
                .bind(scope)
                .bind(delta.total_proposals)
                .bind(delta.promoted_count)
                .bind(delta.hard_fail_count)
                .bind(delta.neutral_loss_count)
                .bind(self.alpha0 + delta.trust_alpha_delta)
                .bind(self.beta0 + delta.trust_beta_delta)
                .bind(now)
                .bind(now)
                .bind(delta.trust_alpha_delta)
                .bind(delta.trust_beta_delta)
                .execute(&mut *conn)
                .await?;
        }
        
        let exec_ms = t_exec.elapsed().as_secs_f64() * 1000.0;
        
        let mut timings = std::collections::HashMap::new();
        timings.insert("t_db_checkout_trust_write".to_string(), checkout_ms);
        timings.insert("t_db_exec_trust_write".to_string(), exec_ms);
        
        Ok(timings)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_logit_and_sigmoid_parity() {
        let conf = 0.8;
        let l_conf = logit(conf);

        // Exact Python equivalent check
        let x_clamped = conf.clamp(0.001, 0.999);
        let expected_logit = (x_clamped / (1.0 - x_clamped)).ln();
        assert!((l_conf - expected_logit).abs() < 1e-9);

        let sig = sigmoid(l_conf);
        assert!((sig - conf).abs() < 1e-9);
    }

    #[cfg(all(feature = "db-sqlite", not(feature = "db-postgres")))]
    #[tokio::test]
    async fn test_trust_service_math_parity() {
        let service = TrustService {
            pool: sqlx::SqlitePool::connect_lazy("sqlite::memory:").unwrap(),
            alpha0: 3.0,
            beta0: 3.0,
            lambda_trust: 0.5,
        };

        let metric = AgentMetric {
            agent_id: "agent_1".to_string(),
            scope: "global".to_string(),
            total_proposals: 0,
            promoted_count: 0,
            hard_fail_count: 0,
            neutral_loss_count: 0,
            trust_alpha: 3.0,
            trust_beta: 3.0,
            last_seen: Utc::now(),
            updated_at: Utc::now(),
        };

        let trust = service.calculate_trust_score(&metric);
        assert_eq!(trust, 0.5);

        let combined = service.compute_combined_score(0.8, trust);
        assert!((combined - 0.8).abs() < 1e-9);

        let metric_good = AgentMetric {
            trust_alpha: 8.0,
            trust_beta: 2.0,
            ..metric.clone()
        };
        let trust_good = service.calculate_trust_score(&metric_good);
        assert_eq!(trust_good, 0.8);

        let combined_good = service.compute_combined_score(0.8, trust_good);
        assert!((combined_good - 0.8888888888888888).abs() < 1e-9);
    }
}
