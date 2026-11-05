-- Last $N traces (default 20): one row per trace with name, span count, tokens, errors.
SELECT
  min(start_ts)                                         AS start_ts,
  any_value(app)                                        AS app,
  coalesce(max(attrs['langfuse.trace.name']), arg_min(name, start_ts)) AS trace,
  count(*)                                              AS spans,
  count(*) FILTER (WHERE is_generation)                 AS generations,
  sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens,
  count(*) FILTER (WHERE is_error)                      AS errors,
  round(max(epoch_ms(start_ts) + duration_ms) - epoch_ms(min(start_ts))) AS ms,
  trace_id
FROM spans
GROUP BY trace_id
ORDER BY start_ts DESC
LIMIT CAST(coalesce(nullif(getenv('N'), ''), '20') AS INTEGER);
