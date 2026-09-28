-- v26 -> v25. Access tokens issued through OAuth are ordinary capability grants and
-- survive; only registrations and in-flight/consumed authorization codes are dropped,
-- so connected clients must re-register after a roll-forward. Lossless for user data.
DROP TABLE oauth_authorizations;
DROP TABLE oauth_clients;
DELETE FROM schema_migrations WHERE version=26;
