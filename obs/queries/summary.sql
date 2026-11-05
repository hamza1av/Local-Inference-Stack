-- Spool overview: traces, spans, errors and time range per app.
SELECT
  app,
  count(DISTINCT trace_id)          AS traces,
  count(*)                          AS spans,
  count(*) FILTER (WHERE is_error)  AS errors,
  min(start_ts)                     AS first,
  max(start_ts)                     AS last
FROM spans
GROUP BY app
ORDER BY last DESC;
