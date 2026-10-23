pub mod gate_runner;
pub mod promotion_engine;
pub mod promotion_orchestrator;
pub mod trust_service;

// Backend-specific type aliases.
// The server crate propagates db-sqlite or db-postgres to this crate,
// so exactly one set of aliases is active per compilation.

#[cfg(all(feature = "db-sqlite", feature = "db-postgres"))]
compile_error!("Must enable exactly one of 'db-sqlite' or 'db-postgres', but both are enabled. Aether does not support running both database backends in the same binary compilation.");

#[cfg(not(any(feature = "db-sqlite", feature = "db-postgres")))]
compile_error!("Must enable exactly one of 'db-sqlite' or 'db-postgres', but neither is enabled.");

#[cfg(all(feature = "db-sqlite", not(feature = "db-postgres")))]
pub mod db {
    pub type Pool = sqlx::SqlitePool;
    pub type Connection = sqlx::SqliteConnection;
}

#[cfg(feature = "db-postgres")]
pub mod db {
    pub type Pool = sqlx::PgPool;
    pub type Connection = sqlx::PgConnection;
}
