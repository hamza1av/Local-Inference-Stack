-- Tokens per app per day (generation spans only), newest day first.
SELECT
  CAST(start_ts AS DATE)            AS day,
  app,
  count(*)                          AS generations,
  sum(coalesce(input_tokens, 0))    AS input_tokens,
  sum(coalesce(output_tokens, 0))   AS output_tokens,
  sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS total_tokens,
  string_agg(DISTINCT model, ', ' ORDER BY model) AS models
FROM spans
WHERE is_generation
GROUP BY ALL
ORDER BY day DESC, total_tokens DESC;
