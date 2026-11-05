-- Slowest generations ($N, default 20), with output tokens/s when known.
SELECT
  start_ts,
  app,
  model,
  name,
  round(duration_ms)                AS ms,
  input_tokens,
  output_tokens,
  round(output_tokens / nullif(duration_ms, 0) * 1000, 1) AS out_tok_per_s,
  trace_id
FROM spans
WHERE is_generation
ORDER BY duration_ms DESC
LIMIT CAST(coalesce(nullif(getenv('N'), ''), '20') AS INTEGER);
