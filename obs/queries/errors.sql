-- Error spans (OTLP status ERROR or langfuse.observation.level=ERROR), newest first ($N, default 50).
SELECT
  start_ts,
  app,
  name,
  coalesce(nullif(status_message, ''), attrs['langfuse.observation.status_message'],
           attrs['error.type'], attrs['exception.message']) AS error,
  round(duration_ms)                AS ms,
  trace_id
FROM spans
WHERE is_error
ORDER BY start_ts DESC
LIMIT CAST(coalesce(nullif(getenv('N'), ''), '50') AS INTEGER);
