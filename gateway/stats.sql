-- Requests / tokens / latency per model and client over the last @h hours.
-- sqlite3 -header -column -cmd ".parameter set @h 24" "$STACK_DB" < stats.sql
SELECT model,
       client,
       count(*)                          AS requests,
       sum(status >= 400)                AS errors,
       coalesce(sum(prompt_tokens), 0)   AS prompt_tok,
       coalesce(sum(completion_tokens), 0) AS completion_tok,
       cast(avg(latency_ms) AS int)      AS avg_ms,
       cast(avg(ttft_ms) AS int)         AS avg_ttft_ms,
       cast(avg(queue_ms) AS int)        AS avg_queue_ms,
       max(queue_ms)                     AS max_queue_ms
FROM gw_requests
WHERE ts >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-' || @h || ' hours')
GROUP BY model, client
ORDER BY requests DESC;
